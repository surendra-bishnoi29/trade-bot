import { Report } from "../api";
import { ago } from "../format";

const ANALYST: Record<string, string> = {
  quant_analyst: "Quant analyst",
  regime: "Regime strategist",
  sector: "Sector specialist",
  news_analyst: "News analyst",
  filings: "Filing analyst",
  macro: "Macro strategist",
  fundamentals: "Fundamentals & events",
  head_trader: "Head trader",
};

function Stance({ r }: { r: Report }) {
  if (!r.stance || r.stance === "none") return null;
  return (
    <span className={`tag stance-${r.stance}`}>
      {r.stance}
      {typeof r.conviction === "number" ? ` ${r.conviction.toFixed(2)}` : ""}
    </span>
  );
}

export function Research({ reports, now }: { reports: Report[] | null; now: number }) {
  if (!reports) return <section className="card"><p className="muted">loading research…</p></section>;
  const regime = reports.find((r) => r.kind === "regime");
  const macro = reports.find((r) => r.kind === "macro");
  const sector = reports.find((r) => r.kind === "sector");
  const bySymbol = new Map<string, Report[]>();
  for (const r of reports) {
    if (!r.symbol || r.kind === "morning_note") continue;
    bySymbol.set(r.symbol, [...(bySymbol.get(r.symbol) ?? []), r]);
  }
  const symbols = [...bySymbol.keys()].sort((a, b) => {
    const ra = bySymbol.get(a)!.find((r) => r.kind === "technical")?.data?.rank ?? 999;
    const rb = bySymbol.get(b)!.find((r) => r.kind === "technical")?.data?.rank ?? 999;
    return ra - rb;
  });
  const cell = (r: Report | undefined) =>
    r ? (
      <>
        <Stance r={r} /> {r.summary}
        <div className="muted small">{ANALYST[r.analyst ?? ""] ?? r.analyst} · {ago(r.at, now)}</div>
      </>
    ) : (
      <span className="muted">–</span>
    );
  return (
    <section className="card">
      <header className="card-head">
        <h2>Research</h2>
        <span className="muted">each analyst's latest view, the head trader reads these every cycle</span>
      </header>
      <div className="research-top">
        <div className="research-block">
          <b>Market regime</b>
          {regime ? (
            <p>
              <Stance r={regime} /> {regime.summary} <span className="muted small">{ago(regime.at, now)}</span>
            </p>
          ) : (
            <p className="muted">No read yet.</p>
          )}
        </div>
        <div className="research-block">
          <b>Macro</b>
          {macro ? (
            <p>
              <Stance r={macro} /> {macro.summary} <span className="muted small">{ago(macro.at, now)}</span>
            </p>
          ) : (
            <p className="muted">Needs a FRED key.</p>
          )}
        </div>
        <div className="research-block">
          <b>Sectors</b>
          {sector ? (
            <p>
              {sector.summary} <span className="muted small">{ago(sector.at, now)}</span>
            </p>
          ) : (
            <p className="muted">No read yet.</p>
          )}
        </div>
      </div>
      {symbols.length === 0 ? (
        <p className="muted">No per-name reports yet.</p>
      ) : (
        <div className="table-wrap">
          <table className="research">
            <thead>
              <tr>
                <th>Name</th>
                <th>Technical read</th>
                <th>News</th>
                <th>Filings</th>
                <th>Fundamentals</th>
                <th>Events</th>
              </tr>
            </thead>
            <tbody>
              {symbols.map((s) => {
                const rs = bySymbol.get(s)!;
                return (
                  <tr key={s}>
                    <td>
                      <b>{s}</b>
                    </td>
                    <td>{cell(rs.find((r) => r.kind === "technical"))}</td>
                    <td>{cell(rs.find((r) => r.kind === "news"))}</td>
                    <td>{cell(rs.find((r) => r.kind === "filing"))}</td>
                    <td>{cell(rs.find((r) => r.kind === "fundamental"))}</td>
                    <td>{cell(rs.find((r) => r.kind === "event"))}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
