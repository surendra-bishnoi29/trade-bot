import { Story, TileKey } from "./types";

export type Grade = "any" | "right" | "wrong" | "pending";
export type How = "any" | "traded" | "paper" | "passed";
export type Dir = "any" | "long" | "short";

export interface Filter {
  grade: Grade;
  how: How;
  dir: Dir;
  cmin: number;
  cmax: number;
}

export const NO_FILTER: Filter = { grade: "any", how: "any", dir: "any", cmin: 0, cmax: 1 };

/** How a call was handled: traded for real, checked on paper, or passed (its lean checked on paper). */
export function howOf(s: Story): How | "unanswered" {
  if (s.outcome === "unanswered") return "unanswered";
  if (s.outcome === "passed") return "passed";
  if (s.outcome === "held_back" || s.outcome === "not_opened") return "paper";
  return "traded";
}

/** Right, wrong, or not decided yet, whether traded or on paper. */
export function gradeOf(s: Story): Grade {
  if (s.outcome === "right" || s.outcome === "wrong") return s.outcome;
  if (s.paper && s.paper.result !== "pending") return s.paper.result;
  return "pending";
}

/** The side the call took, or leaned to. */
export function sideOf(s: Story): "long" | "short" | null {
  if (s.call.direction !== "none") return s.call.direction;
  return s.call.lean ?? null;
}

export function matches(s: Story, f: Filter): boolean {
  if (f.grade !== "any" && gradeOf(s) !== f.grade) return false;
  if (f.how !== "any" && howOf(s) !== f.how) return false;
  if (f.dir !== "any" && sideOf(s) !== f.dir) return false;
  const c = s.call.confidence;
  if (c < f.cmin - 1e-9 || c > f.cmax + 1e-9) return false;
  return true;
}

export function isDefault(f: Filter): boolean {
  return f.grade === "any" && f.how === "any" && f.dir === "any" && f.cmin <= 0 && f.cmax >= 1;
}

const TILE_WORD: Record<TileKey, string> = { equities: "US equities", oil: "Oil and energy", gold: "Gold and metals", crypto: "Crypto" };

function Group<T extends string>({ label, value, options, onChange }: { label: string; value: T; options: [T, string][]; onChange: (v: T) => void }) {
  return (
    <div className="fgroup" role="group" aria-label={label}>
      <span className="flabel">{label}</span>
      <div className="seg">
        {options.map(([k, word]) => (
          <button key={k} type="button" className={value === k ? "on" : ""} onClick={() => onChange(k)} aria-pressed={value === k}>
            {word}
          </button>
        ))}
      </div>
    </div>
  );
}

/** Every way to narrow the calls list, on one bar, with one button that clears it all. */
export function FilterBar({
  f,
  onChange,
  market,
  onMarket,
  picked,
  onClearPicked,
  shown,
  total,
}: {
  f: Filter;
  onChange: (f: Filter) => void;
  market: TileKey | null;
  onMarket: (k: TileKey | null) => void;
  picked: { label: string; n: number } | null;
  onClearPicked: () => void;
  shown: number;
  total: number;
}) {
  const active = !isDefault(f) || market !== null || picked !== null;
  const set = (patch: Partial<Filter>) => onChange({ ...f, ...patch });
  return (
    <div className="pl-filters" aria-label="Filters">
      <Group<Grade>
        label="Result"
        value={f.grade}
        options={[
          ["any", "any"],
          ["right", "right"],
          ["wrong", "wrong"],
          ["pending", "not decided yet"],
        ]}
        onChange={(grade) => set({ grade })}
      />
      <Group<How>
        label="How"
        value={f.how}
        options={[
          ["any", "any"],
          ["traded", "traded"],
          ["paper", "on paper"],
          ["passed", "passed"],
        ]}
        onChange={(how) => set({ how })}
      />
      <Group<Dir>
        label="Side"
        value={f.dir}
        options={[
          ["any", "any"],
          ["long", "long"],
          ["short", "short"],
        ]}
        onChange={(dir) => set({ dir })}
      />
      <div className="fgroup conf" role="group" aria-label="Confidence">
        <span className="flabel">Confidence</span>
        <label>
          <span>from</span>
          <input id="f-cmin" type="range" min={0} max={1} step={0.05} value={f.cmin} onChange={(e) => set({ cmin: Math.min(Number(e.target.value), f.cmax) })} aria-label="lowest confidence" />
          <b>{f.cmin.toFixed(2)}</b>
        </label>
        <label>
          <span>to</span>
          <input id="f-cmax" type="range" min={0} max={1} step={0.05} value={f.cmax} onChange={(e) => set({ cmax: Math.max(Number(e.target.value), f.cmin) })} aria-label="highest confidence" />
          <b>{f.cmax.toFixed(2)}</b>
        </label>
      </div>
      {market && (
        <button type="button" className="fchip" onClick={() => onMarket(null)} title="Remove the market filter">
          {TILE_WORD[market]} <i>×</i>
        </button>
      )}
      {picked && (
        <button type="button" className="fchip amber" onClick={onClearPicked} title="Stop showing only the calls behind the number you clicked">
          {picked.n} {picked.label} <i>×</i>
        </button>
      )}
      <span className="fcount">
        {shown} of {total}
      </span>
      <button
        type="button"
        className={`fclear${active ? " on" : ""}`}
        disabled={!active}
        onClick={() => {
          onChange(NO_FILTER);
          onMarket(null);
          onClearPicked();
        }}
      >
        clear filters
      </button>
    </div>
  );
}
