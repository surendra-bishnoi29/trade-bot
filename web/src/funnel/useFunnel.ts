import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api";
import { Detail, Funnel, MarketInfo } from "./types";

/** The funnel of one market at one moment; refetched every 15 seconds while it is live. */
export function useFunnel(market: string, at: string | null) {
  const [funnel, setFunnel] = useState<Funnel | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const seq = useRef(0);

  const load = useCallback(async () => {
    const mine = ++seq.current;
    setLoading(true);
    try {
      const q = new URLSearchParams({ market });
      if (at) q.set("at", at);
      const f = await api.get<Funnel>(`/api/funnel?${q}`);
      if (mine === seq.current) {
        setFunnel(f);
        setError(null);
      }
    } catch (e) {
      if (mine === seq.current) setError((e as Error).message);
    } finally {
      if (mine === seq.current) setLoading(false);
    }
  }, [market, at]);

  useEffect(() => {
    void load();
  }, [load]);

  const live = !at && !!funnel?.session.is_live;
  useEffect(() => {
    if (at) return;
    const t = window.setInterval(() => void load(), live ? 15000 : 60000);
    return () => window.clearInterval(t);
  }, [at, live, load]);

  return { funnel, error, loading, reload: load };
}

export function useMarkets() {
  const [markets, setMarkets] = useState<MarketInfo[]>([]);
  useEffect(() => {
    api
      .get<MarketInfo[]>("/api/funnel/markets")
      .then(setMarkets)
      .catch(() => setMarkets([]));
  }, []);
  return markets;
}

/** Candles and the journal's trail for the focused instrument, cached per moment. */
export function useDetail(market: string, symbol: string | null, at: string | null, asOf: string | undefined) {
  const [detail, setDetail] = useState<Detail | null>(null);
  const cache = useRef(new Map<string, Detail>());
  useEffect(() => {
    if (!symbol) {
      setDetail(null);
      return;
    }
    const key = `${market}|${symbol}|${at ?? asOf ?? ""}`;
    const hit = cache.current.get(key);
    if (hit) {
      setDetail(hit);
      return;
    }
    setDetail(null);
    let gone = false;
    const timer = window.setTimeout(() => {
      const q = new URLSearchParams({ market, symbol });
      if (at) q.set("at", at);
      api
        .get<Detail>(`/api/funnel/detail?${q}`)
        .then((d) => {
          cache.current.set(key, d);
          if (!gone) setDetail(d);
        })
        .catch(() => undefined);
    }, 140);
    return () => {
      gone = true;
      window.clearTimeout(timer);
    };
  }, [market, symbol, at, asOf]);
  return detail;
}

export function useSize<T extends HTMLElement>() {
  const ref = useRef<T | null>(null);
  const [size, setSize] = useState({ w: 0, h: 0 });
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setSize({ w: el.clientWidth, h: el.clientHeight }));
    ro.observe(el);
    setSize({ w: el.clientWidth, h: el.clientHeight });
    return () => ro.disconnect();
  }, []);
  return { ref, size };
}
