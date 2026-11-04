import { Health } from "../api";
import { ago, seconds, timeUTC } from "../format";

function Chip({ ok, warn, label, title }: { ok: boolean; warn?: boolean; label: string; title?: string }) {
  const cls = ok ? "chip ok" : warn ? "chip warn" : "chip bad";
  return (
    <span className={cls} title={title}>
      <i aria-hidden="true" />
      {label}
    </span>
  );
}

export function HealthStrip({ health, socket, now }: { health: Health | null; socket: boolean; now: number }) {
  if (!health) return <div className="strip muted">loading health…</div>;
  const feedAge = health.feed.age_seconds;
  const feedOk = !health.market_open || (feedAge !== null && feedAge < 600);
  const down = health.components.filter((c) => c.status !== "ok");
  return (
    <div className="strip">
      <Chip
        ok={health.bot.alive}
        label={health.bot.alive ? "bot running" : "bot not running"}
        title={`last heartbeat ${ago(health.bot.last_heartbeat, now)}`}
      />
      <Chip ok={health.market_open} warn={!health.market_open} label={health.market_open ? "market open" : `market closed, opens ${timeUTC(health.next_open)}`} />
      <Chip
        ok={feedOk}
        warn={!health.market_open}
        label={`prices ${health.feed.last_bar ? ago(health.feed.last_bar, now) : "none yet"}`}
        title={`last bar age ${seconds(feedAge)}`}
      />
      <Chip
        ok={!health.kill_switch.engaged}
        label={health.kill_switch.engaged ? `kill switch ON: ${health.kill_switch.reason}` : "kill switch off"}
      />
      <Chip ok={socket} warn={!socket} label={socket ? "live" : "reconnecting…"} />
      {health.bot.next_cycle && <span className="chip plain">next cycle {health.bot.next_cycle}</span>}
      {down.map((c) => (
        <Chip key={c.component} ok={false} warn={c.status === "degraded"} label={`${c.component} ${c.status}`} title={c.detail} />
      ))}
    </div>
  );
}
