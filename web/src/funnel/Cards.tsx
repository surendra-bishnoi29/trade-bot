import { pct, polyline, price, squash } from "./format";
import { CHECKS, FunnelSymbol } from "./types";

interface CardProps {
  s: FunnelSymbol;
  tier: 1 | 2 | 3 | 4;
  focus: string | null;
  pinned: string | null;
  onFocus: (symbol: string | null) => void;
  onPin: (symbol: string) => void;
  register: (el: HTMLElement | null) => void;
}

/** Volume by price for the session: where today's trading happened, highest price on top. */
export function Profile({ rows, side }: { rows: number[]; side: string }) {
  return (
    <div className={`profile ${side}`} aria-hidden="true">
      {rows.map((v, i) => (
        <i key={i} style={{ width: `${Math.max(5, Math.sqrt(v) * 100)}%` }} />
      ))}
    </div>
  );
}

/** One dot per agent check, lit when that agent's test passed. */
export function CheckDots({ s }: { s: FunnelSymbol }) {
  return (
    <span className="dots" role="img" aria-label={`${s.checks_passed} of 5 agent checks passed`}>
      {CHECKS.map((c) => (
        <i key={c.key} className={s.checks[c.key] ? "on" : ""} title={`${c.label}: ${c.what} · ${s.checks[c.key] ? "passed" : "not passed"}`} />
      ))}
    </span>
  );
}

/** The verdict in one word: how strongly the evidence agrees with the day's direction. */
export function verdictWord(s: FunnelSymbol): string {
  if (s.strength < 25) return "MIXED";
  if (s.side === "bull") return s.strength >= 60 ? "BULL" : "BULLISH";
  return s.strength >= 60 ? "BEAR" : "BEARISH";
}

function state(s: FunnelSymbol, focus: string | null, pinned: string | null): string {
  const on = focus === s.symbol || pinned === s.symbol;
  return `${s.side}${on ? " on" : ""}${focus !== null && !on ? " dim" : ""}${s.held ? " held" : ""}`;
}

/** A funnel card. The tier sets how much it says: the columns grow more detailed left to right. */
export function StageCard({ s, tier, focus, pinned, onFocus, onPin, register }: CardProps) {
  return (
    <button
      type="button"
      ref={register}
      className={`fcard t${tier} ${state(s, focus, pinned)}`}
      onMouseEnter={() => onFocus(s.symbol)}
      onMouseLeave={() => onFocus(null)}
      onFocus={() => onFocus(s.symbol)}
      onBlur={() => onFocus(null)}
      onClick={() => onPin(s.symbol)}
      aria-label={`${s.symbol} ${s.name}, ${pct(s.day_pct)} today, ${s.setup.toLowerCase()}`}
    >
      <span className="in">
        <span className="fhead">
          <b className="sym">{s.symbol}</b>
          <span className="chg">{pct(s.day_pct, tier === 1 ? 1 : 2)}</span>
        </span>
        {tier >= 4 && <span className="name">{s.name}</span>}
        {tier >= 3 && <span className="setup">{s.setup}</span>}
        {tier >= 4 && <CheckDots s={s} />}
        {tier >= 2 && (
          <span className="body">
            <Profile rows={s.profile} side={s.side} />
            {tier >= 3 && <span className="last">{price(s.last)}</span>}
          </span>
        )}
        {tier === 1 && (
          <span className="meter" aria-hidden="true">
            <i style={{ width: `${Math.max(6, Math.min(100, s.strength))}%` }} />
          </span>
        )}
      </span>
    </button>
  );
}

export function Ghost() {
  return <span className="fcard ghost" aria-hidden="true" />;
}

/** The day's line with the levels the desk watches. */
export function MiniChart({ s }: { s: FunnelSymbol }) {
  const W = 220;
  const H = 74;
  const vals = s.path.filter((v): v is number => v !== null);
  const L = s.levels;
  const asPct = (p: number | null | undefined) => (p ? (p / L.prior_close - 1) * 100 : null);
  const marks = [
    { k: "R2", v: asPct(L.target ?? L.r2), cls: "res" },
    { k: "R1", v: asPct(L.r1), cls: "res" },
    { k: "S1", v: asPct(L.stop ?? L.s1), cls: "sup" },
  ].filter((m): m is { k: string; v: number; cls: string } => m.v !== null);
  let lo = Math.min(0, ...vals);
  let hi = Math.max(0, ...vals);
  for (const m of marks) if (Math.abs(m.v) < 12) (lo = Math.min(lo, m.v)), (hi = Math.max(hi, m.v));
  const top = squash(hi) + 0.15;
  const bottom = squash(lo) - 0.15;
  const y = (v: number) => 4 + ((top - squash(v)) / (top - bottom)) * (H - 8);
  const n = s.path.length;
  const pts: [number, number][] = [];
  s.path.forEach((v, i) => {
    if (v !== null) pts.push([4 + (i / Math.max(1, n - 1)) * (W - 40), y(v)]);
  });
  return (
    <svg className={`mini ${s.side}`} viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" role="img" aria-label={`${s.symbol} today with support and resistance`}>
      <line x1={0} x2={W - 34} y1={y(0)} y2={y(0)} className="mini-zero" />
      {marks
        .filter((m) => Math.abs(m.v) < 12)
        .map((m) => (
          <g key={m.k} className={`mini-level ${m.cls}`}>
            <line x1={0} x2={W - 34} y1={y(m.v)} y2={y(m.v)} />
            <text x={W - 30} y={y(m.v) + 3}>
              {m.k}
            </text>
          </g>
        ))}
      <polyline points={polyline(pts)} />
      {pts.length > 0 && <circle cx={pts[pts.length - 1][0]} cy={pts[pts.length - 1][1]} r={2.6} />}
    </svg>
  );
}

/** Where the last price sits between support and resistance (or stop and target when held). */
export function LevelBar({ s }: { s: FunnelSymbol }) {
  const L = s.levels;
  const lo = L.stop ?? L.s1 ?? L.last;
  const hi = L.target ?? L.r2 ?? L.r1 ?? L.last;
  const span = hi - lo || 1;
  const at = (v: number) => `${Math.max(0, Math.min(100, ((v - lo) / span) * 100))}%`;
  return (
    <span className="levelbar" aria-hidden="true">
      <i className="zone" style={{ left: at(L.zone[0]), width: `calc(${at(L.zone[1])} - ${at(L.zone[0])})` }} />
      {L.entry ? <i className="entry" style={{ left: at(L.entry) }} /> : null}
      <i className="now" style={{ left: at(L.last) }} />
    </span>
  );
}

/** A final pick: the verdict, the day's chart, the levels and which book it belongs to. */
export function PickCard({ s, focus, pinned, onFocus, onPin, register }: Omit<CardProps, "tier">) {
  const L = s.levels;
  const held = !!L.stop;
  const word = verdictWord(s);
  return (
    <button
      type="button"
      ref={register}
      className={`pick ${state(s, focus, pinned)}`}
      onMouseEnter={() => onFocus(s.symbol)}
      onMouseLeave={() => onFocus(null)}
      onFocus={() => onFocus(s.symbol)}
      onBlur={() => onFocus(null)}
      onClick={() => onPin(s.symbol)}
      aria-label={`${s.symbol} ${s.name}: ${word.toLowerCase()}, ${s.book.tag.toLowerCase()}`}
    >
      <span className="in">
        <span className="pick-top">
          <span className="pick-left">
            <span className="pick-id">
              <b className="sym">{s.symbol}</b>
              <span className="name">{s.name}</span>
            </span>
            <span className="pick-verdict">
              <span className={`badge${s.strength < 25 ? " weak" : ""}`}>{word}</span>
              <span className="ring" title={`sentiment score ${s.score > 0 ? "+" : "−"}${Math.abs(s.score).toFixed(0)} of 100`}>
                {s.strength.toFixed(0)}
              </span>
              <CheckDots s={s} />
            </span>
            <span className="pick-why">{s.book.reasons[0]}</span>
          </span>
          <span className="pick-right">
            <MiniChart s={s} />
            <span className="pick-tags">
              <span className="book">{s.book.tag}</span>
              <span className="setup">{s.setup}</span>
            </span>
          </span>
        </span>
        <span className="pick-levels">
          <span>
            <small>LAST</small>
            <b>
              {price(s.last)} <em>{pct(s.day_pct, 1)}</em>
            </b>
          </span>
          <span>
            <small>{held ? "STOP" : "S1"}</small>
            <b className="sup">{price(L.stop ?? L.s1)}</b>
          </span>
          <span>
            <small>R1</small>
            <b className="res">{price(L.r1)}</b>
          </span>
          <span>
            <small>{held ? "TARGET" : "R2"}</small>
            <b className="res">{price(L.target ?? L.r2)}</b>
          </span>
          <span>
            <small>R:R</small>
            <b>{L.rr === null ? "n/a" : L.rr.toFixed(1)}</b>
          </span>
        </span>
        <LevelBar s={s} />
      </span>
    </button>
  );
}
