"use client";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import { type Market, type Phase } from "@/lib/api";
import { subscribeMarket } from "@/lib/market";
import { GitHubIcon, LogoMark, REPO_URL } from "./Logo";

const LINKS = [
  { href: "/", label: "擂台" },
  { href: "/trade", label: "我的交易台" },
  { href: "/join", label: "加入擂台" },
];

const PHASE: Record<Phase, [string, string]> = {
  trading: ["交易中", "pill-up"], auction: ["集合竞价", "pill-gold"], open_wait: ["等待开盘", "pill-gold"],
  closing_auction: ["收盘竞价", "pill-gold"], waiting: ["等待行情", "pill-gold"],
  lunch: ["午间休市", "pill-gray"], pre_open: ["未开盘", "pill-gray"], closed: ["已收盘", "pill-gray"], holiday: ["休市", "pill-gray"],
};
const DOT: Record<string, string> = { "pill-up": "#f0524b", "pill-gold": "#f5c451", "pill-gray": "#6f7684" };
const fmtClock = (ms: number) => new Date(ms).toLocaleTimeString("zh-CN", { timeZone: "Asia/Shanghai", hour12: false });

/** Status comes from the shared 5s poll; the clock ticks locally every second from the last server reading. */
function useMarketClock() {
  const [market, setMarket] = useState<Market | null>(null);
  const anchor = useRef<{ server: number; local: number } | null>(null);
  const [clock, setClock] = useState("");
  useEffect(() => subscribeMarket((m) => {
    if (m) {
      const server = m.now ? Date.parse(m.now) : NaN;
      anchor.current = !m.simulated && Number.isFinite(server) ? { server, local: performance.now() } : null;
      setClock(anchor.current ? fmtClock(server) : m.clock);
    }
    setMarket(m);
  }), []);
  useEffect(() => {
    const id = setInterval(() => {
      const a = anchor.current;
      if (a) setClock(fmtClock(a.server + performance.now() - a.local));
    }, 1000);
    return () => clearInterval(id);
  }, []);
  return { market, clock };
}

export function Nav() {
  const path = usePathname();
  const { market, clock } = useMarketClock();
  const [base, pill] = market ? PHASE[market.phase ?? (market.trading ? "trading" : "closed")] ?? PHASE.closed : PHASE.closed;
  const label = market?.phase === "auction" ? `${base} · ${clock >= "09:20:00" ? "不可撤单" : "可撤单"}` : base;

  return (
    <header style={{ borderBottom: "1px solid var(--line)", background: "var(--bar)" }}>
      <div style={{ maxWidth: 1360, margin: "0 auto", padding: "14px 24px", display: "flex", flexWrap: "wrap", alignItems: "center", gap: "16px 32px" }}>
        <Link href="/" style={{ display: "flex", alignItems: "center", gap: 10, textDecoration: "none" }}>
          <LogoMark size={30} />
          <span className="mono" style={{ fontWeight: 700, fontSize: 22, letterSpacing: "0.08em", color: "var(--gold)" }}>AGORA</span>
          <span className="small muted">交易即观点，收益即裁决</span>
        </Link>
        <nav aria-label="主导航" style={{ display: "flex", flexWrap: "wrap", gap: 4 }}>
          {LINKS.map((l) => {
            const active = l.href === "/" ? path === "/" : path.startsWith(l.href);
            return (
              <Link key={l.href} href={l.href} aria-current={active ? "page" : undefined}
                style={{ padding: "10px 14px", borderRadius: 8, textDecoration: "none", color: active ? "var(--text)" : "#b9bfcb",
                  background: active ? "#1f222b" : "transparent", fontWeight: active ? 700 : 400 }}>
                {l.label}
              </Link>
            );
          })}
        </nav>
        <div style={{ marginLeft: "auto", display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap" }}>
          {market ? (
            <span className={`pill ${pill}`} style={{ display: "inline-flex", alignItems: "center", gap: 8, padding: "6px 12px" }}>
              <span aria-hidden="true" style={{ width: 8, height: 8, borderRadius: "50%", background: DOT[pill] }} />
              {label}
              <span className="mono" style={{ fontVariantNumeric: "tabular-nums" }}>{clock}</span>
              {market.simulated ? <span className="faint">· 演示行情</span> : null}
            </span>
          ) : (
            <span className="pill pill-gray">连接中…</span>
          )}
          {market?.online ? (
            <span className="small muted" title={market.visitors_today ? `今天 ${market.visitors_today} 人来过` : undefined}>{market.online} 人在围观</span>
          ) : null}
          <a href={REPO_URL} target="_blank" rel="noopener noreferrer" aria-label="GitHub 开源代码" title="开源代码"
            style={{ display: "inline-flex", alignItems: "center", justifyContent: "center", width: 40, height: 40, borderRadius: 8, color: "#b9bfcb" }}>
            <GitHubIcon size={20} />
          </a>
        </div>
      </div>
    </header>
  );
}
