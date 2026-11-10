import { useState } from "react";
import "./lab.css";
import { MarketTiles } from "./Overview";
import { hms } from "./Pieces";
import { TradesPane } from "./Trades";
import { TileKey } from "./types";
import { useLive } from "./useLab";

function callFromUrl(): string | null {
  return new URLSearchParams(window.location.search).get("call");
}

/** /lines: every position the lab has held, charted minute by minute with its three lines. */
export function LinesPage() {
  const { live, error } = useLive();
  const [filter, setFilter] = useState<TileKey | null>(null);
  const [picked, setPicked] = useState<string | null>(callFromUrl);
  const pick = (id: string | null) => {
    setPicked(id);
    const url = id ? `/lines?call=${encodeURIComponent(id)}` : "/lines";
    if (window.location.pathname + window.location.search !== url) window.history.replaceState(null, "", url);
  };
  if (live && live.lab === false)
    return (
      <div className="pl">
        <div className="pl-empty">
          <h1>Lines</h1>
          <p>This desk is the main desk: it has no lines engine. The lines run on the lab desk (branch predictor).</p>
        </div>
      </div>
    );
  return (
    <div className="pl">
      <header className="pl-bar">
        <h1>Lines</h1>
        <span className="pl-rule">base · max loss · min profit, minute by minute</span>
        <span className={`pl-clock${error ? " stale" : ""}`}>{error ? `not updating: ${error}` : live ? `as of ${hms(live.as_of)} UTC · open positions refresh every 10s` : "loading…"}</span>
      </header>
      {live && <MarketTiles live={live} filter={filter} onFilter={setFilter} />}
      <TradesPane picked={picked} onPick={pick} filter={filter} />
    </div>
  );
}
