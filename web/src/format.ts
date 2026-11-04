export const money = (v: number | null | undefined, digits = 2): string =>
  v === null || v === undefined || Number.isNaN(v)
    ? "–"
    : v.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });

export const signed = (v: number | null | undefined, digits = 2): string =>
  v === null || v === undefined ? "–" : (v >= 0 ? "+" : "−") + money(Math.abs(v), digits);

export const pct = (v: number | null | undefined): string =>
  v === null || v === undefined ? "–" : (v >= 0 ? "+" : "−") + Math.abs(v).toFixed(2) + "%";

export const timeUTC = (iso: string | null | undefined): string => {
  if (!iso) return "–";
  const d = new Date(iso);
  return d.toISOString().slice(11, 16) + " UTC";
};

export const dateTimeUTC = (iso: string | null | undefined): string => {
  if (!iso) return "–";
  const d = new Date(iso);
  return d.toISOString().slice(0, 16).replace("T", " ") + " UTC";
};

export const local = (iso: string | null | undefined): string => {
  if (!iso) return "–";
  return new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
};

export const ago = (iso: string | null | undefined, now = Date.now()): string => {
  if (!iso) return "never";
  const s = Math.max(0, Math.round((now - new Date(iso).getTime()) / 1000));
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${(s / 3600).toFixed(1)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
};

export const seconds = (s: number | null | undefined): string => {
  if (s === null || s === undefined) return "–";
  if (s < 60) return `${Math.round(s)} s`;
  if (s < 3600) return `${Math.round(s / 60)} min`;
  return `${(s / 3600).toFixed(1)} h`;
};
