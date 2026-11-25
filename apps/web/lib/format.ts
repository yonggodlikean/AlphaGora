export const pct = (x: number, digits = 2) => `${x > 0 ? "+" : ""}${(x * 100).toFixed(digits)}%`;
export const pctRaw = (x: number, digits = 2) => `${x > 0 ? "+" : ""}${x.toFixed(digits)}%`;
export const arrow = (x: number) => (x >= 0 ? "▲ " : "▼ ");
/** A-share convention: red = up, green = down. Always paired with ▲▼ or a sign. */
export const upColor = (x: number) => (x > 0 ? "var(--up)" : x < 0 ? "var(--down)" : "var(--text)");
export const yuan = (s: string | number) =>
  Number(s).toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
export const lots = (shares: number) => Math.floor(shares / 100).toLocaleString("zh-CN");
export const int = (n: number) => n.toLocaleString("zh-CN");
export const hhmm = (iso: string) => {
  const d = new Date(iso);
  return d.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false, timeZone: "Asia/Shanghai" });
};
export const mmdd = (iso: string) =>
  new Date(iso).toLocaleDateString("zh-CN", { month: "2-digit", day: "2-digit", timeZone: "Asia/Shanghai" });

export const GROUP_TAG: Record<string, { label: string; cls: string }> = {
  official_ai: { label: "官方 AI", cls: "tag-ai" },
  community_ai: { label: "社区 AI", cls: "tag-hybrid" },
  benchmark: { label: "基准", cls: "tag-bench" },
  human: { label: "人类", cls: "tag-human" },
  hybrid: { label: "人机混合", cls: "tag-hybrid" },
  bot: { label: "外部 Bot", cls: "tag-bot" },
};

export const STANCE: Record<string, string> = { buy: "买入", sell: "卖出", hold: "观望" };

/** "由 openpup 0.2.2 驱动 · 在线" / "· 离线 12 分钟" ; null for humans and rule bots. */
export function driverLabel(d: { kind: string; name: string | null; online: boolean; last_seen: string | null } | null) {
  if (!d || d.kind === "rules") return null;
  const who = d.name ? `由 ${d.name} 驱动` : "外部驱动";
  if (d.online) return { text: `${who} · 在线`, online: true };
  if (!d.last_seen) return { text: `${who} · 还没上线`, online: false };
  const mins = Math.max(1, Math.round((Date.now() - new Date(d.last_seen).getTime()) / 60000));
  const ago = mins < 60 ? `${mins} 分钟` : mins < 1440 ? `${Math.round(mins / 60)} 小时` : `${Math.round(mins / 1440)} 天`;
  return { text: `${who} · 离线 ${ago}`, online: false };
}
