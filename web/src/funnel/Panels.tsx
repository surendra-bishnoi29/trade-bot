import { verdictWord } from "./Cards";
import { hhmm, pct, price } from "./format";
import { Candle, CHECKS, Detail, Funnel, FunnelSymbol } from "./types";

/** The day's distribution: one dot per instrument on a percent axis, a bar per bucket above it. */
export function BreadthStrip({ funnel, focus, onFocus }: { funnel: Funnel; focus: string | null; onFocus: (s: string | null) => void }) {
  const W = 1000;
  const H = 64;
  const days = funnel.symbols.map((s) => s.day_pct ?? 0);
  const reach = Math.max(2, ...days.map((d) => Math.abs(d))) * 1.08;
  const x = (v: number) => W / 2 + (Math.max(-reach, Math.min(reach, v)) / reach) * (W / 2 - 14);
  const top = Math.max(1, ...funnel.breadth.bins.map((b) => b.n));
  const b = funnel.breadth;
  return (
    <div className="breadth">
      <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" role="img" aria-label={`${b.bull} instruments up, ${b.bear} down, ${b.flat} flat`}>
        {funnel.breadth.bins.map((bin) => {
          if (!bin.n) return null;
          const lo = x(Math.max(-reach, bin.lo));
          const hi = x(Math.min(reach, bin.hi));
          const hgt = (bin.n / top) * (H - 42);
          return <rect key={bin.lo} x={lo + 1} width={Math.max(2, hi - lo - 2)} y={H - 20 - hgt} height={hgt} className={`breadth-bar ${bin.hi <= 0 ? "bear" : "bull"}`} />;
        })}
        <line x1={8} x2={W - 8} y1={H - 14} y2={H - 14} className="breadth-axis" />
        <line x1={W / 2} x2={W / 2} y1={6} y2={H - 6} className="breadth-zero" />
        {funnel.symbols.map((s) => (
          <circle
            key={s.symbol}
            cx={x(s.day_pct ?? 0)}
            cy={H - 14}
            r={focus === s.symbol ? 6 : s.held ? 4.5 : 3.2}
            className={`breadth-dot ${(s.day_pct ?? 0) >= 0 ? "bull" : "bear"}${focus === s.symbol ? " on" : ""}${s.held ? " held" : ""}`}
            onMouseEnter={() => onFocus(s.symbol)}
            onMouseLeave={() => onFocus(null)}
          >
            <title>{`${s.symbol} ${pct(s.day_pct)}`}</title>
          </circle>
        ))}
      </svg>
      <div className="breadth-legend">
        <span className="bull">▲ BULLISH {b.bull}</span>
        <span className="bear">▼ BEARISH {b.bear}</span>
      </div>
    </div>
  );
}

function CandleChart({ candles, s }: { candles: Candle[]; s: FunnelSymbol }) {
  const W = 560;
  const H = 210;
  const L = s.levels;
  const marks = [
    { k: L.target ? "TARGET" : "R2", v: L.target ?? L.r2, cls: "res" },
    { k: "R1", v: L.r1, cls: "res" },
    { k: "ENTRY", v: L.entry ?? null, cls: "entry" },
    { k: "VWAP", v: L.vwap, cls: "vwap" },
    { k: L.stop ? "STOP" : "S1", v: L.stop ?? L.s1, cls: "sup" },
  ].filter((m): m is { k: string; v: number; cls: string } => typeof m.v === "number");
  let lo = Math.min(...candles.map((c) => c.l));
  let hi = Math.max(...candles.map((c) => c.h));
  const span0 = hi - lo || 1;
  for (const m of marks) if (m.v > lo - span0 * 0.6 && m.v < hi + span0 * 0.6) (lo = Math.min(lo, m.v)), (hi = Math.max(hi, m.v));
  const pad = (hi - lo) * 0.06 || 1;
  lo -= pad;
  hi += pad;
  const y = (v: number) => ((hi - v) / (hi - lo)) * (H - 26) + 6;
  const slots = Math.max(78, candles.length);
  const cw = (W - 64) / slots;
  const vmax = Math.max(1, ...candles.map((c) => c.v));
  return (
    <svg className="candles" viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" role="img" aria-label={`${s.symbol} five-minute candles for the session`}>
      {candles.map((c, i) => {
        const up = c.c >= c.o;
        const cx = 4 + i * cw + cw / 2;
        return (
          <g key={c.t} className={up ? "bull" : "bear"}>
            <rect x={cx - cw * 0.34} width={cw * 0.68} y={H - 20 - (c.v / vmax) * 26} height={(c.v / vmax) * 26} className="vol" />
            <line x1={cx} x2={cx} y1={y(c.h)} y2={y(c.l)} />
            <rect x={cx - cw * 0.34} width={cw * 0.68} y={y(Math.max(c.o, c.c))} height={Math.max(1, Math.abs(y(c.o) - y(c.c)))} />
          </g>
        );
      })}
      {marks
        .filter((m) => m.v >= lo && m.v <= hi)
        .map((m) => (
          <g key={m.k} className={`flevel ${m.cls}`}>
            <line x1={0} x2={W - 60} y1={y(m.v)} y2={y(m.v)} />
            <text x={W - 56} y={y(m.v) + 3}>
              {m.k} {price(m.v)}
            </text>
          </g>
        ))}
      {candles.length > 0 && (
        <>
          <text x={4} y={H - 4} className="axis">
            {hhmm(candles[0].t)}
          </text>
          <text x={W - 64} y={H - 4} className="axis" textAnchor="end">
            {hhmm(candles[candles.length - 1].t)} UTC
          </text>
        </>
      )}
    </svg>
  );
}

const AGENT: Record<string, string> = {
  watcher: "Watcher",
  news: "News agent",
  news_analyst: "News analyst",
  quant_analyst: "Quant analyst",
  cycle: "Head trader",
  head_trader: "Head trader",
  risk: "Risk officer",
  execution: "Execution",
  swing_strategy: "Thesis monitor",
  filings: "Filing analyst",
  fundamentals: "Fundamentals",
};

/** Everything the desk knows about one instrument: chart, book and why, evidence, decision, trail. */
export function FocusPanel({ s, detail, pinned, onClose }: { s: FunnelSymbol; detail: Detail | null; pinned: boolean; onClose: () => void }) {
  const d = s.desk;
  const L = s.levels;
  return (
    <aside className={`focus ${s.side}${pinned ? " pinned" : ""}`} aria-label={`${s.symbol} detail`}>
      <header>
        <div>
          <b className="sym">{s.symbol}</b> <span className="name">{s.name}</span>
          <span className="group">{s.group}</span>
        </div>
        <div className="focus-price">
          {price(s.last)} <em>{pct(s.day_pct)}</em>
          {pinned && (
            <button type="button" className="x" onClick={onClose} aria-label="Close detail">
              ×
            </button>
          )}
        </div>
      </header>
      <div className="focus-tags">
        <span className={`badge ${s.side}${s.strength < 25 ? " weak" : ""}`}>
          {verdictWord(s)} {s.score > 0 ? "+" : "−"}
          {Math.abs(s.score).toFixed(0)}
        </span>
        <span className="book">{s.book.tag}</span>
        <span className="setup">{s.setup}</span>
        {s.held && <span className="heldtag">IN THE BOOK</span>}
      </div>
      {detail && detail.candles.length > 1 ? <CandleChart candles={detail.candles} s={s} /> : <div className="candles wait">loading the session…</div>}
      <div className="focus-grid">
        <section>
          <h3>Why this book</h3>
          {s.book.reasons.map((r, i) => (
            <p key={i}>{r}</p>
          ))}
          <h3>Agent checks</h3>
          <ul className="checks">
            {CHECKS.map((c) => (
              <li key={c.key} className={s.checks[c.key] ? "on" : ""}>
                <i /> <b>{c.label}</b> <span>{c.what}</span>
              </li>
            ))}
          </ul>
        </section>
        <section>
          <h3>Sentiment evidence</h3>
          <ul className="fevidence">
            {s.evidence.map((e) => (
              <li key={e.label} className={e.verdict}>
                <span className="ftag">{e.verdict}</span>
                <span className="lbl">{e.label}</span>
                <b>{e.value}</b>
                <small>{e.source}</small>
              </li>
            ))}
          </ul>
        </section>
      </div>
      {(s.call || s.lines || s.record) && <LabPanel s={s} />}
      {(d.thesis || d.decision) && !s.lines && (
        <section className="focus-desk">
          <h3>What the desk decided</h3>
          {d.decision && (
            <p>
              <b>Head trader</b> {hhmm(d.decision.at)} UTC: {d.decision.action} at conviction {Number(d.decision.conviction).toFixed(2)}. {d.decision.thesis.slice(0, 260)}
              {d.decision.thesis.length > 260 ? "…" : ""}
            </p>
          )}
          {d.decision?.objection && (
            <p className={`objection ${d.decision.objection.severity}`}>
              <b>Skeptic, {d.decision.objection.severity}:</b> {d.decision.objection.argument.slice(0, 260)}
              {d.decision.objection.argument.length > 260 ? "…" : ""}
            </p>
          )}
          {d.risk && (
            <p>
              <b>Risk officer</b> {hhmm(d.risk.at)} UTC: {d.risk.decision}, {d.risk.passed} of {d.risk.rules} rules passed.
            </p>
          )}
          {d.fill && (
            <p>
              <b>Execution</b> {hhmm(d.fill.at)} UTC: {d.fill.side === "buy" ? "bought" : "sold"} {d.fill.qty} at {price(d.fill.price)}.
            </p>
          )}
          {d.thesis && L.stop && L.target && (
            <p>
              <b>Position:</b> {d.thesis.quantity} at {price(d.thesis.entry)}, stop {price(L.stop)}, target {price(L.target)}, reward to risk {L.rr ?? "n/a"}.
            </p>
          )}
        </section>
      )}
      {detail && detail.trail.length > 0 && (
        <section className="focus-trail">
          <h3>Trail today</h3>
          <ol>
            {detail.trail.slice(0, pinned ? 14 : 5).map((t, i) => (
              <li key={i}>
                <time>{hhmm(t.at)}</time>
                <b>{AGENT[t.agent] ?? t.agent}</b>
                <span>{t.text}</span>
              </li>
            ))}
          </ol>
        </section>
      )}
    </aside>
  );
}

/** The lab desk on one instrument: the predictor's latest call, the lines that hold the position,
 * and the record every call is scored against. */
function LabPanel({ s }: { s: FunnelSymbol }) {
  const c = s.call;
  const ln = s.lines;
  const r = s.record;
  const longSide = ln?.direction === "long";
  return (
    <section className="focus-desk lab">
      <h3>Predictor and lines</h3>
      {r && r.n > 0 ? (
        <p className="record">
          <b>Record:</b> {r.hits} of {r.n} calls hit ({(r.hit * 100).toFixed(0)}%), {r.avg_pct >= 0 ? "+" : ""}
          {r.avg_pct.toFixed(2)}% per call after costs.
        </p>
      ) : (
        <p className="record muted">
          <b>Record:</b> no scored calls yet on this instrument.
        </p>
      )}
      {c && (
        <p>
          <b>Predictor</b> {hhmm(c.at)} UTC on {c.trigger}: {c.call.direction} at confidence {Number(c.call.confidence).toFixed(2)}
          {c.acted ? `, loss line ${c.call.max_loss_pct}%, first profit line ${c.call.first_profit_pct}%, step ${c.call.step_pct}%.` : `, no trade: ${c.skipped_because || "declined"}.`}{" "}
          {c.call.reason.slice(0, 280)}
          {c.call.reason.length > 280 ? "…" : ""}
        </p>
      )}
      {c?.objection && (
        <p className="objection concern">
          <b>Skeptic:</b> {c.objection.slice(0, 260)}
          {c.objection.length > 260 ? "…" : ""}
        </p>
      )}
      {ln && (
        <div className="lines">
          <p>
            <b>Lines</b> {hhmm(ln.at)} UTC: {ln.direction} {ln.quantity} from {price(ln.base)}, {ln.event === "opened" ? "waiting for the first profit line" : ln.event === "base" ? "the stop sits on the base, the trade can no longer lose" : `profit line raised ${ln.steps} ${ln.steps === 1 ? "time" : "times"}`}
            .
          </p>
          <ul className="linebar">
            <li className="stop">
              <small>{ln.stop === ln.base ? "STOP = BASE" : "MAX LOSS"}</small>
              <b>{price(ln.stop)}</b>
            </li>
            <li className="base">
              <small>BASE</small>
              <b>{price(ln.base)}</b>
            </li>
            <li className="last">
              <small>NOW</small>
              <b>{price(ln.last)}</b>
              <em>{pct(((ln.last - ln.base) / ln.base) * 100 * (longSide ? 1 : -1))}</em>
            </li>
            <li className="line">
              <small>{ln.armed ? "SELL AT" : "MIN PROFIT"}</small>
              <b>{price(ln.profit_line)}</b>
            </li>
          </ul>
        </div>
      )}
    </section>
  );
}

/** The market verdict with every reading behind it and the agent that measured it. */
export function SentimentChip({ funnel }: { funnel: Funnel }) {
  const s = funnel.sentiment;
  const cls = s.verdict === "BULLISH" ? "bull" : s.verdict === "BEARISH" ? "bear" : "mixed";
  return (
    <details className={`sentiment ${cls}`}>
      <summary>
        <span className="lbl">Market</span>
        <b>{s.verdict}</b>
        <span className="score">
          {s.score > 0 ? "+" : s.score < 0 ? "−" : ""}
          {Math.abs(s.score).toFixed(0)}
        </span>
        <span className="bench">
          {funnel.benchmark.name} {pct(funnel.benchmark.day_pct)}
        </span>
      </summary>
      <ul className="fevidence pop">
        {s.evidence.map((e) => (
          <li key={e.label} className={e.verdict}>
            <span className="ftag">{e.verdict}</span>
            <span className="lbl">{e.label}</span>
            <b>{e.value}</b>
            <small>{e.source}</small>
          </li>
        ))}
      </ul>
    </details>
  );
}
