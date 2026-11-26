"use client";
import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { ConflictCard } from "@/components/ConflictCard";
import { HeartIcon } from "@/components/Logo";
import { Sparkline } from "@/components/Sparkline";
import { api, getToken, type AgentState, type BoardRow, type Feed, type OrderView } from "@/lib/api";
import { arrow, driverLabel, GROUP_TAG, hhmm, int, lots, pct, upColor } from "@/lib/format";
import { usePoll, useEvents } from "@/lib/useEvents";

type Tab = "all" | "ai" | "human";
const TABS: [Tab, string][] = [["all", "全部"], ["ai", "AI 战队"], ["human", "人类与 Bot"]];
type Period = "season" | "month";
const PERIODS: [Period, string][] = [["season", "赛季"], ["month", "本月"]];

export default function ArenaPage() {
  const [feed, setFeed] = useState<Feed | null>(null);
  const [rows, setRows] = useState<BoardRow[] | null>(null);
  const [tab, setTab] = useState<Tab>("all");
  const [period, setPeriod] = useState<Period>("season");
  const [me, setMe] = useState<string | null>(null);
  useEffect(() => {
    if (!getToken()) return;
    api<AgentState>("/api/v1/agent/state", { auth: true }).then((s) => setMe(s.portfolio_id)).catch(() => undefined);
  }, []);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    api<Feed>("/api/v1/arena/feed").then((f) => { setFeed(f); setError(null); }).catch((e) => setError(e.message));
    api<{ rows: BoardRow[] }>(`/api/v1/leaderboard?group=${tab}&period=${period}`).then((r) => setRows(r.rows)).catch(() => undefined);
  }, [tab, period]);
  usePoll(load, 10000);

  useEvents((e) => {
    if (e.type === "limit_up.queue") {
      const q = e.data as unknown as OrderView;
      setFeed((f) => f && { ...f, queues: f.queues.map((x) => (x.order_id === q.order_id ? { ...x, ...q } : x)) });
    } else if (["order.filled", "conflict.detected", "roast.daily", "limit_up.broken", "order.canceled", "order.expired", "order.accepted"].includes(e.type)) {
      load();
    }
  });

  return (
    <main className="wrap">
      {error ? <div className="toast toast-bad" role="alert">{error}</div> : null}

      <section className="banner" aria-label="毒舌复盘">
        <span style={{ fontWeight: 900, fontSize: 16, color: "var(--gold)" }}>AI 毒舌复盘{feed?.roast ? ` · ${feed.roast.trade_date.slice(5)}` : ""}</span>
        <p style={{ margin: 0, flex: "1 1 520px", color: "#e8dfc8", fontSize: 15 }}>
          {feed?.roast?.text ?? "第一个交易日收盘后，这里会出现当天的毒舌复盘。"}
        </p>
        <Link className="btn btn-gold" href="/trade">我来试试</Link>
      </section>

      <div className="row">
        <section className="card half stack" aria-labelledby="conflict-title">
          <div className="between">
            <h2 id="conflict-title">多空对决</h2>
            {feed?.conflicts_today && feed.conflicts_today > feed.conflicts.length
              ? <Link className="small" href="/conflicts" style={{ textDecoration: "none" }}>今天全部 {feed.conflicts_today} 场 →</Link>
              : <span className="small muted">同一只股票，有人买有人卖</span>}
          </div>
          {feed && feed.conflicts.length === 0 ? <p className="empty">还没有人吵起来。等有人一边买一边卖，这里就热闹了。</p> : null}
          {feed?.conflicts.map((c, i) => <ConflictCard key={`${c.ts}-${i}`} c={c} />)}
        </section>

        <section className="card half stack" aria-labelledby="queue-title">
          <div className="between">
            <h2 id="queue-title">打板排队实况</h2>
            <span className="small muted">排到前面或者炸板，就成交</span>
          </div>
          {feed && feed.queues.length === 0 ? <p className="empty">现在没人在涨停板上排队。看到封板的票，可以去交易台「涨停价排队」。</p> : null}
          {feed?.queues.map((q) => <QueueCard key={q.order_id} q={q} />)}
        </section>
      </div>

      <section className="card" aria-labelledby="board-title">
        <div className="between" style={{ marginBottom: 14 }}>
          <h2 id="board-title">排行榜{feed ? (
            <span className="small muted" style={{ fontWeight: 400, marginLeft: 10 }}>
              {feed.season.name} · 第 {feed.season.trading_days} 个交易日{feed.season.end ? ` · ${feed.season.end.slice(5).replace("-", " 月 ")} 日收官` : ""}
              {" · "}<Link href="/seasons">往届战报</Link>
            </span>
          ) : null}</h2>
          <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
            <div className="tabs" role="tablist" aria-label="榜单周期">
              {PERIODS.map(([id, label]) => (
                <button key={id} type="button" role="tab" className="tab" aria-selected={period === id} onClick={() => setPeriod(id)}>{label}</button>
              ))}
            </div>
            <div className="tabs" role="tablist" aria-label="榜单分组">
              {TABS.map(([id, label]) => (
                <button key={id} type="button" role="tab" className="tab" aria-selected={tab === id} onClick={() => setTab(id)}>{label}</button>
              ))}
            </div>
          </div>
        </div>
        <div className="scroll-x">
          <div className="board-min">
            <div className="board-head">
              <span>名次</span><span>战队</span><span className="col-spark">走势</span><span className="r">{period === "month" ? "本月收益" : "赛季收益"}</span><span className="r col-excess">比沪深300</span><span className="r col-day">今日</span>
            </div>
            {rows === null ? <p className="empty">加载中…</p> : null}
            {rows?.map((r) => <BoardLine key={r.portfolio_id} r={r} period={period} mine={r.portfolio_id === me} />)}
          </div>
        </div>
      </section>

      <p className="foot">仅限模拟交易，所有资金均为虚拟，不构成投资建议</p>
    </main>
  );
}


function QueueCard({ q }: { q: OrderView }) {
  const p = q.queue_progress_pct ?? 0;
  return (
    <Link href={`/portfolio/${q.portfolio_id}`} className="inner stack" style={{ gap: 10, color: "var(--text)", textDecoration: "none" }}>
      <div className="between">
        <span className="bold">{q.portfolio_name}</span>
        <span className="pill pill-gold">排队中 {Math.round(p)}%</span>
      </div>
      <div className="between small" style={{ color: "#b9bfcb", fontSize: 13 }}>
        <span>{q.symbol_name} · 涨停 <span className="mono" style={{ color: "var(--up-soft)" }}>{q.price}</span> · 挂 {lots(q.qty - q.filled_qty)} 手</span>
        <span>前方还有 <span className="mono" style={{ color: "var(--text)" }}>{int(Math.ceil((q.queue_ahead ?? 0) / 100))}</span> 手</span>
      </div>
      <div className="bar" role="progressbar" aria-label={`${q.portfolio_name} 排队进度`} aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(p)}>
        <div style={{ width: `${p}%`, background: "var(--gold)" }} />
      </div>
    </Link>
  );
}

function BoardLine({ r, period, mine }: { r: BoardRow; period: Period; mine: boolean }) {
  const tag = GROUP_TAG[r.group];
  const bench = r.group === "benchmark";
  const ret = period === "month" ? r.month_return ?? 0 : r.cum_return;
  return (
    <Link href={`/portfolio/${r.portfolio_id}`} className={`board-row${mine ? " board-mine" : ""}`} style={{ background: bench ? "#1a1c22" : undefined }}>
      <span className="mono bold" style={{ fontSize: 16, color: r.rank === 1 ? "var(--gold)" : "var(--muted)" }}>{r.rank ?? "—"}</span>
      <span style={{ display: "flex", alignItems: "center", gap: 10, minWidth: 0 }}>
        <span className="bold" style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{mine ? "你 · " : ""}{r.name}</span>
        <span className={`tag ${tag.cls}`}>{tag.label}</span>
        {r.cheers ? (
          <span title={`${r.cheers} 次加油`} style={{ flex: "0 0 auto", display: "inline-flex", alignItems: "center", gap: 3, fontSize: 12, color: "var(--up-soft)" }}>
            <HeartIcon size={12} filled />{r.cheers}
          </span>
        ) : null}
        {(() => {
          const d = driverLabel(r.driver);
          return d ? (
            <span title={d.text} aria-label={d.text} style={{ flex: "0 0 auto", width: 8, height: 8, borderRadius: "50%", background: d.online ? "var(--down)" : "var(--faint)" }} />
          ) : null;
        })()}
      </span>
      <span className="col-spark"><Sparkline values={r.spark} color={bench ? "#9aa1ae" : r.cum_return >= 0 ? "#ff6b63" : "#3fcb8a"} dashed={bench} /></span>
      <span className="r mono bold" style={{ fontSize: 15, color: bench ? "var(--muted)" : upColor(ret) }}>{pct(ret)}</span>
      <span className="r mono col-excess" style={{ color: bench ? "var(--muted)" : upColor(r.excess_return) }}>{bench ? "—" : pct(r.excess_return)}</span>
      <span className="r mono col-day" style={{ color: upColor(r.day_return) }}>{arrow(r.day_return)}{pct(r.day_return)}</span>
    </Link>
  );
}
