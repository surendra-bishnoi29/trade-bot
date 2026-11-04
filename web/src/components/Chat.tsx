import { FormEvent, useEffect, useRef, useState } from "react";
import { Markdown } from "./Markdown";

export interface ChatMessage {
  id: string;
  role: "user" | "desk";
  user?: string;
  text: string;
  at: string;
  streaming?: boolean;
  progress?: string | null;
}

const SUGGESTIONS = [
  "What is in the book right now, and why?",
  "Why did we buy the last position? Show the evidence.",
  "What would make you sell today?",
  "Draw the chart of our biggest position with its stop and target.",
];

export function Chat({
  messages,
  onAsk,
  busy,
}: {
  messages: ChatMessage[];
  onAsk: (text: string) => Promise<void>;
  busy: boolean;
}) {
  const [text, setText] = useState("");
  const end = useRef<HTMLDivElement>(null);

  useEffect(() => {
    end.current?.scrollIntoView({ block: "end" });
  }, [messages.length, messages[messages.length - 1]?.text.length]);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const q = text.trim();
    if (!q || busy) return;
    setText("");
    await onAsk(q);
  };

  return (
    <section className="card chat">
      <header className="card-head">
        <h2>Ask the head trader</h2>
        <span className="muted">answers stream as they are written</span>
      </header>
      <div className="messages">
        {messages.length === 0 && (
          <div className="suggestions">
            {SUGGESTIONS.map((s) => (
              <button key={s} type="button" className="suggestion" onClick={() => !busy && onAsk(s)}>
                {s}
              </button>
            ))}
          </div>
        )}
        {messages.map((m) => (
          <div key={m.id} className={`msg ${m.role}`}>
            <div className="msg-meta">
              {m.role === "user" ? m.user ?? "you" : "head trader"}
              {m.progress && <span className="progress"> · {m.progress}</span>}
            </div>
            {m.role === "user" ? <p>{m.text}</p> : <Markdown text={m.text || (m.streaming ? "…" : "")} />}
            {m.streaming && <span className="cursor" aria-hidden="true" />}
          </div>
        ))}
        <div ref={end} />
      </div>
      <form onSubmit={submit} className="ask">
        <input
          id="chat-input"
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder={busy ? "the head trader is answering…" : "Ask anything about the book, a stock, or a decision"}
          disabled={busy}
        />
        <button type="submit" disabled={busy || !text.trim()}>
          Ask
        </button>
      </form>
    </section>
  );
}
