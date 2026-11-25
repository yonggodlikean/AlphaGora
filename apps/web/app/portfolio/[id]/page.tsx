"use client";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useState } from "react";
import { Avatar } from "@/components/Avatar";
import { HeartIcon } from "@/components/Logo";
import { api, type Profile, type TimelineItem, type TimelineOrder } from "@/lib/api";
import { driverLabel, GROUP_TAG, int, lots, mmdd, hhmm, pct, STANCE, upColor, yuan } from "@/lib/format";
import { usePoll } from "@/lib/useEvents";

const ROLE_COLORS: [RegExp, string, string][] = [
  [/多头|bull/i, "#ff8a84", "#1a1214"],
  [/空头|bear/i, "#5fd39a", "#101a15"],
  [/风控|risk|仓位|纪律/i, "#8cc4ff", "#111824"],
  [/主席|chair|judge/i, "#f5c451", "#1c1a12"],
];
const modelLabel = (m: string) =>
  m === "rules-v1" ? "规则引擎 · 非 AI 生成" : m.startsWith("openpup/") ? `OpenPup · ${m.slice(8)} pup` : m;
const roleStyle = (role: string) => ROLE_COLORS.find(([re]) => re.test(role)) ?? [null, "#c3c8d2", "#1a1c22"];

function outcome(orders: TimelineOrder[]): { kind: string; cls: string; text: string; fees: string } {
  if (orders.length === 0) return { kind: "不交易", cls: "pill-gray", text: "决定不下单", fees: "" };
  const o = orders[0];
  const side = o.side === "buy" ? "买入" : "卖出";
  const fees = Number(o.fees_paid) > 0 ? `费用 ¥${yuan(o.fees_paid)}` : "";
  if (o.filled_qty > 0) {
    const kind = o.liquidity.includes("board_break") ? ["炸板成交", "pill-down"] : o.liquidity.includes("queue") ? ["排队成交", "pill-up"] : ["主动成交", "pill-info"];
    return { kind: kind[0], cls: kind[1], text: `${side} ${int(o.filled_qty)} 股 @ ${o.avg_fill_price}`, fees };
  }
  if (o.status === "new" || o.status === "partially_filled") {
    return { kind: o.queue_initial ? "排队中" : "挂单中", cls: "pill-gold",
      text: `${side} ${lots(o.qty)} 手 @ ${o.price ?? "市价"}${o.queue_progress_pct != null ? ` · 进度 ${Math.round(o.queue_progress_pct)}%` : ""}`, fees };
  }
  const why = o.status === "expired" ? (o.queue_initial ? "排了一天没排到" : "收盘未成交") : o.reason === "ioc_remainder" ? "没成交，已撤" : "已撤单";
  return { kind: o.status === "expired" ? "未成交" : "已撤单", cls: "pill-gray", text: `${side} ${lots(o.qty)} 手 · ${why}`, fees: "" };
}

export default function PortfolioPage() {
  const { id } = useParams<{ id: string }>();
  const [p, setP] = useState<Profile | null>(null);
  const [items, setItems] = useState<TimelineItem[] | null>(null);
  const [open, setOpen] = useState<Record<string, boolean>>({});
  const [missing, setMissing] = useState(false);
  const [cheering, setCheering] = useState<"idle" | "busy" | "done">("idle");
  const [cheerMsg, setCheerMsg] = useState<string | null>(null);
  async function cheer() {
    setCheering("busy");
    try {
      const r = await api<{ cheers: number; new: boolean }>(`/api/v1/portfolios/${id}/cheer`, { method: "POST" });
      setP((x) => (x ? { ...x, cheers: r.cheers } : x));
      setCheerMsg(r.new ? "加油成功！" : "今天已经给 TA 加过油了，明天再来");
    } catch (e) {
      setCheerMsg((e as Error).message);
    } finally {
      setCheering("done");
    }
  }

  const load = useCallback(() => {
    api<Profile>(`/api/v1/portfolios/${id}`).then(setP).catch((e) => { if (e.status === 404) setMissing(true); });
    api<{ items: TimelineItem[] }>(`/api/v1/portfolios/${id}/timeline`).then((r) => {
      setItems(r.items);
      setOpen((o) => (Object.keys(o).length || !r.items[0] ? o : { [r.items[0].id]: true }));
    }).catch(() => undefined);
  }, [id]);
  usePoll(load, 10000);

  if (missing) return <main className="wrap"><p className="empty">找不到这支队伍。<Link href="/">回擂台</Link></p></main>;
  if (!p) return <main className="wrap"><p className="empty">加载中…</p></main>;
  const tag = GROUP_TAG[p.group];

  const metrics = [
    { label: "累计收益", value: pct(p.cum_return), color: upColor(p.cum_return), hint: p.group === "benchmark" ? "基准本身" : `比沪深300 ${pct(p.excess_return)}` },
    { label: "最大回撤", value: pct(p.max_drawdown), color: p.max_drawdown < 0 ? "var(--down)" : "var(--text)", hint: "从最高点算起" },
    { label: "胜率", value: p.win_rate == null ? "—" : `${Math.round(p.win_rate * 100)}%`, color: "var(--text)", hint: `已卖出 ${p.closed_trades} 笔` },
    { label: "打板成功率", value: p.queue_success == null ? "—" : `${Math.round(p.queue_success * 100)}%`, color: "var(--gold)", hint: `排过 ${p.queue_count} 次涨停` },
  ];

  return (
    <main className="wrap">
      {p.status !== "active" ? (
        <div className="toast toast-bad" role="status">
          {p.status === "banned" ? "这个账号因违反擂台规则已被封禁，不参与排名。" : "这个账号已被暂停交易，暂不参与排名。"}
          <Link href="/about#rules" style={{ marginLeft: 8 }}>擂台规则</Link>
        </div>
      ) : null}
      <section className="card" aria-label="战队人设" style={{ display: "flex", flexWrap: "wrap", gap: 24, alignItems: "flex-start", padding: 24, borderRadius: 16 }}>
        <Avatar glyph={p.glyph} color={p.color} />
        <div className="stack" style={{ flex: "999 1 420px", minWidth: 0, gap: 10 }}>
          <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 10 }}>
            <h1 style={{ fontSize: 28 }}>{p.name}</h1>
            <span className={`tag ${tag.cls}`} style={{ fontSize: 12, padding: "3px 10px" }}>{tag.label}</span>
            {(() => {
              const d = driverLabel(p.driver);
              return d ? (
                <span className={`pill ${d.online ? "pill-down" : "pill-gray"}`} style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                  <span aria-hidden="true" style={{ width: 8, height: 8, borderRadius: "50%", background: d.online ? "var(--down)" : "var(--faint)" }} />
                  {d.text}
                </span>
              ) : null;
            })()}
          </div>
          {p.brief ? <p style={{ margin: 0, color: "var(--text-2)", fontSize: 15, maxWidth: 720 }}>{p.brief}</p> : null}
          {p.roast_line ? (
            <div style={{ display: "flex", gap: 10, alignItems: "flex-start", padding: "10px 14px", borderRadius: 10, background: "#17140d", border: "1px solid #3a3220", maxWidth: 720 }}>
              <span className="small" style={{ flex: "0 0 auto", fontWeight: 900, color: "var(--gold)", paddingTop: 2 }}>今日点评</span>
              <span style={{ color: "#e8dfc8" }}>{p.roast_line}</span>
            </div>
          ) : null}
        </div>
        <div style={{ flex: "1 1 200px", display: "flex", flexDirection: "column", alignItems: "flex-end", gap: 4, textAlign: "right" }}>
          <span className="small muted">当前名次</span>
          <span className="mono bold" style={{ fontSize: 44, color: "var(--gold)", lineHeight: 1 }}>{p.rank ? `#${p.rank}` : "基准"}</span>
          <span className="mono small muted">总资产 ¥{yuan(p.equity)}</span>
          {p.status === "active" ? (
            <button type="button" className="btn" onClick={cheer} disabled={cheering === "busy"}
              style={{ marginTop: 8, color: "var(--up-soft)", borderColor: "#6b2a2a" }}>
              <HeartIcon size={16} filled={cheering === "done"} /> 为 TA 加油 · {p.cheers}
            </button>
          ) : null}
          {cheerMsg ? <span className="small muted" role="status">{cheerMsg}</span> : null}
        </div>
      </section>

      <section aria-label="核心指标" style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(200px, 1fr))", gap: 12 }}>
        {metrics.map((m) => (
          <div key={m.label} className="card stack" style={{ gap: 4, padding: "16px 18px", borderRadius: 12 }}>
            <span className="small muted">{m.label}</span>
            <span className="mono bold" style={{ fontSize: 26, color: m.color }}>{m.value}</span>
            <span className="small faint">{m.hint}</span>
          </div>
        ))}
      </section>

      <div className="row">
        <div className="side stack" style={{ gap: 24 }}>
          <section className="card stack" aria-labelledby="badge-title">
            <h2 id="badge-title">徽章墙</h2>
            {p.badges.map((b) => (
              <div key={b.code} style={{ display: "flex", gap: 12, alignItems: "center", padding: 12, borderRadius: 12, opacity: b.count ? 1 : 0.6,
                border: `1px solid ${b.count ? "var(--gold-line)" : "var(--line)"}`, background: b.count ? "var(--gold-bg)" : "var(--panel-2)" }}>
                <div aria-hidden="true" style={{ flex: "0 0 auto", width: 44, height: 44, borderRadius: 12, display: "flex", alignItems: "center", justifyContent: "center", fontWeight: 900,
                  background: b.count ? "#3a3220" : "#1f222b", color: b.count ? "var(--gold)" : "var(--faint)" }}>{b.glyph}</div>
                <div className="stack" style={{ gap: 2, minWidth: 0 }}>
                  <span className="bold">{b.name} <span className="small muted" style={{ fontWeight: 400 }}>{b.count ? `×${b.count}` : "未解锁"}</span></span>
                  <span className="small muted">{b.desc}</span>
                </div>
              </div>
            ))}
          </section>
          <section className="card stack" aria-labelledby="pos-title" style={{ gap: 4 }}>
            <h2 id="pos-title" style={{ marginBottom: 6 }}>当前持仓</h2>
            {p.positions.length === 0 ? <p className="empty">空仓。现金 ¥{yuan(p.cash)}</p> : null}
            {p.positions.map((x) => (
              <div key={x.symbol} style={{ display: "grid", gridTemplateColumns: "minmax(0, 1fr) auto", gap: "2px 12px", padding: "10px 0", borderBottom: "1px solid #1f222b" }}>
                <span className="bold">{x.name}</span>
                <span className="r mono bold" style={{ color: upColor(x.pnl_pct) }}>{x.pnl_pct > 0 ? "+" : ""}{x.pnl_pct.toFixed(2)}%</span>
                <span className="small muted">{int(x.qty)} 股 · 成本 {x.cost}</span>
                <span className="r small bold" style={{ color: x.frozen_qty ? "var(--gold)" : "var(--muted)" }}>{x.frozen_qty ? "T+1 锁定 · 明日可卖" : "可卖"}</span>
              </div>
            ))}
          </section>
        </div>

        <section className="card grow stack" aria-labelledby="tl-title" style={{ gap: 16 }}>
          <div className="between">
            <h2 id="tl-title">每一次决定</h2>
            <span className="small muted">先给理由，再出手，过程全部公开</span>
          </div>
          {items && items.length === 0 ? <p className="empty">还没有任何决定。</p> : null}
          {items?.map((it) => {
            if (it.kind === "corporate") {
              const got = [Number(it.cash) > 0 ? `现金 ¥${Number(it.cash).toLocaleString("zh-CN", { minimumFractionDigits: 2 })}` : "",
                it.bonus_shares ? `送转 ${it.bonus_shares.toLocaleString("zh-CN")} 股（明天可卖）` : ""].filter(Boolean).join(" · ");
              return (
                <article key={it.id} style={{ border: "1px solid var(--line)", borderRadius: 12, background: "var(--panel-2)", padding: "14px 16px" }} className="stack">
                  <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 8 }}>
                    <span className="mono small muted">{mmdd(it.created_at)} {hhmm(it.created_at).slice(0, 5)}</span>
                    <span className="bold" style={{ fontSize: 15 }}>{it.symbol_name}：除权除息</span>
                    <span className="pill pill-gold">到账</span>
                  </div>
                  <p style={{ margin: 0, color: "var(--text-2)" }}>{it.memo}{got ? `，${got}` : ""}</p>
                </article>
              );
            }
            const out = outcome(it.orders);
            const title = it.kind === "debate"
              ? it.final_decision?.title || `${it.symbol_name}：${STANCE[it.final_decision?.action ?? ""] ?? "讨论"}`
              : `${it.orders[0]?.side === "buy" ? "买入" : "卖出"} ${it.symbol_name}`;
            const summary = it.kind === "debate" ? it.final_decision?.summary : it.orders[0]?.thesis;
            const isOpen = !!open[it.id];
            return (
              <article key={it.id} style={{ border: "1px solid var(--line)", borderRadius: 12, background: "var(--panel-2)", overflow: "hidden" }}>
                <div className="stack" style={{ padding: "14px 16px", gap: 8 }}>
                  <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 8 }}>
                    <span className="mono small muted">{mmdd(it.created_at)} {hhmm(it.created_at).slice(0, 5)}</span>
                    <span className="bold" style={{ fontSize: 15 }}>{title}</span>
                    {it.has_conflict ? <span className="small bold" style={{ padding: "2px 8px", borderRadius: 999, border: "1px solid #6b2a2a", color: "var(--up-soft)", fontSize: 11 }}>多空分歧</span> : null}
                  </div>
                  {summary ? <p style={{ margin: 0, color: "var(--text-2)" }}>{it.kind === "order" ? `「${summary}」` : summary}</p> : null}
                </div>
                <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: "8px 20px", padding: "12px 16px", borderTop: "1px solid var(--line)", background: "#16171d" }}>
                  <span className={`pill ${out.cls}`}>{out.kind}</span>
                  <span style={{ fontSize: 13 }}>{out.text}</span>
                  {out.fees ? <span className="muted" style={{ fontSize: 13 }}>{out.fees}</span> : null}
                  {it.rounds?.length ? (
                    <button type="button" className="btn small" style={{ marginLeft: "auto", minHeight: 40 }} aria-expanded={isOpen}
                      onClick={() => setOpen((o) => ({ ...o, [it.id]: !o[it.id] }))}>{isOpen ? "收起" : `看 ${it.rounds.length} 方发言`}</button>
                  ) : null}
                </div>
                {isOpen && it.rounds ? (
                  <div className="stack" style={{ gap: 8, padding: "12px 16px 16px", borderTop: "1px solid var(--line)" }}>
                    {it.rounds.map((r, i) => {
                      const [, color, bg] = roleStyle(r.role);
                      return (
                        <div key={i} style={{ display: "grid", gridTemplateColumns: "80px minmax(0, 1fr)", gap: 10, padding: "10px 12px", borderRadius: 10, background: bg }}>
                          <div className="stack" style={{ gap: 2 }}>
                            <span className="bold" style={{ color }}>{r.role}</span>
                            <span className="small muted">{STANCE[r.stance] ?? r.stance}</span>
                          </div>
                          <div className="stack" style={{ gap: 2 }}>
                            <span>{r.argument}</span>
                            {r.model_id ? <span className="mono faint" style={{ fontSize: 11 }}>{modelLabel(r.model_id)}</span> : null}
                          </div>
                        </div>
                      );
                    })}
                  </div>
                ) : null}
              </article>
            );
          })}
        </section>
      </div>
      <p className="foot">仅限模拟交易，所有资金均为虚拟，不构成投资建议</p>
    </main>
  );
}
