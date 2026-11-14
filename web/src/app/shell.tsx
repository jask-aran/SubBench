import { createSignal, For, onMount, Show, type JSX } from "solid-js";
import { render } from "solid-js/web";
import { WalletIcon, ChartScatterIcon, MenuIcon, ExternalLinkIcon, XIcon, PanelLeftCloseIcon, PanelLeftOpenIcon } from "lucide-solid";
import { NavPane, NavBrand, NavGroup, NavRow, NavRail, NavRailSection, RailButton } from "../conduit/ui/nav";
import { Header, HeaderCrumb, HeaderButton, HeaderNote } from "../conduit/ui/header";
import { health, healthState, refreshHealth, poll } from "./data";
// @ts-ignore
import { ago } from "../lib/format.js";
import "./styles";

export type PageKey = "subscriptions" | "models" | "notfound";

const PAGES: Array<{ key: PageKey; href: string; label: string; icon: () => JSX.Element }> = [
  { key: "subscriptions", href: "/", label: "Subscriptions", icon: () => <WalletIcon /> },
  { key: "models", href: "/models/", label: "Models", icon: () => <ChartScatterIcon /> },
];

const go = (href: string) => (event: MouseEvent) => {
  if (event.metaKey || event.ctrlKey || event.button === 1) window.open(href, "_blank");
  else location.assign(href);
};

/** The frame every page shares: Conduit's sidebar on the frame, the page in an inset pane with its header. */
export function Site(props: { page: PageKey; title: string; place?: string; actions?: JSX.Element; wide?: boolean; fill?: boolean; children: JSX.Element }) {
  const [drawer, setDrawer] = createSignal<"open" | "shut">("shut");
  // Narrow, the sidebar is Conduit's phone drawer over the page; wide, it sits on the frame.
  const narrowQuery = matchMedia("(max-width: 760px)");
  const [narrow, setNarrow] = createSignal(narrowQuery.matches);
  narrowQuery.addEventListener("change", () => { setNarrow(narrowQuery.matches); setDrawer("shut"); });
  // Folded to its rail on a desktop, remembered across pages.
  const [folded, setFolded] = createSignal((() => { try { return localStorage.getItem("sb-nav") === "folded"; } catch { return false; } })());
  const fold = (next: boolean) => { setFolded(next); try { localStorage.setItem("sb-nav", next ? "folded" : "open"); } catch { /* not remembered */ } };
  onMount(() => poll(refreshHealth));
  const status = () => healthState();
  return <div class="sb-frame" data-drawer={drawer()}>
    <NavPane drawer={narrow() ? drawer() : undefined} collapsed={!narrow() && folded()} folded={!narrow() && folded()}
      head={<><NavBrand name="SubBench" onClick={go("/")} />
      <Show when={narrow()} fallback={<HeaderButton class="sb-nav-toggle" label={folded() ? "Expand sidebar" : "Collapse sidebar"}
        icon={folded() ? <PanelLeftOpenIcon /> : <PanelLeftCloseIcon />} onClick={() => fold(!folded())} />}>
        <HeaderButton label="Close menu" icon={<XIcon />} onClick={() => setDrawer("shut")} /></Show></>}
      foot={<>
        <div class="sb-nav-status" data-state={status().state}>
          <span class="sb-dot" />
          <span>{status().state === "live" ? `Updated ${ago(health()?.last_report_at)}` : status().state === "demo" ? "Demo data" : status().state === "stale" ? "Stale" : "Offline"}</span>
        </div>
      </>}>
      <NavRail label="Pages">
        <NavRailSection>
          <For each={PAGES}>{(page) => <RailButton aria-label={page.label} title={page.label} current={page.key === props.page} onClick={go(page.href)}>{page.icon()}</RailButton>}</For>
        </NavRailSection>
      </NavRail>
      <NavGroup label="Pages">
        <For each={PAGES}>{(page) =>
          <NavRow icon={page.icon()} current={page.key === props.page} onClick={go(page.href)}>{page.label}</NavRow>}</For>
      </NavGroup>
      <NavGroup label="Sources">
        <NavRow icon={<ExternalLinkIcon />} onClick={go("https://github.com/jask-aran/SubBench")}>SubBench collector</NavRow>
        <NavRow icon={<ExternalLinkIcon />} onClick={go("https://artificialanalysis.ai")}>Artificial Analysis</NavRow>
        <NavRow icon={<ExternalLinkIcon />} onClick={go("https://deepswe.datacurve.ai")}>DeepSWE</NavRow>
      </NavGroup>
    </NavPane>
    <div class="sb-scrim" onClick={() => setDrawer("shut")} />
    <main class="sb-pane" id="main">
      <Header after={<Show when={status().state === "demo"}><HeaderNote label="Demo data" title="The development server serves synthetic subscription reports" /></Show>}
        actions={<>{props.actions}<HeaderButton class="sb-menu-button" label="Menu" icon={<MenuIcon />} onClick={() => setDrawer(drawer() === "open" ? "shut" : "open")} /></>}>
        <HeaderCrumb place={props.place ?? "SubBench"} title={props.title} />
      </Header>
      <div class="sb-scroll">
        <div class="sb-content" classList={{ bleed: !!props.wide, fill: !!props.fill }}>
          {props.children}
          <Foot />
        </div>
      </div>
    </main>
  </div>;
}

function Foot() {
  const status = () => healthState();
  const h = () => health();
  return <footer class="sb-foot" data-stale={status().state === "stale" || status().state === "down" ? "" : undefined}>
    <span>
      <Show when={h()} fallback={"Subscription data offline"}>
        {(status().state === "stale" ? "Stale · " : "") + (h()?.demo ? "Demo data · " : "")
          + `Updated ${ago(h()?.last_report_at)} · ${Number(h()?.entitlement_rows ?? 0).toLocaleString()} readings`}
      </Show>
    </span>
    <span>Benchmarks: Artificial Analysis, DeepSWE, FrontierCode</span>
  </footer>;
}

export function mount(component: () => JSX.Element) {
  document.documentElement.classList.add("dark");
  render(component, document.getElementById("app")!);
}
