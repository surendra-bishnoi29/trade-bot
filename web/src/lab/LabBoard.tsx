import { useEffect, useMemo, useState } from "react";
import { CallDetail, CallList } from "./Calls";
import { Filter, FilterBar, matches, NO_FILTER } from "./Filters";
import "./lab.css";
import { AgentFlow, MarketTiles, OpenLines } from "./Overview";
import { Graded, hms } from "./Pieces";
import { ReportView, Scoreboard } from "./Scoreboard";
import { Story, Tile, TileKey } from "./types";
import { useDays, useLive, useReport } from "./useLab";

type Tab = "live" | "report";

/** The predictor at work: what it is shown, how it reasons, what the other agents do with its
 * calls, and how often it turns out right. Live for today; any earlier day on the same screen. */
export function LabBoard() {
  const { live, error, fetchedAt } = useLive();
  const [tab, setTab] = useState<Tab>("live");
  const [filter, setFilter] = useState<TileKey | null>(null);
  const [f, setF] = useState<Filter>(NO_FILTER);
  const [picked, setPicked] = useState<string | null>(null);
  const [pickedSet, setPickedSet] = useState<{ ids: Set<string>; label: string } | null>(null);
  const [follow, setFollow] = useState(true);
  const [now, setNow] = useState(Date.now());
  const days = useDays();
  const today = new Date().toISOString().slice(0, 10);
  const [day, setDay] = useState<string>(today);
  const past = day !== today;
  const { report, loading } = useReport(past || tab === "report" ? day : null);
  const [opened, setOpened] = useState<Story | null>(null);

  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, []);

  const bar = live?.stories.find((s) => s.inputs?.rules)?.inputs.rules?.min_confidence ?? 0.55;
  // what the live tab shows: today from the live feed, an earlier day from its graded report
  const dayReady = !past || (report !== null && report.day === day);
  const dayStories: Story[] = useMemo(() => (past ? (report && report.day === day ? report.stories : []) : (live?.stories ?? [])), [past, report, day, live]);
  const dayTally = past ? report?.tally ?? null : live?.tally ?? null;
  const dayFindings = past ? report?.findings ?? [] : live?.findings ?? [];
  const dayTiles: Tile[] | undefined = useMemo(() => {
    if (!past || !live || !report || report.day !== day) return undefined;
    return live.tiles.map((t) => {
      const c = report.tally.tiles.find((x) => x.key === t.key);
      return c ? { ...t, ...c, on_lines: [], mood: null } : { ...t, on_lines: [] };
    });
  }, [past, live, report, day]);

  const stories = useMemo(() => {
    return dayStories.filter((s) => (filter === null || s.tile === filter) && matches(s, f) && (pickedSet === null || pickedSet.ids.has(s.call_id)));
  }, [dayStories, filter, f, pickedSet]);
  const current = useMemo(() => {
    if (!stories.length) return null;
    if (follow || !picked) return stories[0];
    return stories.find((s) => s.call_id === picked) ?? dayStories.find((s) => s.call_id === picked) ?? stories[0];
  }, [stories, picked, follow, dayStories]);

  const pick = (items: Graded[], label: string) => {
    setPickedSet({ ids: new Set(items.map((g) => g.s.call_id)), label: items[0]?.grade ?? label });
    setF(NO_FILTER);
    setFollow(true);
    setTab("live");
  };
  const chooseDay = (d: string) => {
    setDay(d);
    setOpened(null);
    setPickedSet(null);
  };

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
            {past ? "The day" : "Live"}
          </button>
          <button type="button" role="tab" aria-selected={tab === "report"} className={tab === "report" ? "on" : ""} onClick={() => setTab("report")}>
            Right / wrong report
          </button>
        </div>
        <label className="pl-day">
          <span>Day (UTC)</span>
          <select id="pl-day" value={day} onChange={(e) => chooseDay(e.target.value)}>
            {dayOptions.map((d) => (
              <option key={d} value={d}>
                {d}
                {d === today ? " (today)" : ""}
              </option>
            ))}
          </select>
        </label>
        <span className="pl-rule" title="The predictor only trades a call at or above this confidence">
          trades at confidence ≥ {bar.toFixed(2)}
        </span>
        <span className={`pl-clock${stale || error ? " stale" : ""}`}>
          {error ? `not updating: ${error}` : past ? `${day} · ${report && report.day === day ? (report.final ? "the day is over" : "still running") : "loading…"}` : live ? `as of ${hms(live.as_of)} UTC · refreshes every 10s` : "loading…"}
        </span>
      </header>

      {!live && !error && <div className="pl-empty">Loading the lab's journal…</div>}
      {live && past && !dayReady && <div className="pl-empty">Grading {day}…</div>}

      {live && tab === "live" && dayReady && dayTally && (
        <>
          <MarketTiles live={live} filter={filter} onFilter={setFilter} tiles={dayTiles} stories={dayStories} />
          {past ? <p className="pl-note">Showing {day}. The agents' activity and the open lines are only shown for today; the counts, the calls and the right/wrong grades below are that day's.</p> : <AgentFlow agents={live.agents} now={now} />}
          <FilterBar
            f={f}
            onChange={setF}
            market={filter}
            onMarket={setFilter}
            picked={pickedSet ? { label: pickedSet.label, n: pickedSet.ids.size } : null}
            onClearPicked={() => setPickedSet(null)}
            shown={stories.length}
            total={dayStories.length}
          />
          <div className="pl-main">
            <section className="pl-col list" aria-label="Calls">
              <header className="col-head">
                <h2>Calls</h2>
                <span className="mute">{stories.length === dayStories.length ? "newest first" : `${stories.length} match`}</span>
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
                  <input id="pl-follow" type="checkbox" checked={follow} onChange={(e) => setFollow(e.target.checked)} /> {past ? "newest first" : "follow the newest call"}
                </label>
              </header>
              {current ? <CallDetail s={current} bar={bar} /> : <div className="calls-empty">{dayStories.length ? "No call matches these filters." : "Waiting for the first call."}</div>}
            </section>
            <aside className="pl-col pl-side">
              <Scoreboard tally={dayTally} findings={dayFindings} title={past ? `${day} (UTC)` : "Today so far (UTC)"} stories={dayStories} onPick={pick} />
              {!past && (
                <section className="held-box">
                  <h2>On lines now</h2>
                  <OpenLines lines={live.lines} onOpen={(id) => window.location.assign(`/lines?call=${encodeURIComponent(id)}`)} />
                </section>
              )}
            </aside>
          </div>
        </>
      )}

      {tab === "report" && (
        <div className="pl-report">
          <div className="report-pick">
            <span className="mute">A day runs midnight to midnight UTC. The New York session, 14:30 to 21:00, sits inside it. Change the day at the top.</span>
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
