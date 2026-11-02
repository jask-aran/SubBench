import { For, Show, type JSX } from "solid-js";
import { Swatch } from "../conduit/ui/controls";

/*
 * Site pieces: what a public analytics page needs and Conduit's app library does not draw (a page hero, a figure, a
 * chart's legend, ranked bars). They take their sizes, faces and tones from the library's tokens.
 */

export function Hero(props: { kicker: string; title: JSX.Element; lede: JSX.Element; small?: boolean; actions?: JSX.Element }) {
  return <header class="sb-hero">
    <p class="sb-kicker">{props.kicker}</p>
    <h1 class="sb-title" classList={{ small: !!props.small }}>{props.title}</h1>
    <p class="sb-lede">{props.lede}</p>
    <Show when={props.actions}><div class="sb-actions">{props.actions}</div></Show>
  </header>;
}

export function Section(props: { title: string; note?: JSX.Element; tools?: JSX.Element; id?: string; children: JSX.Element }) {
  return <section class="sb-section" id={props.id} aria-label={props.title}>
    <div class="sb-section-head">
      <div><h2>{props.title}</h2><Show when={props.note}><p>{props.note}</p></Show></div>
      <Show when={props.tools}><div class="sb-section-tools">{props.tools}</div></Show>
    </div>
    {props.children}
  </section>;
}

export function Inset(props: { title?: string; note?: JSX.Element; aside?: JSX.Element; flush?: boolean; tight?: boolean; href?: string; children?: JSX.Element }) {
  const head = () => <Show when={props.title}><div class="sb-inset-head">
    <div><h3>{props.title}</h3><Show when={props.note}><p>{props.note}</p></Show></div>{props.aside}</div></Show>;
  return <Show when={props.href} fallback={<div class="sb-inset" classList={{ flush: !!props.flush, tight: !!props.tight }}>{head()}{props.children}</div>}>
    <a class="sb-inset" classList={{ tight: !!props.tight }} href={props.href}>{head()}{props.children}</a>
  </Show>;
}

export function Figure(props: { label: string; colour?: string; value?: JSX.Element; unit?: string; meta?: JSX.Element; empty?: string }) {
  return <div class="sb-figure">
    <div class="sb-figure-label"><Show when={props.colour}><Swatch colour={props.colour} /></Show>{props.label}</div>
    <Show when={props.value !== undefined && props.value !== null} fallback={<div class="sb-figure-empty">{props.empty ?? "Not enough evidence yet"}</div>}>
      <div class="sb-figure-value">{props.value}<Show when={props.unit}><small>{props.unit}</small></Show></div>
    </Show>
    <Show when={props.meta}><div class="sb-figure-meta">{props.meta}</div></Show>
  </div>;
}

export type LegendItem = { label: string; colour?: string; kind?: "line" | "dot" | "dash" | "dotted" | "ring" };
export function Legend(props: { items: LegendItem[] }) {
  return <div class="sb-legend"><For each={props.items}>{(item) =>
    <span><i data-dot={item.kind === "dot" ? "" : undefined} data-dash={item.kind === "dash" ? "" : item.kind === "dotted" ? "dot" : undefined} data-ring={item.kind === "ring" ? "" : undefined}
      style={{ background: item.kind === "dash" || item.kind === "dotted" || item.kind === "ring" ? undefined : item.colour, "border-color": item.colour }} />{item.label}</span>}</For></div>;
}

export type BarRow = { key: string; label: string; colour: string; value: number; display: string; sub?: string; tag?: string; title?: string };
export function Bars(props: { rows: BarRow[]; max?: number; empty?: string }) {
  const top = () => props.max ?? Math.max(1e-9, ...props.rows.map((row) => row.value));
  return <Show when={props.rows.length} fallback={<div class="sb-chart-empty">{props.empty ?? "Nothing to show."}</div>}>
    <div class="sb-rows"><For each={props.rows}>{(row) =>
      <div class="sb-bar-row" title={row.title}>
        <span class="name"><Swatch colour={row.colour} /><span>{row.label}</span><Show when={row.tag}><span class="sb-tag">{row.tag}</span></Show></span>
        <span class="track"><i style={{ width: `${Math.max(1, (row.value / top()) * 100)}%`, background: row.colour }} /></span>
        <span class="value">{row.display}<Show when={row.sub}><small>{row.sub}</small></Show></span>
      </div>}</For></div>
  </Show>;
}

export function FamilyChips(props: { families: Array<{ name: string; colour: string; count: number }>; hidden: Set<string>; onToggle: (name: string) => void; onSolo: (name: string) => void }) {
  return <div class="sb-chips" role="group" aria-label="Families">
    <For each={props.families}>{(family) =>
      <button type="button" class="sb-chip" aria-pressed={!props.hidden.has(family.name)} title="Click to hide; double-click to show only this family"
        onClick={() => props.onToggle(family.name)} onDblClick={() => props.onSolo(family.name)}>
        <Swatch colour={family.colour} />{family.name}<small>{family.count}</small>
      </button>}</For>
  </div>;
}

export const Skeleton = (props: { height?: string }) => <div class="sb-skeleton" style={{ height: props.height ?? "12rem" }} />;

export const link = (href: string) => () => location.assign(href);
