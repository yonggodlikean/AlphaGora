"use client";
import { useMemo, useState } from "react";
import type { CurveTrade } from "@/lib/api";

const W = 1200;            // close to the card's real width, so text and strokes keep their size
const H = 300;
const PAD = { l: 10, r: 60, t: 12, b: 26 };
const UP = "#ff6b63";
const DOWN = "#3fcb8a";

const day = (t: number) => new Date(t).toLocaleDateString("zh-CN", { timeZone: "Asia/Shanghai", month: "2-digit", day: "2-digit" }).replace("/", "-");
const hm = (t: number) => new Date(t).toLocaleTimeString("zh-CN", { timeZone: "Asia/Shanghai", hour12: false, hour: "2-digit", minute: "2-digit" });

/** Return curve on a trading-time axis (nights and lunch breaks take no space): the team as a solid line, the
 *  CSI 300 benchmark dashed, buys and sells as dots. Values are basis points of return since the season start. */
export function EquityChart({ series, benchmark, trades, daily, onPick }: {
  series: [number, number][]; benchmark: [number, number][]; trades: CurveTrade[]; daily: boolean;
  onPick: (item: string) => void;
}) {
  const [hover, setHover] = useState<number | null>(null);
  const geo = useMemo(() => {
    if (series.length < 2) return null;
    const ts = series.map((p) => p[0]);
    const near = (t: number) => {           // index of the last point at or before t
      let lo = 0, hi = ts.length - 1;
      while (lo < hi) {
        const mid = (lo + hi + 1) >> 1;
        if (ts[mid] <= t) lo = mid; else hi = mid - 1;
      }
      return lo;
    };
    const bench = benchmark.map(([t, v]) => [near(t), v] as [number, number]);
    const vals = [...series.map((p) => p[1]), ...bench.map((p) => p[1]), 0];
    const lo = Math.min(...vals), hi = Math.max(...vals);
    const span = Math.max(hi - lo, 20) * 1.15;
    const mid = (hi + lo) / 2;
    const y0 = mid - span / 2, y1 = mid + span / 2;
    const x = (i: number) => PAD.l + (i / (ts.length - 1)) * (W - PAD.l - PAD.r);
    const y = (v: number) => PAD.t + ((y1 - v) / (y1 - y0)) * (H - PAD.t - PAD.b);
    const line = series.map((p, i) => `${x(i).toFixed(1)},${y(p[1]).toFixed(1)}`).join(" ");
    const bline = bench.map(([i, v]) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
    const ticks = [0, 1, 2, 3, 4].map((k) => y0 + ((y1 - y0) * k) / 4);
    const days: { x: number; label: string }[] = [];
    if (!daily) {
      ts.forEach((t, i) => { if (i === 0 || day(t) !== day(ts[i - 1])) days.push({ x: x(i), label: day(t) }); });
    } else {
      const every = Math.max(1, Math.ceil(ts.length / 6));
      ts.forEach((t, i) => { if (i % every === 0) days.push({ x: x(i), label: day(t) }); });
    }
    const marks = trades.map((tr) => { const i = near(tr.t); return { tr, x: x(i), y: y(series[i][1]) }; });
    return { line, bline, ticks, y, days, marks, last: series[series.length - 1][1] };
  }, [series, benchmark, trades, daily]);

  if (!geo) return <p className="empty">数据还不够，开盘后每 5 分钟会多一个点。</p>;
  const color = geo.last >= 0 ? UP : DOWN;
  const h = hover !== null ? geo.marks[hover] : null;
  return (
    <svg viewBox={`0 0 ${W} ${H}`} width="100%" role="img" aria-label="收益曲线" style={{ display: "block" }}
      onMouseLeave={() => setHover(null)}>
      {geo.ticks.map((v, k) => (
        <g key={k}>
          <line x1={PAD.l} x2={W - PAD.r} y1={geo.y(v)} y2={geo.y(v)} stroke="#1f222b" />
          <text x={W - 4} y={geo.y(v) + 4} textAnchor="end" fill="#6f7684" fontSize={11} fontFamily="var(--mono)">
            {v > 0 ? "+" : ""}{(v / 100).toFixed(2)}%
          </text>
        </g>
      ))}
      <line x1={PAD.l} x2={W - PAD.r} y1={geo.y(0)} y2={geo.y(0)} stroke="#3a3f4b" />
      {geo.days.map((d, k) => (
        <g key={k}>
          {k > 0 && !daily ? <line x1={d.x} x2={d.x} y1={PAD.t} y2={H - PAD.b} stroke="#262a33" strokeDasharray="3 4" /> : null}
          <text x={d.x + 4} y={H - 8} fill="#6f7684" fontSize={11} fontFamily="var(--mono)">{d.label}</text>
        </g>
      ))}
      {geo.bline ? <polyline points={geo.bline} fill="none" stroke="#9aa1ae" strokeWidth={1.5} strokeDasharray="5 4" /> : null}
      <polyline points={geo.line} fill="none" stroke={color} strokeWidth={2.2} strokeLinejoin="round" />
      {geo.marks.map((m, k) => (
        <circle key={k} cx={m.x} cy={m.y} r={hover === k ? 7 : 5} fill={m.tr.side === "buy" ? UP : DOWN} stroke="#171920" strokeWidth={2}
          style={{ cursor: "pointer" }} onMouseEnter={() => setHover(k)} onClick={() => onPick(m.tr.item)}>
          <title>{`${m.tr.side === "buy" ? "买入" : "卖出"} ${m.tr.name}`}</title>
        </circle>
      ))}
      {h ? (() => {
        const bw = 230, left = h.x + bw + 16 > W ? h.x - bw - 12 : h.x + 12, top = Math.max(PAD.t, h.y - 70);
        const thesis = (h.tr.thesis ?? "").slice(0, 22) + ((h.tr.thesis ?? "").length > 22 ? "…" : "");
        return (
          <g pointerEvents="none">
            <line x1={h.x} x2={h.x} y1={PAD.t} y2={H - PAD.b} stroke="#5a4a22" />
            <rect x={left} y={top} width={bw} height={64} rx={8} fill="#13151b" stroke="#5a4a22" />
            <text x={left + 10} y={top + 20} fill="#e8eaf0" fontSize={12} fontWeight={700}>
              {day(h.tr.t)} {hm(h.tr.t)} · {h.tr.side === "buy" ? "买入" : "卖出"} {h.tr.name}
            </text>
            <text x={left + 10} y={top + 38} fill="#c3c8d2" fontSize={12}>{thesis || `${h.tr.qty} 股 @ ${h.tr.price}`}</text>
            <text x={left + 10} y={top + 55} fill="#f5c451" fontSize={11}>点击 → 看这条决定</text>
          </g>
        );
      })() : null}
    </svg>
  );
}
