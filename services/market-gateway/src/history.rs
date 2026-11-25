//! Daily history for the house teams' strategies, fetched once per trading day before the open.
//!
//! For every symbol in the universe: the last `BARS` daily bars (forward-adjusted with the symbol's ex-rights
//! events, so a dividend never looks like a crash), a per-bar "closed at the limit-up" flag computed on raw
//! prices, trailing-12-month cash dividends per share and the free-float share count. Plus daily bars of the
//! three indices for market context.
//!
//! Redis layout (all JSON, columnar to stay small):
//!   md:hist        hash  symbol -> {"d":[yyyymmdd…],"o","h","l","c","v"(shares),"a"(yuan),"lu":[0|1…],
//!                                   "div_ttm": yuan/share, "float_shares": shares}
//!   md:hist:meta   string {"date","built_at","symbols","failed","members":{index:[symbols]},
//!                          "indices":{code:{"d","c","up","down"}}}
//!
//! About 3 requests per symbol (bars, ex-rights, finance), spread over the worker connections: ~1,300 symbols
//! take well under a minute and only once a day.

use std::collections::BTreeMap;
use std::net::SocketAddr;
use std::sync::mpsc;
use std::thread;

use anyhow::{Context, Result};
use chrono::{Datelike, NaiveDate, NaiveTime};
use log::{info, warn};
use rustdx_complete::limit::{self, Board};
use rustdx_complete::tcp::stock::{ex_right_reference, FinanceInfo, IndexKline, Kline, XdxrData};
use rustdx_complete::tcp::stock::Xdxr;
use rustdx_complete::tcp::{Tcp, Tdx};
use serde::Serialize;

use crate::normalize::{Kind, SymbolMeta};

pub const BARS: u16 = 250;

#[derive(Debug, Default, Serialize, PartialEq)]
pub struct Hist {
    pub d: Vec<u32>,
    pub o: Vec<f64>,
    pub h: Vec<f64>,
    pub l: Vec<f64>,
    pub c: Vec<f64>,
    pub v: Vec<f64>,
    pub a: Vec<f64>,
    pub lu: Vec<u8>,
    pub div_ttm: f64,
    pub float_shares: f64,
}

#[derive(Debug, Clone, Copy)]
pub struct RawBar {
    pub date: u32,
    pub open: f64,
    pub high: f64,
    pub low: f64,
    pub close: f64,
    pub vol_lots: f64,
    pub amount: f64,
}

fn round4(x: f64) -> f64 {
    (x * 10_000.0).round() / 10_000.0
}

fn date_u32(d: NaiveDate) -> u32 {
    d.year() as u32 * 10_000 + d.month() * 100 + d.day()
}

/// Turns raw bars + ex-rights events into the published shape. `today`/`include_today`: the bar dated today is
/// partial (or a pre-open placeholder) until the close, so it is dropped unless the day is over.
pub fn build_hist(raw: &[RawBar], xdxr: &[XdxrData], board: Option<Board>, st: bool, float_shares: f64,
                  today: u32, include_today: bool) -> Hist {
    let bars: Vec<RawBar> = raw.iter().copied()
        .filter(|b| b.vol_lots > 0.0 && (b.date < today || (b.date == today && include_today)))
        .collect();
    let events: Vec<&XdxrData> = xdxr.iter().filter(|e| e.category == 1).collect();
    let n = bars.len();
    // Forward adjustment: walking back from the newest bar, every ex-rights day scales all earlier bars.
    let mut mult = vec![1.0f64; n];
    let mut lu = vec![0u8; n];
    let mut factor = 1.0;
    for i in (0..n).rev() {
        mult[i] = factor;
        if i == 0 {
            break;
        }
        let prev_raw = bars[i - 1].close;
        let mut ref_close = prev_raw;
        for ev in events.iter().filter(|e| e.date > bars[i - 1].date && e.date <= bars[i].date) {
            ref_close = ex_right_reference(ref_close, ev);
        }
        if prev_raw > 0.0 && ref_close > 0.0 && (ref_close - prev_raw).abs() > 1e-9 {
            factor *= ref_close / prev_raw;
        }
        if let Some(b) = board {
            if let Some(up) = limit::limit_up_price_on(ref_close, b, st, bars[i].date) {
                lu[i] = (bars[i].close >= up - 0.005) as u8;
            }
        }
    }
    let year_ago = today.saturating_sub(10_000);
    let div_ttm: f64 = events.iter().filter(|e| e.date > year_ago && e.date <= today)
        .map(|e| e.fh_qltp as f64 / 10.0).sum();
    Hist {
        d: bars.iter().map(|b| b.date).collect(),
        o: bars.iter().zip(&mult).map(|(b, m)| round4(b.open * m)).collect(),
        h: bars.iter().zip(&mult).map(|(b, m)| round4(b.high * m)).collect(),
        l: bars.iter().zip(&mult).map(|(b, m)| round4(b.low * m)).collect(),
        c: bars.iter().zip(&mult).map(|(b, m)| round4(b.close * m)).collect(),
        v: bars.iter().map(|b| (b.vol_lots * 100.0).round()).collect(),
        a: bars.iter().map(|b| b.amount.round()).collect(),
        lu,
        div_ttm: round4(div_ttm) + 0.0,   // an empty f64 sum is -0.0
        float_shares: float_shares.round(),
    }
}

fn fetch_one(tcp: &mut Tcp, m: &SymbolMeta, today: u32, include_today: bool) -> Result<Hist> {
    let mut k = Kline::new(m.market, &m.code, 9, 0, BARS);
    let raw: Vec<RawBar> = k.recv_parsed(tcp).context("bars")?.iter().map(|b| RawBar {
        date: b.dt.to_u32(), open: b.open, high: b.high, low: b.low, close: b.close, vol_lots: b.vol, amount: b.amount,
    }).collect();
    let xdxr: Vec<XdxrData> = Xdxr::new(m.market, &m.code).recv_parsed(tcp).context("xdxr")?.to_vec();
    let (board, float_shares) = match m.kind {
        Kind::Stock(b) => {
            let f = FinanceInfo::new(m.market as u8, &m.code).recv_parsed(tcp).ok()
                .and_then(|v| v.first().map(|x| x.liutongguben)).unwrap_or(0.0);
            (Some(b), f)
        }
        Kind::Fund => (None, 0.0),
    };
    Ok(build_hist(&raw, &xdxr, board, m.st, float_shares, today, include_today))
}

#[derive(Serialize)]
struct IndexHist {
    d: Vec<u32>,
    c: Vec<f64>,
    up: Vec<u16>,
    down: Vec<u16>,
}

fn connect_any(servers: &[SocketAddr], start: usize) -> Option<Tcp> {
    (0..servers.len()).find_map(|i| crate::connect(Some(servers[(start + i) % servers.len()])).ok())
        .or_else(|| crate::connect(None).ok())
}

/// Fetches everything and writes it to Redis. Runs on its own threads; the quote loop is not blocked.
pub fn run(redis_url: &str, metas: Vec<SymbolMeta>, members: BTreeMap<String, Vec<String>>, indices: &[String],
           servers: Vec<SocketAddr>, connections: usize, date: NaiveDate, now: NaiveTime) -> Result<usize> {
    let today = date_u32(date);
    let include_today = now >= NaiveTime::from_hms_opt(15, 30, 0).unwrap();
    let started = std::time::Instant::now();
    let n = connections.clamp(1, 8).min(metas.len().max(1));
    let (tx, rx) = mpsc::channel::<(String, Result<Hist>)>();
    let mut handles = Vec::new();
    for w in 0..n {
        let shard: Vec<SymbolMeta> = metas.iter().skip(w).step_by(n).cloned().collect();
        let (tx, servers) = (tx.clone(), servers.clone());
        handles.push(thread::spawn(move || {
            let mut tcp = connect_any(&servers, w);
            for m in shard {
                let mut res = match tcp.as_mut() {
                    Some(t) => fetch_one(t, &m, today, include_today),
                    None => Err(anyhow::anyhow!("no TDX server reachable")),
                };
                if res.is_err() {               // one reconnect per symbol, then give up on it
                    tcp = connect_any(&servers, w + 1);
                    if let Some(t) = tcp.as_mut() {
                        res = fetch_one(t, &m, today, include_today);
                    }
                }
                let _ = tx.send((m.symbol.clone(), res));
            }
        }));
    }
    drop(tx);

    let client = redis::Client::open(redis_url)?;
    let mut conn = client.get_connection().context("connect redis")?;
    let (mut ok, mut failed, mut pipe, mut pending) = (0usize, Vec::new(), redis::pipe(), 0usize);
    for (sym, res) in rx {
        match res {
            Ok(h) if !h.d.is_empty() => {
                pipe.cmd("HSET").arg("md:hist").arg(&sym).arg(serde_json::to_string(&h)?).ignore();
                ok += 1;
                pending += 1;
                if pending >= 100 {
                    pipe.query::<()>(&mut conn).context("write md:hist")?;
                    (pipe, pending) = (redis::pipe(), 0);
                }
            }
            Ok(_) => failed.push(format!("{sym}: no bars")),
            Err(e) => failed.push(format!("{sym}: {e:#}")),
        }
    }
    if pending > 0 {
        pipe.query::<()>(&mut conn).context("write md:hist")?;
    }
    for h in handles {
        let _ = h.join();
    }

    let mut index_bars = BTreeMap::new();
    if let Some(mut tcp) = connect_any(&servers, 0) {
        for code in indices {
            let market = if code.starts_with('3') { 0 } else { 1 };   // 399xxx are SZ indices, CSI 000xxx on SH
            match IndexKline::new(market, code, 9, 0, BARS).recv_parsed(&mut tcp) {
                Ok(v) => {
                    let v: Vec<_> = v.iter().filter(|b| b.vol > 0.0 && (b.dt.to_u32() < today || include_today)).collect();
                    index_bars.insert(code.clone(), IndexHist {
                        d: v.iter().map(|b| b.dt.to_u32()).collect(), c: v.iter().map(|b| round4(b.close)).collect(),
                        up: v.iter().map(|b| b.up_count).collect(), down: v.iter().map(|b| b.down_count).collect(),
                    });
                }
                Err(e) => warn!("index {code} bars: {e}"),
            }
        }
    }
    let meta = serde_json::json!({
        "date": date.to_string(), "built_at": crate::cst_now().to_rfc3339(), "symbols": ok, "failed": failed.len(),
        "members": members, "indices": index_bars,
    });
    redis::cmd("SET").arg("md:hist:meta").arg(meta.to_string()).query::<()>(&mut conn).context("write md:hist:meta")?;
    if !failed.is_empty() {
        warn!("history: {} symbols failed, e.g. {}", failed.len(), failed[0]);
    }
    info!("history: {ok} symbols in {:?} ({} bars each at most)", started.elapsed(), BARS);
    Ok(ok)
}

/// The date of the last history build, from Redis (None if never built or unreadable).
pub fn built_date(redis_url: &str) -> Option<NaiveDate> {
    let mut conn = redis::Client::open(redis_url).ok()?.get_connection().ok()?;
    let raw: Option<String> = redis::cmd("GET").arg("md:hist:meta").query(&mut conn).ok()?;
    let v: serde_json::Value = serde_json::from_str(&raw?).ok()?;
    NaiveDate::parse_from_str(v.get("date")?.as_str()?, "%Y-%m-%d").ok()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn bar(date: u32, close: f64) -> RawBar {
        RawBar { date, open: close, high: close, low: close, close, vol_lots: 1000.0, amount: close * 100_000.0 }
    }

    fn ex(date: u32, cash_per_10: f32) -> XdxrData {
        XdxrData { market: 1, code: "600000".into(), date, category: 1, fh_qltp: cash_per_10, pgj_qzgb: 0.0,
                   sg_hltp: 0.0, pg_hzgb: 0.0 }
    }

    #[test]
    fn forward_adjusts_dividends_and_flags_limit_ups() {
        // 10.00 -> ex-dividend 1 yuan/share on the 3rd day (reference 9.00), then a limit-up from 9.00 to 9.90.
        let raw = vec![bar(20261120, 10.0), bar(20261123, 10.0), bar(20261124, 9.0), bar(20261125, 9.9)];
        let h = build_hist(&raw, &[ex(20261124, 10.0)], Some(Board::Main), false, 1e9, 20261126, false);
        assert_eq!(h.c, vec![9.0, 9.0, 9.0, 9.9]);          // no fake -10% on the ex-dividend day
        assert_eq!(h.lu, vec![0, 0, 0, 1]);
        assert!((h.div_ttm - 1.0).abs() < 1e-9);
        assert_eq!(h.v[0], 100_000.0);
    }

    #[test]
    fn drops_todays_partial_bar_and_placeholders() {
        let mut raw = vec![bar(20261124, 10.0), bar(20261125, 10.5), bar(20261126, 10.8)];
        raw.push(RawBar { vol_lots: 0.0, ..bar(20261127, 10.8) });
        let h = build_hist(&raw, &[], Some(Board::Main), false, 0.0, 20261126, false);
        assert_eq!(h.d, vec![20261124, 20261125]);
        let h = build_hist(&raw, &[], Some(Board::Main), false, 0.0, 20261126, true);
        assert_eq!(h.d, vec![20261124, 20261125, 20261126]);
    }

    /// Live check against real TDX servers: `cargo test -- --ignored live_history --nocapture`.
    #[test]
    #[ignore]
    fn live_history() {
        let mut tcp = crate::connect(None).expect("TDX");
        let syms = ["600519.SH", "601398.SH", "000001.SZ", "300750.SZ", "688981.SH", "510300.SH"];
        let t = std::time::Instant::now();
        for s in syms {
            let m = SymbolMeta::parse(s, false).unwrap();
            let h = fetch_one(&mut tcp, &m, 20261126, false).expect(s);
            let n = h.d.len();
            println!("{s}: {n} bars {}..{} last close {} div_ttm {} float {:.0} limit-ups {}",
                     h.d[0], h.d[n - 1], h.c[n - 1], h.div_ttm, h.float_shares, h.lu.iter().filter(|x| **x == 1).count());
        }
        println!("{} symbols in {:?} on one connection", syms.len(), t.elapsed());
        let v = IndexKline::new(1, "000300", 9, 0, 5).recv_parsed(&mut tcp).unwrap().to_vec();
        println!("CSI300 last: {} close {} up {} down {}", v.last().unwrap().dt.to_u32(), v.last().unwrap().close,
                 v.last().unwrap().up_count, v.last().unwrap().down_count);
    }
}
