// Plotly parses colours itself and knows nothing of CSS custom properties or oklch, so every token a chart uses is
// resolved here to an rgba() string by painting it once on a 1x1 canvas.

let canvas: CanvasRenderingContext2D | null = null;
const cache = new Map<string, string>();

export function resolveColor(value: string): string {
  const key = value.trim();
  const hit = cache.get(key);
  if (hit) return hit;
  canvas ??= Object.assign(document.createElement("canvas"), { width: 1, height: 1 }).getContext("2d", { willReadFrequently: true });
  if (!canvas) return key;
  canvas.clearRect(0, 0, 1, 1);
  canvas.fillStyle = "#000";
  canvas.fillStyle = key;
  canvas.fillRect(0, 0, 1, 1);
  const [r, g, b, a] = canvas.getImageData(0, 0, 1, 1).data;
  const out = `rgba(${r},${g},${b},${+(a / 255).toFixed(3)})`;
  cache.set(key, out);
  return out;
}

/** A custom property's value, resolved: `token("--foreground")` or `token("var(--series-2)")`. */
export function token(name: string): string {
  const match = /^var\((--[\w-]+)\)$/.exec(name.trim());
  const prop = match ? match[1] : name;
  if (!prop.startsWith("--")) return resolveColor(prop);
  const raw = getComputedStyle(document.documentElement).getPropertyValue(prop).trim();
  return raw ? resolveColor(raw) : "rgba(128,128,128,1)";
}

export function fontOf(name: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || "system-ui, sans-serif";
}

/** The same colour with another alpha. */
export function alpha(rgba: string, value: number): string {
  const m = /rgba?\(([^)]+)\)/.exec(rgba);
  if (!m) return rgba;
  const [r, g, b] = m[1].split(",").map((part) => part.trim());
  return `rgba(${r},${g},${b},${value})`;
}

export const SEQUENTIAL = ["--seq-0", "--seq-1", "--seq-2", "--seq-3", "--seq-4", "--seq-5"];

/** a mixed toward b by t, as an opaque rgb() (WebGL scenes ignore a colour's alpha in places). */
export function mix(a: string, b: string, t: number): string {
  const parse = (c: string) => (/rgba?\(([^)]+)\)/.exec(c)?.[1].split(",").map(Number) ?? [128, 128, 128]);
  const [x, y] = [parse(a), parse(b)];
  return `rgb(${[0, 1, 2].map((i) => Math.round(x[i] + (y[i] - x[i]) * t)).join(",")})`;
}
