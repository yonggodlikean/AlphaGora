"use client";
import Link from "next/link";
import type { Conflict } from "@/lib/api";
import { hhmm, lots } from "@/lib/format";

/** One 多空对决 card: the same stock bought by one team and sold by another, each with its thesis. */
export function ConflictCard({ c }: { c: Conflict }) {
  return (
    <article style={{ border: "1px solid var(--line)", borderRadius: "var(--r-panel)", background: "var(--panel-2)", overflow: "hidden" }}>
      <div className="between" style={{ padding: "10px 14px", background: "var(--panel)", borderBottom: "1px solid var(--line)", fontSize: 13 }}>
        <span className="bold">{c.symbol_name} · {c.symbol}</span>
        <span className="mono muted">{hhmm(c.ts)}</span>
      </div>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(2, minmax(0, 1fr))", gap: 1, background: "var(--line)" }}>
        <Link href={`/portfolio/${c.buyer_id}`} style={{ background: "var(--panel-2)", padding: "12px 14px", display: "flex", flexDirection: "column", gap: 4, color: "var(--text)", textDecoration: "none" }}>
          <span className="small bold" style={{ color: "var(--up)" }}>▲ 买入 {lots(c.buy_qty)} 手</span>
          <span className="bold">{c.buyer}</span>
          <span className="small muted quote">「{c.buy_thesis}」</span>
        </Link>
        <Link href={`/portfolio/${c.seller_id}`} style={{ background: "var(--panel-2)", padding: "12px 14px", display: "flex", flexDirection: "column", gap: 4, color: "var(--text)", textDecoration: "none" }}>
          <span className="small bold" style={{ color: "var(--down)" }}>▼ 卖出 {lots(c.sell_qty)} 手</span>
          <span className="bold">{c.seller}</span>
          <span className="small muted quote">「{c.sell_thesis}」</span>
        </Link>
      </div>
    </article>
  );
}
