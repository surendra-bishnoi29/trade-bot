// The lab desk as the api serves it (desk.lab.report): one story per predictor call, graded.

export type Outcome = "unanswered" | "passed" | "held_back" | "not_opened" | "ordering" | "open" | "right" | "wrong";
export type TileKey = "equities" | "oil" | "gold" | "crypto";

export interface Reading {
  label: string;
  value: string;
  verdict: string;
  source?: string;
}

export interface Call {
  direction: "long" | "short" | "none";
  /** The side it would take if it had to; named on every call from prompt version 3. */
  lean?: "long" | "short" | null;
  confidence: number;
  horizon_minutes: number;
  max_loss_pct: number;
  first_profit_pct: number;
  step_pct: number;
  reason: string;
  supports?: string[];
  against?: string[];
  decisive?: string;
  invalidation?: string;
}

export interface Inputs {
  trigger?: { reason: string; detail: string };
  reading?: { side: string; strength: number; score: number; setup: string };
  last?: number;
  day_pct?: number | null;
  evidence?: Reading[];
  levels?: Record<string, number | null>;
  market?: { label: string; verdict?: string; score?: number; evidence?: Reading[]; at?: string };
  news?: { id: string; at: string; title: string }[];
  trail?: { at: string; agent: string; text: string }[];
  rules?: { shorts: boolean; round_trip_pct: number; min_first_profit_pct: number; min_loss_pct: number; min_confidence: number };
}

export interface Paper {
  result: "right" | "wrong" | "pending";
  reason: string;
  realized_pct?: number;
  mark_pct?: number;
  exit?: number;
  exit_at?: string;
  steps: number;
  armed: boolean;
}

export interface LineEvent {
  event: string;
  at: string;
  base: number | null;
  stop: number | null;
  profit_line: number | null;
  last: number | null;
  steps: number | null;
  armed: boolean | null;
}

export interface Story {
  call_id: string;
  at: string;
  symbol: string;
  name: string;
  market: string;
  tile: TileKey;
  trigger: string;
  price: number | null;
  call: Call;
  acted: boolean;
  skipped_because: string;
  objection: string;
  inputs: Inputs;
  record: { n?: number; hits?: number; hit?: number; avg_pct?: number };
  seconds: number | null;
  tokens: { input?: number; output?: number };
  model: string;
  order?: { side: string; quantity: number | null; stop_price: number | null };
  risk: { decision: string; reason: string; passed: number; rules: number; failed: string[]; at: string } | null;
  fill: { price: number | null; quantity: number | null; at: string } | null;
  lines: LineEvent[];
  score: { hit: boolean | null; realized_pct: number | null; reason: string; how: string; entry: number | null; exit: number | null; closed_at: string } | null;
  paper: Paper | null;
  after: { up_pct: number; down_pct: number; end_pct: number; minutes: number } | null;
  outcome: Outcome;
}

export interface Counts {
  calls: number;
  passed: number;
  directional: number;
  traded: number;
  open: number;
  right: number;
  wrong: number;
  not_opened: number;
  sum_pct: number;
  hit_rate: number | null;
  avg_pct: number | null;
  paper_right: number;
  paper_wrong: number;
  paper_pending: number;
  paper_hit_rate: number | null;
  paper_avg_pct: number | null;
  missed_moves: number;
  unanswered: number;
  lean_right: number;
  lean_wrong: number;
  lean_pending: number;
  lean_hit_rate: number | null;
  lean_avg_pct: number | null;
  all_right: number;
  all_wrong: number;
  all_hit_rate: number | null;
}

export interface Tally {
  total: Counts;
  tiles: (Counts & { key: TileKey; label: string })[];
  directions: { long: Counts; short: Counts };
  exits: { reason: string; how: string; n: number; right: number; avg_pct: number | null }[];
  confidence: { lo: number; hi: number; right: number; wrong: number; traded: number; hit_rate: number | null }[];
}

export interface Tile extends Counts {
  key: TileKey;
  label: string;
  market: string;
  is_traded: boolean;
  is_open: boolean;
  instruments: number;
  symbols: string[];
  on_lines: string[];
  mood: { label: string; verdict?: string; score?: number; at?: string } | null;
  shorts: boolean;
}

export interface Agent {
  key: string;
  name: string;
  role: string;
  count: number;
  last_at: string | null;
  state: "working" | "quiet";
  line: string;
}

export interface OpenLine {
  symbol: string;
  name: string;
  tile: TileKey;
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
  at: string;
}

export interface Live {
  lab: boolean;
  as_of: string;
  tiles: Tile[];
  agents: Agent[];
  stories: Story[];
  lines: OpenLine[];
  tally: Tally;
  findings: string[];
  next_open: string | null;
  nyse_open: boolean;
}

export interface DayReport {
  lab: boolean;
  day: string;
  as_of: string;
  final: boolean;
  nyse_session: boolean;
  nyse_closed: boolean;
  tally: Tally;
  findings: string[];
  stories: Story[];
}

export const OUTCOME: Record<Outcome, { word: string; tone: string; what: string }> = {
  unanswered: { word: "NOT LOOKED AT", tone: "mute", what: "The predictor never looked at this trigger: the hourly call budget was spent, or the model gave no answer. It is not a pass." },
  passed: { word: "PASSED", tone: "mute", what: "The predictor saw no side with better than even odds, so the desk did not trade. The side it leaned to is still graded on paper." },
  held_back: { word: "HELD BACK", tone: "amber", what: "It named a direction, but the call did not clear the bar, so no order was sent." },
  not_opened: { word: "NOT OPENED", tone: "amber", what: "An order was decided but never became a position." },
  ordering: { word: "ORDERING", tone: "held", what: "The order is on its way to the broker." },
  open: { word: "ON LINES", tone: "held", what: "The position is open and the lines engine is holding it." },
  right: { word: "RIGHT", tone: "bull", what: "The position closed with a gain after costs." },
  wrong: { word: "WRONG", tone: "bear", what: "The position closed with a loss after costs." },
};

/** One position the lines engine held, for the list behind the charts. */
export interface TradeRow {
  call_id: string;
  symbol: string;
  name: string;
  tile: TileKey;
  market: string;
  direction: "long" | "short";
  opened_at: string;
  closed_at: string | null;
  base: number | null;
  quantity: number | null;
  armed: boolean;
  steps: number;
  moves: number;
  open: boolean;
  reason: string;
  how: string;
  realized_pct: number | null;
  hit: boolean | null;
}

export interface ChartBar {
  ts: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export interface LineMove {
  at: string;
  event: string;
  base: number | null;
  stop: number | null;
  profit_line: number | null;
  last: number | null;
  steps: number;
  armed: boolean;
  quantity: number | null;
  step_pct?: number;
}

/** One position with the minute bars it lived through and every move of its lines. */
export interface TradeChartData {
  lab: boolean;
  call_id: string;
  symbol: string;
  name: string;
  tile: TileKey;
  market: string;
  direction: "long" | "short";
  quantity: number | null;
  opened_at: string;
  closed_at: string | null;
  open: boolean;
  as_of: string;
  call: Partial<Call>;
  trigger: string;
  confidence: number | null;
  lines: LineMove[];
  score: { hit: boolean | null; reason: string; how: string; realized_pct: number | null; entry: number | null; exit: number | null } | null;
  bars: ChartBar[];
}
