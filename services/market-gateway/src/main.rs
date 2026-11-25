//! Agora market gateway.
//!
//! Polls TDX (通达信) servers through rustdx-complete for L1 5-level quotes every `POLL_MS`, normalizes them
//! into Agora snapshots and publishes them to a Redis Stream (`md:l1`, field `data` = JSON). Every published
//! snapshot is also appended to a daily JSONL tape that `ReplayFeed.from_jsonl` and the Nautilus bridge can read.
//!
//! Env:
//!   REDIS_URL          redis://redis:6379/0
//!   GATEWAY_SYMBOLS    comma list, e.g. 600000.SH,000001.SZ,300750.SZ   (max a few hundred: 60 per request)
//!   POLL_MS            3000
//!   STREAM_KEY         md:l1
//!   STREAM_MAXLEN      500000
//!   TAPE_DIR           /data/tape   (optional)
//!   TDX_SERVER         ip:port      (optional; default = rustdx failover list)
//!   FORCE_SESSION      1 to poll outside trading hours (debugging only; snapshots are still session-filtered)

mod normalize;

use std::collections::HashMap;
use std::fs::{self, File, OpenOptions};
use std::io::{BufWriter, Write};
use std::path::PathBuf;
use std::thread::sleep;
use std::time::{Duration, Instant};

use anyhow::{bail, Context, Result};
use chrono::{DateTime, FixedOffset, NaiveDate, NaiveTime, Utc};
use log::{error, info, warn};
use rustdx_complete::calendar::TradingCalendar;
use rustdx_complete::limit;
use rustdx_complete::tcp::stock::{SecurityList, SecurityQuotes};
use rustdx_complete::tcp::{Tcp, TcpConfig, Tdx};

use normalize::{normalize, parse_servertime, RawQuote, Skip, Snapshot, SymbolMeta};

const BATCH: usize = 60; // servers may truncate or rate-limit above 60 codes per request
const MAX_FUTURE_SKEW: i64 = 60; // seconds: a servertime ahead of our clock is yesterday's data before the open

struct Config {
    redis_url: String,
    symbols: Vec<String>,
    poll: Duration,
    stream_key: String,
    stream_maxlen: usize,
    tape_dir: Option<PathBuf>,
    tdx_server: Option<std::net::SocketAddr>,
    force_session: bool,
}

impl Config {
    fn from_env() -> Result<Self> {
        let env = |k: &str, d: &str| std::env::var(k).unwrap_or_else(|_| d.to_string());
        let symbols: Vec<String> = env("GATEWAY_SYMBOLS", "")
            .split(',')
            .map(|s| s.trim().to_uppercase())
            .filter(|s| !s.is_empty())
            .collect();
        if symbols.is_empty() {
            bail!("GATEWAY_SYMBOLS is empty");
        }
        Ok(Self {
            redis_url: env("REDIS_URL", "redis://127.0.0.1:6379/0"),
            symbols,
            poll: Duration::from_millis(env("POLL_MS", "3000").parse().context("POLL_MS")?),
            stream_key: env("STREAM_KEY", "md:l1"),
            stream_maxlen: env("STREAM_MAXLEN", "500000").parse().context("STREAM_MAXLEN")?,
            tape_dir: std::env::var("TAPE_DIR").ok().filter(|s| !s.is_empty()).map(PathBuf::from),
            tdx_server: match std::env::var("TDX_SERVER") {
                Ok(s) if !s.is_empty() => Some(s.parse().context("TDX_SERVER")?),
                _ => None,
            },
            force_session: env("FORCE_SESSION", "0") == "1",
        })
    }
}

fn cst_now() -> DateTime<FixedOffset> {
    Utc::now().with_timezone(&FixedOffset::east_opt(8 * 3600).unwrap())
}

fn in_poll_window(t: NaiveTime) -> bool {
    t >= NaiveTime::from_hms_opt(9, 24, 0).unwrap() && t <= NaiveTime::from_hms_opt(15, 2, 0).unwrap()
}

// ------------------------------------------------------------------ TDX
fn connect(cfg: &Config) -> Result<Tcp> {
    let mut tc = TcpConfig::default();
    tc.ip = cfg.tdx_server;
    Tcp::with_config(&tc).context("connect TDX")
}

/// Resolves ST status from current security names (names change on 戴帽/摘帽, so refresh daily).
fn resolve_symbols(tcp: &mut Tcp, symbols: &[String]) -> Result<Vec<SymbolMeta>> {
    let mut names: HashMap<(u16, String), String> = HashMap::new();
    for market in [0u16, 1u16] {
        let mut start: u16 = 0;
        for _page in 0..40 {
            let mut list = SecurityList::new(market, start);
            let page = list.recv_parsed(tcp).context("security list")?;
            if page.is_empty() {
                break;
            }
            for s in page.iter() {
                names.insert((market, s.code.clone()), s.display_name());
            }
            start = start.saturating_add(page.len() as u16);
        }
    }
    let mut metas = Vec::with_capacity(symbols.len());
    for sym in symbols {
        let probe = SymbolMeta::parse(sym, false).map_err(anyhow::Error::msg)?;
        let name = names.get(&(probe.market, probe.code.clone())).cloned();
        let is_stock = matches!(probe.kind, normalize::Kind::Stock(_));
        let st = is_stock && name.as_deref().map(limit::is_st_name).unwrap_or(false);
        if name.is_none() {
            warn!("{sym}: not found in security list; assuming non-ST");
        }
        if st {
            info!("{sym}: {} flagged ST", name.as_deref().unwrap_or(""));
        }
        metas.push(SymbolMeta { st, name, ..probe });
    }
    Ok(metas)
}

// ------------------------------------------------------------------ sinks
struct Sink {
    redis_url: String,
    conn: Option<redis::Connection>,
    stream_key: String,
    maxlen: usize,
    tape_dir: Option<PathBuf>,
    tape: Option<(NaiveDate, BufWriter<File>)>,
}

impl Sink {
    fn redis(&mut self) -> Result<&mut redis::Connection> {
        if self.conn.is_none() {
            let client = redis::Client::open(self.redis_url.as_str())?;
            self.conn = Some(client.get_connection().context("connect redis")?);
        }
        Ok(self.conn.as_mut().unwrap())
    }

    fn publish(&mut self, date: NaiveDate, snaps: &[Snapshot]) -> Result<()> {
        if snaps.is_empty() {
            return Ok(());
        }
        let lines: Vec<String> = snaps.iter().map(serde_json::to_string).collect::<Result<_, _>>()?;
        self.write_tape(date, &lines)?;
        let (key, maxlen) = (self.stream_key.clone(), self.maxlen);
        let mut pipe = redis::pipe();
        for line in &lines {
            pipe.cmd("XADD").arg(&key).arg("MAXLEN").arg("~").arg(maxlen).arg("*").arg("data").arg(line).ignore();
        }
        pipe.cmd("HSET").arg("md:status").arg("last_publish").arg(cst_now().to_rfc3339()).ignore();
        let result: redis::RedisResult<()> = pipe.query(self.redis()?);
        if let Err(e) = result {
            self.conn = None; // reconnect next time; the tape still has the data
            return Err(e).context("publish to redis");
        }
        Ok(())
    }

    fn write_tape(&mut self, date: NaiveDate, lines: &[String]) -> Result<()> {
        let Some(dir) = &self.tape_dir else { return Ok(()) };
        if self.tape.as_ref().map(|(d, _)| *d) != Some(date) {
            fs::create_dir_all(dir)?;
            let path = dir.join(format!("{}.jsonl", date.format("%Y-%m-%d")));
            let f = OpenOptions::new().create(true).append(true).open(&path).with_context(|| format!("{path:?}"))?;
            self.tape = Some((date, BufWriter::new(f)));
        }
        let (_, w) = self.tape.as_mut().unwrap();
        for l in lines {
            writeln!(w, "{l}")?;
        }
        w.flush()?;
        Ok(())
    }
}

// ------------------------------------------------------------------ main loop
fn main() -> Result<()> {
    env_logger::Builder::from_env(env_logger::Env::default().default_filter_or("info")).init();
    let cfg = Config::from_env()?;
    // Validate symbols up front so a typo fails at boot, not mid-session.
    for s in &cfg.symbols {
        SymbolMeta::parse(s, false).map_err(anyhow::Error::msg)?;
    }
    info!("watching {} symbols, poll {:?}", cfg.symbols.len(), cfg.poll);

    let mut sink = Sink {
        redis_url: cfg.redis_url.clone(),
        conn: None,
        stream_key: cfg.stream_key.clone(),
        maxlen: cfg.stream_maxlen,
        tape_dir: cfg.tape_dir.clone(),
        tape: None,
    };
    let mut tcp: Option<Tcp> = None;
    let mut backoff = Duration::from_secs(1);
    let mut metas: Vec<SymbolMeta> = Vec::new();
    let mut metas_date: Option<NaiveDate> = None;
    let mut last_seq: HashMap<String, u64> = HashMap::new();
    let mut skips: HashMap<String, u64> = HashMap::new();

    loop {
        let tick = Instant::now();
        let now = cst_now();
        let date = now.date_naive();
        if !cfg.force_session && (!TradingCalendar::is_trading_day(&date) || !in_poll_window(now.time())) {
            if !skips.is_empty() {
                info!("session skips: {skips:?}");
                skips.clear();
            }
            sleep(Duration::from_secs(10));
            continue;
        }
        if !TradingCalendar::has_holiday_data(&date) {
            warn!("rustdx-complete has no holiday data for {date}; update the crate");
        }

        // (Re)connect with exponential backoff; failover across servers happens inside rustdx.
        if tcp.is_none() {
            match connect(&cfg) {
                Ok(t) => {
                    info!("connected to TDX");
                    tcp = Some(t);
                    backoff = Duration::from_secs(1);
                }
                Err(e) => {
                    error!("{e:#}; retry in {backoff:?}");
                    sleep(backoff);
                    backoff = (backoff * 2).min(Duration::from_secs(30));
                    continue;
                }
            }
        }
        let conn = tcp.as_mut().unwrap();

        if metas_date != Some(date) {
            match resolve_symbols(conn, &cfg.symbols) {
                Ok(m) => {
                    metas = m;
                    metas_date = Some(date);
                    last_seq.clear();
                }
                Err(e) => {
                    error!("resolve symbols: {e:#}");
                    tcp = None;
                    continue;
                }
            }
        }

        let by_code: HashMap<(u16, &str), &SymbolMeta> =
            metas.iter().map(|m| ((m.market, m.code.as_str()), m)).collect();
        let mut batch: Vec<Snapshot> = Vec::new();
        let mut failed = false;
        for chunk in metas.chunks(BATCH) {
            let mut req = SecurityQuotes::new(chunk.iter().map(|m| (m.market, m.code.as_str())).collect());
            let data = match req.recv_parsed(conn) {
                Ok(d) => d,
                Err(e) => {
                    error!("quotes: {e:#}");
                    failed = true;
                    break;
                }
            };
            for q in data.iter() {
                let raw = RawQuote::from(q);
                let Some(meta) = by_code.get(&(raw.market, raw.code.as_str())) else { continue };
                // Before the open, TDX still serves yesterday's 15:00 book: its time is ahead of our clock.
                if let Some(t) = parse_servertime(&raw.servertime) {
                    if (t - now.time()).num_seconds() > MAX_FUTURE_SKEW {
                        *skips.entry("stale_previous_session".into()).or_default() += 1;
                        continue;
                    }
                }
                match normalize(&raw, meta, date) {
                    Ok(s) => {
                        let prev = last_seq.get(&s.symbol).copied().unwrap_or(0);
                        if s.seq > prev {
                            last_seq.insert(s.symbol.clone(), s.seq);
                            batch.push(s);
                        }
                    }
                    Err(Skip::LimitMismatch { price, lower, upper }) => {
                        warn!("{}: level {price} outside computed [{lower}, {upper}] (IPO window or ST change?)",
                              meta.symbol);
                        *skips.entry("limit_mismatch".into()).or_default() += 1;
                    }
                    Err(skip) => {
                        *skips.entry(format!("{skip:?}").split('(').next().unwrap().into()).or_default() += 1;
                    }
                }
            }
        }
        if failed {
            tcp = None;
            continue;
        }
        if let Err(e) = sink.publish(date, &batch) {
            error!("{e:#}");
        }
        log::debug!("published {} snapshots in {:?}; skips so far {skips:?}", batch.len(), tick.elapsed());

        let elapsed = tick.elapsed();
        if elapsed < cfg.poll {
            sleep(cfg.poll - elapsed);
        } else {
            warn!("poll took {elapsed:?} (> {:?}); consider fewer symbols", cfg.poll);
        }
    }
}
