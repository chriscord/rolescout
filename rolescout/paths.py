"""Path resolution for the RoleScout repo, projects, and the per-user home dir.

Repo root = the directory holding the prototype assets (scripts/, references/,
.agents/skills/). Resolution order:
  1. env ROLESCOUT_ROOT (explicit override)
  2. walk up from cwd looking for the asset markers
  3. walk up from this package's location (editable install inside the repo)

Per-user state (telemetry, config) lives in ROLESCOUT_HOME (default ~/.rolescout),
NEVER inside the repo — career data stays in the repo's projects/, product
telemetry stays in the user's home (product-spec §7).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

_MARKERS = ("scripts", "references")


class RoleScoutError(RuntimeError):
    """User-facing error: print message, exit non-zero, no traceback."""


def _walk_up(start: Path) -> Path | None:
    p = start.resolve()
    while True:
        if all((p / m).is_dir() for m in _MARKERS) and (p / "scripts" / "store_io.py").exists():
            return p
        if p.parent == p:
            return None
        p = p.parent


def repo_root() -> Path:
    env = os.environ.get("ROLESCOUT_ROOT")
    if env:
        p = Path(env).resolve()
        if not all((p / m).is_dir() for m in _MARKERS):
            raise RoleScoutError(
                f"ROLESCOUT_ROOT={env} is not a RoleScout repo (missing {'/'.join(_MARKERS)})")
        return p
    found = _walk_up(Path.cwd()) or _walk_up(Path(__file__).parent)
    if not found:
        raise RoleScoutError(
            "Not inside a RoleScout repo. cd into your RoleScout clone "
            "(the folder containing scripts/ and references/), or set ROLESCOUT_ROOT.")
    return found


def home_dir() -> Path:
    env = os.environ.get("ROLESCOUT_HOME")
    p = Path(env).expanduser() if env else Path.home() / ".rolescout"
    p.mkdir(parents=True, exist_ok=True)
    return p


def telemetry_db_path() -> Path:
    return home_dir() / "telemetry.db"


def active_project_dir(root: Path | None = None) -> Path | None:
    """Resolve the active search project like scripts/store_io.py does (read-only).

    Returns None when unresolved instead of exiting — callers decide severity.
    """
    env = os.environ.get("RECRUITING_PROJECT_DIR")
    if env:
        p = Path(env).resolve()
        return p if (p / "project.json").exists() else None
    root = root or repo_root()
    ap = root / "active-project.json"
    if not ap.exists():
        return None
    try:
        rel = json.loads(ap.read_text(encoding="utf-8")).get("active", "")
    except (json.JSONDecodeError, OSError):
        return None
    p = (root / rel).resolve()
    return p if (p / "project.json").exists() else None


def manifest_path(root: Path | None = None) -> Path:
    return (root or repo_root()) / "human-inputs" / "manifest.json"
