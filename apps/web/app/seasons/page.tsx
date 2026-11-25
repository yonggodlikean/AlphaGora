"use client";
import Link from "next/link";
import { useEffect, useState } from "react";
import { api, type SeasonInfo, type Standing } from "@/lib/api";
import { pct, upColor, yuan } from "@/lib/format";

type Report = SeasonInfo & { standings: Standing[] };

const MEDAL = ["🥇", "🥈", "🥉"];

export default function SeasonsPage() {
  const [seasons, setSeasons] = useState<SeasonInfo[] | null>(null);
  const [sel, setSel] = useState<string | null>(null);
  const [report, setReport] = useState<Report | null>(null);

  useEffect(() => {
    api<{ seasons: SeasonInfo[] }>("/api/v1/seasons").then((r) => {
      setSeasons(r.seasons);
      const wanted = new URLSearchParams(window.location.search).get("s");
      const first = r.seasons.find((x) => x.slug === wanted) ?? r.seasons.find((x) => x.status === "settled") ?? r.seasons[0];
      setSel(first?.slug ?? null);
    }).catch(() => setSeasons([]));
  }, []);
  useEffect(() => {
    if (!sel) return;
    setReport(null);
    api<Report>(`/api/v1/seasons/${sel}`).then(setReport).catch(() => setReport(null));
    window.history.replaceState(null, "", `/seasons?s=${sel}`);
  }, [sel]);

  const settled = report?.status === "settled";
  const players = report?.standings.filter((s) => !s.is_benchmark) ?? [];
  const bench = report?.standings.find((s) => s.is_benchmark);

  return (
    <main className="wrap" style={{ gap: 20 }}>
      <div className="stack" style={{ gap: 6 }}>
        <h1 style={{ fontSize: 28 }}>往届战报</h1>
        <p className="muted" style={{ margin: 0 }}>每季约三个月：冬（12–2 月）、春（3–5 月）、夏（6–8 月）、秋（9–11 月）。季末按累计收益定名次，下一季所有队伍重新拿 100 万。</p>
      </div>
      {seasons && seasons.length > 0 ? (
        <div className="tabs" role="tablist" aria-label="赛季" style={{ alignSelf: "flex-start", flexWrap: "wrap" }}>
          {seasons.map((s) => (
            <button key={s.slug} type="button" role="tab" className="tab" aria-selected={sel === s.slug} onClick={() => setSel(s.slug)}>
              {s.name}{s.current ? " · 进行中" : ""}
            </button>
          ))}
        </div>
      ) : null}
      {seasons && seasons.length === 0 ? <p className="empty">还没有赛季。</p> : null}

      {report && !settled ? (
        <section className="card stack">
          <h2>{report.name} 进行中</h2>
          <p className="muted" style={{ margin: 0 }}>{report.start} 开赛，{report.end} 收官。收官后的第一个交易日公布最终名次。</p>
          <Link className="btn btn-gold" href="/" style={{ alignSelf: "flex-start" }}>看实时排行榜</Link>
        </section>
      ) : null}

      {report && settled ? (
        <section className="card stack" aria-labelledby="final-title" style={{ gap: 12 }}>
          <div className="between">
            <h2 id="final-title">{report.name} · 最终排名</h2>
            <span className="small muted">{report.start} – {report.end}{bench ? ` · 沪深300 躺平 ${pct(bench.cum_return)}` : ""}</span>
          </div>
          {players.length === 0 ? <p className="empty">这一季没有队伍完赛。</p> : null}
          {players.map((s) => (
            <Link key={s.portfolio_id} href={`/portfolio/${s.portfolio_id}`} className="between"
              style={{ textDecoration: "none", padding: "10px 4px", borderBottom: "1px solid #1f222b", gap: 12 }}>
              <span style={{ display: "flex", alignItems: "center", gap: 12, minWidth: 0 }}>
                <span className="mono bold" style={{ width: 32, color: (s.rank ?? 99) <= 3 ? "var(--gold)" : "var(--muted)" }}>
                  {s.rank && s.rank <= 3 ? MEDAL[s.rank - 1] : s.rank}
                </span>
                <span className="bold" style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{s.name}</span>
                {s.excess_return > 0 && s.trades > 0 ? <span className="pill pill-gold small">跑赢沪深300</span> : null}
              </span>
              <span className="mono" style={{ display: "flex", gap: 16 }}>
                <span className="bold" style={{ color: upColor(s.cum_return) }}>{pct(s.cum_return)}</span>
                <span className="muted small col-excess">¥{yuan(s.final_equity)} · {s.trades} 笔</span>
              </span>
            </Link>
          ))}
        </section>
      ) : null}
    </main>
  );
}
