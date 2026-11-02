# Conduit components v2 (vendored)

These files are copied from [jask-aran/Conduit](https://github.com/jask-aran/Conduit),
`conduit-web/src/components/ui/` and `conduit-web/src/components/menus.tsx`, at commit
`25ca3ca81225e96c0d5617e2a947981d07df87dd`. They are the families SubBench's site uses:
controls, frost, popover, nav, dashboard, header, card, pill, expand, icons, and their
styles, with Paper Mono.

Conduit is licensed under the GNU AGPL v3 (`LICENSE` here); these files keep that licence.
The rest of SubBench is MIT.

Changes from upstream, kept as small as possible so a re-copy is a plain diff:

- `ui/haptics.ts`: no Capacitor plugin; a browser's `navigator.vibrate` stands in for it.
- `ui/styles/foundations.css`: the Inconsolata import and the Meslo terminal font are
  removed (SubBench draws no terminal).
- `ui/styles/nav.css`: the wordmark's font face points at `ui/fonts/druk-wide-web-super.woff2`
  (Druk Wide Super, copied from `conduit-web/src/client/assets/fonts/`). Druk is a commercial
  typeface: this copy is only as licensed as Conduit's.

Do not style these pieces from outside. Per Conduit's DESIGN.md, a piece that needs more is
changed here, in the piece, and the site's own layout lives in `web/src/site.css`.
