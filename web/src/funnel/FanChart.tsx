import { useEffect, useMemo } from "react";
import { hhmm, pct, polyline, squash } from "./format";
import { Funnel, FunnelSymbol } from "./types";
import { useSize } from "./useFunnel";

export interface FanGeometry {
  /** y of each line's end, in px from the top of the fan */
  ends: Record<string, number>;
  zeroY: number;
  height: number;
}

interface Props {
  funnel: Funnel;
  focus: string | null;
  onFocus: (symbol: string | null) => void;
  onPin: (symbol: string) => void;
  onGeometry: (g: FanGeometry) => void;
}

const PAD = { l: 54, r: 10, t: 14, b: 22 };

/** Every watched instrument as one line from a common origin: the day's fan. */
export function FanChart({ funnel, focus, onFocus, onPin, onGeometry }: Props) {
  const { ref, size } = useSize<HTMLDivElement>();
  const { w, h } = size;
  const n = funnel.times.length;

  const model = useMemo(() => {
    if (!w || !h || n < 2) return null;
    let lo = -0.6;
    let hi = 0.6;
    for (const s of funnel.symbols) for (const v of s.path) if (v !== null) (lo = Math.min(lo, v)), (hi = Math.max(hi, v));
    const top = squash(hi) * 1.06;
    const bottom = squash(lo) * 1.06;
    const x = (i: number) => PAD.l + (i / (n - 1)) * (w - PAD.l - PAD.r);
    const y = (v: number) => PAD.t + ((top - squash(v)) / (top - bottom)) * (h - PAD.t - PAD.b);
    const lines = funnel.symbols.map((s) => {
      const pts: [number, number][] = [];
      s.path.forEach((v, i) => {
        if (v !== null) pts.push([x(i), y(v)]);
      });
      return { s, pts, end: pts.length ? pts[pts.length - 1][1] : y(0) };
    });
    const ticks = [hi, hi / 2, lo / 2, lo].filter((v) => Math.abs(v) >= 0.25).map((v) => ({ v, y: y(v) }));
    // the body of the fan: the highest and the lowest line at every step
    const upper: [number, number][] = [];
    const lower: [number, number][] = [];
    for (let i = 0; i < n; i++) {
      let mx = 0;
      let mn = 0;
      for (const s of funnel.symbols) {
        const v = s.path[i];
        if (v !== null) (mx = Math.max(mx, v)), (mn = Math.min(mn, v));
      }
      upper.push([x(i), y(mx)]);
      lower.push([x(i), y(mn)]);
    }
    return { x, y, lines, zeroY: y(0), ticks, upper, lower };
  }, [funnel, w, h, n]);

  useEffect(() => {
    if (!model) return;
    const ends: Record<string, number> = {};
    for (const l of model.lines) ends[l.s.symbol] = l.end;
    onGeometry({ ends, zeroY: model.zeroY, height: h });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [model, h]);

  const nearest = (clientX: number, clientY: number, rect: DOMRect): FunnelSymbol | null => {
    if (!model) return null;
    const px = clientX - rect.left;
    const py = clientY - rect.top;
    const i = Math.max(0, Math.min(n - 1, Math.round(((px - PAD.l) / (w - PAD.l - PAD.r)) * (n - 1))));
    let best: FunnelSymbol | null = null;
    let dist = 16;
    for (const l of model.lines) {
      let v: number | null = null;
      for (let k = i; k >= 0 && v === null; k--) v = l.s.path[k];
      if (v === null) continue;
      const d = Math.abs(model.y(v) - py);
      if (d < dist) (dist = d), (best = l.s);
    }
    return best;
  };

  const bulls = funnel.symbols.filter((s) => s.side === "bull").sort((a, b) => a.rank - b.rank).slice(0, 7);
  const bears = funnel.symbols.filter((s) => s.side === "bear").sort((a, b) => a.rank - b.rank).slice(0, 7);
  const mid = Math.floor((n - 1) / 2);

  return (
    <div
      className="fan"
      ref={ref}
      onMouseMove={(e) => onFocus(nearest(e.clientX, e.clientY, e.currentTarget.getBoundingClientRect())?.symbol ?? null)}
      onMouseLeave={() => onFocus(null)}
      onClick={(e) => {
        const s = nearest(e.clientX, e.clientY, e.currentTarget.getBoundingClientRect());
        if (s) onPin(s.symbol);
      }}
    >
      {model && (
        <svg width={w} height={h} role="img" aria-label={`${funnel.symbols.length} instruments since the open, ${funnel.breadth.bull} up and ${funnel.breadth.bear} down`}>
          <defs>
            <linearGradient id="fan-up" x1="0" y1="1" x2="0" y2="0">
              <stop offset="0" stopColor="var(--bull)" stopOpacity="0.05" />
              <stop offset="1" stopColor="var(--bull)" stopOpacity="0.9" />
            </linearGradient>
            <linearGradient id="fan-down" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0" stopColor="var(--bear)" stopOpacity="0.05" />
              <stop offset="1" stopColor="var(--bear)" stopOpacity="0.9" />
            </linearGradient>
          </defs>
          {/* the scale rail on the left: green above the prior close, red below */}
          <rect x={PAD.l - 46} y={PAD.t} width={3} height={Math.max(0, model.zeroY - PAD.t)} fill="url(#fan-up)" rx={1.5} />
          <rect x={PAD.l - 46} y={model.zeroY} width={3} height={Math.max(0, h - PAD.b - model.zeroY)} fill="url(#fan-down)" rx={1.5} />
          {model.ticks.map((t) => (
            <g key={t.v}>
              <line x1={PAD.l - 40} x2={w - PAD.r} y1={t.y} y2={t.y} className="fan-grid" />
              <text x={PAD.l - 38} y={t.y - 4} className="fan-tick">
                {pct(t.v, 1)}
              </text>
            </g>
          ))}
          <line x1={PAD.l - 46} x2={w - PAD.r} y1={model.zeroY} y2={model.zeroY} className="fan-zero" />
          {/* the body of the fan: everything between the prior close and the day's extremes */}
          <polygon className="fan-area bull" points={`${polyline(model.upper)} ${(w - PAD.r).toFixed(1)},${model.zeroY.toFixed(1)} ${PAD.l},${model.zeroY.toFixed(1)}`} />
          <polygon className="fan-area bear" points={`${polyline(model.lower)} ${(w - PAD.r).toFixed(1)},${model.zeroY.toFixed(1)} ${PAD.l},${model.zeroY.toFixed(1)}`} />
          {model.lines.map(({ s, pts }) => {
            const on = focus === s.symbol;
            const dim = focus !== null && !on;
            const up = (s.day_pct ?? 0) >= 0;
            return (
              <g key={s.symbol} className={`fan-line ${up ? "bull" : "bear"}${on ? " on" : ""}${dim ? " dim" : ""}${s.held ? " held" : ""}`}>
                <polyline points={polyline(pts)} />
                {pts.length > 0 && <circle cx={pts[pts.length - 1][0]} cy={pts[pts.length - 1][1]} r={on ? 4 : s.held ? 3 : 2} />}
              </g>
            );
          })}
          <circle cx={PAD.l} cy={model.zeroY} r={3.5} className="fan-origin" />
          {model.lines
            .filter((l) => l.s.symbol === focus && l.pts.length > 0)
            .map((l) => {
              const [ex, ey] = l.pts[l.pts.length - 1];
              const label = `${l.s.symbol} ${pct(l.s.day_pct)}`;
              const tw = label.length * 6.6 + 12;
              const ty = Math.max(PAD.t + 10, Math.min(h - PAD.b - 10, ey - 14));
              return (
                <g key="tip" className={`fan-tip ${(l.s.day_pct ?? 0) >= 0 ? "bull" : "bear"}`}>
                  <rect x={ex - tw - 8} y={ty - 9} width={tw} height={18} rx={4} />
                  <text x={ex - 14} y={ty + 4} textAnchor="end">
                    {label}
                  </text>
                </g>
              );
            })}
          {/* the neck: where the day's lines are read off and ranked */}
          <line x1={w - PAD.r} x2={w - PAD.r} y1={PAD.t} y2={h - PAD.b} className="fan-neck" />
          {[0, mid, n - 1].map((i) => (
            <text key={i} x={model.x(i)} y={h - 6} className="fan-time" textAnchor={i === 0 ? "start" : i === n - 1 ? "end" : "middle"}>
              {i === 0 ? "prior close" : hhmm(funnel.times[i])}
            </text>
          ))}
        </svg>
      )}
      <ol className="fan-leaders bull" aria-label="Strongest today">
        {bulls.map((s) => (
          <li key={s.symbol} className={focus === s.symbol ? "on" : ""}>
            <span>{s.symbol}</span>
            <b>{pct(s.day_pct)}</b>
          </li>
        ))}
      </ol>
      <ol className="fan-leaders bear" aria-label="Weakest today">
        {bears.map((s) => (
          <li key={s.symbol} className={focus === s.symbol ? "on" : ""}>
            <span>{s.symbol}</span>
            <b>{pct(s.day_pct)}</b>
          </li>
        ))}
      </ol>
    </div>
  );
}
