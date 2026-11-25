export function Sparkline({ values, color, dashed }: { values: number[]; color: string; dashed?: boolean }) {
  const lo = Math.min(...values);
  const hi = Math.max(...values);
  const span = hi - lo || 1;
  const pts = values.map((v, i) => `${(i / Math.max(1, values.length - 1)) * 120},${30 - ((v - lo) / span) * 28}`).join(" ");
  return (
    <svg viewBox="0 0 120 32" preserveAspectRatio="none" width={120} height={32} aria-hidden="true">
      <polyline points={pts} fill="none" stroke={color} strokeWidth={2} strokeDasharray={dashed ? "4 3" : undefined} strokeLinejoin="round" />
    </svg>
  );
}
