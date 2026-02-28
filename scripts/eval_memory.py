#!/usr/bin/env python3
"""CLI wrapper for memory retrieval evaluation."""

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from opencortex.eval.memory_eval import run_cli  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(run_cli())

