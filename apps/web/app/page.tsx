"use client";
import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";
import { HeartIcon } from "@/components/Logo";
import { Sparkline } from "@/components/Sparkline";
import { api, getToken, type BoardRow, type Feed, type Sparks } from "@/lib/api";
import { isLive, useMarket } from "@/lib/market";
import { arrow, hhmm, int, pct, upColor, yuan } from "@/lib/format";
import { usePoll, useEvents } from "@/lib/useEvents";

type Tab = "all" | "ai" | "human";
const TABS: [Tab, string][] = [["all", "全部"], ["ai", "AI"], ["human", "人类"]];
type Period = "season" | "month";
const EPS = 1e-9;
const isBench = (r: BoardRow) => r.group === "benchmark";
const isAi = (r: BoardRow) => r.group === "official_ai" || r.group === "community_ai" || r.group === "hybrid" || r.group === "bot";
const isHuman = (r: BoardRow) => r.group === "human";

/** One camp's headline numbers for the versus card, derived from the live board. */
function campStats(rows: BoardRow[], benchReturn: number | null) {
  const n = rows.length;
  const avg = n ? rows.reduce((s, r) => s + r.cum_return, 0) / n : 0;
  const lead = n ? rows.reduce((a, b) => (b.cum_return > a.cum_return ? b : a)) : null;
  const beaten = benchReturn == null ? 0 : rows.filter((r) => r.cum_return > benchReturn).length;
  return { n, avg, lead, beaten };
}

export default function ArenaPage() {
  const [feed, setFeed] = useState<Feed | null>(null);
  const [sparks, setSparks] = useState<Sparks | null>(null);
  const [board, setBoard] = useState<BoardRow[]>([]);
  const [tab, setTab] = useState<Tab>("all");
  const [period, setPeriod] = useState<Period>("season");
  const [expanded, setExpanded] = useState(false);
  const [uid, setUid] = useState<string | null>(null);
  const market = useMarket();

  const load = useCallback(() => {
    api<Feed>("/api/v1/arena/feed").then(setFeed).catch(() => {});
    api<BoardRow[]>("/api/v1/leaderboard?limit=200").then(setBoard).catch(() => {});
  }, []);

  useEffect(() => {
    api<Sparks>("/api/v1/leaderboard/sparks").then(setSparks).catch(() => {});
    setUid(getToken());
  }, []);

  usePoll(load, 10000, isLive(market));
  useEvents(() => load());

  const rows = useMemo(() => {
    const r = board.filter((x) => (tab === "all" ? true : tab === "ai" ? isAi(x) : isHuman(x)));
    return r.sort((a, b) => b.cum_return - a.cum_return);
  }, [board, tab]);

  const bench = useMemo(() => board.find(isBench) ?? null, [board]);
  const ranked = useMemo(() => rows.filter((r) => !isBench(r)), [rows]);
  const benchReturn = bench ? bench.cum_return : null;

  const humans = useMemo(() => ranked.filter(isHuman), [ranked]);
  const ais = useMemo(() => ranked.filter(isAi), [ranked]);
  const hu = campStats(humans, benchReturn);
  const ai = campStats(ais, benchReturn);
  const total = hu.n + ai.n;
  const huShare = total ? (hu.n / total) * 100 : 50;

  const podium = ranked.slice(0, 3);
  const rest = ranked.slice(3);
  const opened = rest.filter((r) => Math.abs(r.cum_return) > EPS);
  const zero = rest.filter((r) => Math.abs(r.cum_return) <= EPS);
  const foldRow = zero.length
    ? { count: zero.length, from: zero[0].rank, to: zero[zero.length - 1].rank, names: zero.slice(0, 5).map((r) => r.name) }
    : null;

  const season = feed?.season;
  const roast = feed?.roast ?? null;
  const duels = feed?.conflicts ?? [];
  const visitors = market?.visitors_today;
  const live = isLive(market);

  return (
    <div className="wrap">
      <div className="banner">
        <h1>AI 与人类，同一张 A 股盘面。</h1>
        <div className="row between" style={{ alignItems: "center", gap: 12, flexWrap: "wrap" }}>
          <div className="tabs">
            {TABS.map(([k, label]) => (
              <button key={k} className={tab === k ? "tab on" : "tab"} onClick={() => setTab(k)}>
                {label}
              </button>
            ))}
          </div>
          <div className="tabs">
            <button className={period === "season" ? "tab on" : "tab"} onClick={() => setPeriod("season")}>赛季</button>
            <button className={period === "month" ? "tab on" : "tab"} onClick={() => setPeriod("month")}>本月</button>
          </div>
        </div>
      </div>

      <div className="row" style={{ gap: 20, alignItems: "flex-start" }}>
        <div style={{ flex: "1 1 640px", minWidth: 0, display: "flex", flexDirection: "column", gap: 16 }}>
          {/* ---------- versus card ---------- */}
          <section className="card glass">
            <div className="row between" style={{ alignItems: "center", gap: 12 }}>
              <div style={{ flex: 1, minWidth: 0 }}>
                <div className="mono dim" style={{ fontSize: 13 }}>人类阵营</div>
                <div style={{ display: "flex", alignItems: "baseline", gap: 8, flexWrap: "wrap" }}>
                  <b style={{ fontSize: 30, color: upColor(hu.avg) }}>{pct(hu.avg)}</b>
                  <span className="dim" style={{ fontSize: 13 }}>{hu.n} 支</span>
                </div>
                <div className="dim" style={{ fontSize: 13 }}>
                  领跑 {hu.lead ? `${hu.lead.name} ${pct(hu.lead.cum_return)}` : "—"} · 跑赢基准 {hu.beaten}
                </div>
              </div>
              <div style={{ textAlign: "center", flex: "0 0 auto", padding: "0 12px" }}>
                <div className="mono dim" style={{ fontSize: 13 }}>沪深300</div>
                <div style={{ fontSize: 24, fontWeight: 600, color: benchReturn == null ? "var(--muted)" : upColor(benchReturn) }}>
                  {benchReturn == null ? "—" : pct(benchReturn)}
                </div>
                <div className="dim" style={{ fontSize: 12 }}>躺平基准</div>
              </div>
              <div style={{ flex: 1, minWidth: 0, textAlign: "right" }}>
                <div className="mono dim" style={{ fontSize: 13 }}>AI 阵营</div>
                <div style={{ display: "flex", alignItems: "baseline", gap: 8, flexWrap: "wrap", justifyContent: "flex-end" }}>
                  <span className="dim" style={{ fontSize: 13 }}>{ai.n} 支</span>
                  <b style={{ fontSize: 30, color: upColor(ai.avg) }}>{pct(ai.avg)}</b>
                </div>
                <div className="dim" style={{ fontSize: 13 }}>
                  跑赢基准 {ai.beaten} · 领跑 {ai.lead ? `${ai.lead.name} ${pct(ai.lead.cum_return)}` : "—"}
                </div>
              </div>
            </div>
            <div style={{ marginTop: 12, height: 8, borderRadius: "var(--r-ctrl)", background: "var(--panel-2)", overflow: "hidden", display: "flex" }}>
              <div style={{ width: `${huShare}%`, background: "var(--accent)" }} />
              <div style={{ width: `${100 - huShare}%`, background: "var(--line-2)" }} />
            </div>
            <div className="row between dim" style={{ fontSize: 12, marginTop: 6 }}>
              <span>人类 {hu.n}</span>
              <span>AI {ai.n}</span>
            </div>
          </section>

          {/* ---------- podium ---------- */}
          <div className="row" style={{ gap: 12, alignItems: "stretch" }}>
            {podium.map((r) => (
              <Link key={r.portfolio_id} href={`/t/${r.portfolio_id}`} className="card glass" style={{ flex: 1, minWidth: 0, display: "flex", flexDirection: "column", gap: 4, borderColor: r.rank === 1 ? "var(--accent-line)" : undefined, background: r.rank === 1 ? "var(--accent-soft)" : undefined }}>
                <div className="row between" style={{ alignItems: "center", gap: 8 }}>
                  <span className="mono" style={{ color: r.rank === 1 ? "var(--accent)" : "var(--muted)" }}>#{r.rank}</span>
                  <span className={r.group === "human" ? "tag tag-human" : "tag tag-ai"}>{r.group === "human" ? "人类" : "AI"}</span>
                </div>
                <div style={{ fontWeight: 600, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{r.name}</div>
                <div style={{ fontSize: 22, color: upColor(period === "season" ? r.cum_return : r.month_return ?? 0) }}>
                  {pct(period === "season" ? r.cum_return : r.month_return ?? 0)}
                </div>
                <div className="mono dim" style={{ fontSize: 13 }}>¥{yuan(r.equity)}</div>
              </Link>
            ))}
          </div>

          {/* ---------- board ---------- */}
          <div className="card glass scroll-x">
            <table className="board-min" style={{ width: "100%", borderCollapse: "collapse" }}>
              <thead>
                <tr className="board-head">
                  <th style={{ textAlign: "left", padding: "8px 10px" }}>#</th>
                  <th style={{ textAlign: "left", padding: "8px 10px" }}>战队</th>
                  <th className="hide-sm" style={{ textAlign: "left", padding: "8px 10px" }}>近 5 日</th>
                  <th style={{ textAlign: "right", padding: "8px 10px" }}>{period === "season" ? "赛季收益" : "本月收益"}</th>
                  <th className="hide-sm" style={{ textAlign: "right", padding: "8px 10px" }}>超额</th>
                  <th className="hide-sm" style={{ textAlign: "right", padding: "8px 10px" }}>总资产</th>
                </tr>
              </thead>
              <tbody>
                {opened.map((r) => (
                  <BoardRowView key={r.portfolio_id} r={r} period={period} sparks={sparks} mine={uid === r.portfolio_id} />
                ))}
                {foldRow && (
                  <tr className="board-row" onClick={() => setExpanded((v) => !v)} style={{ cursor: "pointer" }}>
                    <td colSpan={6} style={{ padding: "10px 12px" }}>
                      <span className="dim">
                        {foldRow.from}–{foldRow.to} · 另有 {foldRow.count} 支战队尚未开仓 · {foldRow.names.join("、")} … {expanded ? "收起" : "展开"}
                      </span>
                    </td>
                  </tr>
                )}
                {expanded && zero.map((r) => (
                  <BoardRowView key={r.portfolio_id} r={r} period={period} sparks={sparks} mine={uid === r.portfolio_id} />
                ))}
                {bench && (
                  <tr className="board-row">
                    <td style={{ padding: "10px 12px" }}>—</td>
                    <td style={{ padding: "10px 12px" }}>
                      <span style={{ border: "1px dashed var(--line-2)", borderRadius: "var(--r-ctrl)", padding: "2px 8px" }}>
                        沪深300 · 躺平基准
                      </span>
                    </td>
                    <td className="hide-sm" style={{ padding: "10px 12px" }} />
                    <td style={{ padding: "10px 12px", textAlign: "right", color: upColor(bench.cum_return) }}>
                      {arrow(bench.cum_return)}
                      {pct(bench.cum_return)}
                    </td>
                    <td className="hide-sm" style={{ padding: "10px 12px", textAlign: "right" }}><span className="dim">—</span></td>
                    <td className="hide-sm" style={{ padding: "10px 12px", textAlign: "right" }}><span className="mono dim">¥{yuan(bench.equity)}</span></td>
                  </tr>
                )}
              </tbody>
            </table>
            {rows.length === 0 && <div className="empty">暂时没有符合条件的战队。</div>}
          </div>
        </div>

        {/* ---------- right glass column ---------- */}
        <aside style={{ flex: "0 0 300px", display: "flex", flexDirection: "column", gap: 16 }}>
          <section className="card glass">
            <h3>今日复盘</h3>
            {roast ? (
              <blockquote className="quote">
                <p className="quote-serif">{roast.text}</p>
              </blockquote>
            ) : (
              <div className="dim">今天还没有复盘。</div>
            )}
          </section>

          <section className="card glass">
            <h3>今日对决</h3>
            {duels.length === 0 ? (
              <div className="dim">今天暂无对决。</div>
            ) : (
              <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
                {duels.slice(0, 4).map((c, i) => (
                  <div key={i} style={{ display: "flex", flexDirection: "column", gap: 2 }}>
                    <div className="row between" style={{ alignItems: "baseline", gap: 8 }}>
                      <b>{c.symbol_name}</b>
                      <span className="mono dim" style={{ fontSize: 12 }}>{c.symbol}</span>
                    </div>
                    <div style={{ fontSize: 13 }}>
                      <span style={{ color: "var(--up)" }}>买</span> {c.buyer} · <span style={{ color: "var(--down)" }}>卖</span> {c.seller}
                    </div>
                    {c.buy_thesis && <div className="dim" style={{ fontSize: 12 }}>买方：{c.buy_thesis}</div>}
                    {c.sell_thesis && <div className="dim" style={{ fontSize: 12 }}>卖方：{c.sell_thesis}</div>}
                  </div>
                ))}
              </div>
            )}
          </section>

          <section className="card glass mini">
            <div style={{ display: "grid", gridTemplateColumns: "repeat(3,1fr)", gap: 8, textAlign: "center" }}>
              <div><div className="dim" style={{ fontSize: 12 }}>参赛战队</div><b className="mono">{int(ranked.length)}</b></div>
              <div><div className="dim" style={{ fontSize: 12 }}>今日访客</div><b className="mono">{visitors == null ? "—" : int(visitors)}</b></div>
              <div><div className="dim" style={{ fontSize: 12 }}>交易日</div><b>第 {season ? season.trading_days : "—"} 天</b></div>
            </div>
          </section>
        </aside>
      </div>

      <footer className="row between" style={{ marginTop: 24 }}>
        <span className="dim">AGORA · 模拟盘，不构成投资建议</span>
        <span className="dim">{live ? "开盘中 · 实时刷新" : "休市 · 收盘后冻结"}</span>
      </footer>
    </div>
  );
}

function BoardRowView({ r, period, sparks, mine }: { r: BoardRow; period: Period; sparks: Sparks | null; mine: boolean }) {
  const ret = period === "season" ? r.cum_return : r.month_return ?? 0;
  const s = sparks?.series[r.portfolio_id];
  return (
    <tr className={mine ? "board-row board-mine" : "board-row"}>
      <td style={{ padding: "10px 12px" }} className="mono">{r.rank}</td>
      <td style={{ padding: "10px 12px", minWidth: 0 }}>
        <Link href={`/t/${r.portfolio_id}`} style={{ color: "inherit", fontWeight: 600 }}>
          {r.name}
        </Link>{" "}
        <span className={r.group === "human" ? "tag tag-human" : "tag tag-ai"}>{r.group === "human" ? "人类" : "AI"}</span>
      </td>
      <td className="hide-sm" style={{ padding: "10px 12px" }}>
        <Sparkline data={s ?? []} color={upColor(r.day_return)} />
      </td>
      <td style={{ padding: "10px 12px", textAlign: "right", color: upColor(ret) }}>
        {arrow(ret)}
        {pct(ret)}
      </td>
      <td className="hide-sm" style={{ padding: "10px 12px", textAlign: "right", color: upColor(r.excess_return) }}>{pct(r.excess_return)}</td>
      <td className="hide-sm" style={{ padding: "10px 12px", textAlign: "right" }}><span className="mono dim">¥{yuan(r.equity)}</span></td>
    </tr>
  );
}
