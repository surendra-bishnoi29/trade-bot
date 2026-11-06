import { Component, ReactNode, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { Ghost, PickCard, StageCard } from "./Cards";
import { FanChart, FanGeometry } from "./FanChart";
import { hhmm } from "./format";
import "./funnel.css";
import { BreadthStrip, FocusPanel, SentimentChip } from "./Panels";
import { COLUMNS, Funnel, FunnelSymbol, Side } from "./types";
import { useDetail, useFunnel, useMarkets, useSize } from "./useFunnel";

const STAGES = ["Moving", "Confirmed", "Shortlist", "Cleared", "Picks"];
const STAGE_BY = ["watcher", "quant + news", "head trader", "skeptic + risk", "the book"];

interface Link {
  key: string;
  symbol: string;
  side: Side;
  d: string;
}

function curve(x1: number, y1: number, x2: number, y2: number): string {
  const mx = (x1 + x2) / 2;
  return `M${x1.toFixed(1)},${y1.toFixed(1)} C${mx.toFixed(1)},${y1.toFixed(1)} ${mx.toFixed(1)},${y2.toFixed(1)} ${x2.toFixed(1)},${y2.toFixed(1)}`;
}

function ranked(funnel: Funnel, side: Side): FunnelSymbol[] {
  return funnel.symbols.filter((s) => s.side === side).sort((a, b) => a.rank - b.rank);
}

/** A drawing error must say so, not leave a black screen. */
class Guard extends Component<{ children: ReactNode }, { error: string | null }> {
  state = { error: null as string | null };
  static getDerivedStateFromError(e: Error) {
    return { error: e.message };
  }
  render() {
    if (this.state.error)
      return (
        <div className="fb">
          <div className="fb-note bad">
            The funnel could not be drawn: {this.state.error}.{" "}
            <button type="button" onClick={() => window.location.assign(window.location.pathname)}>
              Reload
            </button>
          </div>
        </div>
      );
    return this.props.children;
  }
}

export function FunnelBoard() {
  return (
    <Guard>
      <Board />
    </Guard>
  );
}

/** The whole picture: the day's fan, the narrowing columns, the picks, and the lines that join them. */
function Board() {
  const markets = useMarkets();
  // a moment can be opened directly: /?market=stocks&at=2026-11-05T19:35:00Z
  const initial = useMemo(() => new URLSearchParams(window.location.search), []);
  const [market, setMarket] = useState(initial.get("market") ?? "stocks");
  const [at, setAt] = useState<string | null>(initial.get("at"));
  const { funnel, error, loading } = useFunnel(market, at);
  const [hover, setHover] = useState<string | null>(null);
  const [pinned, setPinned] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [source, setSource] = useState(0); // which column the pointer is in: 0 the fan, 1 to 5 the cards
  const [pinDock, setPinDock] = useState<"mid" | "left">("mid");
  const [geometry, setGeometry] = useState<FanGeometry | null>(null);
  const [links, setLinks] = useState<Link[]>([]);
  const cards = useRef(new Map<string, HTMLElement>());
  const fanBox = useRef<HTMLDivElement | null>(null);
  const { ref: stageRef, size } = useSize<HTMLDivElement>();

  const focus = hover ?? pinned;
  // the panel follows a card under the pointer; over the fan only the line lights up
  const panelSymbol = hover && source > 0 ? hover : pinned;
  const focused = funnel?.symbols.find((s) => s.symbol === panelSymbol) ?? null;
  const detail = useDetail(market, panelSymbol, at, funnel?.session.as_of);
  const dock = hover && source > 0 ? (source <= 4 ? "left" : "mid") : pinDock;
  const info = markets.find((m) => m.key === market);

  const bulls = useMemo(() => (funnel ? ranked(funnel, "bull") : []), [funnel]);
  const bears = useMemo(() => (funnel ? ranked(funnel, "bear") : []), [funnel]);

  const register = useCallback(
    (col: number, symbol: string) => (el: HTMLElement | null) => {
      const key = `${col}:${symbol}`;
      if (el) cards.current.set(key, el);
      else cards.current.delete(key);
    },
    [],
  );

  // the lines that carry each instrument from the fan through the columns it survives
  useLayoutEffect(() => {
    const stage = stageRef.current;
    const fan = fanBox.current;
    if (!stage || !fan || !funnel || !geometry) return;
    const origin = stage.getBoundingClientRect();
    const fanRect = fan.getBoundingClientRect();
    const out: Link[] = [];
    const box = (col: number, symbol: string) => {
      const el = cards.current.get(`${col}:${symbol}`);
      if (!el) return null;
      const r = el.getBoundingClientRect();
      return { l: r.left - origin.left, r: r.right - origin.left, y: r.top - origin.top + r.height / 2 };
    };
    for (const s of funnel.symbols) {
      const first = box(1, s.symbol);
      const endY = geometry.ends[s.symbol];
      if (first && endY !== undefined) {
        out.push({ key: `0:${s.symbol}`, symbol: s.symbol, side: s.side, d: curve(fanRect.right - origin.left - 10, fanRect.top - origin.top + endY, first.l, first.y) });
      }
      for (let col = 1; col < COLUMNS.length; col++) {
        const a = box(col, s.symbol);
        const b = box(col + 1, s.symbol);
        if (a && b) out.push({ key: `${col}:${s.symbol}`, symbol: s.symbol, side: s.side, d: curve(a.r, a.y, b.l, b.y) });
      }
    }
    setLinks(out);
  }, [funnel, geometry, size.w, size.h, stageRef]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setPinned(null);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const pin = (symbol: string) => {
    setPinDock(source >= 1 && source <= 4 ? "left" : "mid");
    setPinned((p) => (p === symbol ? null : symbol));
  };
  const hoverFrom = (col: number) => (symbol: string | null) => {
    if (symbol) setSource(col);
    setHover(symbol);
  };

  const search = (text: string) => {
    setQuery(text);
    const q = text.trim().toUpperCase();
    if (!q || !funnel) return;
    const hit = funnel.symbols.find((s) => s.symbol === q) ?? funnel.symbols.find((s) => s.symbol.startsWith(q) || s.name.toUpperCase().startsWith(q));
    if (hit) setPinned(hit.symbol);
  };

  // the time scrubber: every five minutes of the chosen session
  const session = funnel?.session;
  const slots = useMemo(() => {
    if (!session) return [];
    const out: string[] = [];
    const open = new Date(session.open).getTime();
    const close = new Date(session.close).getTime();
    for (let t = open + 5 * 60000; t <= close; t += 5 * 60000) out.push(new Date(t).toISOString());
    return out;
  }, [session?.open, session?.close]);
  const slotIndex = useMemo(() => {
    if (!session || !slots.length) return 0;
    const now = new Date(at ?? session.as_of).getTime();
    let i = slots.findIndex((t) => new Date(t).getTime() >= now);
    if (i < 0) i = slots.length - 1;
    return i;
  }, [slots, at, session?.as_of]);

  const days = info?.days ?? [];
  const day = session?.day ?? "";
  const pickDay = (d: string) => {
    if (!d) return;
    setAt(market === "crypto" ? `${d}T23:59:00Z` : `${d}T21:00:00Z`);
  };

  const column = (list: FunnelSymbol[], col: number) => {
    const cap = COLUMNS[col - 1];
    const shown = list.slice(0, cap);
    return (
      <div className="half" style={{ gridTemplateRows: col === 5 ? "minmax(0, 1.3fr) minmax(0, 1fr) minmax(0, 1fr)" : `repeat(${cap}, minmax(0, 1fr))` }}>
        {shown.map((s) =>
          col === 5 ? (
            <PickCard key={s.symbol} s={s} focus={focus} pinned={pinned} onFocus={hoverFrom(col)} onPin={pin} register={register(col, s.symbol)} />
          ) : (
            <StageCard key={s.symbol} s={s} tier={col as 1 | 2 | 3 | 4} focus={focus} pinned={pinned} onFocus={hoverFrom(col)} onPin={pin} register={register(col, s.symbol)} />
          ),
        )}
        {Array.from({ length: cap - shown.length }, (_, i) => (
          <Ghost key={`g${i}`} />
        ))}
      </div>
    );
  };

  return (
    <div className="fb">
      <div className="fb-bar">
        <div className="fb-markets" role="tablist" aria-label="Market">
          {(markets.length ? markets : [{ key: "stocks", label: "US stocks", has_data: true } as never]).map((m: { key: string; label: string; has_data: boolean }) => (
            <button
              key={m.key}
              type="button"
              role="tab"
              aria-selected={market === m.key}
              className={`${market === m.key ? "on" : ""}${m.has_data ? "" : " empty"}`}
              title={m.has_data ? m.label : `${m.label}: no prices collected yet`}
              onClick={() => {
                setMarket(m.key);
                setAt(null);
                setPinned(null);
              }}
            >
              {m.label}
            </button>
          ))}
        </div>
        <label className="fb-search">
          <span className="sr">Search instruments</span>
          <input value={query} onChange={(e) => search(e.target.value)} placeholder="Search" spellCheck={false} />
        </label>
        {funnel && <SentimentChip funnel={funnel} />}
        <div className="fb-time">
          {days.length > 0 && (
            <select value={day} onChange={(e) => pickDay(e.target.value)} aria-label="Session day">
              {!days.includes(day) && day && <option value={day}>{day}</option>}
              {days.map((d) => (
                <option key={d} value={d}>
                  {d}
                </option>
              ))}
            </select>
          )}
          {slots.length > 0 && (
            <input
              type="range"
              min={0}
              max={slots.length - 1}
              value={slotIndex}
              onChange={(e) => setAt(slots[Number(e.target.value)])}
              aria-label="Replay the session"
            />
          )}
          <span className="clock">{session ? `${hhmm(at ?? session.as_of)} UTC` : ""}</span>
          <button type="button" className={`live ${!at && session?.is_live ? "on" : ""}`} onClick={() => setAt(null)} title="Follow the market as it trades">
            <i /> {!at && session?.is_live ? "LIVE" : at ? "GO LIVE" : "LAST SESSION"}
          </button>
        </div>
      </div>

      {error && <div className="fb-note bad">The funnel could not be loaded: {error}</div>}
      {funnel && funnel.symbols.length === 0 && (
        <div className="fb-note">
          No prices for {funnel.label.toLowerCase()} in this session yet. {funnel.note}
        </div>
      )}
      {funnel && funnel.note && funnel.symbols.length > 0 && <div className="fb-note">{funnel.note}</div>}

      <div className={`fb-stage${focus ? " focusing" : ""}${loading && !funnel ? " loading" : ""}`} ref={stageRef}>
        {funnel && (
          <>
            <div className="fb-fan">
              <BreadthStrip funnel={funnel} focus={focus} onFocus={hoverFrom(0)} />
              <div className="fb-fanwrap" ref={fanBox}>
                <FanChart funnel={funnel} focus={focus} onFocus={hoverFrom(0)} onPin={pin} onGeometry={setGeometry} />
              </div>
            </div>
            <div className="fb-neck" aria-hidden="true" />
            {COLUMNS.map((_, i) => (
              <div className={`fb-col c${i + 1}`} key={i}>
                <div className="fb-colhead">
                  <b>{STAGES[i]}</b>
                  <span>{STAGE_BY[i]}</span>
                </div>
                {column(bulls, i + 1)}
                <div className="fb-split" aria-hidden="true" />
                {column(bears, i + 1)}
              </div>
            ))}
            <svg className="fb-links" width={size.w} height={size.h} aria-hidden="true">
              {links.map((l) => (
                <path key={l.key} d={l.d} className={`${l.side}${focus === l.symbol ? " on" : focus ? " dim" : ""}`} />
              ))}
            </svg>
            {focused && (
              <div className={`fb-dock ${dock}`}>
                <FocusPanel s={focused} detail={detail} pinned={pinned === focused.symbol && !(hover && source > 0 && hover !== pinned)} onClose={() => setPinned(null)} />
              </div>
            )}
          </>
        )}
        {!funnel && !error && <div className="fb-note">Loading the session…</div>}
      </div>
      {funnel && funnel.wire.length > 0 && (
        <div className="fb-wire" aria-label="Latest agent events">
          <b>LIVE WIRE</b>
          <ol>
            {funnel.wire.slice(0, 6).map((w, i) => (
              <li key={i}>
                <button type="button" onClick={() => w.symbol && funnel.symbols.some((s) => s.symbol === w.symbol) && setPinned(w.symbol)}>
                  <time>{hhmm(w.at)}</time>
                  <em>{w.agent}</em>
                  <span>{w.text}</span>
                </button>
              </li>
            ))}
          </ol>
        </div>
      )}
    </div>
  );
}
