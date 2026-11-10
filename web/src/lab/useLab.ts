import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import { DayReport, Live, TradeChartData, TradeRow } from "./types";

/** The lab's live state, refreshed every `everyMs`; the last good answer stays on screen through a failed poll. */
export function useLive(everyMs = 10000): { live: Live | null; error: string | null; fetchedAt: number | null } {
  const [live, setLive] = useState<Live | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [fetchedAt, setFetchedAt] = useState<number | null>(null);
  useEffect(() => {
    let stop = false;
    const load = async () => {
      try {
        const next = await api.get<Live>("/api/lab/live");
        if (stop) return;
        setLive(next);
        setError(null);
        setFetchedAt(Date.now());
      } catch (e) {
        if (!stop) setError(String((e as Error).message));
      }
    };
    void load();
    const id = window.setInterval(load, everyMs);
    return () => {
      stop = true;
      window.clearInterval(id);
    };
  }, [everyMs]);
  return { live, error, fetchedAt };
}

/** One UTC day's graded report; a day still running is refreshed every minute. */
export function useReport(day: string | null): { report: DayReport | null; loading: boolean } {
  const [report, setReport] = useState<DayReport | null>(null);
  const [loading, setLoading] = useState(false);
  const final = useRef(false);
  useEffect(() => {
    if (!day) return;
    let stop = false;
    final.current = false;
    const load = async (first: boolean) => {
      if (final.current) return;
      if (first) setLoading(true);
      try {
        const next = await api.get<DayReport>(`/api/lab/report?day=${day}`);
        if (stop) return;
        setReport(next);
        final.current = next.final;
      } finally {
        if (!stop && first) setLoading(false);
      }
    };
    void load(true);
    const id = window.setInterval(() => void load(false), 60000);
    return () => {
      stop = true;
      window.clearInterval(id);
    };
  }, [day]);
  return { report, loading };
}

export function useDays(): string[] {
  const [days, setDays] = useState<string[]>([]);
  useEffect(() => {
    api.get<string[]>("/api/lab/days").then(setDays).catch(() => setDays([]));
  }, []);
  return days;
}

/** The positions of the last `days`, refreshed every half minute. */
export function useTrades(days = 7): { trades: TradeRow[]; error: string | null } {
  const [trades, setTrades] = useState<TradeRow[]>([]);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let stop = false;
    const load = async () => {
      try {
        const next = await api.get<{ lab: boolean; trades: TradeRow[] }>(`/api/lab/trades?days=${days}`);
        if (!stop) {
          setTrades(next.trades);
          setError(null);
        }
      } catch (e) {
        if (!stop) setError(String((e as Error).message));
      }
    };
    void load();
    const id = window.setInterval(load, 30000);
    return () => {
      stop = true;
      window.clearInterval(id);
    };
  }, [days]);
  return { trades, error };
}

/** One position's bars and line moves; an open position is refreshed every ten seconds. */
export function useTrade(callId: string | null): { chart: TradeChartData | null; error: string | null } {
  const [chart, setChart] = useState<TradeChartData | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    setChart(null);
    setError(null);
    if (!callId) return;
    let stop = false;
    let id = 0;
    const load = async () => {
      try {
        const next = await api.get<TradeChartData>(`/api/lab/trade/${encodeURIComponent(callId)}`);
        if (stop) return;
        setChart(next);
        setError(null);
        if (!next.open && id) {
          window.clearInterval(id);
          id = 0;
        }
      } catch (e) {
        if (!stop) setError(String((e as Error).message));
      }
    };
    void load();
    id = window.setInterval(load, 10000);
    return () => {
      stop = true;
      if (id) window.clearInterval(id);
    };
  }, [callId]);
  return { chart, error };
}
