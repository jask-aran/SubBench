// Number and date formatting shared by every page. Pure: no DOM access, so the
// Node tests import it directly.

export const money = (value) => {
  const number = Number(value);
  if (!Number.isFinite(number)) return "—";
  if (number >= 1000) return "$" + Math.round(number).toLocaleString("en-US");
  return "$" + number.toFixed(number >= 100 ? 0 : 2);
};

// Compact currency for axes and per-task prices, where cents matter at the bottom and
// thousands at the top.
export const price = (value) => {
  const number = Number(value);
  if (!Number.isFinite(number)) return "—";
  if (number === 0) return "$0";
  if (number < 0.01) return "$" + number.toFixed(4);
  if (number < 1) return "$" + number.toFixed(3);
  if (number < 100) return "$" + number.toFixed(2);
  if (number < 10000) return "$" + Math.round(number).toLocaleString("en-US");
  return "$" + (number / 1000).toFixed(number < 100000 ? 1 : 0) + "k";
};

export const range = (row) => money(row.lower_usd) + "–" + money(row.upper_usd);

export function duration(seconds) {
  const value = Number(seconds);
  if (!Number.isFinite(value)) return "—";
  if (value < 1) return Math.round(value * 1000) + "ms";
  if (value >= 3600) return (value / 3600).toFixed(1) + "h";
  if (value >= 120) return (value / 60).toFixed(1) + "m";
  return value.toFixed(value < 10 ? 1 : 0) + "s";
}

export function integer(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "—";
  const size = Math.abs(number);
  if (size >= 1e9) return (number / 1e9).toFixed(1) + "B";
  if (size >= 1e6) return (number / 1e6).toFixed(1) + "M";
  if (size >= 1e4) return Math.round(number / 1e3) + "k";
  return Math.round(number).toLocaleString("en-US");
}

export function score(value, format, compact = false) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "—";
  if (format === "percent") return (number * 100).toFixed(compact ? 0 : 1) + "%";
  if (format === "score100") return number.toFixed(compact ? 0 : 1);
  return Math.abs(number) < 1 ? number.toFixed(3) : number.toFixed(1);
}

export const plural = (count, word) => `${count} ${word}${count === 1 ? "" : "s"}`;

export function shortDate(value) {
  return new Date(value).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
}

export function dayMonth(value) {
  return new Date(value).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

export function ago(iso, now = Date.now()) {
  if (!iso) return "never";
  const mins = Math.round((now - Date.parse(iso)) / 60000);
  if (!Number.isFinite(mins)) return "unknown";
  if (mins < 1) return "just now";
  if (mins < 90) return mins + " min ago";
  if (mins < 36 * 60) return Math.round(mins / 60) + " h ago";
  return Math.round(mins / 1440) + " d ago";
}

export function escapeHTML(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// Axis ticks on a "nice" step. Returns at least two values spanning [lo, hi].
export function niceTicks(lo, hi, count = 5) {
  if (!(hi > lo)) return [lo];
  const step0 = (hi - lo) / Math.max(1, count);
  const magnitude = 10 ** Math.floor(Math.log10(step0));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * magnitude).find((s) => s >= step0) || 10 * magnitude;
  const ticks = [];
  for (let value = Math.ceil(lo / step) * step; value <= hi + step * 1e-9; value += step) {
    ticks.push(Math.abs(value) < step * 1e-9 ? 0 : value);
  }
  return ticks;
}

// Decade ticks for a log axis, with 2x and 5x subdivisions when the span is short.
export function logTicks(lo, hi) {
  const ticks = [];
  const start = Math.floor(Math.log10(lo));
  const end = Math.ceil(Math.log10(hi));
  const multipliers = end - start <= 2 ? [1, 2, 5] : [1];
  for (let power = start; power <= end; power++) {
    for (const m of multipliers) {
      const value = m * 10 ** power;
      if (value >= lo * 0.999 && value <= hi * 1.001) ticks.push(value);
    }
  }
  return ticks.length >= 2 ? ticks : [lo, hi];
}

export function niceMax(value) {
  if (!(value > 0)) return 10;
  const magnitude = 10 ** Math.floor(Math.log10(value));
  for (const step of [1, 1.5, 2, 2.5, 3, 4, 5, 7.5, 10]) {
    if (value <= step * magnitude) return step * magnitude;
  }
  return 10 * magnitude;
}
