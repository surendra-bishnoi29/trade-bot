import { pct, price } from "../funnel/format";
import { Confidence, Direction, hm, hms, LineLadder, OutcomePill } from "./Pieces";
import { OUTCOME, Story } from "./types";

/** The predictor's calls, newest first: one row a call, the selected one opened beside it. */
export function CallList({ stories, selected, onSelect, bar }: { stories: Story[]; selected: string | null; onSelect: (id: string) => void; bar: number }) {
  if (!stories.length) return <div className="calls-empty">No calls match. The sentinel raises a trigger when an instrument changes; each trigger becomes one row here.</div>;
  return (
    <ul className="calls" aria-label="Predictor calls, newest first">
      {stories.map((s) => (
        <li key={s.call_id}>
          <button type="button" className={`call-row ${OUTCOME[s.outcome].tone}${selected === s.call_id ? " on" : ""}`} onClick={() => onSelect(s.call_id)} aria-pressed={selected === s.call_id}>
            <span className="in">
              <span className="row-top">
                <time>{hms(s.at)}</time>
                <b className="sym">{s.symbol}</b>
                <Direction call={s.call} />
                <Confidence value={s.call.confidence} bar={bar} />
              </span>
              <span className="row-why">{s.trigger}</span>
              <OutcomePill s={s} />
            </span>
          </button>
        </li>
      ))}
    </ul>
  );
}

function Step({ n, who, title, tone, children }: { n: number; who: string; title: string; tone?: string; children: React.ReactNode }) {
  return (
    <section className={`step ${tone ?? ""}`}>
      <header>
        <i>{n}</i>
        <span className="by">{who}</span>
        <h3>{title}</h3>
      </header>
      <div className="step-body">{children}</div>
    </section>
  );
}

const TONE: Record<string, string> = { bullish: "bull", bearish: "bear", neutral: "mute" };

/** One call from trigger to result: what the predictor was shown, how it reasoned, who challenged
 * it, what the desk did, and how it ended. */
export function CallDetail({ s, bar }: { s: Story; bar: number }) {
  const c = s.call;
  const i = s.inputs ?? {};
  const hasInputs = (i.evidence?.length ?? 0) > 0 || !!i.market?.verdict;
  const hasReasoning = (c.supports?.length ?? 0) > 0 || (c.against?.length ?? 0) > 0 || !!c.decisive;
  const directional = c.direction !== "none";
  const gate = s.acted ? "Cleared every check: an order was sent." : s.skipped_because || "No order.";
  let n = 0;
  return (
    <article className="detail" aria-label={`${s.symbol} call at ${hms(s.at)}`}>
      <header className="detail-head">
        <div>
          <b className="sym">{s.symbol}</b> <span className="name">{s.name}</span>
          <time>{s.at.slice(0, 10)} {hms(s.at)} UTC</time>
        </div>
        <div className="detail-verdict">
          <Direction call={c} />
          <Confidence value={c.confidence} bar={bar} wide />
          <OutcomePill s={s} />
        </div>
      </header>

      <Step n={++n} who="Sentinel" title="Why it looked">
        <p>
          <b>{s.trigger}</b>
          {i.reading && (
            <>
              {" "}
              The instrument read <span className={i.reading.side === "bull" ? "bull" : "bear"}>{i.reading.side === "bull" ? "bullish" : "bearish"} {i.reading.strength.toFixed(0)}</span> of 100{i.reading.setup ? `, setup ${i.reading.setup.toLowerCase()}` : ""}, last price {price(s.price)}
              {typeof i.day_pct === "number" ? ` (${pct(i.day_pct)} on the day)` : ""}.
            </>
          )}
        </p>
      </Step>

      <Step n={++n} who="Predictor" title="What it was shown">
        {!hasInputs && <p className="mute">The readings were not recorded for calls made before the 9 Nov update. Newer calls list every reading here.</p>}
        {(i.evidence?.length ?? 0) > 0 && (
          <ul className="readings">
            {i.evidence!.map((e) => (
              <li key={e.label} className={TONE[e.verdict] ?? "mute"}>
                <span className="vtag">{e.verdict}</span>
                <span className="lbl">{e.label}</span>
                <b>{e.value}</b>
                <small>{e.source}</small>
              </li>
            ))}
          </ul>
        )}
        <div className="shown-grid">
          {i.market?.verdict && (
            <div>
              <h4>The market ({i.market.label})</h4>
              <p>
                <span className={i.market.verdict === "BULLISH" ? "bull" : i.market.verdict === "BEARISH" ? "bear" : "mute"}>
                  {i.market.verdict} {typeof i.market.score === "number" ? `${i.market.score > 0 ? "+" : ""}${i.market.score.toFixed(0)}` : ""}
                </span>
                {(i.market.evidence ?? []).map((e) => ` · ${e.label} ${e.value}`).join("")}
              </p>
            </div>
          )}
          {i.levels && Object.keys(i.levels).length > 0 && (
            <div>
              <h4>Levels</h4>
              <p className="mono">
                session {price(i.levels.s1)} to {price(i.levels.r1)} · 20-day {price(i.levels.s2)} to {price(i.levels.r2)} · VWAP {price(i.levels.vwap)} · prior close {price(i.levels.prior_close)}
              </p>
            </div>
          )}
          <div>
            <h4>Its own record on {s.symbol}</h4>
            <p>{s.record?.n ? `${s.record.hits ?? Math.round((s.record.hit ?? 0) * s.record.n)} of ${s.record.n} earlier calls right${typeof s.record.avg_pct === "number" ? `, ${pct(s.record.avg_pct)} a call after costs` : ""}.` : "No scored calls on this instrument yet."}</p>
          </div>
          {i.rules && (
            <div>
              <h4>The book's rules</h4>
              <p>
                Shorts {i.rules.shorts ? "allowed" : "not allowed"} · a round trip costs {i.rules.round_trip_pct.toFixed(2)}% · min profit line at least {i.rules.min_first_profit_pct.toFixed(2)}% · max loss at least {i.rules.min_loss_pct.toFixed(2)}% · trades only at confidence {i.rules.min_confidence.toFixed(2)} or more
              </p>
            </div>
          )}
        </div>
        {(i.news?.length ?? 0) > 0 && (
          <>
            <h4>Headlines in front of it</h4>
            <ul className="headlines">
              {i.news!.map((h) => (
                <li key={h.id}>
                  <time>{h.at.slice(5, 10)} {hm(h.at)}</time>
                  {h.title}
                </li>
              ))}
            </ul>
          </>
        )}
      </Step>

      <Step n={++n} who="Predictor" title="How it reasoned" tone="lead">
        {hasReasoning ? (
          <div className="weigh">
            <div className="for">
              <h4>{directional ? `For the ${c.direction}` : c.lean ? `For the ${c.lean} it leaned to` : "For the side it came closest to"}</h4>
              <ul>{(c.supports ?? []).map((x, k) => <li key={k}>{x}</li>)}</ul>
            </div>
            <div className="against">
              <h4>Against</h4>
              <ul>{(c.against ?? []).map((x, k) => <li key={k}>{x}</li>)}</ul>
            </div>
          </div>
        ) : (
          <p className="mute">The for-and-against breakdown starts with calls made after the 9 Nov update. This call recorded only its conclusion.</p>
        )}
        {c.decisive && (
          <p className="decisive">
            <b>What settled it:</b> {c.decisive}
          </p>
        )}
        <p className="reason">{c.reason}</p>
        {c.invalidation && (
          <p className="invalid">
            <b>It would be wrong if:</b> {c.invalidation}
          </p>
        )}
        <p className="meta">
          {s.model || "model not recorded"}
          {typeof s.seconds === "number" ? ` · ${s.seconds.toFixed(1)}s` : ""}
          {s.tokens?.input ? ` · read ${s.tokens.input.toLocaleString("en-US")} tokens, wrote ${(s.tokens.output ?? 0).toLocaleString("en-US")}` : ""}
        </p>
      </Step>

      {directional && (
        <Step n={++n} who="Predictor" title="The call and its lines">
          <p>
            <Direction call={c} /> at confidence <b>{c.confidence.toFixed(2)}</b>, to work within {c.horizon_minutes} minutes. Lines from the price it saw, {price(s.price)}:
          </p>
          <LineLadder call={c} entry={s.fill?.price ?? s.price} />
        </Step>
      )}

      {!directional && c.lean && s.outcome === "passed" && (
        <Step n={++n} who="Predictor" title={`No trade, but it leaned ${c.lean}`}>
          <p>
            It put the odds of a {c.lean} reaching its first profit line before its maximum loss at <b>{c.confidence.toFixed(2)}</b>; the desk trades at {bar.toFixed(2)} or more. These are the lines it would have used, and the lines its lean is graded on:
          </p>
          <LineLadder call={{ ...c, direction: c.lean }} entry={s.price} />
        </Step>
      )}

      {s.objection && (
        <Step n={++n} who="Skeptic" title="The case against" tone="amber">
          <p>{s.objection}</p>
        </Step>
      )}

      <Step n={++n} who="The bar" title={s.acted ? "Cleared" : directional ? "Held back" : "No call to trade"} tone={s.acted ? "bull" : "mute"}>
        <p>{gate}</p>
      </Step>

      {s.risk && (
        <Step n={++n} who="Risk officer" title={s.risk.decision === "approved" ? "Approved" : "Refused"} tone={s.risk.decision === "approved" ? "bull" : "bear"}>
          <p>
            {s.risk.passed} of {s.risk.rules} rules passed. {s.risk.decision === "approved" ? "" : s.risk.failed.join("; ")}
          </p>
        </Step>
      )}

      {s.fill && (
        <Step n={++n} who="Execution" title="Filled">
          <p>
            {s.order?.side === "sell" ? "Sold short" : "Bought"} {s.fill.quantity} at {price(s.fill.price)} at {hms(s.fill.at)} UTC{s.order?.stop_price ? `, with a stop resting at the broker at ${price(s.order.stop_price)}` : ""}.
          </p>
        </Step>
      )}

      {s.lines.length > 0 && (
        <Step n={++n} who="Lines engine" title="What the lines did" tone="held">
          <ol className="line-events">
            {s.lines.map((l, k) => (
              <li key={k}>
                <time>{hms(l.at)}</time>
                <b>{{ opened: "Opened", base: "Stop moved to the base", ratchet: "Profit line raised", exit: "Closed" }[l.event] ?? l.event}</b>
                <span>
                  stop {price(l.stop)} · profit line {price(l.profit_line)} · last {price(l.last)}
                </span>
              </li>
            ))}
          </ol>
        </Step>
      )}

      <Step n={++n} who="Result" title={OUTCOME[s.outcome].word} tone={OUTCOME[s.outcome].tone}>
        <p>{OUTCOME[s.outcome].what}</p>
        {s.score && typeof s.score.realized_pct === "number" && (
          <p>
            Entry {price(s.score.entry)}, exit {price(s.score.exit)}: <b className={s.score.realized_pct > 0 ? "bull" : "bear"}>{pct(s.score.realized_pct)}</b> after costs. It {s.score.how}.
          </p>
        )}
        {s.paper && s.paper.result !== "pending" && (
          <p>
            <b>Checked on paper:</b> {s.outcome === "passed" && c.lean ? `a ${c.lean}, the side it leaned to, with ` : ""}the same lines run over the real prices that followed would have ended <b className={s.paper.result === "right" ? "bull" : "bear"}>{s.paper.result}, {pct(s.paper.realized_pct)}</b> after costs ({s.paper.reason}). {s.paper.result === "right" ? "Holding back cost the desk a winner here." : "Holding back saved the desk a loss here."}
          </p>
        )}
        {s.paper && s.paper.result === "pending" && <p><b>Checked on paper:</b> still running, {pct(s.paper.mark_pct)} so far against the real prices.</p>}
        {s.after && (
          <p>
            In the next {s.after.minutes} minutes the price went as high as {pct(s.after.up_pct)} and as low as {pct(s.after.down_pct)}, ending {pct(s.after.end_pct)}.
          </p>
        )}
      </Step>
    </article>
  );
}
