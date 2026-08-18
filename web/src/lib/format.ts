/** Formatting helpers. A missing value renders as "—", never as 0. */

export const EMPTY = "—";

export function num(value: number | null | undefined, digits = 0): string {
  if (value === null || value === undefined || Number.isNaN(value)) return EMPTY;
  return value.toLocaleString("ja-JP", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

export function distance(metres: number | null | undefined): string {
  if (metres === null || metres === undefined) return EMPTY;
  return metres >= 1000 ? `${(metres / 1000).toFixed(2)} km` : `${Math.round(metres)} m`;
}

export function percent(fraction: number | null | undefined): string {
  if (fraction === null || fraction === undefined) return EMPTY;
  const pct = fraction * 100;
  return `${pct >= 0 ? "+" : ""}${pct.toFixed(1)}%`;
}

export function score(value: number | null | undefined): string {
  if (value === null || value === undefined) return EMPTY;
  return value.toFixed(1);
}

export function date(value: string | null | undefined): string {
  if (!value) return EMPTY;
  return value.slice(0, 10);
}

/** Colour ramp shared by the score heat map and the score bars. */
export function scoreColor(value: number | null | undefined): string {
  if (value === null || value === undefined) return "#9aa3ad";
  const stops: [number, string][] = [
    [0, "#3b4cc0"],
    [25, "#6f8fd6"],
    [50, "#d9d9d9"],
    [75, "#e88b6a"],
    [100, "#b40426"],
  ];
  const v = Math.max(0, Math.min(100, value));
  for (let i = 1; i < stops.length; i += 1) {
    if (v <= stops[i][0]) {
      const [lo, loColor] = stops[i - 1];
      const [hi, hiColor] = stops[i];
      return mix(loColor, hiColor, (v - lo) / (hi - lo));
    }
  }
  return stops[stops.length - 1][1];
}

function mix(a: string, b: string, t: number): string {
  const pa = [1, 3, 5].map((i) => parseInt(a.slice(i, i + 2), 16));
  const pb = [1, 3, 5].map((i) => parseInt(b.slice(i, i + 2), 16));
  const out = pa.map((v, i) => Math.round(v + (pb[i] - v) * t));
  return `#${out.map((v) => v.toString(16).padStart(2, "0")).join("")}`;
}
