import { useState } from "react";
import { Cycle } from "../api";
import { dateTimeUTC } from "../format";

function One({ c }: { c: Cycle }) {
  const [open, setOpen] = useState(false);
  const trades = c.decisions.filter((d) => d.action === "buy" || d.action === "sell");
  const holds = c.decisions.filter((d) => d.action === "hold");
  const passes = c.decisions.filter((d) => d.action === "none");
  const worst = c.challenge?.objections?.reduce<{ severity: string; symbol: string; argument: string } | null>(
    (acc, o) => (acc === null || rank(o.severity) > rank(acc.severity) ? o : acc),
    null,
  );
  return (
    <article className="cycle">
      <header onClick={() => setOpen(!open)} role="button" tabIndex={0} onKeyDown={(e) => e.key === "Enter" && setOpen(!open)}>
        <b>{c.trigger}</b>
        <span className="muted">{dateTimeUTC(c.time)}</span>
        <span>
          {trades.length} trade{trades.length === 1 ? "" : "s"}, {holds.length} hold{holds.length === 1 ? "" : "s"}, {passes.length} pass
          {passes.length === 1 ? "" : "es"}
          {c.dry_run ? " (dry run)" : ""}
        </span>
        {worst && <span className={`tag sev-${worst.severity}`}>skeptic: {worst.severity}</span>}
        <span className="muted expand">{open ? "hide" : "details"}</span>
      </header>
      {open && (
        <div className="cycle-body">
          {c.market_view && (
            <p>
              <b>Market view.</b> {c.market_view}
            </p>
          )}
          {c.risk_note && (
            <p>
              <b>Risk note.</b> {c.risk_note}
            </p>
          )}
          <table>
            <thead>
              <tr>
                <th>Symbol</th>
                <th>Action</th>
                <th>Conviction</th>
                <th>Thesis</th>
              </tr>
            </thead>
            <tbody>
              {c.decisions.map((d, i) => (
                <tr key={i}>
                  <td>
                    <b>{d.symbol}</b>
                  </td>
                  <td>
                    <span className={`side ${d.action}`}>{d.action}</span> {d.quantity ? d.quantity : ""}
                  </td>
                  <td>{typeof d.conviction === "number" ? d.conviction.toFixed(2) : ""}</td>
                  <td>{d.thesis}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {c.challenge && (
            <div className="skeptic">
              <b>Skeptic.</b> {c.challenge.overall}
              {c.challenge.objections.map((o, i) => (
                <p key={i}>
                  <span className={`tag sev-${o.severity}`}>{o.severity}</span> <b>{o.symbol}</b>: {o.argument}
                </p>
              ))}
            </div>
          )}
          {c.actions.length > 0 && (
            <ul className="actions">
              {c.actions.map((a, i) => (
                <li key={i}>
                  <b>{a.symbol}</b> {a.action}
                  {a.quantity ? ` ${a.quantity}` : ""}: {a.note}
                </li>
              ))}
            </ul>
          )}
          {c.result && <p className="muted">Result: {c.result}</p>}
        </div>
      )}
    </article>
  );
}

const rank = (s: string) => (s === "blocking" ? 2 : s === "concern" ? 1 : 0);

export function Cycles({ cycles, day = null }: { cycles: Cycle[] | null; day?: string | null }) {
  return (
    <section className="card">
      <header className="card-head">
        <h2>{day ? `Decision cycles on ${day}` : "Decision cycles"}</h2>
        <span className="muted">plan, challenge, outcome</span>
      </header>
      {!cycles ? (
        <p className="muted">loading…</p>
      ) : cycles.length === 0 ? (
        <p className="muted">{day ? "No decision cycle ran that day." : "No cycles yet."}</p>
      ) : (
        cycles.map((c) => <One key={c.cycle_id} c={c} />)
      )}
    </section>
  );
}
