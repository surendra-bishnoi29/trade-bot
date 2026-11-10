import { pct } from "../funnel/format";
import { ago, hm, rate, RightWrong } from "./Pieces";
import { Agent, Live, OpenLine, Tile, TileKey } from "./types";

/** The four markets the lab trades: whether each is open, its mood, and today's count. */
export function MarketTiles({ live, filter, onFilter }: { live: Live; filter: TileKey | null; onFilter: (k: TileKey | null) => void }) {
  return (
    <div className="tiles">
      {live.tiles.map((t) => (
        <MarketTile key={t.key} t={t} on={filter === t.key} dim={filter !== null && filter !== t.key} onClick={() => onFilter(filter === t.key ? null : t.key)} nextOpen={live.next_open} />
      ))}
    </div>
  );
}

function MarketTile({ t, on, dim, onClick, nextOpen }: { t: Tile; on: boolean; dim: boolean; onClick: () => void; nextOpen: string | null }) {
  const mood = t.mood?.verdict;
  return (
    <button type="button" className={`tile ${t.key}${on ? " on" : ""}${dim ? " dim" : ""}`} onClick={onClick} aria-pressed={on} title={`Show only ${t.label}`}>
      <span className="in">
        <span className="tile-head">
          <b>{t.label}</b>
          <span className={`state ${t.is_open ? "open" : "closed"}`}>{t.is_open ? "OPEN" : nextOpen ? `CLOSED · opens ${hm(nextOpen)} UTC` : "CLOSED"}</span>
        </span>
        <span className="tile-sub">
          {t.instruments} instruments · {t.shorts ? "buy and short" : "buy, then sell"}
          {mood && (
            <>
              {" · market "}
              <i className={mood === "BULLISH" ? "bull" : mood === "BEARISH" ? "bear" : "mute"}>
                {mood} {typeof t.mood?.score === "number" ? `${t.mood.score > 0 ? "+" : ""}${t.mood.score.toFixed(0)}` : ""}
              </i>
            </>
          )}
        </span>
        <span className="tile-nums">
          <span>
            <b>{t.calls}</b>
            <small>calls</small>
          </span>
          <span>
            <b>{t.directional}</b>
            <small>sided</small>
          </span>
          <span>
            <b>{t.traded}</b>
            <small>traded</small>
          </span>
          <span className="bull">
            <b>{t.all_right}</b>
            <small>right</small>
          </span>
          <span className="bear">
            <b>{t.all_wrong}</b>
            <small>wrong</small>
          </span>
        </span>
        <RightWrong c={t} />
        <span className="tile-foot">{t.on_lines.length ? `On lines now: ${t.on_lines.join(", ")}` : t.calls ? "Nothing on lines right now" : "No trigger yet today"}</span>
      </span>
    </button>
  );
}

const FLOW: { key: string; feeds?: boolean }[] = [{ key: "watcher", feeds: true }, { key: "news", feeds: true }, { key: "sentinel" }, { key: "predictor" }, { key: "skeptic" }, { key: "risk" }, { key: "execution" }, { key: "lines" }, { key: "back_office" }];

/** Every agent in the order work flows through them, with what each did in the last hour. */
export function AgentFlow({ agents, now }: { agents: Agent[]; now: number }) {
  const by = new Map(agents.map((a) => [a.key, a]));
  return (
    <ol className="flow" aria-label="What each agent is doing">
      {FLOW.map((f, i) => {
        const a = by.get(f.key);
        if (!a) return null;
        return (
          <li key={a.key} className={`ag ${a.state}${a.key === "predictor" ? " lead" : ""}`}>
            {i > 0 && <span className="arrow" aria-hidden="true" />}
            <div className="ag-card">
              <header>
                <i className="dot" />
                <b>{a.name}</b>
                <span className="n">{a.count}</span>
              </header>
              <small>{a.role}</small>
              <p>{a.line}</p>
              <time>{a.last_at ? `last activity ${ago(a.last_at, now)}` : "no activity in the last hour"}</time>
            </div>
          </li>
        );
      })}
    </ol>
  );
}

/** The positions the lines engine holds right now, each drawn between its stop and its profit line. */
export function OpenLines({ lines, onOpen }: { lines: OpenLine[]; onOpen?: (callId: string) => void }) {
  if (!lines.length)
    return (
      <div className="held-empty">
        No position is open. When the predictor's call clears the bar and the risk officer approves it, the position appears here with its base, max loss and min profit lines.
      </div>
    );
  return (
    <ul className="held">
      {lines.map((l) => {
        const long = l.direction === "long";
        const lo = Math.min(l.stop, l.profit_line ?? l.last, l.last, l.base);
        const hi = Math.max(l.stop, l.profit_line ?? l.last, l.last, l.base);
        const span = hi - lo || 1;
        const x = (v: number) => `${((v - lo) / span) * 100}%`;
        const gain = ((l.last - l.base) / l.base) * 100 * (long ? 1 : -1);
        return (
          <li key={l.call_id}>
            <header>
              <b>{l.symbol}</b>
              <span className={`dir ${l.direction}`}>{l.direction.toUpperCase()}</span>
              <span className={gain >= 0 ? "bull" : "bear"}>{pct(gain)}</span>
              <small>{l.armed ? (l.steps > 0 ? `line raised ${l.steps}×: cannot lose` : "stop on the base: cannot lose") : "waiting for the min profit line"}</small>
            </header>
            <div className="held-bar">
              <i className="stop" style={{ left: x(l.stop) }} title={`stop ${l.stop}`} />
              <i className="base" style={{ left: x(l.base) }} title={`base ${l.base}`} />
              {l.profit_line !== null && <i className="line" style={{ left: x(l.profit_line) }} title={`profit line ${l.profit_line}`} />}
              <i className="now" style={{ left: x(l.last) }} title={`last ${l.last}`} />
            </div>
            <footer>
              <span className="bear">stop {l.stop}</span>
              <span>base {l.base}</span>
              <span>now {l.last}</span>
              <span className="bull">line {l.profit_line ?? "n/a"}</span>
              {onOpen && (
                <button type="button" className="held-chart" onClick={() => onOpen(l.call_id)}>
                  <span className="in">chart</span>
                </button>
              )}
            </footer>
          </li>
        );
      })}
    </ul>
  );
}

export { rate };
