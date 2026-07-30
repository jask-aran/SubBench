"""Worker entry module.

Sits beside the package rather than inside it. Wrangler bundles the directory containing
the entry file and loads that file as a *top-level* module, so an entry inside the package
cannot use relative imports -- Pyodide reports "attempted relative import with no known
parent package". Keeping it here means `subbench` is bundled as a real package and its
internal relative imports resolve.
"""
from subbench.server.entry import Default

__all__ = ["Default"]
