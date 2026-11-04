import { useState } from "react";
import { TEAM, Team } from "../agents";
import { ago, timeUTC } from "../format";

function status(lastAt: string | null, now: number): "active" | "idle" | "quiet" | "never" {
  if (!lastAt) return "never";
  const s = (now - new Date(lastAt).getTime()) / 1000;
  if (s < 120) return "active";
  if (s < 900) return "idle";
  return "quiet";
}

export function Agents({ team, now, botAlive }: { team: Team; now: number; botAlive: boolean }) {
  const [open, setOpen] = useState<string | null>(null);
  return (
    <section className="card agents">
      <header className="card-head">
        <h2>The team, live</h2>
        <span className="muted">{botAlive ? "every event each agent emits, as it happens" : "the bot is not running: nothing moves until it is back"}</span>
      </header>
      <div className="agent-grid">
        {TEAM.map((a) => {
          const act = team[a.key];
          const st = status(act?.lastAt ?? null, now);
          return (
            <button
              type="button"
              key={a.key}
              className={`agent ${st} ${open === a.key ? "open" : ""}`}
              onClick={() => setOpen(open === a.key ? null : a.key)}
              title={a.role}
            >
              <div className="agent-head">
                <i aria-hidden="true" />
                <b>{a.label}</b>
                <span className="muted small" title="events in the last ten minutes">{act?.count10m ? `${act.count10m} events / 10 min` : ""}</span>
              </div>
              <div className="agent-role muted small">{a.role}</div>
              <div className="agent-last">
                {act?.lastText ? (
                  <>
                    <span>{act.lastText}</span>
                    <span className="muted small"> · {ago(act.lastAt, now)}</span>
                  </>
                ) : (
                  <span className="muted">nothing yet</span>
                )}
              </div>
            </button>
          );
        })}
      </div>
      {open && (
        <ul className="agent-recent">
          {(team[open]?.recent ?? []).length === 0 && <li className="muted">no recent events</li>}
          {(team[open]?.recent ?? []).map((r, i) => (
            <li key={i}>
              <span className="time">{timeUTC(r.at)}</span> {r.text}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
