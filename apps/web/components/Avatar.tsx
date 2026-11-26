/** Team avatar. Weight stays <=600 and colours come from tokens, never hardcoded hex. */
export function Avatar({ glyph, color, size = 96 }: { glyph: string; color: string; size?: number }) {
  return (
    <div aria-hidden="true" style={{ flex: "0 0 auto", width: size, height: size, borderRadius: size / 4.8, border: `2px solid ${color}`,
      background: "var(--panel-2)", display: "flex", alignItems: "center", justifyContent: "center", fontWeight: 600,
      fontSize: size * 0.36, color }}>
      {glyph}
    </div>
  );
}
