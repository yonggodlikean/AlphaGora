//! Agora market gateway.
//!
//! Polls TDX (通达信) servers through rustdx-complete for L1 5-level quotes every `POLL_MS`, normalizes them
//! into Agora snapshots and publishes them to a Redis Stream (`md:l1`, field `data` = JSON). Every published
//! snapshot is also appended to a daily gzip JSONL tape that `ReplayFeed.from_jsonl` and the Nautilus bridge read.
//!
//! The universe is index constituents (CSI files, refreshed daily) plus extra symbols. It is split into
//! 60-code requests across `GATEWAY_CONNECTIONS` worker threads, each with its own TDX connection, so ~1,300
//! symbols fit in a 3s cycle even from a server far from the TDX hosts.
//!
//! Env:
//!   REDIS_URL             redis://redis:6379/0
//!   GATEWAY_INDICES       CSI index codes, e.g. 000300,000016,000852 (CSI 300, SSE 50, CSI 1000)
//!   GATEWAY_SYMBOLS       extra symbols, e.g. 510300.SH (the benchmark ETF); alone = a fixed universe
//!   GATEWAY_CONNECTIONS   4    parallel TDX connections
//!   POLL_MS               3000
//!   STREAM_KEY            md:l1
//!   STREAM_MAXLEN         30000  (the engine consumes live; this is about a minute of headroom)
//!   TAPE_DIR              /data/tape   (optional; also holds universe.json, the last good universe)
//!   TAPE_KEEP_DAYS        7
//!   CSI_URL               constituent file URL template with {code}
//!   TDX_SERVER            ip:port      (optional; pin every connection to one server)
//!   FORCE_SESSION         1 to poll outside trading hours (debugging only; snapshots are still session-filtered)

mod normalize;
mod universe;

use std::collections::{HashMap, HashSet};
use std::fs::{self, File, OpenOptions};
use std::io::{BufWriter, Write};
use std::net::SocketAddr;
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::{self, Receiver, RecvTimeoutError, Sender};
use std::sync::Arc;
use std::thread::{self, sleep, JoinHandle};
use std::time::{Duration, Instant};

use anyhow::{bail, Context, Result};
use chrono::{DateTime, FixedOffset, NaiveDate, NaiveTime, Utc};
use flate2::write::GzEncoder;
use flate2::Compression;
use log::{debug, error, info, warn};
use rustdx_complete::calendar::TradingCalendar;
use rustdx_complete::limit;
use rustdx_complete::tcp::ip::STOCK_IP;
use rustdx_complete::tcp::stock::{SecurityList, SecurityQuotes};
use rustdx_complete::tcp::{Tcp, TcpConfig, Tdx};

use normalize::{normalize, parse_servertime, RawQuote, Skip, Snapshot, SymbolMeta};

const BATCH: usize = 60; // servers may truncate or rate-limit above 60 codes per request
const MAX_FUTURE_SKEW: i64 = 60; // seconds: a servertime ahead of our clock is yesterday's data before the open

#[derive(Clone)]
struct Config {
    redis_url: String,
    indices: Vec<String>,
    extras: Vec<String>,
    connections: usize,
    poll: Duration,
    stream_key: String,
    stream_maxlen: usize,
    tape_dir: Option<PathBuf>,
    tape_keep_days: i64,
    csi_url: String,
    tdx_server: Option<SocketAddr>,
    force_session: bool,
}

fn list(s: &str, upper: bool) -> Vec<String> {
    s.split(',').map(|x| if upper { x.trim().to_uppercase() } else { x.trim().to_string() })
        .filter(|x| !x.is_empty()).collect()
}

impl Config {
    fn from_env() -> Result<Self> {
        let env = |k: &str, d: &str| std::env::var(k).unwrap_or_else(|_| d.to_string());
        let cfg = Self {
            redis_url: env("REDIS_URL", "redis://127.0.0.1:6379/0"),
            indices: list(&env("GATEWAY_INDICES", ""), false),
            extras: list(&env("GATEWAY_SYMBOLS", ""), true),
            connections: env("GATEWAY_CONNECTIONS", "4").parse::<usize>().context("GATEWAY_CONNECTIONS")?.clamp(1, 16),
            poll: Duration::from_millis(env("POLL_MS", "3000").parse().context("POLL_MS")?),
            stream_key: env("STREAM_KEY", "md:l1"),
            stream_maxlen: env("STREAM_MAXLEN", "30000").parse().context("STREAM_MAXLEN")?,
            tape_dir: std::env::var("TAPE_DIR").ok().filter(|s| !s.is_empty()).map(PathBuf::from),
            tape_keep_days: env("TAPE_KEEP_DAYS", "7").parse().context("TAPE_KEEP_DAYS")?,
            csi_url: env("CSI_URL", universe::DEFAULT_CSI_URL),
            tdx_server: match std::env::var("TDX_SERVER") {
                Ok(s) if !s.is_empty() => Some(s.parse().context("TDX_SERVER")?),
                _ => None,
            },
            force_session: env("FORCE_SESSION", "0") == "1",
        };
        if cfg.indices.is_empty() && cfg.extras.is_empty() {
            bail!("set GATEWAY_INDICES (e.g. 000300,000016,000852) and/or GATEWAY_SYMBOLS");
        }
        Ok(cfg)
    }
}

fn cst_now() -> DateTime<FixedOffset> {
    Utc::now().with_timezone(&FixedOffset::east_opt(8 * 3600).unwrap())
}

fn in_poll_window(t: NaiveTime) -> bool {
    t >= NaiveTime::from_hms_opt(9, 24, 0).unwrap() && t <= NaiveTime::from_hms_opt(15, 2, 0).unwrap()
}

fn session_now(cfg: &Config) -> bool {
    let now = cst_now();
    cfg.force_session || (TradingCalendar::is_trading_day(&now.date_naive()) && in_poll_window(now.time()))
}

// ------------------------------------------------------------------ TDX
fn connect(addr: Option<SocketAddr>) -> Result<Tcp> {
    let mut tc = TcpConfig::default();
    tc.ip = addr;
    Tcp::with_config(&tc).context("connect TDX")
}

/// Which TDX servers answer from here, fastest first. Plain TCP connects in parallel with a short timeout,
/// so dead entries in the built-in list never stall a worker.
fn probe_servers(candidates: &[SocketAddr]) -> Vec<SocketAddr> {
    let (ptx, prx) = mpsc::channel();
    for &addr in candidates {
        let ptx = ptx.clone();
        thread::spawn(move || {
            let t = Instant::now();
            if std::net::TcpStream::connect_timeout(&addr, Duration::from_secs(2)).is_ok() {
                let _ = ptx.send((t.elapsed(), addr));
            }
        });
    }
    drop(ptx);
    let mut ok: Vec<(Duration, SocketAddr)> = prx.iter().collect();
    ok.sort();
    ok.into_iter().map(|(_, a)| a).collect()
}

/// Current names (for ST detection) of the universe, from the full security list. Once per day.
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
    let (mut metas, mut st, mut bad) = (Vec::with_capacity(symbols.len()), 0, Vec::new());
    for sym in symbols {
        let probe = match SymbolMeta::parse(sym, false) {
            Ok(p) => p,
            Err(e) => {
                bad.push(e);
                continue;
            }
        };
        let name = names.get(&(probe.market, probe.code.clone())).cloned();
        let is_stock = matches!(probe.kind, normalize::Kind::Stock(_));
        let is_st = is_stock && name.as_deref().map(limit::is_st_name).unwrap_or(false);
        st += is_st as usize;
        metas.push(SymbolMeta { st: is_st, name, ..probe });
    }
    if !bad.is_empty() {
        warn!("skipped {} unsupported symbols, e.g. {}", bad.len(), bad[0]);
    }
    info!("universe resolved: {} symbols ({} ST)", metas.len(), st);
    Ok(metas)
}

// ------------------------------------------------------------------ workers
struct Batch {
    worker: usize,
    snaps: Vec<Snapshot>,
    skips: HashMap<String, u64>,
    took: Duration,
}

/// One TDX connection polling a fixed shard of the universe until `stop` is set.
fn spawn_worker(id: usize, metas: Vec<SymbolMeta>, cfg: Config, healthy: Vec<SocketAddr>, tx: Sender<Batch>,
                stop: Arc<AtomicBool>) -> JoinHandle<()> {
    thread::Builder::new().name(format!("tdx-{id}")).spawn(move || {
        // Each worker starts at a different healthy server so the load spreads; failover walks the healthy
        // list from there, then the rest of the built-in list as a last resort.
        let servers: Vec<Option<SocketAddr>> = match cfg.tdx_server {
            Some(a) => vec![Some(a)],
            None => {
                let mut v: Vec<Option<SocketAddr>> = (0..healthy.len()).map(|i| Some(healthy[(i + id) % healthy.len()])).collect();
                v.extend(STOCK_IP.iter().filter(|a| !healthy.contains(a)).map(|a| Some(*a)));
                v
            }
        };
        let mut cursor = 0usize;
        let mut tcp: Option<Tcp> = None;
        let mut last_seq: HashMap<String, u64> = HashMap::new();
        let mut warned: HashSet<String> = HashSet::new();
        let mut day: Option<NaiveDate> = None;
        while !stop.load(Ordering::Relaxed) {
            let tick = Instant::now();
            if !session_now(&cfg) {
                sleep(Duration::from_secs(5));
                continue;
            }
            let now = cst_now();
            let date = now.date_naive();
            if day != Some(date) {
                day = Some(date);
                last_seq.clear();
                warned.clear();
            }
            if tcp.is_none() {
                for _ in 0..servers.len() {
                    match connect(servers[cursor % servers.len()]) {
                        Ok(t) => {
                            debug!("worker {id}: connected to {:?}", servers[cursor % servers.len()]);
                            tcp = Some(t);
                            break;
                        }
                        Err(e) => {
                            debug!("worker {id}: {:?} unreachable ({e:#})", servers[cursor % servers.len()]);
                            cursor += 1
                        }
                    }
                }
                if tcp.is_none() {
                    error!("worker {id}: no TDX server reachable; retrying in 10s");
                    sleep(Duration::from_secs(10));
                    continue;
                }
            }
            let conn = tcp.as_mut().unwrap();
            let by_code: HashMap<(u16, &str), &SymbolMeta> =
                metas.iter().map(|m| ((m.market, m.code.as_str()), m)).collect();
            let mut batch = Batch { worker: id, snaps: Vec::new(), skips: HashMap::new(), took: Duration::ZERO };
            let mut failed = false;
            for chunk in metas.chunks(BATCH) {
                let mut req = SecurityQuotes::new(chunk.iter().map(|m| (m.market, m.code.as_str())).collect());
                let data = match req.recv_parsed(conn) {
                    Ok(d) => d,
                    Err(e) => {
                        warn!("worker {id}: quotes failed ({e:#}); switching server");
                        failed = true;
                        break;
                    }
                };
                for q in data.iter() {
                    let raw = RawQuote::from(q);
                    let Some(meta) = by_code.get(&(raw.market, raw.code.as_str())) else { continue };
                    if let Some(t) = parse_servertime(&raw.servertime) {
                        if (t - now.time()).num_seconds() > MAX_FUTURE_SKEW {
                            *batch.skips.entry("stale_previous_session".into()).or_default() += 1;
                            continue;
                        }
                    }
                    match normalize(&raw, meta, date) {
                        Ok(s) => {
                            if s.seq > last_seq.get(&s.symbol).copied().unwrap_or(0) {
                                last_seq.insert(s.symbol.clone(), s.seq);
                                batch.snaps.push(s);
                            }
                        }
                        Err(Skip::LimitMismatch { price, lower, upper }) => {
                            if warned.insert(meta.symbol.clone()) {
                                warn!("{}: level {price} outside computed [{lower}, {upper}] (IPO window or ST change?); \
                                       dropping its frames today", meta.symbol);
                            }
                            *batch.skips.entry("limit_mismatch".into()).or_default() += 1;
                        }
                        Err(skip) => {
                            *batch.skips.entry(format!("{skip:?}").split('(').next().unwrap().into()).or_default() += 1;
                        }
                    }
                }
            }
            if failed {
                tcp = None;
                cursor += 1;
            }
            batch.took = tick.elapsed();
            if tx.send(batch).is_err() {
                return;
            }
            let elapsed = tick.elapsed();
            if elapsed < cfg.poll {
                sleep(cfg.poll - elapsed);
            }
        }
    }).expect("spawn worker")
}

struct Pool {
    stop: Arc<AtomicBool>,
    handles: Vec<JoinHandle<()>>,
}

impl Pool {
    fn start(metas: &[SymbolMeta], cfg: &Config, healthy: &[SocketAddr], tx: &Sender<Batch>) -> Self {
        let n = cfg.connections.min(metas.len().div_ceil(BATCH)).max(1);
        // Deal 60-code chunks round-robin so every worker has the same number of requests.
        let mut shards: Vec<Vec<SymbolMeta>> = vec![Vec::new(); n];
        for (i, chunk) in metas.chunks(BATCH).enumerate() {
            shards[i % n].extend_from_slice(chunk);
        }
        let stop = Arc::new(AtomicBool::new(false));
        let handles = shards.into_iter().enumerate()
            .map(|(id, shard)| spawn_worker(id, shard, cfg.clone(), healthy.to_vec(), tx.clone(), stop.clone()))
            .collect();
        info!("{n} TDX workers polling {} symbols every {:?}", metas.len(), cfg.poll);
        Self { stop, handles }
    }

    fn shutdown(self) {
        self.stop.store(true, Ordering::Relaxed);
        for h in self.handles {
            let _ = h.join();
        }
    }
}

// ------------------------------------------------------------------ sinks
struct Sink {
    redis_url: String,
    conn: Option<redis::Connection>,
    stream_key: String,
    maxlen: usize,
    tape_dir: Option<PathBuf>,
    keep_days: i64,
    tape: Option<(NaiveDate, GzEncoder<BufWriter<File>>)>,
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
        if let Err(e) = self.write_tape(date, &lines) {
            warn!("tape: {e:#}");
        }
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
        let Some(dir) = self.tape_dir.clone() else { return Ok(()) };
        if self.tape.as_ref().map(|(d, _)| *d) != Some(date) {
            if let Some((_, old)) = self.tape.take() {
                old.finish()?.flush()?; // write the gzip trailer of the previous day
            }
            fs::create_dir_all(&dir)?;
            self.prune_tapes(&dir, date);
            let path = dir.join(format!("{}.jsonl.gz", date.format("%Y-%m-%d")));
            let f = OpenOptions::new().create(true).append(true).open(&path).with_context(|| format!("{path:?}"))?;
            self.tape = Some((date, GzEncoder::new(BufWriter::new(f), Compression::fast())));
        }
        let (_, w) = self.tape.as_mut().unwrap();
        for l in lines {
            writeln!(w, "{l}")?;
        }
        w.flush()?; // sync flush: a crash loses at most the current batch, and the file stays readable
        Ok(())
    }

    fn prune_tapes(&self, dir: &PathBuf, today: NaiveDate) {
        let Ok(entries) = fs::read_dir(dir) else { return };
        for e in entries.flatten() {
            let name = e.file_name().to_string_lossy().to_string();
            let Some(day) = name.split('.').next().and_then(|d| NaiveDate::parse_from_str(d, "%Y-%m-%d").ok()) else {
                continue;
            };
            if (today - day).num_days() > self.keep_days {
                if fs::remove_file(e.path()).is_ok() {
                    info!("pruned tape {name}");
                }
            }
        }
    }
}

// ------------------------------------------------------------------ main loop
fn main() -> Result<()> {
    env_logger::Builder::from_env(env_logger::Env::default().default_filter_or("info")).init();
    let cfg = Config::from_env()?;
    info!("indices {:?} + {} extra symbols · {} connections · poll {:?}",
          cfg.indices, cfg.extras.len(), cfg.connections, cfg.poll);

    let mut sink = Sink {
        redis_url: cfg.redis_url.clone(),
        conn: None,
        stream_key: cfg.stream_key.clone(),
        maxlen: cfg.stream_maxlen,
        tape_dir: cfg.tape_dir.clone(),
        keep_days: cfg.tape_keep_days,
        tape: None,
    };
    let (tx, rx): (Sender<Batch>, Receiver<Batch>) = mpsc::channel();
    let mut pool: Option<Pool> = None;
    let mut universe_date: Option<NaiveDate> = None;
    let mut stats_at = Instant::now();
    let (mut published, mut worst) = (0u64, Duration::ZERO);
    let mut skips: HashMap<String, u64> = HashMap::new();

    loop {
        let now = cst_now();
        let date = now.date_naive();
        if !session_now(&cfg) {
            if let Some(p) = pool.take() {
                info!("session over: stopping workers (published {published} snapshots, skips {skips:?})");
                p.shutdown();
                (published, worst) = (0, Duration::ZERO);
                skips.clear();
            }
            sleep(Duration::from_secs(10));
            continue;
        }
        if !TradingCalendar::has_holiday_data(&date) {
            warn!("rustdx-complete has no holiday data for {date}; update the crate");
        }

        if universe_date != Some(date) || pool.is_none() {
            let symbols = match universe::build(&cfg.indices, &cfg.extras, &cfg.csi_url, cfg.tape_dir.as_deref(),
                                                &date.to_string()) {
                Ok(s) => s,
                Err(e) => {
                    error!("universe: {e:#}; retry in 60s");
                    sleep(Duration::from_secs(60));
                    continue;
                }
            };
            let metas = match connect(cfg.tdx_server).and_then(|mut t| resolve_symbols(&mut t, &symbols)) {
                Ok(m) => m,
                Err(e) => {
                    error!("resolve symbols: {e:#}; retry in 30s");
                    sleep(Duration::from_secs(30));
                    continue;
                }
            };
            let healthy = probe_servers(&STOCK_IP);
            info!("{} of {} TDX servers reachable, fastest: {:?}", healthy.len(), STOCK_IP.len(),
                  healthy.iter().take(4).collect::<Vec<_>>());
            if let Some(p) = pool.take() {
                p.shutdown();
            }
            pool = Some(Pool::start(&metas, &cfg, &healthy, &tx));
            universe_date = Some(date);
        }

        match rx.recv_timeout(Duration::from_secs(1)) {
            Ok(batch) => {
                debug!("worker {}: {} snapshots, poll took {:?}", batch.worker, batch.snaps.len(), batch.took);
                published += batch.snaps.len() as u64;
                worst = worst.max(batch.took);
                for (k, v) in batch.skips {
                    *skips.entry(k).or_default() += v;
                }
                if batch.took > cfg.poll {
                    warn!("worker {} took {:?} (> {:?}); add GATEWAY_CONNECTIONS", batch.worker, batch.took, cfg.poll);
                }
                if let Err(e) = sink.publish(date, &batch.snaps) {
                    error!("{e:#}");
                }
            }
            Err(RecvTimeoutError::Timeout) => {}
            Err(RecvTimeoutError::Disconnected) => bail!("worker channel closed"),
        }
        if stats_at.elapsed() >= Duration::from_secs(60) {
            info!("last minute: published {published} snapshots, slowest poll {worst:?}, skips {skips:?}");
            stats_at = Instant::now();
            (published, worst) = (0, Duration::ZERO);
            skips.clear();
        }
    }
}
