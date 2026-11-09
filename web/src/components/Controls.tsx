import { useState } from "react";
import { api, Health, User } from "../api";

export interface CommandResult {
  request_id: string;
  command: string;
  ok: boolean | null;
  text: string | null;
}

export function Controls({
  user,
  health,
  results,
  onSent,
}: {
  user: User;
  health: Health | null;
  results: CommandResult[];
  onSent: (r: CommandResult) => void;
}) {
  const [confirm, setConfirm] = useState<string | null>(null);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const operator = user.role === "operator";
  const engaged = health?.kill_switch.engaged ?? false;
  const lab = health?.lab ?? false; // no cycle on the lab desk: the predictor decides on its own triggers

  const send = async (command: string, args = "") => {
    setBusy(command);
    try {
      const r = await api.post<{ request_id: string }>("/api/commands", {
        command,
        args,
      });
      onSent({ request_id: r.request_id, command, ok: null, text: null });
    } catch (e) {
      onSent({
        request_id: "",
        command,
        ok: false,
        text: String((e as Error).message),
      });
    } finally {
      setBusy(null);
      setConfirm(null);
      setReason("");
    }
  };

  return (
    <section className="card controls">
      <header className="card-head">
        <h2>Controls</h2>
        <span className="muted">
          {operator ? "operator" : "viewer: watch and ask, no trading controls"}
        </span>
      </header>
      {operator && (
        <div className="buttons">
          {lab ? (
            <>
              <button
                type="button"
                disabled={busy !== null}
                onClick={() => send("lines")}
                className="primary"
                title="The predictor's record, the positions on lines and the recent calls"
              >
                Lines and record
              </button>
            </>
          ) : (
            <>
              <button
                type="button"
                disabled={busy !== null}
                onClick={() => send("plan")}
                title="Plan and challenge, place no orders"
              >
                Dry-run cycle
              </button>
              <button
                type="button"
                disabled={busy !== null}
                onClick={() => setConfirm("cycle")}
                className="primary"
              >
                Run a cycle
              </button>
            </>
          )}
          {engaged ? (
            <button
              type="button"
              disabled={busy !== null}
              onClick={() => setConfirm("resume")}
              className="good"
            >
              Release kill switch
            </button>
          ) : (
            <button
              type="button"
              disabled={busy !== null}
              onClick={() => setConfirm("kill")}
              className="danger"
            >
              Kill switch
            </button>
          )}
          <button
            type="button"
            disabled={busy !== null}
            onClick={() => send("status")}
          >
            Status
          </button>
        </div>
      )}
      {confirm && (
        <div className="confirm">
          {confirm === "cycle" && (
            <p>
              Run a real cycle now? The head trader may place orders through the
              risk officer.
            </p>
          )}
          {confirm === "kill" && (
            <p>
              Engage the kill switch? New orders are rejected until it is
              released; exits still pass.
            </p>
          )}
          {confirm === "resume" && (
            <p>Release the kill switch and let the desk trade again?</p>
          )}
          {confirm !== "cycle" && (
            <input
              id="confirm-reason"
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              placeholder="reason (recorded in the journal)"
            />
          )}
          <div className="buttons">
            <button
              type="button"
              onClick={() => send(confirm, reason)}
              className={confirm === "kill" ? "danger" : "primary"}
            >
              Yes,{" "}
              {confirm === "cycle"
                ? "run it"
                : confirm === "kill"
                  ? "engage"
                  : "release"}
            </button>
            <button type="button" onClick={() => setConfirm(null)}>
              Cancel
            </button>
          </div>
        </div>
      )}
      {results.length > 0 && (
        <ul className="results">
          {results.slice(0, 6).map((r, i) => (
            <li key={r.request_id || i} className={r.ok === false ? "neg" : ""}>
              <b>{r.command}</b>:{" "}
              {r.ok === null ? "sent, waiting for the bot…" : r.text}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
