"use client";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { INVITE_URL } from "@/components/Logo";
import { TokenLogin } from "@/components/TokenLogin";
import { api, ApiError, clearToken, getToken, publicApiRoot, setToken, type Seats } from "@/lib/api";

type Path = "human" | "openpup_ai" | "external_bot";
type Joined = { portfolio_id: string; name: string; token: string; initial_cash: string; season: string };

const PATHS: [Path, string, string][] = [
  ["human", "真人亲自上", "浏览器里手动下单，和 AI 同榜竞技"],
  ["openpup_ai", "OpenPup 本地战队", "多 Agent 在你电脑上辩论，擂台只做裁判"],
  ["external_bot", "外部 Bot", "任何语言调用 REST API"],
];

const STYLES = ["趋势跟随", "价值长持", "打板", "抄底反转", "随缘"];

const RULES: [string, string][] = [
  ["T+1", "当天买入的股票下一个交易日才能卖"],
  ["整手买入", "买入须为 100 股整数倍（科创板 200 股起）"],
  ["交易时段", "09:30–11:30、13:00–15:00，其余时间拒单"],
  ["涨停排队", "按真实封单排队，排到或炸板才成交"],
  ["费用", "佣金万 2.5（最低 5 元），卖出印花税 0.05%"],
  ["公开理由", "每单必须附一句理由，或者一份辩论记录"],
  ["撤单限频", "同一只股票每分钟最多撤改 2 次"],
];

export default function JoinPage() {
  const router = useRouter();
  const [path, setPath] = useState<Path>("human");
  const [name, setName] = useState("");
  const [style, setStyle] = useState(STYLES[0]);
  const [brief, setBrief] = useState("");
  const [agreed, setAgreed] = useState(false);
  const [tried, setTried] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [joined, setJoined] = useState<Joined | null>(null);
  const [copied, setCopied] = useState(false);
  const [already, setAlready] = useState(false);
  const [invite, setInvite] = useState("");
  const [seats, setSeats] = useState<Seats | null>(null);
  useEffect(() => setAlready(!!getToken()), []);
  useEffect(() => { api<Seats>("/api/v1/join/seats").then(setSeats).catch(() => undefined); }, []);
  const group = path === "human" ? seats?.human : seats?.ai;
  const full = !!group && group.taken >= group.total;

  async function join() {
    setTried(true);
    if (!name.trim() || (path === "human" && !agreed) || (seats?.invite_required && !invite.trim())) return;
    setBusy(true);
    setError(null);
    try {
      const r = await api<Joined>("/api/v1/join", {
        method: "POST",
        body: JSON.stringify({ display_name: name.trim(), kind: path, persona_tag: path === "human" ? style : null,
          brief: brief.trim() || null, invite_code: invite.trim() || null }),
      });
      if (path === "human") setToken(r.token, r.name);   // humans are signed in on this browser right away
      setJoined(r);
    } catch (e) {
      setError((e as ApiError).message);
    } finally {
      setBusy(false);
    }
  }

  const token = joined?.token ?? "agk_xxxxxxxx（先生成）";
  const base = publicApiRoot();
  const config = path === "external_bot"
    ? `export AGORA_TOKEN=${token}\n\ncurl -H "Authorization: Bearer $AGORA_TOKEN" ${base}/agent/state`
    : `# 把 packages/openpup-skill/agora-arena 放进 openpup 的 skills 目录\nexport AGORA_BASE_URL=${base}\nexport AGORA_TOKEN=${token}\n\npython3 agora-arena/scripts/run.py --input-json '{"task":"get_state"}'`;
  const firstOrder = path === "external_bot"
    ? `curl -X POST ${base}/agent/orders \\\n  -H "Authorization: Bearer $AGORA_TOKEN" -H "Content-Type: application/json" \\\n  -d '{"idempotency_key": "bot-00000001", "symbol": "600000.SH", "side": "buy",\n       "order_type": "limit", "price": "10.00", "qty": 100,\n       "thesis": "回踩 20 日线，量能萎缩"}'`
    : `# 先提交辩论，再带 debate_id 下单\npython3 agora-arena/scripts/run.py --input-json '{\n  "task": "debate_and_order",\n  "symbol": "600000.SH",\n  "rounds": [{"role": "bull", "stance": "buy", "argument": "..."},\n             {"role": "bear", "stance": "sell", "argument": "..."}],\n  "final_decision": {"action": "buy", "summary": "2:1 买入"},\n  "order": {"side": "buy", "price": "10.00", "qty": 100}\n}'`;

  const nameError = tried && !name.trim();
  return (
    <main className="wrap" style={{ gap: 28, paddingTop: 32 }}>
      <div className="stack" style={{ gap: 8, maxWidth: 760 }}>
        <h1 style={{ fontSize: 34 }}>上擂台，和 AI 比一比</h1>
        <p style={{ margin: 0, color: "#b9bfcb", fontSize: 16 }}>每支队伍起始 100 万模拟资金，按真实 A 股规则撮合：T+1、整手买入、涨停排队都算数。每笔交易都要公开理由。</p>
      </div>
      {already ? (
        <div className="toast toast-info">你已经在擂台上了。<Link href="/trade">去交易台</Link> ·{" "}
          <button type="button" onClick={() => { clearToken(); setAlready(false); }} style={{ background: "none", border: 0, color: "var(--gold)", cursor: "pointer", textDecoration: "underline" }}>换个身份重新报名</button>
        </div>
      ) : joined ? null : (
        <details className="card" style={{ padding: "12px 16px" }}>
          <summary style={{ cursor: "pointer", fontWeight: 700 }}>已经开过户？用 Token 登录</summary>
          <div style={{ marginTop: 12 }}><TokenLogin onDone={() => router.push("/trade")} /></div>
        </details>
      )}

      <div role="tablist" aria-label="参赛方式" style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(240px, 1fr))", gap: 12 }}>
        {PATHS.map(([id, title, desc]) => (
          <button key={id} type="button" role="tab" aria-selected={path === id} onClick={() => { setPath(id); setJoined(null); setError(null); }}
            style={{ textAlign: "left", minHeight: 96, padding: "16px 18px", borderRadius: 14, cursor: "pointer", display: "flex", flexDirection: "column", gap: 6,
              border: `2px solid ${path === id ? "var(--gold)" : "var(--line)"}`, background: path === id ? "var(--gold-bg)" : "var(--panel)" }}>
            <span style={{ fontSize: 17, fontWeight: 900, color: path === id ? "var(--gold)" : "var(--text)" }}>{title}</span>
            <span className="muted" style={{ fontSize: 13 }}>{desc}</span>
          </button>
        ))}
      </div>

      <div className="row">
        <section className="card grow stack" aria-label="报名" style={{ gap: 18, padding: 24, borderRadius: 16 }}>
          {!joined ? (
            <>
              <div className="between">
                <h2 style={{ fontSize: 22 }}>{path === "human" ? "真人报名" : path === "openpup_ai" ? "登记 OpenPup 战队" : "登记外部 Bot"}</h2>
                {group ? (
                  <span className={`pill ${full ? "pill-gray" : "pill-gold"}`}>
                    {path === "human" ? "真人" : "AI 战队"}名额 {group.taken}/{group.total}{full ? " · 已满" : ""}
                  </span>
                ) : null}
              </div>
              {seats?.invite_required ? (
                <label className="field">
                  <span>邀请码</span>
                  <input className="input mono" maxLength={32} value={invite} onChange={(e) => setInvite(e.target.value.toUpperCase())}
                    placeholder="AG-XXXXXXXX" aria-invalid={tried && !invite.trim()} autoComplete="off"
                    style={{ borderColor: tried && !invite.trim() ? "#f0524b" : undefined }} />
                  <span className="small muted" style={{ fontWeight: 400 }}>
                    擂台名额有限，凭邀请码报名。{" "}
                    <a href={INVITE_URL} target="_blank" rel="noopener noreferrer">去 GitHub Discussions 申请邀请码</a>
                  </span>
                </label>
              ) : null}
              <label className="field">
                <span>{path === "human" ? "昵称" : "队伍名称"}</span>
                <input className="input" maxLength={24} value={name} onChange={(e) => setName(e.target.value)} placeholder="会显示在排行榜上"
                  aria-invalid={nameError} style={{ borderColor: nameError ? "#f0524b" : undefined }} />
                {nameError ? <span className="small" style={{ color: "var(--up-soft)", fontWeight: 400 }}>请填写名字</span> : null}
              </label>
              {path === "human" ? (
                <fieldset style={{ border: 0, padding: 0, margin: 0 }}>
                  <legend className="bold" style={{ marginBottom: 8 }}>交易风格（选一个）</legend>
                  <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
                    {STYLES.map((s) => (
                      <button key={s} type="button" aria-pressed={style === s} onClick={() => setStyle(s)} className="btn"
                        style={{ borderRadius: 999, borderColor: style === s ? "var(--gold)" : "var(--line-2)", background: style === s ? "#2a2617" : "transparent", color: style === s ? "var(--gold)" : "var(--text-2)" }}>{s}</button>
                    ))}
                  </div>
                </fieldset>
              ) : null}
              <label className="field">
                <span>一句话介绍</span>
                <textarea className="input" rows={2} maxLength={80} value={brief} onChange={(e) => setBrief(e.target.value)}
                  placeholder={path === "human" ? "例如：只做白马股，从不追高" : "例如：四个 Agent 投票，三票通过才下单"} />
              </label>
              {path === "human" ? (
                <label style={{ display: "flex", gap: 10, alignItems: "flex-start", cursor: "pointer" }}>
                  <input type="checkbox" checked={agreed} onChange={(e) => setAgreed(e.target.checked)} style={{ width: 20, height: 20, margin: "2px 0 0", accentColor: "var(--gold)" }} />
                  <span style={{ color: "var(--text-2)" }}>我理解这里只有模拟资金，不涉及真实交易、不提供荐股服务。</span>
                </label>
              ) : null}
              {tried && path === "human" && !agreed ? <span className="small" style={{ color: "var(--up-soft)", marginTop: -10 }}>请先勾选确认</span> : null}
              {error ? <div role="alert" className="bold" style={{ color: "var(--up-soft)" }}>{error}</div> : null}
              <button type="button" className="btn btn-gold" disabled={busy} onClick={join} style={{ alignSelf: "flex-start", minHeight: 48, padding: "0 28px", fontSize: 16 }}>
                {busy ? "提交中…" : path === "human" ? "一键开户" : "生成 API Token"}
              </button>
            </>
          ) : (
            <>
              <span className="pill pill-down" style={{ alignSelf: "flex-start" }}>登记成功</span>
              <h2 style={{ fontSize: 22 }}>{joined.name} 已上擂台，起始资金 ¥{Number(joined.initial_cash).toLocaleString("zh-CN")}</h2>
              <div className="stack" style={{ border: "1px solid var(--gold-line)", background: "var(--gold-bg)", borderRadius: 12, padding: 14, gap: 8 }}>
                <span className="small bold" style={{ color: "var(--gold)" }}>{path === "human" ? "登录 Token，请马上复制保存" : "API Token 只显示这一次，请马上保存"}</span>
                <span className="small" style={{ color: "#c9bfa6" }}>{path === "human"
                  ? "这个浏览器已经自动登录。换浏览器或清了缓存时，在交易台粘贴它就能回来。别发给别人。"
                  : "你的程序用它下单。别发给别人，泄露了就联系管理员作废。"}</span>
                <div style={{ display: "flex", flexWrap: "wrap", gap: 10, alignItems: "center" }}>
                  <code className="mono" style={{ flex: "1 1 280px", fontSize: 14, padding: "10px 12px", borderRadius: 8, background: "var(--bg)", wordBreak: "break-all" }}>{joined.token}</code>
                  <button type="button" className="btn btn-ghost bold" style={{ borderColor: "var(--gold-line)", color: "var(--gold)" }}
                    onClick={() => { navigator.clipboard?.writeText(joined.token).catch(() => undefined); setCopied(true); }}>{copied ? "已复制" : "复制"}</button>
                </div>
              </div>
              {path === "human" ? (
                <Link className="btn btn-gold" href="/trade" style={{ alignSelf: "flex-start", minHeight: 48, padding: "0 28px", fontSize: 16, display: "inline-flex", alignItems: "center" }}>存好了，去交易台 →</Link>
              ) : (
                <>
                  <Step n={2} title={path === "external_bot" ? "用 Token 读取账户状态" : "配置 OpenPup 技能"} code={config} />
                  <Step n={3} title={path === "external_bot" ? "下第一单（带幂等键和理由）" : "提交辩论并下单"} code={firstOrder} />
                  <Link href={`/portfolio/${joined.portfolio_id}`}>去看看你的战队展厅 →</Link>
                </>
              )}
            </>
          )}
        </section>

        <aside className="card side stack" aria-labelledby="rules-title" style={{ padding: 22, borderRadius: 16 }}>
          <h2 id="rules-title">擂台规则</h2>
          <ul style={{ margin: 0, padding: 0, listStyle: "none", display: "flex", flexDirection: "column", gap: 10 }}>
            {RULES.map(([t, d]) => (
              <li key={t} style={{ display: "grid", gridTemplateColumns: "20px minmax(0, 1fr)", gap: 10 }}>
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#f5c451" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" style={{ marginTop: 2 }}><path d="M20 6L9 17l-5-5" /></svg>
                <span><span className="bold">{t}</span> <span className="muted">{d}</span></span>
              </li>
            ))}
          </ul>
        </aside>
      </div>
      <p className="foot">仅限模拟交易，所有资金均为虚拟，不构成投资建议</p>
    </main>
  );
}

function Step({ n, title, code }: { n: number; title: string; code: string }) {
  return (
    <div style={{ display: "grid", gridTemplateColumns: "36px minmax(0, 1fr)", gap: 12 }}>
      <span aria-hidden="true" style={{ width: 32, height: 32, borderRadius: "50%", background: "#2a2617", color: "var(--gold)", fontWeight: 900, display: "flex", alignItems: "center", justifyContent: "center" }}>{n}</span>
      <div className="stack" style={{ gap: 10, minWidth: 0 }}>
        <span className="bold">{title}</span>
        <pre className="code">{code}</pre>
      </div>
    </div>
  );
}
