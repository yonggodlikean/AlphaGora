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
      {camp === "human" && <span className="dot" style={{ background: "var(--text-2)" }} />}
      {label}
    </span>
  );
}

/** Segmented control with a sliding selection block. */
export function Segmented<T extends string>({ value, onChange, options, size }: {
  value: T; onChange: (v: T) => void; options: { value: T; label: ReactNode; title?: string }[]; size?: "sm" | "md";
}) {
  return (
    <div role="tablist" aria-label="切换" className="segmented" data-size={size} style={{ display: "inline-flex", gap: 2, padding: 2,
      borderRadius: "var(--r-ctrl)", background: "var(--panel-2)", border: "1px solid var(--line)" }}>
      {options.map((o) => {
        const active = o.value === value;
        return (
          <button key={o.value} role="tab" aria-selected={active} title={o.title} onClick={() => onChange(o.value)}
            style={{ appearance: "none", border: "none", cursor: "pointer", font: "inherit", fontWeight: 500, color: active ? "var(--text)" : "var(--muted)",
              background: active ? "var(--accent-soft)" : "transparent", borderRadius: "var(--r-ctrl)",
              padding: size === "sm" ? "3px 10px" : "5px 14px", fontSize: size === "sm" ? "var(--fs-12)" : "var(--fs-13)",
              transition: "background var(--t-fast), color var(--t-fast)" }}>
            {o.label}
          </button>
        );
      })}
    </div>
  );
}

/** A big stat. Numbers stay proportional; labels and notes are secondary. */
export function Stat({ label, value, note, color }: { label: ReactNode; value: ReactNode; note?: ReactNode; color?: string }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
      <span className="section-label">{label}</span>
      <span className="num" style={{ fontSize: "var(--fs-28)", fontWeight: 600, color, fontVariantNumeric: "tabular-nums" }}>{value}</span>
      {note ? <span className="small muted" style={{ fontVariantNumeric: "tabular-nums" }}>{note}</span> : null}
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
