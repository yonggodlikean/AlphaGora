"use client";
import { useState } from "react";
import { api, ApiError, getToken, setToken, type AgentState } from "@/lib/api";

/** Sign in on a new browser by pasting the token from the old one. The token is checked against /agent/state first. */
export function TokenLogin({ onDone }: { onDone: () => void }) {
  const [value, setValue] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function login() {
    const t = value.trim();
    if (!t) return;
    setBusy(true);
    setError(null);
    try {
      const s = await api<AgentState>("/api/v1/agent/state", { headers: { Authorization: `Bearer ${t}` } });
      setToken(t, s.portfolio_name);
      onDone();
    } catch (e) {
      const err = e as ApiError;
      setError(err.status === 401 ? "这个 Token 不对，或者账号已被封禁" : err.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="stack" style={{ gap: 8 }} onSubmit={(e) => { e.preventDefault(); login(); }}>
      <span className="small muted">换了浏览器？把原来浏览器里复制的登录 Token 粘贴进来</span>
      <div style={{ display: "flex", flexWrap: "wrap", gap: 10 }}>
        <input className="input mono" style={{ flex: "1 1 260px", minWidth: 0 }} value={value} autoComplete="off" spellCheck={false}
          onChange={(e) => setValue(e.target.value)} placeholder="agk_…" aria-label="登录 Token" />
        <button type="submit" className="btn" disabled={busy || !value.trim()}>{busy ? "验证中…" : "用 Token 登录"}</button>
      </div>
      {error ? <span className="small" role="alert" style={{ color: "var(--up)" }}>{error}</span> : null}
    </form>
  );
}

/** Shows the current browser's token so the player can carry it to another browser. Hidden until asked. */
export function DeviceToken() {
  const [shown, setShown] = useState(false);
  const [copied, setCopied] = useState(false);
  const token = shown ? getToken() : null;
  return (
    <section className="card stack" aria-labelledby="device-title" style={{ gap: 10 }}>
      <h2 id="device-title">换浏览器继续玩</h2>
      <p className="small muted" style={{ margin: 0 }}>登录状态只保存在这个浏览器里。复制下面的登录 Token 存好，换浏览器或清了缓存后，在「交易台」粘贴就能回来。不要发给别人，拿到它就能操作你的账户。</p>
      {token ? (
        <div style={{ display: "flex", flexWrap: "wrap", gap: 10, alignItems: "center" }}>
          <code className="mono" style={{ flex: "1 1 260px", minWidth: 0, padding: "10px 12px", borderRadius: 8, background: "var(--bg)", wordBreak: "break-all" }}>{token}</code>
          <button type="button" className="btn" onClick={() => { navigator.clipboard?.writeText(token).catch(() => undefined); setCopied(true); }}>{copied ? "已复制" : "复制"}</button>
        </div>
      ) : (
        <button type="button" className="btn" style={{ alignSelf: "flex-start" }} onClick={() => setShown(true)}>显示登录 Token</button>
      )}
    </section>
  );
}
