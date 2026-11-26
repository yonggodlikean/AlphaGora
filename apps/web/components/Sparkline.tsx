/** A team's recent return path (any unit; 0 = where it started). A dashed zero line appears when the path
 *  crosses it, so above/below the starting point reads at a glance. */
export function Sparkline({ values, color, dashed }: { values: number[]; color: string; dashed?: boolean }) {
  const lo = Math.min(...values);
  const hi = Math.max(...values);
  const span = hi - lo || 1;
  const y = (v: number) => 30 - ((v - lo) / span) * 28;
  const pts = values.map((v, i) => `${((i / Math.max(1, values.length - 1)) * 120).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
  return (
    <svg viewBox="0 0 120 32" preserveAspectRatio="none" width={120} height={32} aria-hidden="true">
      {lo < 0 && hi > 0 ? <line x1={0} x2={120} y1={y(0)} y2={y(0)} stroke="#2e323c" strokeDasharray="2 3" /> : null}
      <polyline points={pts} fill="none" stroke={color} strokeWidth={1.6} strokeDasharray={dashed ? "4 3" : undefined} strokeLinejoin="round" />
    </svg>
  );
}
