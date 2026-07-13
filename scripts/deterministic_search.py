#!/usr/bin/env python3
"""Run RoleNavi deterministic provider-first search.

This is a thin script wrapper so the runner can execute search through the same
subprocess and telemetry path as the existing validators and store writers.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from rolenavi.search.deterministic import main


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
