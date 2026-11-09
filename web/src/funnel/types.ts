// The funnel as the api serves it (desk.funnel.build). One build is one moment of one market.

export type Side = "bull" | "bear";
export type Verdict = "bullish" | "bearish" | "neutral";
export type CheckKey = "watcher" | "quant" | "news" | "head" | "risk";

export const CHECKS: { key: CheckKey; label: string; what: string }[] = [
  { key: "watcher", label: "Watcher", what: "unusual move or volume today" },
  { key: "quant", label: "Quant analyst", what: "multi-day trend agrees" },
  { key: "news", label: "News analyst", what: "news tone agrees" },
  { key: "head", label: "Head trader", what: "proposed or holds it" },
  { key: "risk", label: "Risk officer", what: "approved, or already held" },
];

/** On the lab desk the predictor stands where the head trader stands on the main desk. */
export const LAB_CHECKS: typeof CHECKS = CHECKS.map((c) =>
  c.key === "head"
    ? {
        key: "head",
        label: "Predictor",
        what: "called it today, or it is on lines",
      }
    : c.key === "risk"
      ? { ...c, what: "approved the order, or it is on lines" }
      : c,
);

export const checksFor = (lab: boolean | undefined) =>
  lab ? LAB_CHECKS : CHECKS;

export interface Evidence {
  label: string;
  value: string;
  verdict: Verdict;
  source: string;
}

export interface Levels {
  last: number;
  s1: number | null;
  s2: number | null;
  r1: number | null;
  r2: number | null;
  zone: [number, number];
  vwap: number;
  prior_close: number;
  entry?: number | null;
  stop?: number;
  target?: number;
  rr: number | null;
}

export interface DeskState {
  thesis?: {
    narrative: string;
    conviction: number;
    horizon_days: number;
    quantity: number;
    entry: number | null;
    stop: number | null;
    target: number | null;
    status: string;
    created_at: string;
  };
  position?: {
    qty: number;
    avg_entry: number | null;
    unrealized_pl: number | null;
  };
  decision?: {
    action: string;
    conviction: number;
    thesis: string;
    at: string;
    trigger: string;
    objection: { severity: string; argument: string } | null;
  };
  risk?: {
    decision: string;
    reason: string;
    passed: number;
    rules: number;
    at: string;
  };
  fill?: { side: string; qty: number; price: number; at: string };
}

export interface FunnelSymbol {
  symbol: string;
  name: string;
  group: string;
  side: Side;
  score: number;
  strength: number;
  rank: number;
  day_pct: number | null;
  open_pct: number | null;
  vs_avg20_pct: number | null;
  last: number;
  last_at: string;
  path: (number | null)[];
  profile: number[];
  checks: Record<CheckKey, boolean>;
  checks_passed: number;
  setup: string;
  book: { tag: string; reasons: string[] };
  held: boolean;
  desk: DeskState;
  levels: Levels;
  evidence: Evidence[];
  alerts: Record<string, number>;
  alerts_total: number;
  /** The lab desk: the predictor's record on this instrument, the open lines, its latest call. */
  record?: LabRecord | null;
  lines?: LabLines | null;
  call?: LabCall | null;
}

export interface LabRecord {
  n: number;
  hits: number;
  hit: number;
  avg_pct: number;
}

export interface LabLines {
  call_id: string;
  direction: string;
  base: number;
  stop: number;
  profit_line: number | null;
  step_pct: number;
  armed: boolean;
  steps: number;
  event: string;
  last: number;
  quantity: number;
  opened_at: string | null;
  at: string;
}

export interface LabCall {
  call_id: string;
  trigger: string;
  call: {
    direction: string;
    confidence: number;
    horizon_minutes: number;
    max_loss_pct: number;
    first_profit_pct: number;
    step_pct: number;
    reason: string;
  };
  price: number;
  acted: boolean;
  skipped_because: string;
  objection: string;
  at: string;
}

export interface Funnel {
  market: string;
  label: string;
  traded: boolean;
  note: string;
  session: {
    day: string;
    open: string;
    close: string;
    as_of: string;
    is_live: boolean;
    step_minutes: number;
  };
  times: string[];
  benchmark: {
    symbol: string;
    name: string;
    day_pct: number | null;
    last: number | null;
    path: (number | null)[];
  };
  sentiment: {
    score: number;
    verdict: "BULLISH" | "BEARISH" | "MIXED";
    evidence: Evidence[];
  };
  breadth: {
    bull: number;
    bear: number;
    flat: number;
    bins: { lo: number; hi: number; n: number }[];
  };
  wire: { at: string; agent: string; symbol: string | null; text: string }[];
  symbols: FunnelSymbol[];
  /** True when this is the lab desk's market: the predictor decides and the lines hold. */
  lab?: boolean;
}

export interface MarketInfo {
  key: string;
  label: string;
  traded: boolean;
  note: string;
  instruments: number;
  last_bar: string | null;
  has_data: boolean;
  days: string[];
}

export interface Candle {
  t: string;
  o: number;
  h: number;
  l: number;
  c: number;
  v: number;
}

export interface Detail {
  symbol: string;
  candles: Candle[];
  trail: { at: string; agent: string; type: string; text: string }[];
}

/** How many cards each column shows per side: the funnel narrows left to right. */
export const COLUMNS = [8, 6, 4, 3, 3] as const;
