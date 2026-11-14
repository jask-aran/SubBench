import { createEffect, createSignal, onCleanup, onMount, type JSX } from "solid-js";

/*
 * One ECharts instance for a chart that switches between 2D and 3D. ECharts draws the 2D views on canvas and, with
 * echarts-gl, the 3D views in WebGL on the same element, so switching is a setOption on the same instance: no
 * library swap, no reload. Both are loaded on first use and kept.
 */

let enginePromise: Promise<any> | undefined;
export function engine() {
  return (enginePromise ??= Promise.all([import("echarts"), import("echarts-gl")]).then(([echarts]) => echarts));
}

export type Size = { width: number; height: number };

/** What a page can do to a chart after drawing it. */
export type ChartApi = {
  snap: (view: { alpha: number; beta: number; distance?: number }) => void;
  /** Bring one named series forward and fade the rest; null restores them all. */
  focus: (series: string | null) => void;
};

export function EChart(props: {
  option: (size: Size) => any;
  onClick?: (params: any) => void;
  api?: (api: ChartApi) => void;
  class?: string;
  label?: string;
}) {
  let element!: HTMLDivElement;
  let chart: any;
  const [size, setSize] = createSignal<Size>({ width: 0, height: 0 });
  const [ready, setReady] = createSignal(false);

  onMount(async () => {
    const echarts = await engine();
    if (!element) return;
    chart = echarts.init(element, null, { renderer: "canvas" });
    (element as any).__chart = chart;
    chart.on("click", (params: any) => props.onClick?.(params));
    const observer = new ResizeObserver(() => {
      setSize({ width: element.clientWidth, height: element.clientHeight });
      chart?.resize();
    });
    observer.observe(element);
    setSize({ width: element.clientWidth, height: element.clientHeight });
    setReady(true);
    // A 3D view turns to a named angle by animating the camera, not by redrawing the chart.
    props.api?.({
      snap: (view) => chart?.setOption({ grid3D: { viewControl: {
        alpha: view.alpha, beta: view.beta, ...(view.distance ? { distance: view.distance } : {}),
        animation: true, animationDurationUpdate: 650, animationEasingUpdate: "cubicInOut" } } }),
      focus: (series) => {
        chart?.dispatchAction({ type: "downplay" });
        if (series) chart?.dispatchAction({ type: "highlight", seriesName: series });
      },
    });
    onCleanup(() => observer.disconnect());
  });

  createEffect(() => {
    if (!ready()) return;
    const s = size();
    if (!s.width || !s.height) return;
    // notMerge: a 2D option and a 3D option share nothing, so each replaces the last outright. A 3D chart drawn again
    // (a new axis, a filter, a click) keeps the angle the reader turned it to: echarts-gl writes the camera back into
    // the option as it moves, and that camera is carried into the new option.
    const next = props.option(s);
    const camera = next?.grid3D ? chart.getOption?.()?.grid3D?.[0]?.viewControl : null;
    if (camera && Number.isFinite(camera.alpha)) {
      next.grid3D.viewControl = { ...next.grid3D.viewControl, alpha: camera.alpha, beta: camera.beta,
        ...(Number.isFinite(camera.distance) ? { distance: camera.distance } : {}), ...(camera.center ? { center: camera.center } : {}) };
    }
    chart.setOption(next, { notMerge: true, lazyUpdate: true });
  });

  onCleanup(() => chart?.dispose());
  return <div ref={element} class={props.class ?? "sb-chart"} role="img" aria-label={props.label} />;
}
