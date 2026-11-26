"use client";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import { type Market, type Phase } from "@/lib/api";
import { subscribeMarket } from "@/lib/market";
import { GitHubIcon, LogoMark, REPO_URL } from "./Logo";

const LINKS = [
  { href: "/", label: "擂台" },
  { href: "/trade", label: "交易台" },
  { href: "/seasons", label: "往届" },
  { href: "/about", label: "规则" },
];

const PHASE: Record<Phase, [string, string]> = {
  trading: ["交易中", "pill-up"], auction: ["集合竞价", "pill-act"], open_wait: ["等待开盘", "pill-act"],
  closing_auction: ["收盘竞价", "pill-act"], waiting: ["等待行情", "pill-act"],
  lunch: ["午间休市", "pill-gray"], pre_open: ["未开盘", "pill-gray"], closed: ["已收盘", "pill-gray"], holiday: ["休市", "pill-gray"],
};
/** Dots read from tokens; "trading" gets a faint cold breathing halo. */
const DOT: Record<string, string> = { "pill-up": "var(--up)", "pill-act": "var(--accent)", "pill-gray": "var(--faint)" };
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
  const [open, setOpen] = useState(false);
  const [base, pill] = market ? PHASE[market.phase ?? (market.trading ? "trading" : "closed")] ?? PHASE.closed : PHASE.closed;
  const label = market?.phase === "auction" ? `${base} · ${clock >= "09:20:00" ? "不可撤单" : "可撤单"}` : base;

  return (
    <header className="nav-float">
      <div className="nav-row">
        <Link href="/" style={{ display: "flex", alignItems: "center", gap: 10, textDecoration: "none" }}>
          <LogoMark size={26} />
          <span className="mono" style={{ fontWeight: 600, fontSize: 20, letterSpacing: "0.1em", color: "var(--text)" }}>AGORA</span>
        </Link>
        <nav aria-label="主导航" className="nav-links">
          {LINKS.map((l) => {
            const active = l.href === "/" ? path === "/" : path.startsWith(l.href);
            return (
              <Link key={l.href} href={l.href} aria-current={active ? "page" : undefined} className="nav-link" data-active={active || undefined}>
                {l.label}
              </Link>
            );
          })}
        </nav>
        <div className="nav-right">
          {market ? (
            <span className={`pill ${pill} nav-phase`} style={{ gap: 8 }}>
              <span aria-hidden="true" className={pill === "pill-up" ? "dot-breathe" : undefined} style={{ width: 8, height: 8, borderRadius: "50%", background: DOT[pill] }} />
              {label}
              <span className="mono num">{clock}</span>
              {market.simulated ? <span className="faint">· 演示行情</span> : null}
            </span>
          ) : (
            <span className="pill pill-gray">连接中…</span>
          )}
          {market?.online ? (
            <span className="small muted" title={market.visitors_today ? `今天 ${market.visitors_today} 人来过` : undefined}>{market.online} 人在围观</span>
          ) : null}
          <Link href="/trade" className="btn btn-primary nav-cta">上场交易</Link>
          <a href={REPO_URL} target="_blank" rel="noopener noreferrer" aria-label="GitHub 开源代码" title="开源代码"
            style={{ display: "inline-flex", alignItems: "center", justifyContent: "center", width: 34, height: 34, borderRadius: "var(--r-ctrl)", color: "var(--muted)" }}>
            <GitHubIcon size={19} />
          </a>
          <button className="nav-burger" aria-label="菜单" aria-expanded={open} onClick={() => setOpen((v) => !v)}>☰</button>
        </div>
      </div>
      {open && (
        <nav aria-label="移动端导航" className="nav-links nav-drawer">
          {LINKS.map((l) => (
            <Link key={l.href} href={l.href} className="nav-link" onClick={() => setOpen(false)}>{l.label}</Link>
          ))}
        </nav>
      )}
    </header>
  );
}
