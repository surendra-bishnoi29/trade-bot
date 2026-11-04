// The team: which agent does what, and how to read one of its events in a few words.
import { LiveMessage } from "./live";

export interface AgentInfo {
  key: string;
  label: string;
  role: string;
}

export const TEAM: AgentInfo[] = [
  { key: "head_trader", label: "Head trader", role: "plans the book, answers questions" },
  { key: "cycle", label: "Swing cycle", role: "scorecard, plan, challenge, orders" },
  { key: "quant_analyst", label: "Quant analyst", role: "scorecard and a technical read per name" },
  { key: "regime", label: "Regime strategist", role: "trend, breadth and volatility of the tape" },
  { key: "sector", label: "Sector specialist", role: "leaders and laggards across sectors" },
  { key: "news_analyst", label: "News analyst", role: "tone and themes per name from the scored news" },
  { key: "filings", label: "Filing analyst", role: "new 8-K, 10-Q and 10-K filings from EDGAR" },
  { key: "macro", label: "Macro strategist", role: "rates, curve, VIX, dollar, oil, releases (FRED)" },
  { key: "fundamentals", label: "Fundamentals & events", role: "valuation, growth, margins, earnings dates" },
  { key: "scheduler", label: "Scheduler", role: "pre-market, mid-session, post-close" },
  { key: "risk", label: "Risk officer", role: "approves or rejects every order" },
  { key: "execution", label: "Execution", role: "orders to the broker, fills" },
  { key: "swing_strategy", label: "Thesis monitor", role: "stops, targets, time exits" },
  { key: "back_office", label: "Back office", role: "positions, P&L, reconciliation" },
  { key: "watcher", label: "Watcher", role: "live minute bars, alerts, feed health" },
  { key: "news", label: "News agent", role: "fetches and scores news" },
  { key: "data", label: "Data agent", role: "price history from the broker" },
  { key: "monitor", label: "Monitor", role: "kill switch on stale feed or broker trouble" },
  { key: "brief", label: "Daily brief", role: "writes the day up after the close" },
  { key: "telegram", label: "Telegram", role: "notifications to your phone" },
  { key: "commands", label: "Commands", role: "requests from this interface" },
  { key: "ops", label: "Ops", role: "heartbeat, restarts, watchdog" },
];

export interface AgentActivity {
  lastType: string | null;
  lastAt: string | null;
  lastText: string;
  count10m: number;
  recent: { at: string; text: string }[];
}

export type Team = Record<string, AgentActivity>;

const num = (v: unknown, d = 2) => (typeof v === "number" ? v.toFixed(d) : typeof v === "string" && !isNaN(Number(v)) ? Number(v).toFixed(d) : String(v ?? ""));

export function describe(m: LiveMessage): string {
  const p = m.payload ?? {};
  const s = m.symbol ?? "";
  switch (m.type) {
    case "market.bar.1m":
      return `bar ${s} ${num(p.close)}`;
    case "market.bar.1d":
      return `daily bar ${s} ${num(p.close)}`;
    case "news.article.raw":
      return `fetched: ${String(p.title ?? "").slice(0, 80)}`;
    case "news.article.scored":
      return `scored ${s}: ${p.impact} impact, tone ${num(p.polarity, 1)}`;
    case "research.request":
      return `research requested: ${s}`;
    case "news.state":
      return `news state ${s}: tone ${num(p.tone_48h)}, ${p.count_24h ?? 0} in 24h`;
    case "filing.new":
      return `${p.form} filed by ${s}: ${(p.items ?? []).join(", ") || "no items"}`;
    case "research.report":
      if (p.kind === "technical" || p.kind === "news" || p.kind === "filing") return `${p.kind} ${s}: ${p.stance ?? ""} ${String(p.summary ?? "").slice(0, 90)}`;
      if (p.kind === "regime" || p.kind === "sector" || p.kind === "macro") return `${p.kind}: ${String(p.summary ?? "").slice(0, 100)}`;
      if (p.kind === "fundamental" || p.kind === "event") return `${p.kind} ${s}: ${String(p.summary ?? "").slice(0, 90)}`;
      if (p.kind === "scorecard") return `scorecard (${p.when ?? ""}): top ${(p.top ?? []).slice(0, 5).join(", ")}`;
      if (p.kind === "morning_note") return `morning note: ${p.note?.headline ?? ""}`;
      return `report: ${p.analyst ?? ""}`;
    case "cycle.stage.completed":
      return `${p.stage}: ${p.detail ?? ""}`;
    case "watcher.alert":
      return `${p.kind} ${s}`;
    case "order.intent":
      return `wants to ${p.side} ${num(p.quantity, 0)} ${s}`;
    case "risk.approved":
      return `approved ${p.intent?.side ?? ""} ${num(p.intent?.quantity, 0)} ${s}`;
    case "risk.rejected":
      return `rejected ${p.intent?.side ?? ""} ${num(p.intent?.quantity, 0)} ${s}: ${p.reason ?? ""}`;
    case "risk.kill_switch":
      return p.engaged === false ? `kill switch released: ${p.reason ?? ""}` : `kill switch engaged: ${p.reason ?? ""}`;
    case "order.submitted":
      return `sent ${p.side ?? ""} ${s} to the broker`;
    case "order.filled":
      return `filled ${p.side ?? ""} ${num(p.filled_quantity, 0)} ${s} at ${num(p.avg_price)}`;
    case "order.partial":
      return `partial fill ${s}`;
    case "order.rejected":
      return `broker rejected ${s}: ${p.detail ?? ""}`;
    case "order.canceled":
      return `canceled ${s}`;
    case "portfolio.reconciled":
      return `snapshot: equity ${num(p.equity)}, ${(p.positions ?? []).length} positions`;
    case "portfolio.position.updated":
      return `position ${s}: ${num(p.qty, 0)}`;
    case "cycle.started":
      return `cycle started (${p.detail ?? ""})`;
    case "cycle.plan": {
      const d = (p.plan?.decisions ?? []) as any[];
      const trades = d.filter((x) => x.action === "buy" || x.action === "sell").length;
      return `plan ${p.trigger ?? ""}: ${trades} trades, ${d.length} names considered${p.dry_run ? " (dry run)" : ""}`;
    }
    case "thesis.challenge":
      return `skeptic: ${(p.challenge?.objections ?? []).length} objections`;
    case "cycle.completed":
      return `cycle done: ${p.detail ?? ""}`;
    case "cycle.failed":
      return `cycle failed: ${p.detail ?? ""}`;
    case "thesis.created":
      return `new thesis ${s}: stop ${num(p.stop_pct, 1)}%, target ${num(p.target_pct, 1)}%`;
    case "thesis.updated":
      return `thesis ${s} ${p.status ?? ""}`;
    case "thesis.invalidated":
      return `thesis ${s} invalidated: ${p.reason ?? ""}`;
    case "thesis.closed":
      return `thesis ${s} closed: ${p.reason ?? ""}`;
    case "console.question":
      return `asked by ${p.user || "console"}: ${String(p.text ?? "").slice(0, 60)}`;
    case "console.progress":
      return p.stage === "tool" ? `reading ${p.detail}` : p.stage === "thinking" ? "thinking" : p.stage === "done" ? "answered" : `error: ${p.detail ?? ""}`;
    case "console.answer.delta":
      return "writing an answer";
    case "console.answer":
      return "answered";
    case "desk.command":
      return `command ${p.command} from ${p.user || "?"}`;
    case "desk.command.result":
      return `${p.command}: ${p.ok ? "done" : "failed"}`;
    case "desk.brief":
      return `brief written: ${p.brief?.headline ?? ""}`;
    case "system.heartbeat":
      return `heartbeat, ${p.status}${p.next_cycle ? `, next ${p.next_cycle}` : ""}`;
    case "system.restarted":
      return "started";
    case "system.health":
      return `${p.component ?? ""} ${p.status ?? ""}`;
    default:
      return m.type;
  }
}

export function agentKey(source: string | undefined): string {
  return (source ?? "").split("@")[0];
}
