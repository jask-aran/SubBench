import { defineConfig, type Plugin } from "vite";
import solid from "vite-plugin-solid";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { PHONE_LAYOUT_QUERY, NON_PHONE_LAYOUT_QUERY } from "./src/conduit/ui/layout";

const root = path.dirname(fileURLToPath(import.meta.url));

// The Conduit library's stylesheets name their layouts as custom media; Conduit's own
// build substitutes the queries, and so does this one.
function conduitMedia(): Plugin {
  const replacements: Array<[string, string]> = [
    ["@media (--conduit-phone-layout)", `@media ${PHONE_LAYOUT_QUERY}`],
    ["@media (--conduit-wide-layout)", `@media ${NON_PHONE_LAYOUT_QUERY}`],
  ];
  return {
    name: "conduit-custom-media",
    enforce: "pre",
    transform(code, id) {
      if (!id.split("?", 1)[0].endsWith(".css")) return null;
      let out = code;
      for (const [from, to] of replacements) out = out.replaceAll(from, to);
      return out === code ? null : { code: out, map: null };
    },
  };
}

// `npm run dev` serves /api/* from the synthetic demo reports (every response says
// demo: true and the page shows it). SUBBENCH_API=https://subbench.jask-aran.com proxies
// the live Worker instead.
function demoApi(): Plugin {
  return {
    name: "subbench-demo-api",
    configureServer(server) {
      if (process.env.SUBBENCH_API) return;
      server.middlewares.use(async (req, res, next) => {
        const match = /^\/api\/(\w+)\/?$/.exec((req.url || "").split("?")[0]);
        if (!match) return next();
        // @ts-ignore -- a plain ES module with no types
        const { demoReports } = await import("../scripts/demo-api.mjs");
        const reports = demoReports() as Record<string, unknown>;
        res.setHeader("content-type", "application/json; charset=utf-8");
        if (!(match[1] in reports)) { res.statusCode = 404; res.end('{"error":"not found"}'); return; }
        res.end(JSON.stringify(reports[match[1]]));
      });
    },
  };
}

export default defineConfig({
  root,
  base: "/",
  plugins: [conduitMedia(), demoApi(), solid()],
  resolve: { alias: { "@": path.join(root, "src") } },
  server: {
    host: "0.0.0.0",
    port: 5173,
    proxy: process.env.SUBBENCH_API ? { "/api": { target: process.env.SUBBENCH_API, changeOrigin: true } } : undefined,
  },
  build: {
    outDir: path.join(root, "../dist/site"),
    emptyOutDir: true,
    target: "es2022",
    chunkSizeWarningLimit: 4000,
    rollupOptions: {
      input: {
        overview: path.join(root, "index.html"),
        subscriptions: path.join(root, "subscriptions/index.html"),
        frontier: path.join(root, "frontier/index.html"),
        surface: path.join(root, "surface/index.html"),
        notfound: path.join(root, "404.html"),
      },
    },
  },
});
