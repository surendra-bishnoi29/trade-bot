/** A price with the decimals its size deserves: none above 10,000, four below 1, two otherwise. */
export const price = (v: number | null | undefined): string => {
  if (v === null || v === undefined || Number.isNaN(v)) return "n/a";
  const digits = Math.abs(v) >= 10000 ? 0 : Math.abs(v) < 1 ? 4 : 2;
  return v.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
};

export const pct = (v: number | null | undefined, digits = 2): string =>
  v === null || v === undefined || Number.isNaN(v) ? "n/a" : `${v > 0 ? "+" : v < 0 ? "−" : ""}${Math.abs(v).toFixed(digits)}%`;

export const hhmm = (iso: string): string => iso.slice(11, 16);

export const tone = (v: number | null | undefined): "up" | "down" | "flat" => (v === null || v === undefined || Math.abs(v) < 0.005 ? "flat" : v > 0 ? "up" : "down");

/** Compress large moves so a quiet day and a wild day both use the height: sign(v)·sqrt(|v|). */
export const squash = (v: number): number => Math.sign(v) * Math.sqrt(Math.abs(v));

export function polyline(points: [number, number][]): string {
  return points.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join(" ");
}
