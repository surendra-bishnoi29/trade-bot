import { AnalystScores, Graduation, Postmortem, WeeklyReview } from "../api";
import { dateUTC } from "../format";
import { Markdown } from "./Markdown";

function pct(v: number | null | undefined, digits = 1): string {
  return typeof v === "number" ? `${v >= 0 ? "+" : ""}${v.toFixed(digits)}%` : "n/a";
}

export function Reviews({
  postmortems,
  scores,
  weekly,
  graduation,
}: {
  postmortems: Postmortem[];
  scores: AnalystScores | null;
  weekly: WeeklyReview | null;
  graduation: Graduation | null;
}) {
  const weights = scores ? Object.entries(scores.weights).sort((a, b) => b[1].weight - a[1].weight) : [];
  return (
    <div className="reviews">
      <section className="card">
        <header className="card-head">
          <h2>Graduation checklist</h2>
          <span className="muted">
            {graduation
              ? graduation.ready
                ? "every criterion passes: ready to discuss live trading with an approval gate"
                : `${graduation.failing.length} failing, ${graduation.pending.length} pending; ${graduation.sessions} sessions so far`
              : "computed from the journal"}
          </span>
        </header>
        {graduation ? (
          <ul className="checklist">
            {graduation.criteria.map((c) => (
              <li key={c.key} className={`crit-${c.status}`}>
                <span className={`tag standing-${c.status === "pass" ? "trusted" : c.status === "fail" ? "doubted" : "neutral"}`}>{c.status}</span>{" "}
                <b>{c.title}</b>: {c.value} <span className="muted">(target {c.target}{c.detail ? `; ${c.detail}` : ""})</span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="muted">Loading…</p>
        )}
      </section>
      <section className="card">
        <header className="card-head">
          <h2>Weekly review</h2>
          <span className="muted">{weekly ? `week ending ${weekly.week_ending}` : "written by the reviewer every Friday after the close"}</span>
        </header>
        {weekly ? <Markdown text={weekly.markdown} /> : <p className="muted">No weekly review yet. The first one is written on Friday after the close.</p>}
      </section>
      <section className="card">
        <header className="card-head">
          <h2>Trade post-mortems</h2>
          <span className="muted">every closed thesis, graded by the reviewer after the close</span>
        </header>
        {postmortems.length === 0 ? (
          <p className="muted">No closed theses yet. The first post-mortem is written after the first exit.</p>
        ) : (
          <div className="table-wrap">
            <table className="research">
              <thead>
                <tr>
                  <th>Closed</th>
                  <th>Name</th>
                  <th>Exit</th>
                  <th>Realized</th>
                  <th>Expected</th>
                  <th>Best / worst</th>
                  <th>Verdict</th>
                </tr>
              </thead>
              <tbody>
                {postmortems.map((p) => (
                  <tr key={p.thesis_id}>
                    <td>{dateUTC(p.closed_at)}</td>
                    <td>
                      <b>{p.symbol}</b> <span className="muted">{p.direction}</span>
                    </td>
                    <td>
                      <span className={`tag exit-${p.exit_kind}`}>{p.exit_kind}</span>
                    </td>
                    <td className={typeof p.realized_pct === "number" && p.realized_pct < 0 ? "neg" : "pos"}>{pct(p.realized_pct, 2)}</td>
                    <td>{pct(p.expected_pct)}</td>
                    <td>
                      {pct(p.mfe_pct)} / {pct(p.mae_pct)}
                    </td>
                    <td>
                      <div>{p.verdict}</div>
                      {p.lesson && (
                        <div className="muted">
                          Lesson{p.process_sound === false ? " (process flawed)" : p.process_sound ? " (process sound)" : ""}: {p.lesson}
                        </div>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
      <section className="card">
        <header className="card-head">
          <h2>Analyst scoreboard</h2>
          <span className="muted">
            {scores ? `${scores.calls} calls over ${scores.days} days, scored against what prices did` : "scored weekly by the reviewer"}
          </span>
        </header>
        {weights.length === 0 ? (
          <p className="muted">No scores yet. The reviewer needs a week of calls and the sessions after them.</p>
        ) : (
          <div className="table-wrap">
            <table className="research">
              <thead>
                <tr>
                  <th>Analyst / kind</th>
                  <th>Hit rate (5 sessions)</th>
                  <th>Scored calls</th>
                  <th>Weight</th>
                  <th>Standing</th>
                </tr>
              </thead>
              <tbody>
                {weights.map(([key, w]) => (
                  <tr key={key}>
                    <td>{key}</td>
                    <td>{typeof w.hit_5d === "number" ? `${(w.hit_5d * 100).toFixed(0)}%` : "n/a"}</td>
                    <td>{w.n}</td>
                    <td>{w.weight.toFixed(2)}</td>
                    <td>
                      <span className={`tag standing-${w.status}`}>{w.status}</span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}
