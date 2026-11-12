import { useState } from "react";
import { Trade } from "../api";
import { dateTimeUTC, money, pct, signed } from "../format";

const TRIGGER_WORDS: Record<string, string> = {
  pre_market: "the pre-market cycle",
  mid_session: "the mid-session cycle",
  post_close: "the post-close cycle",
  cli: "a cycle run by hand",
  console: "a cycle run by hand",
  "thesis monitor": "the thesis monitor (stop, target or time exit)",
  "head trader": "the head trader on request",
  predictor: "the predictor, answering a sentinel trigger",
  lines: "the lines engine (profit line, stop, horizon or the bell)",
};

function Card({ t }: { t: Trade }) {
  const [open, setOpen] = useState(false);
  const trigger = t.when.trigger?.startsWith("news:") ? `news on ${t.when.trigger.slice(5)}` : TRIGGER_WORDS[t.when.trigger] ?? t.when.trigger;
  const sev = t.skeptic?.severity;
  return (
    <article className={`trade ${t.side}`}>
      <header onClick={() => setOpen(!open)} role="button" tabIndex={0} onKeyDown={(e) => e.key === "Enter" && setOpen(!open)}>
        <span className={`side ${t.side}`}>{t.side.toUpperCase()}</span>
        <b>{t.what}</b>
        <span className="muted">{dateTimeUTC(t.time)}</span>
        {t.thesis && (
          <span className={`tag ${t.thesis.status}`}>
            {t.thesis.status}
            {t.thesis.realized_pnl !== null ? ` ${signed(t.thesis.realized_pnl)}` : ""}
          </span>
        )}
        <span className="muted expand">{open ? "hide" : "why?"}</span>
      </header>
      {open && (
        <div className="trade-body">
          <dl>
            <dt>When</dt>
            <dd>
              {dateTimeUTC(t.time)}, decided by {trigger}.
            </dd>
            <dt>Why</dt>
            <dd>{t.why || "No written reason (an order placed outside a cycle or a call)."}</dd>
            {t.evidence.length > 0 && (
              <>
                <dt>Evidence</dt>
                <dd>
                  <ul className="evidence">
                    {t.evidence.map((e) => (
                      <li key={e.id}>
                        {e.url ? (
                          <a href={e.url} target="_blank" rel="noreferrer">
                            {e.title}
                          </a>
                        ) : (
                          e.title
                        )}
                        <span className="muted small">
                          {" "}
                          {e.source}
                          {e.impact ? ` · ${e.impact} impact` : ""}
                          {e.polarity !== null ? ` · tone ${e.polarity > 0 ? "+" : ""}${e.polarity}` : ""}
                        </span>
                      </li>
                    ))}
                  </ul>
                </dd>
              </>
            )}
            {t.exit_plan && (
              <>
                <dt>Exit plan</dt>
                <dd>
                  Stop {money(t.exit_plan.stop)} ({pct(t.exit_plan.stop_pct !== null ? -t.exit_plan.stop_pct : null)}), target{" "}
                  {money(t.exit_plan.target)} ({pct(t.exit_plan.target_pct)}), or after {t.exit_plan.horizon_days} sessions.
                  {t.exit_plan.invalidation.length > 0 && (
                    <ul>
                      {t.exit_plan.invalidation.map((s, i) => (
                        <li key={i}>{s}</li>
                      ))}
                    </ul>
                  )}
                </dd>
              </>
            )}
            {t.skeptic && (
              <>
                <dt>Skeptic</dt>
                <dd>
                  {sev && sev !== "none" && <span className={`tag sev-${sev}`}>{sev}</span>} {t.skeptic.argument}
                </dd>
              </>
            )}
            {t.risk && (
              <>
                <dt>Risk officer</dt>
                <dd>
                  <span className={`tag ${t.risk.decision}`}>{t.risk.decision}</span> {t.risk.reason}
                  <div className="rules">
                    {t.risk.rules.map((r) => (
                      <span key={r.rule} className={r.passed ? "rule ok" : "rule bad"} title={r.detail}>
                        {r.rule}
                      </span>
                    ))}
                  </div>
                </dd>
              </>
            )}
            {t.plan?.market_view && (
              <>
                <dt>Market view then</dt>
                <dd>{t.plan.market_view}</dd>
              </>
            )}
          </dl>
        </div>
      )}
    </article>
  );
}

export function Trades({ trades, day = null }: { trades: Trade[] | null; day?: string | null }) {
  return (
    <section className="card">
      <header className="card-head">
        <h2>{day ? `Trades on ${day}` : "Trades, with their reasons"}</h2>
        <span className="muted">{trades ? `${trades.length} fill${trades.length === 1 ? "" : "s"}` : ""}</span>
      </header>
      {!trades ? (
        <p className="muted">loading…</p>
      ) : trades.length === 0 ? (
        <p className="muted">{day ? "No fill that day." : "No fills yet."}</p>
      ) : (
        <div className="trades">
          {trades.map((t) => (
            <Card key={t.trade_id} t={t} />
          ))}
        </div>
      )}
    </section>
  );
}
