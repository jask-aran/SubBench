// What each plan costs its subscriber: list prices in US dollars per month, as published by the provider. The
// collector measures what a plan delivers (its API-equivalent value per weekly limit); this is the other side of the
// ratio. Prices change: edit this table, with its date, when a provider changes one.
//
// Checked 2026-11-02 against the providers' pricing pages as reported by third-party pricing summaries.
// "ChatGPT Pro" is the $200 tier; a $100 Pro tier launched in April 2026 and the meter does not yet say which one an
// account holds, so a $100 subscriber's value per dollar reads half its true figure until the collector reports it.
export const PRICES_CHECKED = "2026-11-02";
export const PLAN_PRICES = {
  "claude pro": 20,
  "claude max 5x": 100,
  "claude max 20x": 200,
  "chatgpt go": 8,
  "chatgpt plus": 20,
  "chatgpt pro": 200,
};

const WEEKS_PER_MONTH = 52 / 12;

export function planPrice(name) {
  const monthly = PLAN_PRICES[String(name || "").trim().toLowerCase()];
  return Number.isFinite(monthly) ? { monthly, weekly: monthly / WEEKS_PER_MONTH } : null;
}

// How many times its price a plan's measured weekly value is: 15 means a week of use would have cost 15 times the
// week's share of the subscription at public API prices.
export function valueMultiple(name, weeklyValue) {
  const price = planPrice(name);
  return price && weeklyValue > 0 ? weeklyValue / price.weekly : null;
}

// Benchmark tasks per dollar of subscription: the tasks one weekly limit buys over the week's share of the price.
export function tasksPerDollar(name, weeklyValue, costPerTask) {
  const price = planPrice(name);
  return price && weeklyValue > 0 && costPerTask > 0 ? weeklyValue / costPerTask / price.weekly : null;
}
