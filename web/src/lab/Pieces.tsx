import { pct, price } from "../funnel/format";
import { Call, Counts, Outcome, OUTCOME, Story } from "./types";

export const hms = (iso: string | null | undefined): string => (iso ? iso.slice(11, 19) : "");
export const hm = (iso: string | null | undefined): string => (iso ? iso.slice(11, 16) : "");

export function ago(iso: string | null | undefined, now: number): string {
  if (!iso) return "never";
  const s = Math.max(0, Math.round((now - Date.parse(iso)) / 1000));
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m ago`;
}

/** The predictor's confidence against the bar it must clear to trade. */
export function Confidence({ value, bar = 0.6, wide = false }: { value: number; bar?: number; wide?: boolean }) {
  const clears = value >= bar;
  return (
    <span className={`conf${wide ? " wide" : ""}${clears ? " clears" : ""}`} role="img" aria-label={`confidence ${value.toFixed(2)}, the bar is ${bar.toFixed(2)}`}>
      <span className="conf-track">
        <i style={{ width: `${Math.min(100, value * 100)}%` }} />
        <b style={{ left: `${bar * 100}%` }} title={`the bar to trade: ${bar.toFixed(2)}`} />
      </span>
      <em>{value.toFixed(2)}</em>
    </span>
  );
}

export function Direction({ call }: { call: Call }) {
  const d = call.direction;
  return <span className={`dir ${d}`}>{d === "none" ? "NO CALL" : d.toUpperCase()}</span>;
}

/** Where a call ended, in one word; for an untraded call, also how it did on paper. */
export function OutcomePill({ s }: { s: Story }) {
  const o = OUTCOME[s.outcome];
  const real = s.score?.realized_pct;
  return (
    <span className="outcome-wrap">
      <span className={`pill ${o.tone}`} title={o.what}>
        {o.word}
        {(s.outcome === "right" || s.outcome === "wrong") && typeof real === "number" ? ` ${pct(real)}` : ""}
      </span>
      {s.paper && s.paper.result !== "pending" && (
        <span className={`pill ghost ${s.paper.result === "right" ? "bull" : "bear"}`} title="Not traded. The same lines run over the real prices that followed.">
          on paper {s.paper.result} {pct(s.paper.realized_pct)}
        </span>
      )}
      {s.paper && s.paper.result === "pending" && (
        <span className="pill ghost mute" title="Not traded. Still running on paper against the real prices.">
          on paper {pct(s.paper.mark_pct)} so far
        </span>
      )}
      {s.outcome === "passed" && s.after && Math.max(s.after.up_pct, -s.after.down_pct) >= 0.5 && (
        <span className="pill ghost amber" title={`In the next ${s.after.minutes} minutes the price went as high as ${pct(s.after.up_pct)} and as low as ${pct(s.after.down_pct)}.`}>
          then moved {pct(Math.abs(s.after.up_pct) >= Math.abs(s.after.down_pct) ? s.after.up_pct : s.after.down_pct)}
        </span>
      )}
    </span>
  );
}

/** The call's lines drawn to scale around the entry: maximum loss below (above for a short),
 * the first profit line, and the next two steps. */
export function LineLadder({ call, entry }: { call: Call; entry: number | null }) {
  if (call.direction === "none" || !entry) return null;
  const sign = call.direction === "long" ? 1 : -1;
  const at = (p: number) => entry * (1 + (sign * p) / 100);
  const rows = [
    { key: "s2", label: "second step", p: call.first_profit_pct + 2 * call.step_pct, tone: "bull faint" },
    { key: "s1", label: "first step", p: call.first_profit_pct + call.step_pct, tone: "bull faint" },
    { key: "fp", label: "MIN PROFIT LINE", p: call.first_profit_pct, tone: "bull" },
    { key: "base", label: "BASE (the fill)", p: 0, tone: "base" },
    { key: "ml", label: "MAX LOSS LINE", p: -call.max_loss_pct, tone: "bear" },
  ];
  const top = call.first_profit_pct + 2 * call.step_pct;
  const span = top + call.max_loss_pct;
  const H = 168;
  const y = (p: number) => 12 + ((top - p) / span) * (H - 24);
  return (
    <div className="ladder" role="img" aria-label={`Lines for a ${call.direction}: base ${price(entry)}, maximum loss ${price(at(-call.max_loss_pct))}, first profit line ${price(at(call.first_profit_pct))}`}>
      <svg viewBox={`0 0 420 ${H}`} preserveAspectRatio="none">
        <rect x="0" y={y(top)} width="420" height={y(0) - y(top)} className="zone-profit" />
        <rect x="0" y={y(0)} width="420" height={y(-call.max_loss_pct) - y(0)} className="zone-loss" />
        {rows.map((r) => (
          <line key={r.key} x1="0" x2="420" y1={y(r.p)} y2={y(r.p)} className={`lvl ${r.tone}`} />
        ))}
      </svg>
      {rows.map((r) => (
        <div key={r.key} className={`rung ${r.tone}`} style={{ top: `${(y(r.p) / H) * 100}%` }}>
          <span>{r.label}</span>
          <b>{price(at(r.p))}</b>
          <em>{r.p === 0 ? "entry" : `${r.p > 0 ? "+" : "−"}${Math.abs(r.p).toFixed(2)}%`}</em>
        </div>
      ))}
      <p className="ladder-note">
        {call.direction === "short" ? "A short: profit is below the base, loss above it. " : ""}
        When the price reaches the min profit line the stop moves to the base, and every further {call.step_pct}% lifts the line one step. The position is closed when the price comes back to the latest line, at the max loss line, or after {call.horizon_minutes} minutes.
      </p>
    </div>
  );
}

/** Right against wrong as one bar; traded calls solid, calls graded on paper hatched. */
export function RightWrong({ c, height = 10 }: { c: Counts; height?: number }) {
  const parts = [
    { n: c.right, cls: "bull", what: "right, traded" },
    { n: c.paper_right, cls: "bull paper", what: "right on paper" },
    { n: c.paper_wrong, cls: "bear paper", what: "wrong on paper" },
    { n: c.wrong, cls: "bear", what: "wrong, traded" },
  ];
  const total = parts.reduce((a, p) => a + p.n, 0);
  if (!total) return <span className="rw empty" style={{ height }} title="nothing graded yet" />;
  return (
    <span className="rw" style={{ height }} role="img" aria-label={`${c.all_right} right, ${c.all_wrong} wrong`}>
      {parts
        .filter((p) => p.n > 0)
        .map((p) => (
          <i key={p.cls} className={p.cls} style={{ flexGrow: p.n }} title={`${p.n} ${p.what}`} />
        ))}
    </span>
  );
}

export const rate = (v: number | null | undefined): string => (v === null || v === undefined ? "n/a" : `${(v * 100).toFixed(0)}%`);

export function outcomeTone(o: Outcome): string {
  return OUTCOME[o].tone;
}
