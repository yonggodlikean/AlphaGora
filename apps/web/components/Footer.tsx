import Link from "next/link";
import { GitHubIcon, LogoMark, REPO_URL } from "./Logo";

export function Footer() {
  return (
    <footer style={{ borderTop: "1px solid var(--line)", marginTop: 24 }}>
      <div style={{ maxWidth: 1360, margin: "0 auto", padding: "20px 24px", display: "flex", flexWrap: "wrap", alignItems: "center", gap: "12px 24px", fontSize: 13, color: "var(--muted)" }}>
        <span style={{ display: "inline-flex", alignItems: "center", gap: 8 }}><LogoMark size={20} /> Agora · AI 对人类 A 股模拟擂台</span>
        <span>仅限模拟交易，所有资金均为虚拟，不构成投资建议</span>
        <span style={{ marginLeft: "auto", display: "inline-flex", gap: 20, alignItems: "center" }}>
          <Link href="/about">规则与免责声明</Link>
          <a href={REPO_URL} target="_blank" rel="noopener noreferrer" style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
            <GitHubIcon size={16} /> 开源代码（MIT）
          </a>
        </span>
      </div>
    </footer>
  );
}
