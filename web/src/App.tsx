import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError, Book as BookT, Brief as BriefT, ChatPair, Cycle, Health, JournalItem, MorningNote as NoteT, Trade, User } from "./api";
import { Agents } from "./components/Agents";
import { agentKey, describe, Team } from "./agents";
import { Book } from "./components/Book";
import { MorningNote } from "./components/MorningNote";
import { Brief } from "./components/Brief";
import { Chat, ChatMessage } from "./components/Chat";
import { Controls, CommandResult } from "./components/Controls";
import { Cycles } from "./components/Cycles";
import { HealthStrip } from "./components/HealthStrip";
import { Journal } from "./components/Journal";
import { Login } from "./components/Login";
import { Trades } from "./components/Trades";
import { LiveMessage, useLive } from "./live";

type Tab = "activity" | "cycles";

export default function App() {
  const [user, setUser] = useState<User | null | undefined>(undefined);
  const [book, setBook] = useState<BookT | null>(null);
  const [health, setHealth] = useState<Health | null>(null);
  const [trades, setTrades] = useState<Trade[] | null>(null);
  const [journal, setJournal] = useState<JournalItem[] | null>(null);
  const [cycles, setCycles] = useState<Cycle[] | null>(null);
  const [brief, setBrief] = useState<BriefT | null>(null);
  const [team, setTeam] = useState<Team>({});
  const [note, setNote] = useState<NoteT | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [results, setResults] = useState<CommandResult[]>([]);
  const [tab, setTab] = useState<Tab>("activity");
  const [now, setNow] = useState(Date.now());
  const pending = useRef<Set<string>>(new Set());
  const refreshTimer = useRef<number | undefined>(undefined);

  useEffect(() => {
    const t = window.setInterval(() => setNow(Date.now()), 15000);
    return () => window.clearInterval(t);
  }, []);

  useEffect(() => {
    api
      .get<User>("/api/me")
      .then(setUser)
      .catch(() => setUser(null));
  }, []);

  const loadAll = useCallback(async () => {
    try {
      const [state, t, j, c, chat, briefs, agents, notes] = await Promise.all([
        api.get<{ book: BookT; health: Health }>("/api/state"),
        api.get<Trade[]>("/api/trades?limit=40"),
        api.get<JournalItem[]>("/api/journal?limit=120"),
        api.get<Cycle[]>("/api/cycles?limit=20"),
        api.get<ChatPair[]>("/api/chat/history?limit=20"),
        api.get<BriefT[]>("/api/brief?limit=1"),
        api.get<{ agent: string; last_type: string; last_at: string; last_text: string; events_10m: number }[]>("/api/agents"),
        api.get<NoteT[]>("/api/research?kind=morning_note&limit=1"),
      ]);
      setBook(state.book);
      setBrief(briefs[0] ?? null);
      setNote(notes[0] ?? null);
      setTeam((prev) => {
        const next: Team = { ...prev };
        for (const a of agents) {
          const cur = next[a.agent];
          if (!cur || !cur.lastAt || new Date(a.last_at) > new Date(cur.lastAt)) {
            next[a.agent] = { lastType: a.last_type, lastAt: a.last_at, lastText: a.last_text, count10m: a.events_10m, recent: cur?.recent ?? [] };
          }
        }
        return next;
      });
      setHealth(state.health);
      setTrades(t);
      setJournal(j);
      setCycles(c);
      setMessages(
        chat.flatMap((p) => {
          const out: ChatMessage[] = [{ id: p.request_id + ":q", role: "user", user: p.user, text: p.question, at: p.asked_at }];
          if (p.answer) out.push({ id: p.request_id + ":a", role: "desk", text: p.answer, at: p.answered_at ?? p.asked_at });
          return out;
        }),
      );
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) setUser(null);
    }
  }, []);

  useEffect(() => {
    if (user) void loadAll();
  }, [user, loadAll]);

  const refreshSoon = useCallback(() => {
    if (refreshTimer.current) return;
    refreshTimer.current = window.setTimeout(() => {
      refreshTimer.current = undefined;
      void loadAll();
    }, 1500);
  }, [loadAll]);

  const onLive = useCallback(
    (m: LiveMessage) => {
      const p = m.payload ?? {};
      const who = agentKey(m.source);
      if (who) {
        const text = describe(m);
        setTeam((prev) => {
          const cur = prev[who] ?? { lastType: null, lastAt: null, lastText: "", count10m: 0, recent: [] };
          return {
            ...prev,
            [who]: {
              lastType: m.type,
              lastAt: m.at,
              lastText: text,
              count10m: cur.count10m + 1,
              recent: [{ at: m.at, text }, ...cur.recent].slice(0, 8),
            },
          };
        });
      }
      switch (m.type) {
        case "market.bar.1m":
          setBook((b) => {
            if (!b || !m.symbol) return b;
            const positions = b.positions.map((pos) => {
              if (pos.symbol !== m.symbol) return pos;
              const price = Number(p.close);
              const pl = pos.avg_entry !== null ? (price - pos.avg_entry) * pos.qty : pos.unrealized_pl;
              const th = pos.thesis
                ? {
                    ...pos.thesis,
                    to_stop_pct: pos.thesis.stop ? ((price - pos.thesis.stop) / pos.thesis.stop) * 100 : null,
                    to_target_pct: pos.thesis.target ? ((pos.thesis.target - price) / price) * 100 : null,
                  }
                : null;
              return {
                ...pos,
                price,
                price_at: m.at,
                market_value: price * pos.qty,
                unrealized_pl: pl,
                unrealized_pct: pos.avg_entry ? ((price - pos.avg_entry) / pos.avg_entry) * 100 : null,
                thesis: th,
              };
            });
            return { ...b, positions };
          });
          setHealth((h) => (h ? { ...h, feed: { last_bar: m.at, age_seconds: 0 } } : h));
          break;
        case "system.heartbeat":
          setHealth((h) =>
            h
              ? {
                  ...h,
                  bot: { ...h.bot, alive: true, last_heartbeat: m.at, status: p.status, next_cycle: p.next_cycle, open_theses: p.open_theses },
                  kill_switch: { ...h.kill_switch, engaged: !!p.kill_switch },
                }
              : h,
          );
          break;
        case "risk.kill_switch":
          setHealth((h) => (h ? { ...h, kill_switch: { engaged: p.engaged !== false, reason: p.reason ?? "", since: m.at } } : h));
          refreshSoon();
          break;
        case "console.question":
          if (p.request_id && !pending.current.has(p.request_id)) {
            setMessages((ms) => [...ms, { id: p.request_id + ":q", role: "user", user: p.user || "console", text: p.text, at: m.at }]);
          }
          break;
        case "console.progress":
          setMessages((ms) =>
            ms.map((x) =>
              x.id === p.request_id + ":a"
                ? { ...x, progress: p.stage === "tool" ? `reading ${p.detail}` : p.stage === "thinking" ? "thinking" : p.stage === "error" ? "error" : null }
                : x,
            ),
          );
          break;
        case "console.answer.delta":
          setMessages((ms) => {
            const id = p.request_id + ":a";
            if (ms.some((x) => x.id === id)) return ms.map((x) => (x.id === id ? { ...x, text: x.text + p.text, streaming: true } : x));
            return [...ms, { id, role: "desk", text: p.text, at: m.at, streaming: true }];
          });
          break;
        case "console.answer":
          pending.current.delete(p.request_id);
          setMessages((ms) => {
            const id = p.request_id + ":a";
            if (ms.some((x) => x.id === id)) return ms.map((x) => (x.id === id ? { ...x, text: p.text, streaming: false, progress: null } : x));
            return [...ms, { id, role: "desk", text: p.text, at: m.at }];
          });
          break;
        case "research.report":
          if (p.kind === "morning_note") setNote({ id: m.id ?? "", at: m.at, day: p.day, note: p.note });
          break;
        case "desk.brief":
          setBrief({ id: m.id ?? "", day: p.day, generated_at: p.generated_at ?? m.at, brief: p.brief });
          break;
        case "desk.command.result":
          setResults((rs) => rs.map((r) => (r.request_id === p.request_id ? { ...r, ok: p.ok, text: p.text } : r)));
          refreshSoon();
          break;
        case "order.filled":
        case "order.submitted":
        case "order.rejected":
        case "risk.approved":
        case "risk.rejected":
        case "thesis.created":
        case "thesis.updated":
        case "thesis.closed":
        case "cycle.started":
        case "cycle.completed":
        case "cycle.failed":
        case "portfolio.reconciled":
        case "system.restarted":
        case "system.health":
          refreshSoon();
          break;
        default:
          break;
      }
    },
    [refreshSoon],
  );

  const socket = useLive(onLive, !!user);

  const ask = async (text: string) => {
    try {
      const r = await api.post<{ request_id: string }>("/api/ask", { text });
      pending.current.add(r.request_id);
      setMessages((ms) => [
        ...ms,
        { id: r.request_id + ":q", role: "user", user: user?.name, text, at: new Date().toISOString() },
        { id: r.request_id + ":a", role: "desk", text: "", at: new Date().toISOString(), streaming: true, progress: "sending" },
      ]);
    } catch (e) {
      const why = e instanceof ApiError && e.status === 503 ? e.message : `Could not send: ${(e as Error).message}`;
      setMessages((ms) => [...ms, { id: "err" + Date.now(), role: "desk", text: why, at: new Date().toISOString() }]);
    }
  };

  // a question the bot never picked up (it was down, or restarted) must not spin forever
  useEffect(() => {
    const t = window.setInterval(() => {
      setMessages((ms) =>
        ms.map((m) =>
          m.streaming && m.text === "" && Date.now() - new Date(m.at).getTime() > 90_000
            ? { ...m, streaming: false, progress: null, text: "No answer arrived. The bot may have been down or restarting; please ask again." }
            : m,
        ),
      );
    }, 5000);
    return () => window.clearInterval(t);
  }, []);

  const busy = messages.some((m) => m.streaming);

  if (user === undefined) return <main className="login muted">loading…</main>;
  if (user === null) return <Login onLogin={setUser} />;

  return (
    <>
      <header className="top">
        <div className="brand">
          <h1>Trading Desk</h1>
          <span className="muted">paper account · US equities · autonomous</span>
        </div>
        <HealthStrip health={health} socket={socket} now={now} />
        <div className="who">
          {user.name} <span className="muted">({user.role})</span>
          <button
            type="button"
            className="link"
            onClick={async () => {
              await api.post("/api/logout", {});
              setUser(null);
            }}
          >
            sign out
          </button>
        </div>
      </header>
      {health && !health.bot.alive && (
        <div className="banner">
          The bot is not running. Prices, questions and controls resume when it is back; the interface keeps showing the journal.
        </div>
      )}
      <main className="grid">
        <div className="wide">
          <Agents team={team} now={now} botAlive={!!health?.bot.alive} />
        </div>
        <div className="col">
          <Book book={book} now={now} />
          <MorningNote note={note} />
          <Brief brief={brief} user={user} onSent={(id) => setResults((rs) => [{ request_id: id, command: "brief", ok: null, text: null }, ...rs])} />
          <Trades trades={trades} />
        </div>
        <div className="col">
          <Chat messages={messages} onAsk={ask} busy={busy} />
          <Controls user={user} health={health} results={results} onSent={(r) => setResults((rs) => [r, ...rs])} />
        </div>
        <div className="wide">
          <nav className="tabs">
            <button type="button" className={tab === "activity" ? "on" : ""} onClick={() => setTab("activity")}>
              Activity
            </button>
            <button type="button" className={tab === "cycles" ? "on" : ""} onClick={() => setTab("cycles")}>
              Decision cycles
            </button>
          </nav>
          {tab === "activity" ? <Journal items={journal} /> : <Cycles cycles={cycles} />}
        </div>
      </main>
    </>
  );
}
