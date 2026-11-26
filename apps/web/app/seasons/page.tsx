"use client";
import Link from "next/link";
import { useEffect, useState } from "react";
import { api, type SeasonInfo, type Standing } from "@/lib/api";
import { pct, upColor, yuan } from "@/lib/format";

type SeasonTrade = { team: string; portfolio_id: string; symbol: string; name: string; pnl: string; pnl_pct: number | null; thesis: string | null; at: string };
type Report = SeasonInfo & {
  standings: Standing[];
  curves?: { portfolio_id: string; name: string; rank: number | null; benchmark: boolean; points: [string, number][] }[];
  best_trade?: SeasonTrade | null; worst_trade?: SeasonTrade | null; recap?: string[];
};
const LINE = ["#f5c451", "#c9a8ff", "#8cc4ff"];

/** Top three and the benchmark over the season, one point per trading day (basis points of return). */
function SeasonChart({ curves }: { curves: NonNullable<Report["curves"]> }) {
  const W = 620, H = 220, P = 10;
  const dates = Array.from(new Set(curves.flatMap((c) => c.points.map((p) => p[0])))).sort();
  if (dates.length < 2) return <p className="empty">这一季的交易日太少，画不出曲线。</p>;
  const vals = curves.flatMap((c) => c.points.map((p) => p[1])).concat(0);
  const lo = Math.min(...vals), hi = Math.max(...vals), span = (hi - lo) || 1;
  const x = (d: string) => P + (dates.indexOf(d) / (dates.length - 1)) * (W - 2 * P);
  const y = (v: number) => P + ((hi - v) / span) * (H - 2 * P);
  return (
    <svg viewBox={`0 0 ${W} ${H}`} width="100%" role="img" aria-label="前三名整季走势" style={{ display: "block" }}>
      <line x1={P} x2={W - P} y1={y(0)} y2={y(0)} stroke="#3a3f4b" />
      {[...curves].reverse().map((c) => (
        <polyline key={c.portfolio_id} fill="none" points={c.points.map(([d, v]) => `${x(d).toFixed(1)},${y(v).toFixed(1)}`).join(" ")}
          stroke={c.benchmark ? "#9aa1ae" : LINE[(c.rank ?? 1) - 1]} strokeWidth={c.rank === 1 ? 2.4 : 1.6}
          strokeDasharray={c.benchmark ? "5 4" : undefined} strokeLinejoin="round" />
      ))}
    </svg>
  );
}

function TradeCard({ t, best }: { t: SeasonTrade; best: boolean }) {
  return (
    <Link href={`/portfolio/${t.portfolio_id}`} className="inner stack" style={{ gap: 6, textDecoration: "none", color: "var(--text)" }}>
      <div className="between">
        <span className="bold">{t.name} · {t.team}</span>
        <span className={`pill ${best ? "pill-up" : "pill-down"}`}>
          {t.pnl_pct != null ? `${pct(t.pnl_pct)} · ` : ""}{best ? "赚" : "亏"} ¥{yuan(String(Math.abs(Number(t.pnl))))}
        </span>
      </div>
      {t.thesis ? <span className="small muted">「{t.thesis}」</span> : null}
    </Link>
  );
}

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

      {report && settled && report.curves && report.curves.length ? (
        <div className="row">
          <section className="card stack grow" style={{ gap: 12 }}>
            <div className="between"><h2>前三名走势</h2><span className="small muted">{report.start} – {report.end}</span></div>
            <div style={{ display: "flex", gap: 16, flexWrap: "wrap", fontSize: 12, color: "var(--muted)" }}>
              {report.curves.map((c) => (
                <span key={c.portfolio_id}>
                  <i style={{ display: "inline-block", width: 18, verticalAlign: "middle", marginRight: 6,
                    borderTop: `2px ${c.benchmark ? "dashed #9aa1ae" : `solid ${LINE[(c.rank ?? 1) - 1]}`}` }} />
                  {c.benchmark ? "" : MEDAL[(c.rank ?? 1) - 1] + " "}{c.name}
                  {c.points.length ? <b className="mono" style={{ marginLeft: 6 }}>{pct(c.points[c.points.length - 1][1] / 10000)}</b> : null}
                </span>
              ))}
            </div>
            <SeasonChart curves={report.curves} />
          </section>
          <div className="side stack" style={{ gap: 20 }}>
            {report.best_trade || report.worst_trade ? (
              <section className="card stack">
                <h2>本季最佳 / 最差一笔</h2>
                {report.best_trade ? <TradeCard t={report.best_trade} best /> : null}
                {report.worst_trade ? <TradeCard t={report.worst_trade} best={false} /> : null}
              </section>
            ) : null}
            {report.recap && report.recap.length ? (
              <section className="card stack">
                <h2>赛季回顾</h2>
                {report.recap.map((l, i) => <p key={i} style={{ margin: 0, color: "var(--text-2)" }}>{l}</p>)}
              </section>
            ) : null}
          </div>
        </div>
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
