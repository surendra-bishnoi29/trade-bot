// The api client: same-origin JSON over the session cookie.

export type Role = "operator" | "viewer";
export interface User {
  name: string;
  role: Role;
}

export interface Thesis {
  thesis_id: string;
  status: string;
  conviction: number;
  stop: number | null;
  target: number | null;
  entry: number | null;
  horizon_days: number;
  created_at: string;
  to_stop_pct: number | null;
  to_target_pct: number | null;
  narrative: string;
}

export interface Position {
  symbol: string;
  qty: number;
  avg_entry: number | null;
  price: number | null;
  price_at: string | null;
  market_value: number | null;
  unrealized_pl: number | null;
  unrealized_pct: number | null;
  thesis: Thesis | null;
}

export interface Book {
  as_of: string | null;
  broker: string | null;
  equity: number | null;
  cash: number | null;
  buying_power: number | null;
  gross_exposure: number;
  positions: Position[];
  open_theses: any[];
}

export interface Health {
  now: string;
  market_open: boolean;
  next_open: string;
  bot: {
    alive: boolean;
    last_heartbeat: string | null;
    status: string | null;
    next_cycle: string | null;
    open_theses: number | null;
    started_at: string | null;
    gap_seconds: number | null;
  };
  feed: { last_bar: string | null; age_seconds: number | null };
  kill_switch: { engaged: boolean; reason: string; since: string | null };
  components: { component: string; status: string; detail: string; at: string }[];
  live_feed?: { connected: boolean; last_event_at: string | null; clients: number };
}

export interface Evidence {
  id: string;
  kind: "news" | "event";
  title: string | null;
  source: string | null;
  url: string | null;
  time: string | null;
  impact: string | null;
  polarity: number | null;
}

export interface Trade {
  trade_id: string;
  time: string;
  symbol: string;
  side: string;
  quantity: number;
  price: number | null;
  notional: number | null;
  book: string | null;
  what: string;
  when: { time: string; trigger: string; cycle_id: string | null };
  why: string;
  evidence: Evidence[];
  exit_plan: {
    stop: number | null;
    target: number | null;
    stop_pct: number | null;
    target_pct: number | null;
    horizon_days: number | null;
    invalidation: string[];
  } | null;
  skeptic: { severity: string; argument: string } | null;
  risk: { decision: string; reason: string; rules: { rule: string; passed: boolean; detail: string }[] } | null;
  thesis: {
    thesis_id: string;
    status: string;
    conviction: number | null;
    expected_return_pct: number | null;
    realized_pnl: number | null;
    closed_at: string | null;
    reason: string | null;
  } | null;
  plan: { trigger: string; market_view: string | null; risk_note: string | null; at: string } | null;
}

export interface Cycle {
  cycle_id: string;
  time: string;
  trigger: string;
  dry_run: boolean;
  market_view: string | null;
  risk_note: string | null;
  decisions: any[];
  challenge: { overall: string; objections: { symbol: string; severity: string; argument: string }[] } | null;
  actions: any[];
  result: string | null;
}

export interface JournalItem {
  id: string;
  time: string;
  type: string;
  symbol: string | null;
  source: string;
  text: string;
}

export interface ChatPair {
  request_id: string;
  user: string;
  question: string;
  asked_at: string;
  answer: string | null;
  answered_at: string | null;
}

export interface Bar {
  ts: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const r = await fetch(path, { credentials: "same-origin", ...init });
  if (!r.ok) {
    let detail = r.statusText;
    try {
      const body = await r.json();
      detail = body.detail ?? detail;
    } catch {
      /* no body */
    }
    throw new ApiError(r.status, String(detail));
  }
  return (await r.json()) as T;
}

export const api = {
  get: <T>(path: string) => request<T>(path),
  post: <T>(path: string, body: unknown) =>
    request<T>(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }),
};
