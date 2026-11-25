"use client";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useState } from "react";
import { api, type Market } from "@/lib/api";
import { GitHubIcon, LogoMark, REPO_URL } from "./Logo";
import { usePoll } from "@/lib/useEvents";

const LINKS = [
  { href: "/", label: "擂台" },
  { href: "/trade", label: "我的交易台" },
  { href: "/join", label: "加入擂台" },
];

export function Nav() {
  const path = usePathname();
  const [market, setMarket] = useState<Market | null>(null);
  usePoll(() => {
    api<Market>("/api/v1/market/status").then(setMarket).catch(() => setMarket(null));
  }, 5000);

  return (
    <header style={{ borderBottom: "1px solid var(--line)", background: "var(--bar)" }}>
      <div style={{ maxWidth: 1360, margin: "0 auto", padding: "14px 24px", display: "flex", flexWrap: "wrap", alignItems: "center", gap: "16px 32px" }}>
        <Link href="/" style={{ display: "flex", alignItems: "center", gap: 10, textDecoration: "none" }}>
          <LogoMark size={30} />
          <span className="mono" style={{ fontWeight: 700, fontSize: 22, letterSpacing: "0.08em", color: "var(--gold)" }}>AGORA</span>
          <span className="small muted">AI 对人类 · A 股模拟擂台</span>
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
            <span className={`pill ${market.trading ? "pill-up" : "pill-gray"}`} style={{ display: "inline-flex", alignItems: "center", gap: 8, padding: "6px 12px" }}>
              <span aria-hidden="true" style={{ width: 8, height: 8, borderRadius: "50%", background: market.trading ? "#f0524b" : "#6f7684" }} />
              {market.trading ? "交易中" : market.day_open ? "休市中" : "已收盘"}
              <span className="mono">{market.clock}</span>
              {market.simulated ? <span className="faint">· 演示行情</span> : null}
            </span>
          ) : (
            <span className="pill pill-gray">连接中…</span>
          )}
          {market?.online ? <span className="small muted">{market.online} 人在围观</span> : null}
          <a href={REPO_URL} target="_blank" rel="noopener noreferrer" aria-label="GitHub 开源代码" title="开源代码"
            style={{ display: "inline-flex", alignItems: "center", justifyContent: "center", width: 40, height: 40, borderRadius: 8, color: "#b9bfcb" }}>
            <GitHubIcon size={20} />
          </a>
        </div>
      </div>
    </header>
  );
}
