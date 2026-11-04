import { useEffect, useRef, useState } from "react";

export interface LiveMessage {
  type: string;
  id?: string;
  symbol?: string | null;
  at: string;
  correlation_id?: string | null;
  source?: string;
  payload?: any;
  [key: string]: any;
}

/** One WebSocket to the api, reconnecting with backoff. Messages go to the latest handler. */
export function useLive(onMessage: (m: LiveMessage) => void, enabled: boolean): boolean {
  const [connected, setConnected] = useState(false);
  const handler = useRef(onMessage);
  handler.current = onMessage;

  useEffect(() => {
    if (!enabled) return;
    let socket: WebSocket | null = null;
    let closed = false;
    let delay = 1000;
    let timer: number | undefined;

    const connect = () => {
      const proto = location.protocol === "https:" ? "wss" : "ws";
      socket = new WebSocket(`${proto}://${location.host}/ws`);
      socket.onopen = () => {
        setConnected(true);
        delay = 1000;
      };
      socket.onmessage = (ev) => {
        try {
          handler.current(JSON.parse(ev.data));
        } catch {
          /* ignore a bad frame */
        }
      };
      socket.onclose = () => {
        setConnected(false);
        if (closed) return;
        timer = window.setTimeout(connect, delay);
        delay = Math.min(delay * 2, 15000);
      };
      socket.onerror = () => socket?.close();
    };
    connect();
    return () => {
      closed = true;
      if (timer) window.clearTimeout(timer);
      socket?.close();
    };
  }, [enabled]);

  return connected;
}
