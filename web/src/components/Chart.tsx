import { useEffect, useState } from "react";
import { api, Bar } from "../api";
import { money } from "../format";

export interface ChartSpec {
  symbol: string;
  interval?: "1d" | "1m";
  n?: number;
  title?: string;
  levels?: Record<string, number>;
}

const LEVEL_CLASS: Record<string, string> = { stop: "lvl-stop", target: "lvl-target", entry: "lvl-entry" };

/** A price line with horizontal thesis levels, drawn to scale from the api's bars. */
export function Chart({ spec }: { spec: ChartSpec }) {
  const [bars, setBars] = useState<Bar[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const interval = spec.interval ?? "1d";
  const n = Math.min(spec.n ?? (interval === "1d" ? 40 : 390), 2000);

  useEffect(() => {
    let alive = true;
    setBars(null);
    api
      .get<Bar[]>(`/api/bars?symbol=${encodeURIComponent(spec.symbol)}&interval=${interval}&n=${n}`)
      .then((b) => alive && setBars(b))
      .catch((e) => alive && setError(String(e.message ?? e)));
    return () => {
      alive = false;
    };
  }, [spec.symbol, interval, n]);

  if (error) return <div className="chart-note">chart unavailable: {error}</div>;
  if (!bars) return <div className="chart-note">loading {spec.symbol}…</div>;
  if (bars.length < 2) return <div className="chart-note">no {interval} bars stored for {spec.symbol}</div>;

  const W = 640;
  const H = 260;
  const padL = 12;
  const padR = 96;
  const padT = 18;
  const padB = 28;
  const levels = Object.entries(spec.levels ?? {}).filter(([, v]) => typeof v === "number");
  const values = bars.map((b) => b.close).concat(levels.map(([, v]) => v));
  let lo = Math.min(...values);
  let hi = Math.max(...values);
  const span = hi - lo || 1;
  lo -= span * 0.06;
  hi += span * 0.06;
  const x = (i: number) => padL + (i / (bars.length - 1)) * (W - padL - padR);
  const y = (v: number) => H - padB - ((v - lo) / (hi - lo)) * (H - padT - padB);
  const path = bars.map((b, i) => `${i === 0 ? "M" : "L"}${x(i).toFixed(1)},${y(b.close).toFixed(1)}`).join(" ");
  const last = bars[bars.length - 1];
  const first = bars[0];
  const label = (iso: string) =>
    interval === "1d" ? iso.slice(5, 10) : new Date(iso).toISOString().slice(11, 16);
  const ticks = [0, Math.floor((bars.length - 1) / 2), bars.length - 1];

  return (
    <figure className="chart">
      <figcaption>
        <b>{spec.symbol}</b> {spec.title ?? (interval === "1d" ? `last ${bars.length} days` : "minute bars")}
        <span className="muted"> · last {money(last.close)}</span>
      </figcaption>
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={`${spec.symbol} price chart`}>
        <rect x={padL} y={padT} width={W - padL - padR} height={H - padT - padB} className="chart-bg" />
        {levels.map(([name, v]) => (
          <g key={name} className={`level ${LEVEL_CLASS[name] ?? "lvl-other"}`}>
            <line x1={padL} x2={W - padR} y1={y(v)} y2={y(v)} />
            <text x={W - padR + 6} y={y(v) + 4}>
              {name} {money(v)}
            </text>
          </g>
        ))}
        <path d={path} className="price" />
        <circle cx={x(bars.length - 1)} cy={y(last.close)} r={4} className="price-dot" />
        {ticks.map((i) => (
          <text key={i} x={x(i)} y={H - 8} textAnchor={i === 0 ? "start" : i === bars.length - 1 ? "end" : "middle"} className="tick">
            {label(bars[i].ts)}
          </text>
        ))}
        <text x={padL + 4} y={padT + 12} className="tick">
          {money(first.close)} → {money(last.close)}
        </text>
      </svg>
    </figure>
  );
}
