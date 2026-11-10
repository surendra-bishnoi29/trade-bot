import { pct } from "../funnel/format";
import { hms } from "./Pieces";
import { TradeChart } from "./TradeChart";
import { TileKey, TradeRow } from "./types";
import { useTrade, useTrades } from "./useLab";

const TILE_WORD: Record<TileKey, string> = { equities: "US equities", oil: "Oil and energy", gold: "Gold and metals", crypto: "Crypto" };

function Result({ t }: { t: TradeRow }) {
  if (t.open) return <span className="pill held">ON LINES</span>;
  if (t.realized_pct === null) return <span className="pill mute">{t.reason === "unfilled" ? "NEVER FILLED" : t.reason === "risk" ? "REFUSED" : "CLOSED"}</span>;
  return (
    <span className={`pill ${t.hit ? "bull" : "bear"}`} title={t.how}>
      {t.hit ? "RIGHT" : "WRONG"} {pct(t.realized_pct)}
    </span>
  );
}

/** Every position the lines engine has held: pick one and replay it minute by minute. */
export function TradesPane({ picked, onPick, filter }: { picked: string | null; onPick: (id: string | null) => void; filter: TileKey | null }) {
  const { trades, error } = useTrades(7);
  const rows = trades.filter((t) => filter === null || t.tile === filter);
  const current = picked && rows.some((t) => t.call_id === picked) ? picked : rows[0]?.call_id ?? null;
  const { chart, error: chartError } = useTrade(current);
  const row = rows.find((t) => t.call_id === current) ?? null;
  return (
    <div className="pl-trades">
      <section className="pl-col list" aria-label="Positions">
        <header className="col-head">
          <h2>Positions, last 7 days</h2>
          <span className="mute">{rows.filter((t) => t.open).length} open</span>
        </header>
        {error && <div className="calls-empty">not updating: {error}</div>}
        {!rows.length && !error && <div className="calls-empty">No position yet. The first one appears here when a call clears the bar and fills.</div>}
        <ul className="calls" aria-label="Positions, newest first">
          {rows.map((t) => (
            <li key={t.call_id}>
              <button type="button" className={`call-row ${t.open ? "held" : t.hit === null ? "mute" : t.hit ? "bull" : "bear"}${current === t.call_id ? " on" : ""}`} onClick={() => onPick(t.call_id)} aria-pressed={current === t.call_id}>
                <span className="in">
                  <span className="row-top">
                    <time>{t.opened_at.slice(5, 10)} {hms(t.opened_at)}</time>
                    <b className="sym">{t.symbol}</b>
                    <span className={`dir ${t.direction}`}>{t.direction.toUpperCase()}</span>
                  </span>
                  <span className="row-why">
                    {t.moves > 1 ? `${t.moves - 1} line move${t.moves === 2 ? "" : "s"}` : t.open ? "waiting for the min profit line" : "no line move"}
                    {t.armed ? ` · stop on the base${t.steps ? `, line raised ${t.steps}×` : ""}` : ""}
                    {t.closed_at ? ` · closed ${hms(t.closed_at)}` : ""}
                  </span>
                  <Result t={t} />
                </span>
              </button>
            </li>
          ))}
        </ul>
      </section>
      <section className="pl-col chart" aria-label="The position's chart">
        {row && (
          <header className="col-head tc-head">
            <h2>
              <b className="sym">{row.symbol}</b> <span className="name">{row.name}</span> <span className={`dir ${row.direction}`}>{row.direction.toUpperCase()}</span>
            </h2>
            <span className="mute">
              {TILE_WORD[row.tile]} · {row.quantity ?? "?"} at {row.base ?? "?"} · opened {hms(row.opened_at)} UTC{row.closed_at ? ` · closed ${hms(row.closed_at)}` : " · still open, refreshes every 10s"}
              {row.how ? ` · ${row.how}` : ""}
            </span>
            <Result t={row} />
          </header>
        )}
        {chartError && <div className="calls-empty">chart unavailable: {chartError}</div>}
        {!chart && !chartError && current && <div className="calls-empty">Loading the bars…</div>}
        {chart && <TradeChart chart={chart} />}
      </section>
    </div>
  );
}
