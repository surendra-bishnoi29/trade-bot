import { JournalItem } from "../api";
import { timeUTC } from "../format";

const ICON: Record<string, string> = {
  "order.filled": "✅",
  "order.submitted": "📤",
  "order.rejected": "❌",
  "risk.approved": "🛡️",
  "risk.rejected": "⛔",
  "risk.kill_switch": "🛑",
  "watcher.alert": "⚠️",
  "cycle.started": "🧠",
  "cycle.completed": "🧠",
  "cycle.failed": "💥",
  "thesis.created": "🧭",
  "thesis.updated": "🧭",
  "thesis.closed": "🏁",
  "system.health": "🩺",
  "system.restarted": "🔁",
  "console.question": "💬",
  "console.answer": "🗣️",
};

export function Journal({ items, day = null }: { items: JournalItem[] | null; day?: string | null }) {
  return (
    <section className="card">
      <header className="card-head">
        <h2>{day ? `Activity on ${day}` : "Activity"}</h2>
        <span className="muted">everything the desk did, newest first</span>
      </header>
      {!items ? (
        <p className="muted">loading…</p>
      ) : items.length === 0 ? (
        <p className="muted">{day ? "Nothing in the journal that day." : "Nothing yet."}</p>
      ) : (
        <ul className="journal">
          {items.map((j) => (
            <li key={j.id}>
              <span className="time">{timeUTC(j.time)}</span>
              <span className="icon" aria-hidden="true">
                {ICON[j.type] ?? "•"}
              </span>
              <span className="text">
                {j.symbol && <b>{j.symbol} </b>}
                {j.text}
              </span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
