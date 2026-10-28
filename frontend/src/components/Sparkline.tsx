export function Sparkline({ points, color = "var(--accent)", max, height = 36 }: { points: number[]; color?: string; max?: number; height?: number }) {
  if (points.length < 2) return <div className="small muted">мало данных</div>;
  const w = 200;
  const lo = Math.min(...points);
  const hi = max ?? Math.max(...points);
  const d = points
    .map((v, i) => {
      const x = (i / (points.length - 1)) * w;
      const y = hi === lo ? height / 2 : height - ((v - lo) / (hi - lo)) * (height - 4) - 2;
      return `${i === 0 ? "M" : "L"}${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");
  return (
    <svg className="sparkline" style={{ height }} viewBox={`0 0 ${w} ${height}`} preserveAspectRatio="none">
      <path d={d} fill="none" stroke={color} strokeWidth={2} />
    </svg>
  );
}

export function UptimeBar({ samples }: { samples: { ok: boolean }[] }) {
  if (!samples.length) return <div className="small muted">проверок ещё не было</div>;
  return (
    <div style={{ display: "flex", gap: 1, height: 14 }}>
      {samples.map((s, i) => (
        <div key={i} style={{ flex: 1, background: s.ok ? "var(--ok)" : "var(--danger)", borderRadius: 2, opacity: 0.85 }} />
      ))}
    </div>
  );
}
