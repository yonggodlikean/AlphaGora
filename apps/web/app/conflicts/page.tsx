"use client";
import Link from "next/link";
import { useEffect, useState } from "react";
import { ConflictCard } from "@/components/ConflictCard";
import { api, type ConflictsDay } from "@/lib/api";

/** Every 多空对决 of a trading day, newest day first; pages back one trading day at a time. */
export default function ConflictsPage() {
  const [days, setDays] = useState<ConflictsDay[] | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api<ConflictsDay>("/api/v1/arena/conflicts").then((d) => setDays([d])).catch(() => setDays([]));
  }, []);
  const prev = days?.[days.length - 1]?.prev ?? null;
  async function older() {
    if (!prev) return;
    setBusy(true);
    try {
      const d = await api<ConflictsDay>(`/api/v1/arena/conflicts?date=${prev}`);
      setDays((x) => [...(x ?? []), d]);
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="wrap" style={{ gap: 20 }}>
      <div className="stack" style={{ gap: 6 }}>
        <h1 style={{ fontSize: 28 }}>多空对决</h1>
        <p className="muted" style={{ margin: 0 }}>同一只股票，有人买有人卖，理由摆在一起。<Link href="/">回擂台</Link></p>
      </div>
      <section className="card stack" style={{ gap: 12 }}>
        {days === null ? <p className="empty">加载中…</p> : null}
        {days?.map((d) => (
          <div key={d.date} className="stack" style={{ gap: 10 }}>
            <div style={{ display: "flex", alignItems: "center", gap: 10, color: "var(--muted)", fontSize: 12 }}>
              <span className="mono bold" style={{ color: "var(--text-2)" }}>{d.date.slice(5)} 周{d.weekday}</span>
              <span>{d.conflicts.length} 场</span>
              <span style={{ flex: 1, borderTop: "1px solid #1f222b" }} />
            </div>
            {d.conflicts.length === 0 ? <p className="empty">这一天还没有人吵起来。</p> : null}
            {d.conflicts.map((c, i) => <ConflictCard key={`${c.ts}-${i}`} c={c} />)}
          </div>
        ))}
        {prev ? (
          <button type="button" className="btn btn-ghost" style={{ alignSelf: "center" }} disabled={busy} onClick={older}>
            {busy ? "加载中…" : "往前翻一个交易日"}
          </button>
        ) : days && days.length ? <span className="small faint" style={{ alignSelf: "center" }}>没有更早的对决了</span> : null}
      </section>
    </main>
  );
}
