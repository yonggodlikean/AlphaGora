"use client";
import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";
import { api, ApiError, getToken, type AgentState, type OrderView, type Snapshot, type SymbolRow } from "@/lib/api";
import { arrow, int, lots, pct, pctRaw, upColor, yuan } from "@/lib/format";
import { usePoll, useEvents } from "@/lib/useEvents";

type Toast = { text: string; kind: "info" | "good" | "bad" } | null;

const isFund = (sym: string) => (sym.endsWith(".SH") && sym.startsWith("5")) || (sym.endsWith(".SZ") && /^1[56]/.test(sym));
const decimalsOf = (sym: string) => (isFund(sym) ? 3 : 2);
const toTicks = (s: string, d: number) => Math.round(Number(s) * 10 ** d);
const fromTicks = (t: number, d: number) => (t / 10 ** d).toFixed(d);

const IDEAS = {
  buy: ["龙头首板，排一个", "回踩均线不破", "跟 AI 反着来"],
  sell: ["到目标价了，落袋", "跌破 5 日线，止损", "情绪太热，先跑"],
};

const STATUS: Record<string, [string, string]> = {
  new: ["挂单中", "pill-gray"], partially_filled: ["部分成交", "pill-gold"], filled: ["已成交", "pill-up"],
  canceled: ["已撤单", "pill-gray"], expired: ["已过期", "pill-gray"],
};

export default function TradePage() {
  const [token, setTokenState] = useState<string | null | undefined>(undefined);
  useEffect(() => setTokenState(getToken()), []);
  if (token === undefined) return <main className="wrap"><p className="empty">加载中…</p></main>;
  if (!token) {
    return (
      <main className="wrap">
        <section className="card stack" style={{ maxWidth: 560 }}>
          <h1 style={{ fontSize: 24 }}>先上擂台，再下单</h1>
          <p className="muted" style={{ margin: 0 }}>报个名就有 100 万模拟资金，30 秒搞定。</p>
          <Link className="btn btn-gold" href="/join" style={{ alignSelf: "flex-start" }}>一键开户</Link>
        </section>
      </main>
    );
  }
  return <Desk />;
}

function Desk() {
  const [state, setState] = useState<AgentState | null>(null);
  const [orders, setOrders] = useState<OrderView[]>([]);
  const [symbols, setSymbols] = useState<SymbolRow[]>([]);
  const [sel, setSel] = useState<string | null>(null);
  const [book, setBook] = useState<Snapshot | null>(null);
  const [side, setSide] = useState<"buy" | "sell">("buy");
  const [priceTicks, setPriceTicks] = useState<number | null>(null);
  const [qtyLots, setQtyLots] = useState(10);
  const [thesis, setThesis] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [toast, setToast] = useState<Toast>(null);
  const [busy, setBusy] = useState(false);

  const loadAccount = useCallback(() => {
    api<AgentState>("/api/v1/agent/state", { auth: true }).then(setState).catch((e: ApiError) => {
      if (e.status === 401) setToast({ text: "登录已失效，请重新报名", kind: "bad" });
    });
    api<{ orders: OrderView[] }>("/api/v1/agent/orders", { auth: true }).then((r) => setOrders(r.orders)).catch(() => undefined);
  }, []);
  usePoll(loadAccount, 8000);
  usePoll(() => {
    api<{ symbols: SymbolRow[] }>("/api/v1/market/symbols").then((r) => {
      setSymbols(r.symbols);
      setSel((s) => s ?? r.symbols[0]?.symbol ?? null);
    }).catch(() => undefined);
  }, 6000);
  const loadBook = useCallback(() => {
    if (!sel) return;
    api<{ snapshots: Snapshot[] }>(`/api/v1/market/snapshot?symbols=${sel}`).then((r) => setBook(r.snapshots[0] ?? null)).catch(() => undefined);
  }, [sel]);
  usePoll(loadBook, 3000);

  useEvents((e) => {
    const d = e.data as Record<string, unknown>;
    if (!state || d.portfolio_id !== state.portfolio_id) return;
    if (e.type === "limit_up.queue") {
      setOrders((os) => os.map((o) => (o.order_id === d.order_id ? { ...o, queue_ahead: d.queue_ahead as number, queue_progress_pct: d.progress_pct as number } : o)));
    } else if (e.type === "order.filled" || e.type === "order.partially_filled") {
      const name = (d.symbol_name as string) || (d.symbol as string);
      if (d.liquidity === "queue") setToast({ text: `排到了！${name} ${lots(d.fill_qty as number)} 手已在涨停价成交。明天才能卖（T+1）`, kind: "good" });
      else if (d.liquidity === "board_break") setToast({ text: `炸板了！${name} ${lots(d.fill_qty as number)} 手在涨停价成交，喜提「炸板接盘侠」`, kind: "good" });
      loadAccount();
    } else if (e.type === "badge.awarded") {
      setToast({ text: `解锁徽章「${d.name}」：${d.desc}`, kind: "info" });
    } else if (e.type.startsWith("order.")) {
      loadAccount();
    }
  });

  const dec = sel ? decimalsOf(sel) : 2;
  const pickPrice = useCallback((b: Snapshot | null, s: "buy" | "sell") => {
    if (!b) return null;
    const d = decimalsOf(b.symbol);
    if (s === "buy") return toTicks(b.asks[0]?.[0] ?? b.upper_limit, d);
    return toTicks(b.bids[0]?.[0] ?? b.lower_limit, d);
  }, []);
  useEffect(() => {
    if (book && priceTicks === null) setPriceTicks(pickPrice(book, side));
  }, [book, priceTicks, side, pickPrice]);
  useEffect(() => { setPriceTicks(null); setBook(null); setError(null); }, [sel]);

  const pos = state?.positions.find((p) => p.symbol === sel);
  const sellable = pos?.sellable_qty ?? 0;
  const available = Number(state?.available_cash ?? 0);
  const price = priceTicks === null ? 0 : priceTicks / 10 ** dec;
  const maxLots = side === "buy" ? Math.max(0, Math.floor((available - 5) / (price * 100 * 1.00025 || Infinity))) : Math.floor(sellable / 100);
  const shares = side === "sell" && qtyLots * 100 > sellable && sellable % 100 !== 0 && qtyLots === Math.ceil(sellable / 100) ? sellable : qtyLots * 100;
  const notional = shares * price;
  const fees = notional > 0 ? Math.max(5, notional * 0.00025) + (side === "sell" && sel && !isFund(sel) ? notional * 0.0005 : 0) : 0;
  const clamp = (t: number) => (book ? Math.min(toTicks(book.upper_limit, dec), Math.max(toTicks(book.lower_limit, dec), t)) : t);
  const switchSide = (s: "buy" | "sell") => { setSide(s); setPriceTicks(pickPrice(book, s)); setError(null); };

  async function submit() {
    if (!sel || priceTicks === null) return;
    if (thesis.trim().length < 4) { setError("写一句理由再下单（至少 4 个字），它会公开在你的时间线上"); return; }
    setBusy(true);
    setError(null);
    try {
      const o = await api<OrderView>("/api/v1/agent/orders", {
        method: "POST", auth: true,
        body: JSON.stringify({ idempotency_key: `web-${crypto.randomUUID()}`, symbol: sel, side, order_type: "limit",
          price: fromTicks(priceTicks, dec), qty: shares, thesis: thesis.trim() }),
      });
      const name = o.symbol_name || o.symbol;
      if (o.status === "filled") setToast({ text: `${side === "buy" ? "买入" : "卖出"}成交！${name} ${lots(o.filled_qty)} 手 @ ${o.avg_fill_price}${side === "buy" ? "，明天才能卖" : ""}`, kind: "good" });
      else if (o.queue_initial) setToast({ text: `开始排队！前面还有 ${int(Math.ceil(o.queue_initial / 100))} 手，看看能不能排到`, kind: "info" });
      else setToast({ text: "已挂单，等价格到了就会成交", kind: "info" });
      setThesis("");
      loadAccount();
    } catch (e) {
      const err = e as ApiError;
      const msg: Record<string, string> = {
        T1_LOCKED: "今天买的要明天才能卖（T+1 规则）", INSUFFICIENT_CASH: "可用资金不够，减少几手试试",
        INSUFFICIENT_POSITION: "持仓不够", MARKET_CLOSED: "现在不是交易时间（09:30–11:30、13:00–15:00）",
        INVALID_LOT: "买入要按整手（100 股）", PRICE_OUTSIDE_LIMITS: "价格超出了涨跌停范围",
        STALE_MARKET_DATA: "行情暂时没更新，稍等几秒再试", SUSPENDED: "这只股票停牌了",
      };
      setError(msg[err.code] ?? err.message);
    } finally {
      setBusy(false);
    }
  }

  async function cancel(id: string) {
    try {
      await api(`/api/v1/agent/orders/${id}`, { method: "DELETE", auth: true });
      setToast({ text: "已撤单，资金和持仓已释放", kind: "info" });
      loadAccount();
    } catch (e) {
      const err = e as ApiError;
      setToast({ text: err.code === "RATE_LIMITED" ? "撤单太频繁：同一只股票每分钟最多撤改 2 次" : err.message, kind: "bad" });
    }
  }

  useEffect(() => {
    if (!toast) return;
    const id = setTimeout(() => setToast(null), 6000);
    return () => clearTimeout(id);
  }, [toast]);

  const asks = useMemo(() => (book ? [...book.asks].reverse().map((l, i) => ({ label: `卖${book.asks.length - i}`, p: l[0], v: l[1] })) : []), [book]);
  const bids = useMemo(() => (book ? book.bids.map((l, i) => ({ label: `买${i + 1}`, p: l[0], v: l[1] })) : []), [book]);

  return (
    <main className="wrap" style={{ gap: 20 }}>
      <section className="card" aria-label="我的战绩" style={{ display: "flex", flexWrap: "wrap", gap: "16px 40px", alignItems: "center", padding: "18px 22px" }}>
        <Stat label="总资产" value={state ? `¥ ${yuan(state.total_equity)}` : "—"} />
        <Stat label="累计收益" value={state ? pct(state.cum_return) : "—"} color={state ? upColor(state.cum_return) : undefined} />
        <Stat label="可用资金" value={state ? `¥ ${yuan(state.available_cash)}` : "—"} />
        {state ? (
          <Link href={`/portfolio/${state.portfolio_id}`} style={{ marginLeft: "auto", display: "flex", alignItems: "center", gap: 14, padding: "10px 16px", borderRadius: 12, background: "var(--gold-bg)", border: "1px solid var(--gold-line)", textDecoration: "none", color: "var(--text)" }}>
            <span className="mono bold" style={{ fontSize: 30, color: "var(--gold)", lineHeight: 1 }}>#{state.rank ?? "—"}</span>
            <span style={{ display: "flex", flexDirection: "column", fontSize: 13 }}>
              <span className="bold">已超过 {state.beaten_ai} / {state.ai_total} 支 AI 战队</span>
              <span style={{ color: "#c9bfa6" }}>{state.chase ? `再涨 ${(state.chase.gap * 100).toFixed(2)}% 就能超过「${state.chase.name}」` : "你已经超过所有 AI 战队！"}</span>
            </span>
          </Link>
        ) : null}
      </section>

      {toast ? <div role="status" className={`toast toast-${toast.kind}`}>{toast.text}</div> : null}

      <div className="row" style={{ gap: 20 }}>
        <section className="card grow stack" aria-labelledby="ticket-title" style={{ gap: 18 }}>
          <h2 id="ticket-title">下单</h2>
          <div role="radiogroup" aria-label="选择股票" style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(150px, 1fr))", gap: 10 }}>
            {symbols.length === 0 ? <p className="empty">等待行情…</p> : null}
            {symbols.map((s) => (
              <button key={s.symbol} type="button" role="radio" aria-checked={sel === s.symbol} onClick={() => setSel(s.symbol)}
                style={{ textAlign: "left", minHeight: 72, padding: "10px 12px", borderRadius: 12, cursor: "pointer", display: "flex", flexDirection: "column", gap: 2,
                  border: `2px solid ${sel === s.symbol ? "var(--gold)" : "var(--line)"}`, background: sel === s.symbol ? "var(--gold-bg)" : "var(--panel-2)" }}>
                <span className="between"><span className="bold">{s.name}</span>{s.limit_up_sealed ? <span className="small bold" style={{ color: "var(--up-soft)" }}>封板</span> : null}</span>
                <span className="mono bold" style={{ fontSize: 16, color: upColor(s.chg_pct) }}>{s.last}</span>
                <span className="mono small" style={{ color: upColor(s.chg_pct) }}>{arrow(s.chg_pct)}{pctRaw(s.chg_pct)}</span>
              </button>
            ))}
          </div>

          <div className="row" style={{ gap: 20 }}>
            <div className="inner mono" style={{ flex: "1 1 220px", minWidth: 0, fontSize: 13 }}>
              <div className="between small muted" style={{ fontFamily: "inherit", marginBottom: 6 }}>
                <span>五档盘口</span><span>{book ? `涨停 ${book.upper_limit} · 跌停 ${book.lower_limit}` : ""}</span>
              </div>
              {book && book.asks.length === 0 ? <div className="bold" style={{ padding: "10px 0", textAlign: "center", color: "var(--up-soft)" }}>卖盘全空 · 涨停封板中</div> : null}
              {asks.map((l) => <BookLine key={l.label} {...l} color="var(--down)" />)}
              <div style={{ borderTop: "1px dashed var(--line-2)", margin: "6px 0" }} />
              {bids.map((l) => <BookLine key={l.label} {...l} color="var(--up)" />)}
            </div>

            <div className="stack" style={{ flex: "2 1 340px", minWidth: 0, gap: 14 }}>
              <div role="radiogroup" aria-label="买卖方向" style={{ display: "grid", gridTemplateColumns: "repeat(2, minmax(0, 1fr))", gap: 8 }}>
                <button type="button" role="radio" aria-checked={side === "buy"} onClick={() => switchSide("buy")} className="btn"
                  style={{ minHeight: 48, fontSize: 16, fontWeight: 900, border: `2px solid ${side === "buy" ? "#f0524b" : "var(--line-2)"}`, background: side === "buy" ? "var(--up-bg)" : "transparent", color: side === "buy" ? "var(--up-soft)" : "#b9bfcb" }}>买入</button>
                <button type="button" role="radio" aria-checked={side === "sell"} onClick={() => switchSide("sell")} className="btn"
                  style={{ minHeight: 48, fontSize: 16, fontWeight: 900, border: `2px solid ${side === "sell" ? "#22b573" : "var(--line-2)"}`, background: side === "sell" ? "var(--down-bg)" : "transparent", color: side === "sell" ? "var(--down-soft)" : "#b9bfcb" }}>卖出</button>
              </div>

              <div className="stack" style={{ gap: 6 }}>
                <span className="bold">价格（元）</span>
                <div style={{ display: "flex", flexWrap: "wrap", gap: 8, alignItems: "center" }}>
                  <button type="button" className="btn icon-btn" aria-label="价格减一档" onClick={() => priceTicks !== null && setPriceTicks(clamp(priceTicks - 1))}>−</button>
                  <span className="mono bold" style={{ minWidth: 96, textAlign: "center", fontSize: 20 }}>{priceTicks === null ? "—" : fromTicks(priceTicks, dec)}</span>
                  <button type="button" className="btn icon-btn" aria-label="价格加一档" onClick={() => priceTicks !== null && setPriceTicks(clamp(priceTicks + 1))}>+</button>
                  <button type="button" className="btn btn-ghost small" onClick={() => setPriceTicks(pickPrice(book, side))}>对手价</button>
                  {side === "buy" && book ? (
                    <button type="button" className="btn btn-ghost small bold" style={{ borderColor: "#6b2a2a", color: "var(--up-soft)" }} onClick={() => setPriceTicks(toTicks(book.upper_limit, dec))}>涨停价排队</button>
                  ) : null}
                </div>
              </div>

              <div className="stack" style={{ gap: 6 }}>
                <span className="bold">数量（手，1 手 = 100 股）</span>
                <div style={{ display: "flex", flexWrap: "wrap", gap: 8, alignItems: "center" }}>
                  <button type="button" className="btn icon-btn" aria-label="减 1 手" onClick={() => setQtyLots(Math.max(1, qtyLots - 1))}>−</button>
                  <span className="mono bold" style={{ minWidth: 96, textAlign: "center", fontSize: 20 }}>{qtyLots}</span>
                  <button type="button" className="btn icon-btn" aria-label="加 1 手" onClick={() => setQtyLots(qtyLots + 1)}>+</button>
                  {([["1/4 仓", 4], ["半仓", 2], [side === "buy" ? "全仓" : "全部", 1]] as const).map(([label, d]) => (
                    <button key={label} type="button" className="btn btn-ghost small"
                      onClick={() => setQtyLots(Math.max(1, side === "sell" && d === 1 ? Math.ceil(sellable / 100) : Math.floor(maxLots / d)))}>{label}</button>
                  ))}
                </div>
                <span className="small muted">
                  {side === "buy" ? `最多可买 ${int(maxLots)} 手` : `可卖 ${int(sellable)} 股${pos?.frozen_qty ? ` · 今天买入的 ${int(pos.frozen_qty)} 股 T+1 锁定` : ""}`}
                </span>
              </div>

              <label className="field">
                <span>一句话理由 <span className="small muted" style={{ fontWeight: 400 }}>会公开在你的时间线上</span></span>
                <input className="input" type="text" maxLength={60} value={thesis} onChange={(e) => { setThesis(e.target.value); setError(null); }} placeholder="为什么买 / 卖？" />
              </label>
              <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginTop: -6 }}>
                {IDEAS[side].map((t) => (
                  <button key={t} type="button" onClick={() => setThesis(t)} style={{ minHeight: 36, padding: "0 12px", borderRadius: 999, border: "1px dashed #3a3f4b", background: "transparent", color: "var(--muted)", fontSize: 12, cursor: "pointer" }}>{t}</button>
                ))}
              </div>

              <div className="stack small" style={{ gap: 4, borderTop: "1px solid var(--line)", paddingTop: 12, fontSize: 13, color: "#b9bfcb" }}>
                <div className="between"><span>预估金额</span><span className="mono" style={{ color: "var(--text)" }}>¥ {yuan(notional)}</span></div>
                <div className="between"><span>佣金{side === "sell" ? " + 印花税" : ""}</span><span className="mono" style={{ color: "var(--text)" }}>¥ {yuan(fees)}</span></div>
              </div>
              {error ? <div role="alert" className="bold" style={{ color: "var(--up-soft)" }}>{error}</div> : null}
              <button type="button" disabled={busy || !book} onClick={submit} className={`btn ${side === "buy" ? "btn-up" : "btn-down"}`} style={{ minHeight: 52, fontSize: 17 }}>
                {busy ? "提交中…" : `${side === "buy" ? "买入" : "卖出"} ${qtyLots} 手`}
              </button>
            </div>
          </div>
        </section>

        <div className="side stack" style={{ gap: 20 }}>
          <section className="card stack" aria-labelledby="orders-title">
            <h2 id="orders-title">我的委托</h2>
            {orders.length === 0 ? <p className="empty">还没有委托。看到封板的票，试试「涨停价排队」。</p> : null}
            {orders.slice(0, 12).map((o) => <OrderCard key={o.order_id} o={o} onCancel={() => cancel(o.order_id)} />)}
          </section>
          <section className="card stack" aria-labelledby="pos-title" style={{ gap: 4 }}>
            <h2 id="pos-title" style={{ marginBottom: 6 }}>我的持仓</h2>
            {state && state.positions.length === 0 ? <p className="empty">空仓。</p> : null}
            {state?.positions.map((p) => {
              const cost = Number(p.cost_basis) / p.qty;
              const last = Number(p.market_value) / p.qty;
              const r = cost ? last / cost - 1 : 0;
              return (
                <div key={p.symbol} style={{ display: "grid", gridTemplateColumns: "minmax(0, 1fr) auto", gap: "2px 12px", padding: "10px 0", borderBottom: "1px solid #1f222b" }}>
                  <span className="bold">{p.name}</span>
                  <span className="r mono bold" style={{ color: upColor(r) }}>{pct(r)}</span>
                  <span className="small muted">{int(p.qty)} 股 · 成本 {cost.toFixed(decimalsOf(p.symbol))}</span>
                  <span className="r small bold" style={{ color: p.frozen_qty ? "var(--gold)" : "var(--muted)" }}>{p.frozen_qty ? `T+1 锁定 ${int(p.frozen_qty)} 股` : "可卖"}</span>
                </div>
              );
            })}
          </section>
        </div>
      </div>
      <p className="foot">仅限模拟交易，所有资金均为虚拟，不构成投资建议</p>
    </main>
  );
}

function Stat({ label, value, color }: { label: string; value: string; color?: string }) {
  return (
    <div style={{ display: "flex", flexDirection: "column" }}>
      <span className="small muted">{label}</span>
      <span className="mono bold" style={{ fontSize: 22, color }}>{value}</span>
    </div>
  );
}

function BookLine({ label, p, v, color }: { label: string; p: string; v: number; color: string }) {
  return (
    <div className="between" style={{ padding: "2px 0", flexWrap: "nowrap" }}>
      <span className="muted">{label}</span><span style={{ color }}>{p}</span><span>{int(Math.round(v / 100))}</span>
    </div>
  );
}

function OrderCard({ o, onCancel }: { o: OrderView; onCancel: () => void }) {
  const open = o.status === "new" || o.status === "partially_filled";
  const queued = open && o.queue_ahead !== null && o.queue_initial !== null && o.queue_initial > 0;
  const [label, cls] = o.status === "filled" && o.queue_initial ? ["排到了 · 已成交", "pill-up"] : queued ? ["排队中", "pill-gold"] : STATUS[o.status] ?? [o.status, "pill-gray"];
  const p = o.queue_progress_pct ?? 0;
  return (
    <article className="inner stack" style={{ gap: 8, padding: "12px 14px" }}>
      <div className="between">
        <span className="bold">
          <span style={{ color: o.side === "buy" ? "var(--up)" : "var(--down)" }}>{o.side === "buy" ? "买" : "卖"}</span> {o.symbol_name ?? o.symbol} {lots(o.qty)} 手 @ {o.avg_fill_price ?? o.price ?? "市价"}
        </span>
        <span className={`pill ${cls}`}>{label}</span>
      </div>
      {o.thesis ? <span className="small muted">「{o.thesis}」</span> : null}
      {queued ? (
        <>
          <div className="bar" role="progressbar" aria-label="排队进度" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(p)}>
            <div style={{ width: `${p}%`, background: "var(--gold)" }} />
          </div>
          <span className="mono small muted">前方还有 {int(Math.ceil((o.queue_ahead ?? 0) / 100))} 手 · {Math.round(p)}%</span>
        </>
      ) : null}
      {open ? <button type="button" className="btn btn-ghost small" style={{ alignSelf: "flex-start", minHeight: 40 }} onClick={onCancel}>撤单</button> : null}
    </article>
  );
}
