import { createMemo, createSignal, For, Show, type JSX } from "solid-js";
import { ChevronDownIcon } from "lucide-solid";
import { Popover, PopoverTrigger, PopoverContent } from "../conduit/menus";
import { SearchSection, Section, Item } from "../conduit/ui/popover";

/*
 * A grouped pick list in a popover: the trigger is a compact pill (a short label, the current value, a chevron),
 * the popover a search over titled sections. Used for the benchmark and for each chart axis.
 */

export type PickItem = { value: string; label: string; meta?: string; detail?: string; /** A shorter name, where the group title says the rest. */ short?: string };
export type PickGroup = { title: string; items: PickItem[] };

export function Picker(props: {
  label?: string; value: string; groups: PickGroup[]; onChoose: (value: string) => void;
  placeholder?: string; lead?: boolean; empty?: string; title?: string; footer?: JSX.Element; width?: "md" | "lg" | "xl";
}) {
  const [open, setOpen] = createSignal(false);
  const [query, setQuery] = createSignal("");
  const current = createMemo(() => props.groups.flatMap((g) => g.items).find((i) => i.value === props.value));
  const groups = createMemo(() => {
    const needle = query().trim().toLowerCase();
    if (!needle) return props.groups;
    return props.groups.map((g) => ({ ...g, items: g.items.filter((i) => `${i.label} ${g.title} ${i.detail ?? ""}`.toLowerCase().includes(needle)) }))
      .filter((g) => g.items.length);
  });
  const choose = (value: string) => { props.onChoose(value); setOpen(false); setQuery(""); };
  return <Popover open={open()} onOpenChange={(next) => { setOpen(next); if (!next) setQuery(""); }} placement="bottom-start">
    <PopoverTrigger class="sb-pick" data-lead={props.lead ? "" : undefined} data-empty={!current() ? "" : undefined} title={props.title ?? current()?.label}>
      <Show when={props.label}><b>{props.label}</b></Show>
      <span>{current()?.label ?? props.empty ?? "Choose"}</span>
      <ChevronDownIcon />
    </PopoverTrigger>
    <PopoverContent aria-label={props.title ?? props.label ?? "Choose"} width={props.width ?? "lg"}>
      <SearchSection placeholder={props.placeholder ?? "Find"} value={query()} onInput={setQuery} field={{ autofocus: true }} />
      <div class="sb-pick-list">
        <For each={groups()} fallback={<Section><Item disabled>No matches</Item></Section>}>{(group) =>
          <Section title={group.title}>
            <For each={group.items}>{(item) =>
              <Item checked={item.value === props.value} meta={item.meta} detail={item.detail} onClick={() => choose(item.value)}>{item.label}</Item>}</For>
          </Section>}</For>
      </div>
      {props.footer}
    </PopoverContent>
  </Popover>;
}

/** A popover of on/off rows (display options, families): each row toggles and the popover stays open. */
export function ToggleMenu(props: { label: string; icon?: JSX.Element; rows: Array<{ label: string; on: boolean; toggle: () => void; meta?: string; swatch?: string }>; actions?: JSX.Element; count?: string }) {
  const [open, setOpen] = createSignal(false);
  return <Popover open={open()} onOpenChange={setOpen} placement="bottom-end">
    <PopoverTrigger class="sb-pick" title={props.label}>{props.icon}<span>{props.label}{props.count ? ` · ${props.count}` : ""}</span><ChevronDownIcon /></PopoverTrigger>
    <PopoverContent aria-label={props.label} width="md">
      <div class="sb-pick-list">
        <Section>
          <For each={props.rows}>{(row) =>
            <Item picked={row.on} meta={row.meta} onClick={() => row.toggle()}
              icon={row.swatch ? <i class="ui-swatch" style={{ background: row.swatch }} /> : undefined}>{row.label}</Item>}</For>
        </Section>
      </div>
      {props.actions}
    </PopoverContent>
  </Popover>;
}
