#!/usr/bin/env node
// Preview of the built site (dist/site, from `npm run build`) with no Cloudflare account.
// `npm run dev` is the live-reloading development server; this one serves exactly what
// `wrangler deploy` uploads.
//
//   node scripts/dev-server.mjs [port]            built site + demo API
//   node scripts/dev-server.mjs [port] --proxy    built site + the live API
//   node scripts/dev-server.mjs [port] --api-dir DIR  built site + saved reports (DIR/history.json, ...)
//
// The demo API is synthetic and labels itself as such on every page. `--proxy` forwards
// /api/* to https://subbench.jask-aran.com so the new page can be tried on real data.
// `npx wrangler dev` remains the way to run the real Worker against a local D1.

import http from "node:http";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { demoReports } from "./demo-api.mjs";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../dist/site");
const args = process.argv.slice(2);
const PORT = Number(args.find((arg) => /^\d+$/.test(arg)) || 8787);
const PROXY = args.includes("--proxy") ? "https://subbench.jask-aran.com" : null;
const API_DIR = args.includes("--api-dir") ? path.resolve(args[args.indexOf("--api-dir") + 1]) : null;

const MIME = {
  ".html": "text/html; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".mjs": "text/javascript; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".svg": "image/svg+xml",
  ".png": "image/png",
  ".ico": "image/x-icon",
};

function send(res, status, body, type = "text/plain; charset=utf-8") {
  res.writeHead(status, { "content-type": type, "cache-control": "no-cache" });
  res.end(body);
}

// Mirrors Cloudflare's default html_handling: /frontier and /frontier/ both serve
// frontier/index.html.
function resolve(urlPath) {
  const clean = decodeURIComponent(urlPath.split("?")[0].split("#")[0]);
  const target = path.normalize(path.join(ROOT, clean));
  if (!target.startsWith(ROOT)) return null;
  for (const candidate of [target, path.join(target, "index.html"), target + ".html"]) {
    try {
      if (fs.statSync(candidate).isFile()) return candidate;
    } catch { /* try the next form */ }
  }
  return null;
}

http.createServer(async (req, res) => {
  const url = new URL(req.url, "http://localhost");
  if (url.pathname.startsWith("/api/")) {
    if (PROXY) {
      try {
        const upstream = await fetch(PROXY + url.pathname);
        return send(res, upstream.status, await upstream.text(), "application/json; charset=utf-8");
      } catch (error) {
        return send(res, 502, JSON.stringify({ error: String(error) }), "application/json; charset=utf-8");
      }
    }
    const kind = url.pathname.replace(/^\/api\//, "").replace(/\/$/, "");
    if (API_DIR) {
      const saved = path.join(API_DIR, path.basename(kind) + ".json");
      return fs.existsSync(saved)
        ? send(res, 200, fs.readFileSync(saved), "application/json; charset=utf-8")
        : send(res, 404, JSON.stringify({ error: "not saved" }), "application/json; charset=utf-8");
    }
    const reports = demoReports();
    if (!(kind in reports)) return send(res, 404, JSON.stringify({ error: "not found" }), "application/json; charset=utf-8");
    return send(res, 200, JSON.stringify(reports[kind]), "application/json; charset=utf-8");
  }
  const file = resolve(url.pathname);
  if (!file) {
    const notFound = path.join(ROOT, "404.html");
    return fs.existsSync(notFound) ? send(res, 404, fs.readFileSync(notFound), MIME[".html"]) : send(res, 404, "Not found");
  }
  send(res, 200, fs.readFileSync(file), MIME[path.extname(file)] || "application/octet-stream");
}).listen(PORT, "0.0.0.0", () => {
  console.log(`SubBench site on http://localhost:${PORT} (${PROXY ? "live API via proxy" : API_DIR ? `saved reports from ${API_DIR}` : "demo API"})`);
});
