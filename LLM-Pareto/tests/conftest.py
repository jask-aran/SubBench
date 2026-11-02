# The LLM-Pareto scripts are standalone files, not a package; make them importable when the
# tests run from the repository root (CI) as well as from this directory.
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
