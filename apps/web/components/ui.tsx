"use client";
import type { ReactNode } from "react";

/** Flat content panel (no glass on main content). Glass is reserved for floating layers. */
export function Panel({ title, actions, children, className }: { title?: ReactNode; actions?: ReactNode; children: ReactNode; className?: string }) {
  return (
    <section className={className} style={{ background: "var(--panel)", border: "1px solid var(--line)", borderRadius: "var(--r-panel)", overflow: "hidden" }}>
      {(title || actions) && (
        <header className="between" style={{ padding: "12px 16px", borderBottom: "1px solid var(--line)" }}>
          <span className="section-label" style={{ fontSize: "var(--fs-13)", color: "var(--text-2)", fontWeight: 500 }}>{title}</span>
          {actions}
        </header>
      )}
      {children}
    </section>
  );
}

/** Numeric cell: mono + tabular-nums for column alignment. */
export function Num({ children, color, title }: { children: ReactNode; color?: string; title?: string }) {
  return <span className="num" style={{ color, fontVariantNumeric: "tabular-nums" }} title={title}>{children}</span>;
}

/** Neutral camp tag. Colour dot only separates AI vs human; label stays grey. */
export function Tag({ label, camp, title }: { label: string; camp?: "ai" | "human" | null; title?: string }) {
  return (
    <span className="tag" title={title}>
      {camp === "ai" && <span className="dot" style={{ background: "var(--accent)" }} />}
      {camp === "human" && <span className="dot" style={{ background: "#c69a6b" }} />}
      {label}
    </span>
  );
}

/** Segmented control with a sliding selection block. */
export function Segmented<T extends string>({ options, value, onChange, ariaLabel }: {
  options: { value: T; label: ReactNode }[];
  value: T;
  onChange: (v: T) => void;
  ariaLabel?: string;
}) {
  return (
    <div role="tablist" aria-label={ariaLabel} className="tabs" style={{ minHeight: 34, gap: 2, padding: 3 }}>
      {options.map((o) => (
        <button key={o.value} role="tab" aria-selected={o.value === value} className="tab" style={{ minHeight: 28, padding: "4px 12px", fontSize: "var(--fs-13)" }}
          onClick={() => onChange(o.value)}>
          {o.label}
        </button>
      ))}
    </div>
  );
}

/** Compact stat: label + tabular number in one row (replaces big metric cards). */
export function Stat({ label, value, color, sub }: { label: ReactNode; value: ReactNode; color?: string; sub?: ReactNode }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
      <span className="small muted">{label}</span>
      <span className="num" style={{ fontSize: "var(--fs-16)", fontWeight: 600, color, fontVariantNumeric: "tabular-nums" }}>{value}</span>
      {sub ? <span className="small faint">{sub}</span> : null}
    </div>
  );
}

/** Skeleton block replaces "loading…". */
export function Skeleton({ w = "100%", h = 14, style }: { w?: number | string; h?: number | string; style?: React.CSSProperties }) {
  return <div className="skeleton" aria-hidden="true" style={{ width: w, height: h, ...style }} />;
}

/** Serif pull-quote for 复盘 / 点评. */
export function Quote({ children, cite }: { children: ReactNode; cite?: ReactNode }) {
  return (
    <blockquote className="quote" style={{ margin: 0, padding: "4px 0 4px 14px", borderLeft: "2px solid var(--accent-line)", color: "var(--text-2)", fontSize: "var(--fs-14)" }}>
      {children}
      {cite ? <footer className="small faint" style={{ marginTop: 4 }}>{cite}</footer> : null}
    </blockquote>
  );
}
