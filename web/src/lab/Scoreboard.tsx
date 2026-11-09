import { pct } from "../funnel/format";
import { Confidence, Direction, hms, OutcomePill, rate, RightWrong } from "./Pieces";
import { Counts, DayReport, Story, Tally } from "./types";

const signed = (v: number | null | undefined): string => (v === null || v === undefined ? "n/a" : pct(v));

/** Right and wrong, counted three ways: real trades, untraded calls checked on paper, and passes. */
export function Scoreboard({ tally, findings, title }: { tally: Tally; findings: string[]; title: string }) {
  const t = tally.total;
  const done = t.right + t.wrong;
  const paper = t.paper_right + t.paper_wrong;
  const leans = t.lean_right + t.lean_wrong;
  return (
    <section className="score" aria-label={title}>
      <h2>{title}</h2>
      <div className="score-big">
        <div className="bull">
          <b>{t.all_right}</b>
          <span>right</span>
        </div>
        <div className="bear">
          <b>{t.all_wrong}</b>
          <span>wrong</span>
        </div>
        <div>
          <b>{rate(t.all_hit_rate)}</b>
          <span>right, of {t.all_right + t.all_wrong} graded</span>
        </div>
      </div>
      <RightWrong c={t} height={14} />
      <p className="score-key">
        <i className="k bull" /> traded <i className="k bull paper" /> on paper <i className="k bear paper" /> on paper <i className="k bear" /> traded
      </p>
      <dl className="score-rows">
        <div>
          <dt>Real trades, closed</dt>
          <dd>
            {done ? (
              <>
                {t.right} right, {t.wrong} wrong · {rate(t.hit_rate)} · <b className={(t.avg_pct ?? 0) >= 0 ? "bull" : "bear"}>{signed(t.avg_pct)}</b> a trade after costs
              </>
            ) : (
              "none yet"
            )}
          </dd>
        </div>
        <div>
          <dt>Open on lines</dt>
          <dd>{t.open}</dd>
        </div>
        <div>
          <dt title="Calls that named a direction but were not traded, graded by running the same lines over the real prices that followed">Named a side, not traded</dt>
          <dd>
            {paper || t.paper_pending ? (
              <>
                {t.paper_right} right, {t.paper_wrong} wrong on paper
                {paper ? ` · ${rate(t.paper_hit_rate)} · ${signed(t.paper_avg_pct)} each` : ""}
                {t.paper_pending ? ` · ${t.paper_pending} still running` : ""}
              </>
            ) : (
              "none yet"
            )}
          </dd>
        </div>
        <div>
          <dt>Passes</dt>
          <dd>
            {t.passed}
            {t.passed ? ` · ${t.missed_moves} followed by a move worth trading` : ""}
          </dd>
        </div>
        <div>
          <dt title="On a pass the predictor still names the side it would take. That side is graded by running its lines over the real prices that followed.">The side it leaned to on passes</dt>
          <dd>
            {leans || t.lean_pending ? (
              <>
                {t.lean_right} right, {t.lean_wrong} wrong on paper
                {leans ? ` · ${rate(t.lean_hit_rate)} · ${signed(t.lean_avg_pct)} each` : ""}
                {t.lean_pending ? ` · ${t.lean_pending} still running` : ""}
              </>
            ) : (
              "none graded yet"
            )}
          </dd>
        </div>
        {t.unanswered > 0 && (
          <div>
            <dt title="Triggers the predictor never looked at because the hourly call budget was spent. They are not passes.">Not looked at</dt>
            <dd>{t.unanswered} · the hourly call budget was spent</dd>
          </div>
        )}
      </dl>
      <ul className="findings">
        {findings.map((f, i) => (
          <li key={i}>{f}</li>
        ))}
      </ul>
    </section>
  );
}

function CountsRow({ label, c }: { label: string; c: Counts }) {
  return (
    <tr>
      <th scope="row">{label}</th>
      <td>{c.calls}</td>
      <td>{c.passed}</td>
      <td>{c.traded}</td>
      <td className="bull">{c.right}</td>
      <td className="bear">{c.wrong}</td>
      <td>{rate(c.hit_rate)}</td>
      <td className={(c.avg_pct ?? 0) >= 0 ? "bull" : "bear"}>{signed(c.avg_pct)}</td>
      <td className="bull">{c.paper_right}</td>
      <td className="bear">{c.paper_wrong}</td>
      <td>
        <RightWrong c={c} />
      </td>
    </tr>
  );
}

/** Was the predictor's confidence worth anything? Hit rate by how sure it said it was. */
export function Calibration({ tally, bar }: { tally: Tally; bar: number }) {
  const any = tally.confidence.some((b) => b.right + b.wrong > 0);
  return (
    <div className="calib">
      <h3>Did higher confidence mean more often right?</h3>
      {!any && <p className="mute">No graded directional calls yet. Each band fills as calls are graded, traded or on paper.</p>}
      <ul>
        {tally.confidence.map((b) => {
          const n = b.right + b.wrong;
          return (
            <li key={b.lo} className={b.lo >= bar ? "tradable" : ""}>
              <span className="band">
                {b.lo.toFixed(1)} to {b.hi.toFixed(1)}
                {b.lo >= bar ? " · traded" : ""}
              </span>
              <span className="band-bar">
                {n > 0 && <i style={{ width: `${(b.right / n) * 100}%` }} />}
                <em style={{ left: "55%" }} title="the 55% bar" />
              </span>
              <span className="band-n">{n ? `${rate(b.hit_rate)} of ${n}` : "none"}</span>
            </li>
          );
        })}
      </ul>
    </div>
  );
}

export function Breakdown({ tally }: { tally: Tally }) {
  return (
    <div className="tbl">
      <table>
        <thead>
          <tr>
            <th />
            <th>calls</th>
            <th>passed</th>
            <th>traded</th>
            <th>right</th>
            <th>wrong</th>
            <th>hit rate</th>
            <th>avg after costs</th>
            <th title="untraded directional calls graded on paper">paper right</th>
            <th>paper wrong</th>
            <th>right / wrong</th>
          </tr>
        </thead>
        <tbody>
          {tally.tiles.map((t) => (
            <CountsRow key={t.key} label={t.label} c={t} />
          ))}
          <CountsRow label="Long (buy)" c={tally.directions.long} />
          <CountsRow label="Short (sell)" c={tally.directions.short} />
          <CountsRow label="All" c={tally.total} />
        </tbody>
      </table>
    </div>
  );
}

/** One UTC day, graded: the numbers, the findings in sentences, and every call. */
export function ReportView({ report, bar, onOpen, selected }: { report: DayReport; bar: number; onOpen: (s: Story) => void; selected: string | null }) {
  const t = report.tally;
  return (
    <div className="report">
      <header className="report-head">
        <h2>{report.day}</h2>
        <span className={`pill ${report.final ? "bull" : "amber"}`}>{report.final ? "FINAL" : "STILL RUNNING"}</span>
        <span className="mute">
          {report.nyse_session ? (report.nyse_closed ? "New York session closed (21:00 UTC): stock, oil and gold numbers are final." : "New York session not yet closed: stock, oil and gold numbers will move.") : "No New York session on this day: crypto only."}
          {!report.final && " Crypto runs to midnight UTC."}
        </span>
      </header>
      <div className="report-grid">
        <Scoreboard tally={t} findings={report.findings} title="Right and wrong" />
        <div className="report-side">
          <Calibration tally={t} bar={bar} />
          <div className="exits">
            <h3>How the real trades ended</h3>
            {t.exits.length === 0 ? (
              <p className="mute">No trade has closed on this day.</p>
            ) : (
              <ul>
                {t.exits.map((e) => (
                  <li key={e.reason}>
                    <b>{e.n}</b> {e.how} · {e.right} right · <span className={(e.avg_pct ?? 0) >= 0 ? "bull" : "bear"}>{signed(e.avg_pct)}</span> each
                  </li>
                ))}
              </ul>
            )}
          </div>
        </div>
      </div>
      <h3 className="report-h">By market and direction</h3>
      <Breakdown tally={t} />
      <h3 className="report-h">Every call ({report.stories.length})</h3>
      <div className="tbl">
        <table className="calls-table">
          <thead>
            <tr>
              <th>time UTC</th>
              <th>instrument</th>
              <th>call</th>
              <th>confidence</th>
              <th>result</th>
              <th>what triggered it</th>
            </tr>
          </thead>
          <tbody>
            {report.stories.map((s) => (
              <tr key={s.call_id} className={selected === s.call_id ? "on" : ""} onClick={() => onOpen(s)} tabIndex={0} onKeyDown={(e) => e.key === "Enter" && onOpen(s)}>
                <td className="mono">{hms(s.at)}</td>
                <td>
                  <b>{s.symbol}</b>
                </td>
                <td>
                  <Direction call={s.call} />
                </td>
                <td>
                  <Confidence value={s.call.confidence} bar={bar} />
                </td>
                <td>
                  <OutcomePill s={s} />
                </td>
                <td className="why">{s.trigger}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
