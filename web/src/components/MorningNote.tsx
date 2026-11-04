import { MorningNote as NoteT } from "../api";
import { dateTimeUTC } from "../format";

export function MorningNote({ note }: { note: NoteT | null }) {
  return (
    <section className="card">
      <header className="card-head">
        <h2>Morning note</h2>
        <span className="muted">{note ? `written ${dateTimeUTC(note.at)}` : "the head trader's read before the open"}</span>
      </header>
      {!note ? (
        <p className="muted">Not written yet today. It follows the first pre-market screen.</p>
      ) : (
        <div className="brief-body">
          <h3>{note.note.headline}</h3>
          <p>{note.note.tape}</p>
          {note.note.overnight_news.length > 0 && (
            <>
              <b className="small muted">OVERNIGHT</b>
              <ul>
                {note.note.overnight_news.map((n, i) => (
                  <li key={i}>{n}</li>
                ))}
              </ul>
            </>
          )}
          {note.note.watchlist.length > 0 && (
            <>
              <b className="small muted">WATCHLIST</b>
              <ul>
                {note.note.watchlist.map((w, i) => (
                  <li key={i}>
                    <b>{w.symbol}</b>: {w.why} <span className="muted">Watch for: {w.what_to_watch}</span>
                  </li>
                ))}
              </ul>
            </>
          )}
          {note.note.risks && (
            <p>
              <b>Risks.</b> {note.note.risks}
            </p>
          )}
        </div>
      )}
    </section>
  );
}
