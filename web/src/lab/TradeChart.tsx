import { useEffect, useMemo, useState } from "react";
import { pct, price } from "../funnel/format";
import { hms } from "./Pieces";
import { LineMove, TradeChartData } from "./types";

/** Where the three lines stood during one bar, read from the line moves up to that minute. */
interface Standing {
  base: number | null;
  stop: number | null;
  profit_line: number | null;
  next_step: number | null;
  armed: boolean;
  steps: number;
  move: LineMove | null; // the move that happened inside this bar, if any
  phase: "before" | "held" | "after";
}

const EVENT_WORDS: Record<string, string> = {
  opened: "Opened: lines drawn from the fill",
  base: "One step up: max loss line moved to the base",
  ratchet: "Another step up: min profit line raised",
  exit: "Closed",
};

function standings(chart: TradeChartData): Standing[] {
  const sign = chart.direction === "long" ? 1 : -1;
  const moves = chart.lines;
  let k = -1; // the latest move applied
  let exited = false;
  return chart.bars.map((b) => {
    const barEnd = Date.parse(b.ts) + 60000;
    let move: LineMove | null = null;
    while (k + 1 < moves.length && Date.parse(moves[k + 1].at) < barEnd) {
      k += 1;
      move = moves[k];
      if (move.event === "exit") exited = true;
    }
    const m = k >= 0 ? moves[k] : null;
    if (!m) return { base: null, stop: null, profit_line: null, next_step: null, armed: false, steps: 0, move, phase: "before" };
    const wasExit = exited && !(move && move.event === "exit");
    const step = Number(m.step_pct ?? chart.call.step_pct ?? 0);
    const cleared = m.armed ? m.steps + 1 : 0;
    const next = m.base !== null && step ? m.base * (1 + (sign * (cleared + 1) * step) / 100) : null;
    return {
      base: m.base,
      stop: m.stop,
      profit_line: m.profit_line,
      next_step: next,
      armed: m.armed,
      steps: m.steps,
      move,
      phase: wasExit ? "after" : "held",
    };
  });
}

function stateWords(s: Standing, direction: string): string {
  if (s.phase === "before") return "Not yet filled.";
  if (s.phase === "after") return "Closed.";
  if (!s.armed) return `Waiting for the price to move one step in the trade's favour. The max loss line is ${direction === "long" ? "below" : "above"} the base.`;
  if (s.steps === 0) return "One step up: the max loss line now sits on the base, so the worst case is zero. One more step and the min profit line starts rising; it closes if the price comes back to the base first.";
  return `Min profit line raised ${s.steps}× (one step below the best price). It closes if the price comes back to the line.`;
}

/** Candles, the three lines as they moved, and a time runner that reads every value at any minute. */
export function TradeChart({ chart }: { chart: TradeChartData }) {
  const bars = chart.bars;
  const rows = useMemo(() => standings(chart), [chart]);
  const [at, setAt] = useState<number | null>(null); // null: follow the newest bar
  const [playing, setPlaying] = useState(false);
  const [hover, setHover] = useState<{ i: number; px: number; py: number } | null>(null);
  const idx = at === null ? bars.length - 1 : Math.min(at, bars.length - 1);

  useEffect(() => {
    if (!playing) return;
    const id = window.setInterval(() => {
      setAt((cur) => {
        const n = (cur === null ? -1 : cur) + 1;
        if (n >= bars.length - 1) {
          setPlaying(false);
          return null;
        }
        return n;
      });
    }, 140);
    return () => window.clearInterval(id);
  }, [playing, bars.length]);

  if (bars.length < 2) return <div className="tc-empty">No minute bars are stored for {chart.symbol} around this position yet.</div>;

  // ---- scales ----
  const W = 960;
  const H = 440;
  const padL = 10;
  const padR = 92;
  const padT = 16;
  const volH = 46;
  const padB = 26 + volH;
  const values: number[] = [];
  for (const b of bars) values.push(b.low, b.high);
  for (const s of rows) for (const v of [s.base, s.stop, s.profit_line, s.next_step]) if (v !== null) values.push(v);
  let lo = Math.min(...values);
  let hi = Math.max(...values);
  const span = hi - lo || lo * 0.01 || 1;
  lo -= span * 0.08;
  hi += span * 0.08;
  const slot = (W - padL - padR) / bars.length;
  const x = (i: number) => padL + (i + 0.5) * slot;
  const y = (v: number) => padT + ((hi - v) / (hi - lo)) * (H - padT - padB);
  const maxVol = Math.max(1, ...bars.map((b) => b.volume));
  const vy = (v: number) => H - 26 - (v / maxVol) * (volH - 6);
  const candleW = Math.max(1.5, Math.min(9, slot * 0.66));

  // a stepped path for one line: horizontal across each bar it stood at that value. The part up
  // to the time runner is drawn solid, so the lines take their new values as time moves; the
  // part beyond it is drawn faint.
  const stepPath = (pick: (s: Standing) => number | null, from: number, to: number): string => {
    let d = "";
    let prev: number | null = null;
    rows.forEach((s, i) => {
      const v = s.phase === "held" && i >= from && i <= to ? pick(s) : null;
      if (v === null) {
        prev = null;
        return;
      }
      const x0 = padL + i * slot;
      const x1 = x0 + slot;
      if (prev === null) d += `M${x0.toFixed(1)},${y(v).toFixed(1)} `;
      else if (prev !== v) d += `V${y(v).toFixed(1)} `;
      d += `H${x1.toFixed(1)} `;
      prev = v;
    });
    return d;
  };
  const zones: { x0: number; x1: number; a: number; b: number; cls: string }[] = [];
  rows.forEach((s, i) => {
    if (s.phase !== "held" || s.base === null || s.stop === null || s.profit_line === null) return;
    const x0 = padL + i * slot;
    const x1 = x0 + slot;
    if (s.stop !== s.base) zones.push({ x0, x1, a: s.stop, b: s.base, cls: "zone-loss" });
    zones.push({ x0, x1, a: s.base, b: s.profit_line, cls: "zone-profit" });
  });

  const ticks: number[] = [];
  const every = Math.max(1, Math.round(bars.length / 8));
  for (let i = 0; i < bars.length; i += every) ticks.push(i);
  const moves = rows.map((s, i) => ({ s, i })).filter(({ s }) => s.move);
  const cur = rows[idx];
  const bar = bars[idx];
  const sign = chart.direction === "long" ? 1 : -1;
  const gain = cur.base ? ((bar.close - cur.base) / cur.base) * 100 * sign : null;
  const lineVals = [
    { key: "stop", label: cur.armed ? "max loss line (on the base)" : "max loss line", v: cur.stop, cls: "bear" },
    { key: "base", label: "base (the fill)", v: cur.base, cls: "base" },
    { key: "line", label: cur.armed ? `min profit line${cur.steps ? ` (raised ${cur.steps}×)` : " (on the base)"}` : "first step (arms the lines)", v: cur.profit_line, cls: "bull" },
    { key: "next", label: "next step", v: cur.next_step, cls: "faint" },
  ];
  const label = (iso: string) => iso.slice(11, 16);

  const locate = (e: React.MouseEvent<SVGSVGElement>) => {
    const r = e.currentTarget.getBoundingClientRect();
    const px = ((e.clientX - r.left) / r.width) * W;
    const py = ((e.clientY - r.top) / r.height) * H;
    const i = Math.max(0, Math.min(bars.length - 1, Math.floor((px - padL) / slot)));
    return { i, px, py };
  };
  const onMove = (e: React.MouseEvent<SVGSVGElement>) => setHover(locate(e));
  const onClick = (e: React.MouseEvent<SVGSVGElement>) => {
    setAt(locate(e).i);
    setPlaying(false);
  };
  const priceAt = (py: number) => hi - ((py - padT) / (H - padT - padB)) * (hi - lo);
  const hb = hover ? bars[hover.i] : null;
  const hs = hover ? rows[hover.i] : null;
  const tipLeft = hover ? hover.px > W * 0.62 : false;

  return (
    <div className="tc">
      <svg viewBox={`0 0 ${W} ${H}`} className="tc-svg" onMouseMove={onMove} onMouseLeave={() => setHover(null)} onClick={onClick} role="img" aria-label={`${chart.symbol} ${chart.direction}: price and lines, minute by minute`}>
        {zones.map((z, k) => (
          <rect key={k} x={z.x0} y={Math.min(y(z.a), y(z.b))} width={z.x1 - z.x0} height={Math.abs(y(z.a) - y(z.b))} className={z.cls} />
        ))}
        {ticks.map((i) => (
          <g key={i} className="tick">
            <line x1={x(i)} x2={x(i)} y1={padT} y2={H - padB + 4} />
            <text x={x(i)} y={H - 8}>
              {label(bars[i].ts)}
            </text>
          </g>
        ))}
        {bars.map((b, i) => {
          const up = b.close >= b.open;
          const top = y(Math.max(b.open, b.close));
          const bottom = y(Math.min(b.open, b.close));
          return (
            <g key={b.ts} className={`candle ${up ? "up" : "down"}${i > idx ? " future" : ""}`}>
              <line x1={x(i)} x2={x(i)} y1={y(b.high)} y2={y(b.low)} />
              <rect x={x(i) - candleW / 2} y={top} width={candleW} height={Math.max(1, bottom - top)} />
              <rect className="vol" x={x(i) - candleW / 2} y={vy(b.volume)} width={candleW} height={H - 26 - vy(b.volume)} />
            </g>
          );
        })}
        <g className="ahead">
          <path d={stepPath((s) => s.base, idx + 1, bars.length)} className="ln base" />
          <path d={stepPath((s) => s.stop, idx + 1, bars.length)} className="ln stop" />
          <path d={stepPath((s) => s.profit_line, idx + 1, bars.length)} className="ln line" />
        </g>
        <path d={stepPath((s) => s.next_step, 0, idx)} className="ln next" />
        <path d={stepPath((s) => s.base, 0, idx)} className="ln base" />
        <path d={stepPath((s) => s.stop, 0, idx)} className="ln stop" />
        <path d={stepPath((s) => s.profit_line, 0, idx)} className="ln line" />
        {moves.map(({ s, i }) => {
          const m = s.move!;
          const v = m.event === "exit" ? (m.last ?? m.stop) : m.event === "opened" ? m.base : m.event === "base" ? m.stop : m.profit_line;
          if (v === null) return null;
          return (
            <g key={m.at} className={`mark ${m.event}${i > idx ? " ahead" : ""}`}>
              <circle cx={x(i)} cy={y(v)} r={5} />
              <text x={x(i)} y={y(v) - 9}>
                {{ opened: "fill", base: "stop → base", ratchet: `line +${m.steps}`, exit: "exit" }[m.event] ?? m.event}
              </text>
            </g>
          );
        })}
        {lineVals.map((l) =>
          l.v === null ? null : (
            <text key={l.key} x={W - padR + 6} y={y(l.v) + 4} className={`axis ${l.cls}`}>
              {price(l.v)}
            </text>
          ),
        )}
        <g className="cursor">
          <line x1={x(idx)} x2={x(idx)} y1={padT} y2={H - padB} />
          <text x={x(idx)} y={padT - 4}>
            {hms(bar.ts)}
          </text>
        </g>
        {hover && hb && hs && hover.py >= padT && hover.py <= H - padB && (
          <g className="xhair" pointerEvents="none">
            <line x1={x(hover.i)} x2={x(hover.i)} y1={padT} y2={H - padB} />
            <line x1={padL} x2={W - padR} y1={hover.py} y2={hover.py} />
            <rect x={x(hover.i) - 24} y={H - padB + 2} width={48} height={15} rx={3} className="tag" />
            <text x={x(hover.i)} y={H - padB + 13} className="tag-text">
              {hb.ts.slice(11, 16)}
            </text>
            <rect x={W - padR + 2} y={hover.py - 8} width={padR - 4} height={16} rx={3} className="tag" />
            <text x={W - padR + 6} y={hover.py + 4} className="tag-text left">
              {price(priceAt(hover.py))}
            </text>
            <g transform={`translate(${tipLeft ? hover.px - 214 : hover.px + 14}, ${Math.min(Math.max(padT, hover.py - 40), H - padB - 96)})`} className="tip">
              <rect width={200} height={hs.phase === "held" ? 92 : 48} rx={6} />
              <text x={8} y={16} className="t1">
                {hb.ts.slice(0, 10)} {hb.ts.slice(11, 16)} UTC
              </text>
              <text x={8} y={32} className="t2">
                O {price(hb.open)} H {price(hb.high)} L {price(hb.low)} C {price(hb.close)}
              </text>
              {hs.phase === "held" && (
                <>
                  <text x={8} y={50} className="t2 bear">
                    max loss {price(hs.stop)}
                    {hs.armed ? " (on the base)" : ""}
                  </text>
                  <text x={8} y={66} className="t2 base">
                    base {price(hs.base)}
                  </text>
                  <text x={8} y={82} className="t2 bull">
                    min profit {price(hs.profit_line)}
                    {hs.steps ? ` (raised ${hs.steps}×)` : ""}
                  </text>
                </>
              )}
            </g>
          </g>
        )}
      </svg>

      <div className="tc-runner">
        <button type="button" className={`play${playing ? " on" : ""}`} onClick={() => setPlaying((p) => !p)} aria-label={playing ? "pause" : "play"}>
          <span className="in">{playing ? "❚❚" : "▶"}</span>
        </button>
        <input type="range" min={0} max={bars.length - 1} value={idx} onChange={(e) => { setAt(Number(e.target.value)); setPlaying(false); }} aria-label="Time runner" />
        <button type="button" className={`follow${at === null ? " on" : ""}`} onClick={() => { setAt(null); setPlaying(false); }}>
          <span className="in">{chart.open ? "now" : "end"}</span>
        </button>
      </div>

      <div className="tc-read">
        <div className="tc-time">
          <b>{bar.ts.slice(0, 10)} {hms(bar.ts)} UTC</b>
          <span className="mono">
            O {price(bar.open)} H {price(bar.high)} L {price(bar.low)} C {price(bar.close)}
          </span>
          {gain !== null && cur.phase === "held" && <span className={gain >= 0 ? "bull" : "bear"}>{pct(gain)} from the base</span>}
        </div>
        <dl className="tc-lines">
          {lineVals
            .filter((l) => l.key !== "next" || l.v !== null)
            .map((l) => (
              <div key={l.key} className={l.cls}>
                <dt>{l.label}</dt>
                <dd>{l.v === null ? "—" : price(l.v)}</dd>
              </div>
            ))}
        </dl>
        <p className="tc-state">{cur.move ? <b>{EVENT_WORDS[cur.move.event] ?? cur.move.event}. </b> : null}{stateWords(cur, chart.direction)}</p>
      </div>

      <ol className="tc-moves">
        {chart.lines.map((m) => (
          <li key={m.at} className={m.event}>
            <button type="button" onClick={() => { const i = rows.findIndex((s) => s.move === m); if (i >= 0) { setAt(i); setPlaying(false); } }}>
              <span className="in">
                <time>{hms(m.at)}</time>
                <b>{EVENT_WORDS[m.event] ?? m.event}</b>
                <span className="mono">
                  max loss {price(m.stop)} · base {price(m.base)} · min profit {price(m.profit_line)} · price {price(m.last)}
                </span>
              </span>
            </button>
          </li>
        ))}
      </ol>
    </div>
  );
}
