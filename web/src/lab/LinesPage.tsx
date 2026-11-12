import { useMemo, useState } from "react";
import "./lab.css";
import { MarketTiles } from "./Overview";
import { hms } from "./Pieces";
import { TradesPane } from "./Trades";
import { Tile, TileKey } from "./types";
import { useDays, useLive, useReport } from "./useLab";

const today = () => new Date().toISOString().slice(0, 10);

function fromUrl(key: string): string | null {
  return new URLSearchParams(window.location.search).get(key);
}

/** /lines: every position the lab has held, charted minute by minute with its three lines.
 * The day picker shows one UTC day: the positions opened on it and that day's counts on the tiles. */
export function LinesPage() {
  const { live, error } = useLive();
  const days = useDays();
  const [filter, setFilter] = useState<TileKey | null>(null);
  const [picked, setPicked] = useState<string | null>(() => fromUrl("call"));
  const [day, setDay] = useState<string | null>(() => fromUrl("day"));
  const past = day !== null && day !== today();
  const { report } = useReport(past ? day : null);
  const sync = (id: string | null, d: string | null) => {
    const q = new URLSearchParams();
    if (d) q.set("day", d);
    if (id) q.set("call", id);
    const url = q.toString() ? `/lines?${q}` : "/lines";
    if (window.location.pathname + window.location.search !== url) window.history.replaceState(null, "", url);
  };
  const pick = (id: string | null) => {
    setPicked(id);
    sync(id, day);
  };
  const chooseDay = (d: string | null) => {
    setDay(d);
    setPicked(null);
    sync(null, d);
  };
  // on a past day the tiles carry that day's counts instead of today's
  const dayTiles: Tile[] | undefined = useMemo(() => {
    if (!past || !live || !report || report.day !== day) return undefined;
    return live.tiles.map((t) => {
      const c = report.tally.tiles.find((x) => x.key === t.key);
      return c ? { ...t, ...c, on_lines: [], mood: null } : { ...t, on_lines: [] };
    });
  }, [past, live, report, day]);
  if (live && live.lab === false)
    return (
      <div className="pl">
        <div className="pl-empty">
          <h1>Lines</h1>
          <p>This desk is the main desk: it has no lines engine. The lines run on the lab desk (branch predictor).</p>
        </div>
      </div>
    );
  const dayOptions = Array.from(new Set([today(), ...days])).sort().reverse();
  return (
    <div className="pl">
      <header className="pl-bar">
        <h1>Lines</h1>
        <span className="pl-rule">base · max loss · min profit, minute by minute</span>
        <label className="pl-day">
          <span>Day (UTC)</span>
          <select id="lines-day" value={day ?? ""} onChange={(e) => chooseDay(e.target.value || null)}>
            <option value="">last 7 days</option>
            {dayOptions.map((d) => (
              <option key={d} value={d}>
                {d}
                {d === today() ? " (today)" : ""}
              </option>
            ))}
          </select>
        </label>
        <span className={`pl-clock${error ? " stale" : ""}`}>
          {error ? `not updating: ${error}` : past ? `${day}: the tiles count that day's calls` : live ? `as of ${hms(live.as_of)} UTC · open positions refresh every 10s` : "loading…"}
        </span>
      </header>
      {live && <MarketTiles live={live} filter={filter} onFilter={setFilter} tiles={dayTiles} />}
      <TradesPane picked={picked} onPick={pick} filter={filter} day={day} />
    </div>
  );
}
