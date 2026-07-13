#!/usr/bin/env python3
"""Single local/CI release gate."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(*args: str) -> int:
    print("+", " ".join(args), flush=True)
    return subprocess.run(args, cwd=ROOT).returncode


def main() -> int:
    commands = [
        (sys.executable, "-m", "compileall", "-q", "rolenavi", "scripts", "tests"),
        (sys.executable, "-m", "pytest", "-q"),
        (sys.executable, "-m", "ruff", "check", "rolenavi"),
        (sys.executable, "scripts/test_deterministic_search.py"),
        (sys.executable, "scripts/test_score_finalize.py"),
        (sys.executable, "scripts/build_skills.py", "--check"),
        (sys.executable, "scripts/privacy_scan.py", "--history"),
    ]
    for command in commands:
        if run(*command):
            return 1
    print("PASS: RoleNavi release gate")
    return 0


if __name__ == "__main__":
    sys.exit(main())
