//! TDX quote -> Agora canonical L1 snapshot (the JSON shape `arena_engine.market.feed.snapshot_from_dict` reads).
//!
//! Pure functions, no I/O. Units: TDX volumes are in 手 (lots of 100); Agora uses shares everywhere.

use chrono::{NaiveDate, NaiveTime, Timelike};
use rustdx_complete::limit::{self, Board};
use rustdx_complete::tcp::stock::QuoteData;
use serde::Serialize;

/// First and last exchange times we publish. 09:15-09:25 and 14:57-15:00 are call auctions: published as
/// `call_auction` snapshots (empty book, the exchange's indicative match in `auction`), so the arena can take
/// auction orders against today's reference prices and match them at the price the exchange sets.
pub const SESSION_START: (u32, u32) = (9, 15);
pub const SESSION_END: (u32, u32, u32) = (15, 0, 59);

fn in_call_auction(t: NaiveTime, indicative: bool) -> bool {
    let hm = |h, m| NaiveTime::from_hms_opt(h, m, 0).unwrap();
    (t >= hm(9, 15) && t < hm(9, 25)) || (t >= hm(14, 57) && t < hm(15, 0))
        // the indicative-match encoding can linger a few seconds past 09:25 / 15:00 until the price is set
        || (indicative && ((t >= hm(9, 25) && t < hm(9, 26)) || (t >= hm(15, 0) && t < hm(15, 1))))
}

/// The fields we need from a TDX quote, decoupled from rustdx so the logic is testable.
#[derive(Debug, Clone, Default)]
pub struct RawQuote {
    pub market: u16, // 0 = SZ, 1 = SH
    pub code: String,
    pub price: f64,
    pub last_close: f64,
    pub vol_lots: f64,
    pub servertime: String, // "HH:MM:SS.mmm", no date
    pub bids: [(f64, f64); 5], // (price, lots)
    pub asks: [(f64, f64); 5],
    /// Call-auction indicative match: (price, matched lots, unmatched buy lots, unmatched sell lots).
    pub auction: Option<(f64, f64, f64, f64)>,
}

impl From<&QuoteData> for RawQuote {
    fn from(q: &QuoteData) -> Self {
        Self {
            market: q.market,
            code: q.code.clone(),
            price: q.price,
            last_close: q.last_close,
            vol_lots: q.vol,
            servertime: q.servertime.clone(),
            bids: [
                (q.bid1, q.bid1_vol), (q.bid2, q.bid2_vol), (q.bid3, q.bid3_vol), (q.bid4, q.bid4_vol), (q.bid5, q.bid5_vol),
            ],
            asks: [
                (q.ask1, q.ask1_vol), (q.ask2, q.ask2_vol), (q.ask3, q.ask3_vol), (q.ask4, q.ask4_vol), (q.ask5, q.ask5_vol),
            ],
            auction: q.auction().map(|a| (a.price, a.volume, a.unmatched_buy, a.unmatched_sell)),
        }
    }
}

/// What kind of security a symbol is; decides price precision and the limit band.
#[derive(Debug, Clone, Copy, PartialEq)]
pub enum Kind {
    Stock(Board),
    /// Exchange-traded fund (SH 5xxxxx, SZ 15/16xxxx): 0.001 tick, ±10% band.
    Fund,
}

/// Static facts about a watched symbol, resolved once at startup (and on each new trade date).
#[derive(Debug, Clone)]
pub struct SymbolMeta {
    pub symbol: String, // "600000.SH"
    pub market: u16,
    pub code: String,
    pub kind: Kind,
    pub st: bool,
    pub name: Option<String>,
}

pub fn is_fund_code(code: &str, market: u16) -> bool {
    code.len() == 6 && ((market == 1 && code.starts_with('5')) || (market == 0 && (code.starts_with("15") || code.starts_with("16"))))
}

impl SymbolMeta {
    /// Parses "600000.SH" / "000001.SZ" / "510300.SH". Beijing (.BJ), indices and bonds are rejected for now.
    pub fn parse(symbol: &str, st: bool) -> Result<Self, String> {
        let (code, exch) = symbol.split_once('.').ok_or_else(|| format!("{symbol}: expected CODE.SH|SZ"))?;
        let market = match exch {
            "SH" => 1,
            "SZ" => 0,
            other => return Err(format!("{symbol}: exchange {other} not supported by the TDX quote feed yet")),
        };
        // board_of() looks at the code alone; on SH, 000xxx/399xxx-style codes are indices, not stocks.
        let exchange_ok = match market {
            1 => code.starts_with('6'),
            _ => code.starts_with('0') || code.starts_with('3'),
        };
        let kind = if is_fund_code(code, market) {
            Kind::Fund
        } else if !exchange_ok {
            return Err(format!("{symbol}: not a stock code on {exch} (index?)"));
        } else {
            match limit::board_of(code) {
                Some(Board::Beijing) => return Err(format!("{symbol}: Beijing exchange not supported yet")),
                Some(b) => Kind::Stock(b),
                None => return Err(format!("{symbol}: not an A-share stock or ETF code")),
            }
        };
        Ok(Self { symbol: symbol.to_string(), market, code: code.to_string(), kind, st, name: None })
    }

    fn decimals(&self) -> u32 {
        match self.kind {
            Kind::Fund => 3,
            Kind::Stock(_) => 2,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct Snapshot {
    pub symbol: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub name: Option<String>,
    pub ts: String, // RFC 3339, +08:00
    pub seq: u64,   // ms since midnight of exchange time: monotonic per day and stable across gateway restarts
    pub bids: Vec<(String, u64)>,
    pub asks: Vec<(String, u64)>,
    pub last_price: String,
    pub cumulative_volume: u64,
    pub upper_limit: String,
    pub lower_limit: String,
    pub prev_close: String,
    pub suspended: bool,
    #[serde(skip_serializing_if = "std::ops::Not::not")]
    pub call_auction: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub auction: Option<AuctionInfo>,
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct AuctionInfo {
    pub price: String,
    pub volume: u64,          // shares
    pub unmatched_buy: u64,   // shares
    pub unmatched_sell: u64,  // shares
}

/// Why a quote was not published. Counted and logged by the caller, never fatal.
#[derive(Debug, Clone, PartialEq)]
pub enum Skip {
    BadServerTime(String),
    OutsideSession,
    NoPrevClose,
    NoLimitRule,
    CrossedBook,
    /// A visible level is outside our computed limits: the limit rule (board/ST/IPO) is wrong for this symbol.
    LimitMismatch { price: String, lower: String, upper: String },
}

/// Price in integer units of the tick (cents for stocks, mills for funds) so comparisons are exact.
fn units(x: f64, scale: i64) -> i64 {
    (x * scale as f64).round() as i64
}

fn fmt_units(u: i64, decimals: u32) -> String {
    let scale = 10i64.pow(decimals);
    format!("{}.{:0width$}", u / scale, u % scale, width = decimals as usize)
}

/// prev_close × (1 ± pct), rounded half-up to the tick (exchange rule), in tick units.
fn band(prev_units: i64, pct: i64, up: bool) -> i64 {
    let num = prev_units * (100 + if up { pct } else { -pct });
    (num + 50).div_euclid(100)
}

pub fn parse_servertime(s: &str) -> Option<NaiveTime> {
    NaiveTime::parse_from_str(s.trim(), "%H:%M:%S%.f").ok()
}

pub fn normalize(q: &RawQuote, meta: &SymbolMeta, trade_date: NaiveDate) -> Result<Snapshot, Skip> {
    let t = parse_servertime(&q.servertime).ok_or_else(|| Skip::BadServerTime(q.servertime.clone()))?;
    let start = NaiveTime::from_hms_opt(SESSION_START.0, SESSION_START.1, 0).unwrap();
    let end = NaiveTime::from_hms_opt(SESSION_END.0, SESSION_END.1, SESSION_END.2).unwrap();
    if t < start || t > end {
        return Err(Skip::OutsideSession);
    }

    let decimals = meta.decimals();
    let scale = 10i64.pow(decimals);
    let px = |u: i64| fmt_units(u, decimals);
    let prev_u = units(q.last_close, scale);
    if prev_u <= 0 {
        return Err(Skip::NoPrevClose);
    }
    let (up_u, down_u) = match meta.kind {
        Kind::Fund => (band(prev_u, 10, true), band(prev_u, 10, false)),
        Kind::Stock(board) => {
            let prev_close = prev_u as f64 / scale as f64;
            let up = limit::limit_up_price(prev_close, board, meta.st).ok_or(Skip::NoLimitRule)?;
            let down = limit::limit_down_price(prev_close, board, meta.st).ok_or(Skip::NoLimitRule)?;
            (units(up, scale), units(down, scale))
        }
    };

    let ts = format!("{}T{}+08:00", trade_date.format("%Y-%m-%d"), t.format("%H:%M:%S%.3f"));
    let ms = t.num_seconds_from_midnight() as u64 * 1000 + (t.nanosecond() / 1_000_000) as u64;
    let vol_shares = (q.vol_lots.max(0.0).round() as u64) * 100;
    if in_call_auction(t, q.auction.is_some()) {
        let info = q.auction.filter(|a| a.0 > 0.0).map(|(p, v, ub, us)| AuctionInfo {
            price: px(units(p, scale)),
            volume: (v.max(0.0).round() as u64) * 100,
            unmatched_buy: (ub.max(0.0).round() as u64) * 100,
            unmatched_sell: (us.max(0.0).round() as u64) * 100,
        });
        // Opening: no trade yet, show the indicative price. Closing: the last continuous price until the close.
        let last_u = if q.price > 0.0 { units(q.price, scale) }
            else { q.auction.filter(|a| a.0 > 0.0).map(|a| units(a.0, scale)).unwrap_or(prev_u) };
        return Ok(Snapshot {
            symbol: meta.symbol.clone(), name: meta.name.clone(), ts, seq: ms, bids: vec![], asks: vec![],
            last_price: px(last_u.clamp(down_u, up_u)), cumulative_volume: vol_shares, upper_limit: px(up_u),
            lower_limit: px(down_u), prev_close: px(prev_u), suspended: false, call_auction: true, auction: info,
        });
    }

    let side = |levels: &[(f64, f64); 5]| -> Vec<(i64, u64)> {
        levels
            .iter()
            .filter(|(p, v)| *p > 0.0 && *v > 0.0)
            .map(|(p, v)| (units(*p, scale), (v.round() as u64) * 100))
            .collect()
    };
    let bids = side(&q.bids);
    let asks = side(&q.asks);

    if let (Some(b), Some(a)) = (bids.first(), asks.first()) {
        if b.0 >= a.0 {
            return Err(Skip::CrossedBook); // a virtual matched book outside the auction windows: not tradable
        }
    }
    if let Some(&(p, _)) = bids.iter().chain(asks.iter()).find(|(p, _)| *p < down_u || *p > up_u) {
        return Err(Skip::LimitMismatch { price: px(p), lower: px(down_u), upper: px(up_u) });
    }

    let last_u = if q.price > 0.0 { units(q.price, scale) } else { prev_u };

    Ok(Snapshot {
        symbol: meta.symbol.clone(),
        name: meta.name.clone(),
        ts,
        seq: ms,
        bids: bids.iter().map(|(p, v)| (px(*p), *v)).collect(),
        asks: asks.iter().map(|(p, v)| (px(*p), *v)).collect(),
        last_price: px(last_u),
        cumulative_volume: vol_shares,
        upper_limit: px(up_u),
        lower_limit: px(down_u),
        prev_close: px(prev_u),
        suspended: bids.is_empty() && asks.is_empty() && vol_shares == 0,
        call_auction: false,
        auction: None,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn d() -> NaiveDate {
        NaiveDate::from_ymd_opt(2026, 11, 20).unwrap()
    }

    fn q(code: &str, market: u16, t: &str) -> RawQuote {
        // Shape taken from a live 600000 quote on 2026-11-20.
        RawQuote {
            market,
            code: code.into(),
            price: 10.87,
            last_close: 10.7,
            vol_lots: 1_008_667.0,
            servertime: t.into(),
            bids: [(10.86, 493.0), (10.85, 100.0), (10.84, 100.0), (10.83, 100.0), (10.8, 239.0)],
            asks: [(10.87, 1439.0), (10.88, 10.0), (10.89, 10.0), (10.90, 10.0), (10.91, 1397.0)],
            auction: None,
        }
    }

    #[test]
    fn normalizes_units_limits_and_time() {
        let meta = SymbolMeta::parse("600000.SH", false).unwrap();
        let s = normalize(&q("600000", 1, "14:30:02.010"), &meta, d()).unwrap();
        assert_eq!(s.ts, "2026-11-20T14:30:02.010+08:00");
        assert_eq!(s.seq, (14 * 3600 + 30 * 60 + 2) * 1000 + 10);
        assert_eq!(s.cumulative_volume, 100_866_700); // 手 -> shares
        assert_eq!(s.bids[0], ("10.86".to_string(), 49_300));
        assert_eq!(s.asks[4], ("10.91".to_string(), 139_700));
        assert_eq!((s.upper_limit.as_str(), s.lower_limit.as_str()), ("11.77", "9.63")); // 10.70 * 1.1 / 0.9
        assert_eq!(s.prev_close, "10.70");
        assert!(!s.suspended);
    }

    #[test]
    fn star_board_uses_20pct_and_sealed_board_keeps_empty_asks() {
        let meta = SymbolMeta::parse("688981.SH", false).unwrap();
        let mut r = q("688981", 1, "10:00:00.000");
        r.last_close = 100.0;
        r.price = 120.0;
        r.bids = [(120.0, 5000.0), (119.99, 10.0), (0.0, 0.0), (0.0, 0.0), (0.0, 0.0)];
        r.asks = [(0.0, 0.0); 5];
        let s = normalize(&r, &meta, d()).unwrap();
        assert_eq!((s.upper_limit.as_str(), s.lower_limit.as_str()), ("120.00", "80.00"));
        assert!(s.asks.is_empty());
        assert_eq!(s.bids.len(), 2);
    }

    #[test]
    fn skips_crossed_out_of_session_and_limit_mismatch() {
        let meta = SymbolMeta::parse("600000.SH", false).unwrap();
        assert_eq!(normalize(&q("600000", 1, "09:14:00.000"), &meta, d()), Err(Skip::OutsideSession));
        assert_eq!(normalize(&q("600000", 1, "15:30:02.010"), &meta, d()), Err(Skip::OutsideSession));
        let mut crossed = q("600000", 1, "10:58:00.000");
        crossed.asks[0] = (10.86, 100.0);
        assert_eq!(normalize(&crossed, &meta, d()), Err(Skip::CrossedBook));
        let mut wrong = q("600000", 1, "10:00:00.000");
        wrong.asks[4] = (12.00, 1.0); // above +10%: wrong board/ST/IPO assumption
        assert!(matches!(normalize(&wrong, &meta, d()), Err(Skip::LimitMismatch { .. })));
    }

    #[test]
    fn call_auction_snapshots_carry_the_indicative_match() {
        let meta = SymbolMeta::parse("600000.SH", false).unwrap();
        let mut r = q("600000", 1, "09:22:00.000");
        r.price = 0.0;
        r.vol_lots = 0.0;
        r.auction = Some((10.80, 2266.0, 1256.0, 0.0));
        let s = normalize(&r, &meta, d()).unwrap();
        assert!(s.call_auction && !s.suspended && s.bids.is_empty() && s.asks.is_empty());
        assert_eq!(s.last_price, "10.80");
        assert_eq!(s.auction, Some(AuctionInfo { price: "10.80".into(), volume: 226_600, unmatched_buy: 125_600,
                                                 unmatched_sell: 0 }));
        // closing auction: last price stays the last continuous trade until 15:00
        let mut c = q("600000", 1, "14:59:58.000");
        c.auction = Some((10.90, 1634.0, 0.0, 3.0));
        let s = normalize(&c, &meta, d()).unwrap();
        assert!(s.call_auction && s.last_price == "10.87" && s.cumulative_volume == 100_866_700);
        // the encoding lingering at 09:25:01 is still the auction; a normal book at 09:25:03 is priced
        let mut late = q("600000", 1, "09:25:01.000");
        late.auction = Some((10.80, 2266.0, 0.0, 0.0));
        assert!(normalize(&late, &meta, d()).unwrap().call_auction);
        assert!(!normalize(&q("600000", 1, "09:25:03.000"), &meta, d()).unwrap().call_auction);
        let json = serde_json::to_string(&normalize(&q("600000", 1, "10:00:00.000"), &meta, d()).unwrap()).unwrap();
        assert!(!json.contains("call_auction") && !json.contains("auction"));
    }

    #[test]
    fn suspended_when_book_and_volume_are_empty() {
        let meta = SymbolMeta::parse("000001.SZ", false).unwrap();
        let mut r = q("000001", 0, "10:00:00.000");
        r.price = 0.0;
        r.vol_lots = 0.0;
        r.bids = [(0.0, 0.0); 5];
        r.asks = [(0.0, 0.0); 5];
        let s = normalize(&r, &meta, d()).unwrap();
        assert!(s.suspended);
        assert_eq!(s.last_price, "10.70");
    }

    #[test]
    fn rejects_unsupported_symbols() {
        assert!(SymbolMeta::parse("830799.BJ", false).is_err());
        assert!(SymbolMeta::parse("000300.SH", false).is_err()); // index on the SH market
        assert!(SymbolMeta::parse("600000", false).is_err());
    }

    #[test]
    fn etf_uses_three_decimals_and_ten_pct_band() {
        let mut meta = SymbolMeta::parse("510300.SH", false).unwrap();
        meta.name = Some("沪深300ETF".into());
        assert_eq!(meta.kind, Kind::Fund);
        let mut r = q("510300", 1, "10:00:00.000");
        r.last_close = 4.123;
        r.price = 4.131;
        r.bids = [(4.130, 5000.0), (4.129, 100.0), (0.0, 0.0), (0.0, 0.0), (0.0, 0.0)];
        r.asks = [(4.131, 3000.0), (4.132, 100.0), (0.0, 0.0), (0.0, 0.0), (0.0, 0.0)];
        let s = normalize(&r, &meta, d()).unwrap();
        assert_eq!((s.upper_limit.as_str(), s.lower_limit.as_str()), ("4.535", "3.711")); // 4.5353 / 3.7107
        assert_eq!(s.last_price, "4.131");
        assert_eq!(s.name.as_deref(), Some("沪深300ETF"));
        assert_eq!(s.bids[0], ("4.130".to_string(), 500_000));
    }
}
