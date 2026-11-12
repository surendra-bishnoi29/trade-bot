import { Book as BookT, Position } from "../api";
import { ago, money, pct, signed, timeUTC } from "../format";

function Ladder({ p }: { p: Position }) {
  const t = p.thesis;
  if (!t || t.stop === null || t.target === null || p.price === null) return <span className="muted">no thesis</span>;
  const span = t.target - t.stop || 1;
  const pos = Math.min(100, Math.max(0, ((p.price - t.stop) / span) * 100));
  const entry = t.entry !== null ? Math.min(100, Math.max(0, ((t.entry - t.stop) / span) * 100)) : null;
  return (
    <div className="ladder" title={`stop ${money(t.stop)} · target ${money(t.target)}`}>
      <div className="ladder-bar">
        {entry !== null && <i className="ladder-entry" style={{ left: `${entry}%` }} />}
        <i className="ladder-now" style={{ left: `${pos}%` }} />
      </div>
      <div className="ladder-labels">
        <span>stop {money(t.stop)}</span>
        <span>{pct(t.to_stop_pct)} above stop · {pct(t.to_target_pct)} to target</span>
        <span>target {money(t.target)}</span>
      </div>
    </div>
  );
}

export function Book({ book, now, day = null }: { book: BookT | null; now: number; day?: string | null }) {
  if (!book) return <section className="card">loading the book…</section>;
  const pl = book.positions.reduce((s, p) => s + (p.unrealized_pl ?? 0), 0);
  const past = day !== null;
  return (
    <section className="card">
      <header className="card-head">
        <h2>{past ? `The book on ${day}` : "The book"}</h2>
        <span className="muted">
          {past ? (book.as_of ? `last broker snapshot of the day, ${timeUTC(book.as_of)}` : "no broker snapshot that day") : `broker snapshot ${ago(book.as_of, now)}`}
        </span>
      </header>
      <div className="stats">
        <div>
          <b>Equity</b>
          <span>${money(book.equity)}</span>
        </div>
        <div>
          <b>Cash</b>
          <span>${money(book.cash)}</span>
        </div>
        <div>
          <b>In positions</b>
          <span>${money(book.gross_exposure)}</span>
        </div>
        <div>
          <b>Open P&amp;L</b>
          <span className={pl >= 0 ? "pos" : "neg"}>{signed(pl)}</span>
        </div>
      </div>
      {book.positions.length === 0 ? (
        <p className="muted">{past ? (book.as_of ? "No open position at the end of that day; the desk was in cash." : "The desk took no broker snapshot that day.") : "No open positions. The desk is in cash."}</p>
      ) : (
        <div className="table-wrap">
          <table className="positions">
            <thead>
              <tr>
                <th>Symbol</th>
                <th className="num">Qty</th>
                <th className="num">Entry</th>
                <th className="num">Price</th>
                <th className="num">P&amp;L</th>
                <th>Thesis</th>
              </tr>
            </thead>
            <tbody>
              {book.positions.map((p) => (
                <tr key={p.symbol}>
                  <td>
                    <b>{p.symbol}</b>
                    <div className="muted small">{p.price_at ? (past ? `at ${timeUTC(p.price_at)}` : ago(p.price_at, now)) : ""}</div>
                  </td>
                  <td className="num">{p.qty}</td>
                  <td className="num">{money(p.avg_entry)}</td>
                  <td className="num">{money(p.price)}</td>
                  <td className={`num ${(p.unrealized_pl ?? 0) >= 0 ? "pos" : "neg"}`}>
                    {signed(p.unrealized_pl)}
                    <div className="small">{pct(p.unrealized_pct)}</div>
                  </td>
                  <td className="ladder-cell">
                    <Ladder p={p} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
