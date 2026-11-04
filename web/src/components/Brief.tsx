import { useState } from "react";
import { api, Brief as BriefT, User } from "../api";
import { dateTimeUTC, money } from "../format";

export function Brief({ brief, user, onSent }: { brief: BriefT | null; user: User; onSent: (requestId: string) => void }) {
  const [busy, setBusy] = useState(false);
  const operator = user.role === "operator";

  const write = async () => {
    setBusy(true);
    try {
      const r = await api.post<{ request_id: string }>("/api/commands", { command: "brief" });
      onSent(r.request_id);
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="card brief">
      <header className="card-head">
        <h2>Daily brief</h2>
        <span className="muted">
          {brief ? `${brief.day}, written ${dateTimeUTC(brief.generated_at)}` : "written by the head trader after each close"}
        </span>
      </header>
      {!brief ? (
        <p className="muted">No brief yet. The first one is written after today's close.</p>
      ) : (
        <div className="brief-body">
          <h3>{brief.brief.headline}</h3>
          <p>{brief.brief.summary}</p>
          {brief.brief.trades.length > 0 && (
            <ul>
              {brief.brief.trades.map((t, i) => (
                <li key={i}>
                  <span className={`side ${t.side}`}>{t.side}</span> {t.quantity} {t.symbol} at {money(t.price)}: {t.why}
                </li>
              ))}
            </ul>
          )}
          <dl>
            <dt>Book tonight</dt>
            <dd>{brief.brief.book}</dd>
            <dt>Incidents</dt>
            <dd>{brief.brief.incidents}</dd>
            <dt>Tomorrow</dt>
            <dd>{brief.brief.tomorrow}</dd>
          </dl>
        </div>
      )}
      {operator && (
        <div className="buttons">
          <button type="button" onClick={write} disabled={busy}>
            {busy ? "Asking…" : "Write the brief now"}
          </button>
        </div>
      )}
    </section>
  );
}
