// Keep this port equal to subbench.products.pool_history and its golden cases.
const DAY = 86400000;
const pyString = (value) => value == null ? "None" : String(value);
const planOf = (row) => typeof row.plan === "string" && row.plan ? row.plan : null;
const covered = (row) => Number(row.covered_quota_percent || 0);

// JavaScript has uppercase mappings but no Unicode titlecase mappings.
const titleRanges = [
  [0x10d0, 0x10fa, 0],
  [0x10fd, 0x10ff, 0],
  [0x1f80, 0x1f87, 8],
  [0x1f88, 0x1f8f, 0],
  [0x1f90, 0x1f97, 8],
  [0x1f98, 0x1f9f, 0],
  [0x1fa0, 0x1fa7, 8],
  [0x1fa8, 0x1faf, 0],
];
const titleExceptions = {
  "\u00df": "Ss",
  "\u01c4": "\u01c5",
  "\u01c5": "\u01c5",
  "\u01c6": "\u01c5",
  "\u01c7": "\u01c8",
  "\u01c8": "\u01c8",
  "\u01c9": "\u01c8",
  "\u01ca": "\u01cb",
  "\u01cb": "\u01cb",
  "\u01cc": "\u01cb",
  "\u01f1": "\u01f2",
  "\u01f2": "\u01f2",
  "\u01f3": "\u01f2",
  "\u0587": "\u0535\u0582",
  "\u1fb2": "\u1fba\u0345",
  "\u1fb3": "\u1fbc",
  "\u1fb4": "\u0386\u0345",
  "\u1fb7": "\u0391\u0342\u0345",
  "\u1fbc": "\u1fbc",
  "\u1fc2": "\u1fca\u0345",
  "\u1fc3": "\u1fcc",
  "\u1fc4": "\u0389\u0345",
  "\u1fc7": "\u0397\u0342\u0345",
  "\u1fcc": "\u1fcc",
  "\u1ff2": "\u1ffa\u0345",
  "\u1ff3": "\u1ffc",
  "\u1ff4": "\u038f\u0345",
  "\u1ff7": "\u03a9\u0342\u0345",
  "\u1ffc": "\u1ffc",
  "\ufb00": "Ff",
  "\ufb01": "Fi",
  "\ufb02": "Fl",
  "\ufb03": "Ffi",
  "\ufb04": "Ffl",
  "\ufb05": "St",
  "\ufb06": "St",
  "\ufb13": "\u0544\u0576",
  "\ufb14": "\u0544\u0565",
  "\ufb15": "\u0544\u056b",
  "\ufb16": "\u054e\u0576",
  "\ufb17": "\u0544\u056d",
};

function pyTitle(value) {
  const letters = Array.from(value);
  const isCased = (letter) => letter !== undefined && /\p{Cased}/u.test(letter);
  const isIgnorable = (letter) => letter !== undefined && /\p{Case_Ignorable}/u.test(letter);
  let previous = false;
  return letters.map((letter, index) => {
    let result;
    if (!previous) {
      const point = letter.codePointAt(0);
      const range = titleRanges.find(([first, last]) => point >= first && point <= last);
      result = range ? String.fromCodePoint(point + range[2]) : titleExceptions[letter] ?? letter.toUpperCase();
    } else if (letter === "Σ") {
      let before = index - 1, after = index + 1;
      while (isIgnorable(letters[before])) before--;
      while (isIgnorable(letters[after])) after++;
      result = isCased(letters[before]) && !isCased(letters[after]) ? "ς" : "σ";
    } else {
      result = letter.toLowerCase();
    }
    previous = isCased(letter);
    return result;
  }).join("");
}

function productLabel(provider, plan) {
  const name = new Map([["codex", "ChatGPT"], ["claude", "Claude"]]).get(provider)
    ?? pyTitle(provider.replaceAll("_", " "));
  if (plan && plan.includes("+")) {
    const at = plan.indexOf("+");
    return `${productLabel(provider, plan.slice(0, at) || null)} · ${pyTitle(plan.slice(at + 1).replaceAll("_", " "))}`;
  }
  if (!plan) return name;
  const detail = pyTitle(plan.replaceAll("_", " "));
  return detail.toLowerCase().startsWith(name.toLowerCase()) ? detail : `${name} ${detail}`;
}

// Python compares Unicode code points, including characters outside the BMP. Without
// surrogate pairs, UTF-16 code-unit order (JavaScript's < ) is the same order, and this
// was 15% of a pooling pass when done the long way for every comparison.
const SURROGATE = /[\uD800-\uDFFF]/;

function compare(a, b) {
  if (!SURROGATE.test(a) && !SURROGATE.test(b)) return a < b ? -1 : a > b ? 1 : 0;
  const left = Array.from(a, (c) => c.codePointAt(0));
  const right = Array.from(b, (c) => c.codePointAt(0));
  for (let i = 0; i < Math.min(left.length, right.length); i++) {
    if (left[i] !== right[i]) return left[i] < right[i] ? -1 : 1;
  }
  return left.length - right.length;
}

function sorted(rows, fields) {
  return rows.sort((a, b) => {
    for (const field of fields) {
      const order = compare(a[field], b[field]);
      if (order) return order;
    }
    return 0;
  });
}

function weightedQuantile(values, weights, probability) {
  const pairs = values.map((value, i) => [value, weights[i]]).sort((a, b) => a[0] - b[0]);
  const total = pairs.reduce((sum, pair) => sum + pair[1], 0);
  if (total <= 0) {
    const middle = Math.floor(pairs.length / 2);
    return pairs.length % 2 ? pairs[middle][0] : (pairs[middle - 1][0] + pairs[middle][0]) / 2;
  }
  let cumulative = 0;
  for (const [value, weight] of pairs) {
    cumulative += weight;
    if (cumulative >= total * probability) return value;
  }
  return pairs.at(-1)[0];
}

function pool(rows) {
  const weights = rows.map((row) => Math.max(covered(row), 1e-9));
  const quantile = (field) => weightedQuantile(rows.map((row) => Number(row[field] || row.estimate_usd)), weights, 0.5);
  return {
    estimate_usd: quantile("estimate_usd"),
    lower_usd: quantile("lower_usd"),
    upper_usd: quantile("upper_usd"),
    window_count: rows.length,
    account_count: new Set(rows.map((row) => pyString(row.account_id))).size,
  };
}

// One pass parses the same reset keys several times (each filter, then each period).
const MOMENTS = new Map();

function moment(value) {
  let parsed = MOMENTS.get(value);
  if (parsed === undefined) {
    parsed = parseMoment(value);
    MOMENTS.set(value, parsed);
  }
  return parsed;
}

function parseMoment(value) {
  // Python also accepts ISO week dates, which Date.parse does not accept.
  const week = /^(\d{4})(-?)W(\d{2})(?:\2([1-7]))?(?=$|[^0-9])/.exec(value);
  if (week) {
    const [, year, , number, weekday = "1"] = week;
    const january = new Date(0);
    january.setUTCFullYear(+year, 0, 4);
    january.setUTCHours(0, 0, 0, 0);
    const monday = +january - (january.getUTCDay() + 6) % 7 * DAY;
    const date = new Date(monday + ((+number - 1) * 7 + (+weekday - 1)) * DAY);
    const thursday = new Date(monday + ((+number - 1) * 7 + 3) * DAY);
    if (+year < 1 || +number < 1 || +number > 53 || thursday.getUTCFullYear() !== +year) return NaN;
    value = date.toISOString().slice(0, 10) + value.slice(week[0].length);
  }
  // Parse ISO dates explicitly. Date.parse also accepts invalid calendar dates.
  const match = /^(\d{4})-?(\d{2})-?(\d{2})(?:.(\d{2})(?::?(\d{2}))?(?::?(\d{2}))?([.,]\d+)?(Z|[+-]\d{2}(?::?\d{2})?(?::?\d{2})?(?:[.,]\d+)?)?)?$/u.exec(value);
  if (!match) return NaN;
  const [, year, month, date, hour = "0", minute = "0", second = "0", fraction = "", zone = "Z"] = match;
  const day = new Date(0);
  day.setUTCFullYear(+year, +month - 1, +date);
  day.setUTCHours(0, 0, 0, 0);
  if (+year < 1 || day.getUTCMonth() !== +month - 1 || day.getUTCDate() !== +date
      || +hour > 23 || +minute > 59 || +second > 59) return NaN;
  let offset = 0;
  if (zone !== "Z") {
    const parts = /^([+-])(\d{2})(?::?(\d{2}))?(?::?(\d{2}))?([.,]\d+)?$/.exec(zone);
    const [, sign, hours, minutes = "0", seconds = "0", part = ""] = parts;
    if (+hours > 23 || +minutes > 59 || +seconds > 59) return NaN;
    offset = (Number(hours) * 3600 + Number(minutes) * 60 + Number(seconds)
      + Number(part.replace(",", "."))) * 1000 * (sign === "+" ? 1 : -1);
  }
  return +day + (+hour * 3600 + +minute * 60 + +second + Number(fraction.replace(",", "."))) * 1000 - offset;
}

// A scoped limit ("weekly_fable") is a product of its own: the scope moves into the plan
// ("max_5x+fable") and the window becomes the weekly one (subbench.products.product_view).
const SCOPED_PREFIX = "weekly_";
function productView(row) {
  const window = typeof row.window === "string" ? row.window : "";
  if (!window.startsWith(SCOPED_PREFIX) || window.length === SCOPED_PREFIX.length) return row;
  return { ...row, plan: `${planOf(row) ?? ""}+${window.slice(SCOPED_PREFIX.length)}`, window: "weekly" };
}

function directWindows(windows, now, tiers, finished = true, withinDays = 35) {
  return windows.map(productView).filter((row) => {
    const key = pyString(row.reset_key || "");
    if (!key || key.includes("~via~") || !tiers.includes(row.tier) || Number(row.estimate_usd || 0) <= 0) return false;
    const reset = moment(key);
    if (!Number.isFinite(reset)) return false;
    return finished
      ? reset <= now && (withinDays === null || reset >= now - withinDays * DAY)
      : reset > now;
  });
}

function periodStart(row) {
  const day = new Date(moment(row.reset_key));
  day.setUTCHours(0, 0, 0, 0);
  const minutes = row.window === "five_hour" ? 300 : row.window === "weekly" ? 10080 : 1440;
  if (minutes >= 1440) day.setUTCDate(day.getUTCDate() - (day.getUTCDay() + 6) % 7);
  return day.toISOString().slice(0, 10);
}

function group(rows, series = false) {
  const groups = new Map();
  for (const row of rows) {
    const provider = pyString(row.provider), plan = planOf(row), window = pyString(row.window);
    const identity = { provider, plan, product: productLabel(provider, plan), window };
    if (series) identity.period_start = periodStart(row);
    const key = JSON.stringify([provider, plan, window, identity.period_start]);
    if (!groups.has(key)) groups.set(key, { identity, members: [] });
    groups.get(key).members.push(row);
  }
  return [...groups.values()];
}

function productEstimates(windows, now, tier) {
  return sorted(group(directWindows(windows, now, [tier])).map(({ identity, members }) => ({
    ...identity, ...pool(members),
    latest_completed_at: members.map((row) => pyString(row.reset_key)).sort(compare).at(-1),
  })), ["product", "window"]);
}

// The window fields pooling reads (subbench.products.POOL_FIELDS). Ingest stores only these
// per collector, so a pooling pass parses a fraction of each history report.
const POOL_FIELDS = ["provider", "account_id", "plan", "window", "reset_key", "tier", "estimate_usd",
  "lower_usd", "upper_usd", "covered_quota_percent", "latest_observed_at"];

export function poolInput(report) {
  return { windows: (report?.windows || []).map((row) =>
    Object.fromEntries(POOL_FIELDS.map((field) => [field, row[field] ?? null]))) };
}

// One row per product and limit, nothing per account (subbench.products.measured_products).
function measuredProducts(windows) {
  const seen = new Map();
  for (const row of windows.map(productView)) {
    const provider = pyString(row.provider), plan = planOf(row), window = pyString(row.window);
    const key = JSON.stringify([provider, plan, window]);
    if (!seen.has(key)) seen.set(key, { provider, plan, product: productLabel(provider, plan), window });
  }
  return sorted([...seen.values()], ["product", "window"]);
}

export function mergeAgentWindows(reports) {
  const best = new Map();
  for (const report of reports) {
    for (const row of report.windows || []) {
      const key = JSON.stringify([row.provider ?? null, row.account_id ?? null, row.plan ?? null,
        row.window ?? null, pyString(row.reset_key)]);
      const seen = best.get(key);
      if (!seen || covered(row) > covered(seen) || (covered(row) === covered(seen)
          && compare(pyString(row.latest_observed_at || ""), pyString(seen.latest_observed_at || "")) > 0)) best.set(key, row);
    }
  }
  return [...best.values()];
}

export function poolHistory(reports, now) {
  MOMENTS.clear();
  const windows = mergeAgentWindows(reports);
  const latest = new Map();
  for (const row of directWindows(windows, +now, ["confirmed", "likely", "provisional"], false, null)) {
    if (pyString(row.window) !== "weekly") continue;
    const account = typeof row.account_id === "string" ? row.account_id : null;
    const key = JSON.stringify([pyString(row.provider), planOf(row), account]);
    const seen = latest.get(key);
    if (!seen || compare(pyString(row.latest_observed_at), pyString(seen.latest_observed_at)) > 0) latest.set(key, row);
  }
  const productsOpen = group([...latest.values()]).map(({ identity, members }) => ({
    ...identity, ...pool(members), covered_quota_percent: Math.max(...members.map(covered)),
  }));
  const series = group(directWindows(windows, +now, ["confirmed"], true, null), true)
    .map(({ identity, members }) => {
      const { estimate_usd, window_count, account_count } = pool(members);
      return { ...identity, estimate_usd, window_count, account_count };
    });
  return {
    windows: measuredProducts(windows),
    products: productEstimates(windows, +now, "confirmed"),
    products_likely: productEstimates(windows, +now, "likely"),
    products_open: sorted(productsOpen, ["product", "window"]),
    product_series: sorted(series, ["product", "window", "period_start"]),
    agent_count: reports.length,
  };
}
