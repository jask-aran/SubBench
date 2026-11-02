import { createEffect, onCleanup, onMount, type JSX } from "solid-js";
import { alpha, fontOf, token } from "./tokens";

/*
 * One Plotly chart. Plotly is loaded on demand and split by page: the 2D pages take the basic bundle (scatter, bar),
 * the surface page the gl3d bundle (scatter3d, surface). The chart re-renders with Plotly.react whenever its data or
 * layout change, follows its container's size, and is purged when the component goes.
 */

type PlotlyModule = any;
export type Loader = () => Promise<PlotlyModule>;

let basicPromise: Promise<PlotlyModule> | undefined;
let gl3dPromise: Promise<PlotlyModule> | undefined;
export const basic: Loader = () => (basicPromise ??= import("plotly.js-basic-dist-min").then((m: any) => m.default ?? m));
export const gl3d: Loader = () => (gl3dPromise ??= import("plotly.js-gl3d-dist-min").then((m: any) => m.default ?? m));

/** The house style every chart starts from: transparent ground, Conduit's faces and tones, hairline grid. */
export function theme() {
  const ink = token("--foreground");
  const muted = token("--muted-foreground");
  const grid = token("--border");
  const sans = fontOf("--font-sans");
  const data = fontOf("--font-data");
  const axis = {
    gridcolor: grid, zerolinecolor: grid, linecolor: alpha(muted, 0.35), tickcolor: alpha(muted, 0.35),
    tickfont: { family: data, size: 11, color: muted }, title: { font: { family: sans, size: 12, color: muted } },
    showline: false, zeroline: false, ticks: "", automargin: true,
  };
  return {
    ink, muted, grid, sans, data, axis,
    layout: {
      paper_bgcolor: "rgba(0,0,0,0)", plot_bgcolor: "rgba(0,0,0,0)",
      font: { family: sans, size: 12, color: ink },
      margin: { l: 56, r: 16, t: 12, b: 44 },
      hovermode: "closest", dragmode: false, showlegend: false,
      hoverlabel: {
        bgcolor: token("--popover"), bordercolor: alpha(ink, 0.18),
        font: { family: sans, size: 12, color: ink }, align: "left",
      },
      xaxis: { ...axis }, yaxis: { ...axis },
    },
    config: { displayModeBar: false, responsive: true, scrollZoom: false, displaylogo: false },
  };
}

export function Plot(props: {
  plotly: Loader;
  data: () => any[];
  layout: () => Record<string, any>;
  config?: Record<string, any>;
  class?: string;
  style?: JSX.CSSProperties;
  label?: string;
  onHover?: (event: any) => void;
  onUnhover?: (event: any) => void;
  onClick?: (event: any) => void;
  onRelayout?: (event: any) => void;
  ref?: (api: { element: HTMLDivElement; plotly: PlotlyModule }) => void;
}) {
  let element!: HTMLDivElement;
  let plotly: PlotlyModule | undefined;
  let alive = true;
  const config = () => ({ ...theme().config, ...(props.config || {}) });

  onMount(async () => {
    plotly = await props.plotly();
    if (!alive) return;
    await plotly.newPlot(element, props.data(), props.layout(), config());
    const host = element as any;
    if (props.onHover) host.on("plotly_hover", props.onHover);
    if (props.onUnhover) host.on("plotly_unhover", props.onUnhover);
    if (props.onClick) host.on("plotly_click", props.onClick);
    if (props.onRelayout) host.on("plotly_relayout", props.onRelayout);
    props.ref?.({ element, plotly });
    const observer = new ResizeObserver(() => plotly?.Plots.resize(element));
    observer.observe(element);
    onCleanup(() => observer.disconnect());
  });

  createEffect(() => {
    const data = props.data();
    const layout = props.layout();
    if (plotly && element) plotly.react(element, data, layout, config());
  });

  onCleanup(() => { alive = false; if (plotly && element) plotly.purge(element); });

  return <div ref={element} class={props.class ?? "sb-chart"} style={props.style} role="img" aria-label={props.label} />;
}
