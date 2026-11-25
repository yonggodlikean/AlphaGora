//! The arena's tradable universe: index constituents (from CSI, the index provider) plus extra symbols.
//!
//! TDX's block files carry CSI 300 / SSE 50 but list CSI 500 / 1000 with zero members, so constituents come
//! from the official CSI files (`{code}cons.xls`), refreshed once per trading day. The last good universe is
//! cached on disk and reused when the download fails, so a CSI outage never empties the arena.

use std::collections::{BTreeMap, BTreeSet};
use std::io::{Cursor, Read};
use std::path::{Path, PathBuf};
use std::time::Duration;

use anyhow::{anyhow, bail, Context, Result};
use calamine::{open_workbook_from_rs, Data, Reader, Xls};
use log::{info, warn};
use serde::{Deserialize, Serialize};

pub const DEFAULT_CSI_URL: &str =
    "https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/file/autofile/cons/{code}cons.xls";

/// "深圳证券交易所" -> SZ, "上海证券交易所" -> SH. Beijing and anything else: not supported by the feed yet.
pub fn exchange_suffix(exchange: &str) -> Option<&'static str> {
    if exchange.contains("深圳") || exchange.eq_ignore_ascii_case("Shenzhen Stock Exchange") {
        Some("SZ")
    } else if exchange.contains("上海") || exchange.eq_ignore_ascii_case("Shanghai Stock Exchange") {
        Some("SH")
    } else {
        None
    }
}

fn cell(d: &Data) -> String {
    match d {
        Data::String(s) => s.trim().to_string(),
        Data::Float(f) => format!("{:06}", *f as i64), // codes stored as numbers lose leading zeros
        Data::Int(i) => format!("{:06}", i),
        other => other.to_string().trim().to_string(),
    }
}

/// Parses a CSI constituent file: columns are located by header text, not position.
pub fn parse_constituents(xls: &[u8]) -> Result<Vec<String>> {
    let mut wb: Xls<_> = open_workbook_from_rs(Cursor::new(xls)).context("not an xls file")?;
    let range = wb.worksheet_range_at(0).ok_or_else(|| anyhow!("no worksheet"))??;
    let mut rows = range.rows();
    let header: Vec<String> = rows.next().ok_or_else(|| anyhow!("empty sheet"))?.iter().map(cell).collect();
    let find = |pred: &dyn Fn(&str) -> bool| header.iter().position(|h| pred(h));
    let code_i = find(&|h| (h.contains("成份券代码") || h.contains("成分券代码")) || h.contains("Constituent Code"))
        .ok_or_else(|| anyhow!("no constituent code column in {header:?}"))?;
    let ex_i = find(&|h| h.starts_with("交易所") && !h.contains("英文"))
        .ok_or_else(|| anyhow!("no exchange column in {header:?}"))?;
    let mut out = Vec::new();
    for row in rows {
        let code = row.get(code_i).map(cell).unwrap_or_default();
        let ex = row.get(ex_i).map(cell).unwrap_or_default();
        if code.len() != 6 || !code.bytes().all(|b| b.is_ascii_digit()) {
            continue;
        }
        if let Some(sfx) = exchange_suffix(&ex) {
            out.push(format!("{code}.{sfx}"));
        }
    }
    if out.is_empty() {
        bail!("no constituents parsed");
    }
    Ok(out)
}

fn download(url: &str) -> Result<Vec<u8>> {
    let resp = ureq::get(url).timeout(Duration::from_secs(30)).call().with_context(|| format!("GET {url}"))?;
    let mut buf = Vec::new();
    resp.into_reader().take(20 * 1024 * 1024).read_to_end(&mut buf)?;
    Ok(buf)
}

#[derive(Serialize, Deserialize)]
struct Cache {
    date: String,
    indices: Vec<String>,
    symbols: Vec<String>,
    #[serde(default)]
    members: BTreeMap<String, Vec<String>>,
}

/// The universe plus which index each symbol came from (the bots screen CSI 300 / SSE 50 members).
pub struct Universe {
    pub symbols: Vec<String>,
    pub members: BTreeMap<String, Vec<String>>,
}

/// Builds the universe: constituents of `indices` (e.g. ["000300", "000016", "000852"]) ∪ `extras`, sorted.
/// Falls back to the cached list (per index set) when any download fails.
pub fn build(indices: &[String], extras: &[String], url_tmpl: &str, cache_dir: Option<&Path>, today: &str)
    -> Result<Vec<String>> {
    build_with_members(indices, extras, url_tmpl, cache_dir, today).map(|u| u.symbols)
}

pub fn build_with_members(indices: &[String], extras: &[String], url_tmpl: &str, cache_dir: Option<&Path>,
                          today: &str) -> Result<Universe> {
    let cache_path: Option<PathBuf> = cache_dir.map(|d| d.join("universe.json"));
    let mut set: BTreeSet<String> = extras.iter().cloned().collect();
    let mut members: BTreeMap<String, Vec<String>> = BTreeMap::new();
    if indices.is_empty() {
        return Ok(Universe { symbols: set.into_iter().collect(), members });
    }
    let fetched: Result<BTreeMap<String, Vec<String>>> = (|| {
        let mut all = BTreeMap::new();
        for code in indices {
            let syms = parse_constituents(&download(&url_tmpl.replace("{code}", code))?)
                .with_context(|| format!("index {code}"))?;
            info!("index {code}: {} constituents", syms.len());
            all.insert(code.clone(), syms);
        }
        Ok(all)
    })();
    match fetched {
        Ok(all) => {
            set.extend(all.values().flatten().cloned());
            members = all;
            if let Some(p) = &cache_path {
                let c = Cache { date: today.to_string(), indices: indices.to_vec(), symbols: set.iter().cloned().collect(),
                                members: members.clone() };
                if let Err(e) = std::fs::create_dir_all(p.parent().unwrap()).and_then(|_| {
                    std::fs::write(p, serde_json::to_vec_pretty(&c).unwrap_or_default())
                }) {
                    warn!("could not write universe cache {p:?}: {e}");
                }
            }
        }
        Err(e) => {
            let cached: Option<Cache> = cache_path.as_ref()
                .and_then(|p| std::fs::read(p).ok())
                .and_then(|b| serde_json::from_slice(&b).ok())
                .filter(|c: &Cache| c.indices == indices);
            match cached {
                Some(c) => {
                    warn!("constituent download failed ({e:#}); using cached universe from {}", c.date);
                    set.extend(c.symbols);
                    members = c.members;
                }
                None => return Err(e.context("constituent download failed and no cached universe")),
            }
        }
    }
    Ok(Universe { symbols: set.into_iter().collect(), members })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn maps_exchanges() {
        assert_eq!(exchange_suffix("深圳证券交易所"), Some("SZ"));
        assert_eq!(exchange_suffix("上海证券交易所"), Some("SH"));
        assert_eq!(exchange_suffix("北京证券交易所"), None);
    }

    #[test]
    fn extras_only_when_no_indices() {
        let u = build(&[], &["510300.SH".into(), "600000.SH".into()], DEFAULT_CSI_URL, None, "2026-11-25").unwrap();
        assert_eq!(u, vec!["510300.SH", "600000.SH"]);
    }

    #[test]
    fn falls_back_to_cache_and_dedupes() {
        let dir = std::env::temp_dir().join(format!("agora-universe-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let c = Cache { date: "2026-11-24".into(), indices: vec!["000300".into()],
                        symbols: vec!["600000.SH".into(), "000001.SZ".into()], members: BTreeMap::new() };
        std::fs::write(dir.join("universe.json"), serde_json::to_vec(&c).unwrap()).unwrap();
        // unreachable URL -> cached list, merged with extras
        let u = build(&["000300".into()], &["600000.SH".into(), "510300.SH".into()],
                      "http://127.0.0.1:9/{code}.xls", Some(&dir), "2026-11-25").unwrap();
        assert_eq!(u, vec!["000001.SZ", "510300.SH", "600000.SH"]);
        // a different index set never reuses that cache
        assert!(build(&["000852".into()], &[], "http://127.0.0.1:9/{code}.xls", Some(&dir), "2026-11-25").is_err());
        std::fs::remove_dir_all(&dir).ok();
    }

    /// Uses the real CSI file when present (download it to /tmp/000852cons.xls to run this).
    #[test]
    fn parses_real_csi_file_if_available() {
        let Ok(bytes) = std::fs::read("/tmp/000852cons.xls") else { return };
        let syms = parse_constituents(&bytes).unwrap();
        assert_eq!(syms.len(), 1000);
        assert!(syms.iter().all(|s| s.ends_with(".SH") || s.ends_with(".SZ")));
    }
}
