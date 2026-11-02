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

export function EChart(props: {
  option: (size: Size) => any;
  onClick?: (params: any) => void;
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
    onCleanup(() => observer.disconnect());
  });

  createEffect(() => {
    if (!ready()) return;
    const s = size();
    if (!s.width || !s.height) return;
    // notMerge: a 2D option and a 3D option share nothing, so each replaces the last outright.
    chart.setOption(props.option(s), { notMerge: true, lazyUpdate: true });
  });

  onCleanup(() => chart?.dispose());
  return <div ref={element} class={props.class ?? "sb-chart"} role="img" aria-label={props.label} />;
}
