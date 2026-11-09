import { useEffect, useMemo, useState } from "react";
import { CallDetail, CallList } from "./Calls";
import "./lab.css";
import { AgentFlow, MarketTiles, OpenLines } from "./Overview";
import { hms } from "./Pieces";
import { ReportView, Scoreboard } from "./Scoreboard";
import { Story, TileKey } from "./types";
import { useDays, useLive, useReport } from "./useLab";

type Tab = "live" | "report";
type Show = "all" | "sided" | "traded";

/** The predictor at work: what it is shown, how it reasons, what the other agents do with its
 * calls, and how often it turns out right. Live, with a graded report for each day. */
export function LabBoard() {
  const { live, error, fetchedAt } = useLive();
  const [tab, setTab] = useState<Tab>("live");
  const [filter, setFilter] = useState<TileKey | null>(null);
  const [show, setShow] = useState<Show>("all");
  const [picked, setPicked] = useState<string | null>(null);
  const [follow, setFollow] = useState(true);
  const [now, setNow] = useState(Date.now());
  const days = useDays();
  const today = new Date().toISOString().slice(0, 10);
  const [day, setDay] = useState<string>(today);
  const { report, loading } = useReport(tab === "report" ? day : null);
  const [opened, setOpened] = useState<Story | null>(null);

  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, []);

  const bar = live?.stories.find((s) => s.inputs?.rules)?.inputs.rules?.min_confidence ?? 0.6;
  const stories = useMemo(() => {
    const rows = live?.stories ?? [];
    return rows.filter((s) => (filter === null || s.tile === filter) && (show === "all" || (show === "sided" && s.call.direction !== "none") || (show === "traded" && s.acted)));
  }, [live, filter, show]);
  const current = useMemo(() => {
    if (!stories.length) return null;
    if (follow || !picked) return stories[0];
    return stories.find((s) => s.call_id === picked) ?? (live?.stories ?? []).find((s) => s.call_id === picked) ?? stories[0];
  }, [stories, picked, follow, live]);

  if (live && live.lab === false)
    return (
      <div className="pl">
        <div className="pl-empty">
          <h1>Predictor</h1>
          <p>This desk is the main desk: it has no predictor. The predictor, the sentinel and the lines run on the lab desk (branch predictor).</p>
        </div>
      </div>
    );

  const stale = fetchedAt !== null && now - fetchedAt > 30000;
  const dayOptions = Array.from(new Set([today, ...days])).sort().reverse();

  return (
    <div className="pl">
      <header className="pl-bar">
        <h1>Predictor</h1>
        <div className="pl-tabs" role="tablist">
          <button type="button" role="tab" aria-selected={tab === "live"} className={tab === "live" ? "on" : ""} onClick={() => setTab("live")}>
            Live
          </button>
          <button type="button" role="tab" aria-selected={tab === "report"} className={tab === "report" ? "on" : ""} onClick={() => setTab("report")}>
            Right / wrong report
          </button>
        </div>
        <span className="pl-rule" title="The predictor only trades a call at or above this confidence">
          trades at confidence ≥ {bar.toFixed(2)}
        </span>
        <span className={`pl-clock${stale || error ? " stale" : ""}`}>
          {error ? `not updating: ${error}` : live ? `as of ${hms(live.as_of)} UTC · refreshes every 10s` : "loading…"}
        </span>
      </header>

      {!live && !error && <div className="pl-empty">Loading the lab's journal…</div>}

      {live && tab === "live" && (
        <>
          <MarketTiles live={live} filter={filter} onFilter={setFilter} />
          <AgentFlow agents={live.agents} now={now} />
          <div className="pl-main">
            <section className="pl-col list" aria-label="Calls">
              <header className="col-head">
                <h2>Calls</h2>
                <div className="seg" role="group" aria-label="Which calls to show">
                  {(
                    [
                      ["all", "all"],
                      ["sided", "named a side"],
                      ["traded", "traded"],
                    ] as [Show, string][]
                  ).map(([k, label]) => (
                    <button key={k} type="button" className={show === k ? "on" : ""} onClick={() => setShow(k)}>
                      {label}
                    </button>
                  ))}
                </div>
              </header>
              <CallList
                stories={stories}
                selected={current?.call_id ?? null}
                bar={bar}
                onSelect={(id) => {
                  setPicked(id);
                  setFollow(false);
                }}
              />
            </section>
            <section className="pl-col think" aria-label="The selected call">
              <header className="col-head">
                <h2>How it thought</h2>
                <label className="follow">
                  <input id="pl-follow" type="checkbox" checked={follow} onChange={(e) => setFollow(e.target.checked)} /> follow the newest call
                </label>
              </header>
              {current ? <CallDetail s={current} bar={bar} /> : <div className="calls-empty">Waiting for the first call.</div>}
            </section>
            <aside className="pl-col pl-side">
              <Scoreboard tally={live.tally} findings={live.findings} title="Today so far (UTC)" />
              <section className="held-box">
                <h2>On lines now</h2>
                <OpenLines lines={live.lines} />
              </section>
            </aside>
          </div>
        </>
      )}

      {tab === "report" && (
        <div className="pl-report">
          <div className="report-pick">
            <label htmlFor="pl-day">Day (UTC)</label>
            <select id="pl-day" value={day} onChange={(e) => { setDay(e.target.value); setOpened(null); }}>
              {dayOptions.map((d) => (
                <option key={d} value={d}>
                  {d}
                  {d === today ? " (today)" : ""}
                </option>
              ))}
            </select>
            <span className="mute">A day runs midnight to midnight UTC. The New York session, 14:30 to 21:00, sits inside it.</span>
          </div>
          {loading && !report && <div className="pl-empty">Grading the day…</div>}
          {report && report.day === day && (
            <div className={`report-wrap${opened ? " with-detail" : ""}`}>
              <ReportView report={report} bar={bar} onOpen={setOpened} selected={opened?.call_id ?? null} />
              {opened && (
                <aside className="report-detail">
                  <button type="button" className="x" onClick={() => setOpened(null)} aria-label="Close the call">
                    ×
                  </button>
                  <CallDetail s={opened} bar={bar} />
                </aside>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
