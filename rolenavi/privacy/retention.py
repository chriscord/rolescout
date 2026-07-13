"""Deterministic privacy audit, runtime cleanup, and person deletion manifests."""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

from ..paths import home_dir, repo_root, telemetry_db_path

RUNTIME_RELATIVE = (
    "runs",
    "runtime",
    "data/chat-session.json",  # legacy location
    "targets/deterministic-search",
)


def _entry(path: Path, root: Path, kind: str) -> dict:
    try:
        rel = path.relative_to(root).as_posix()
    except ValueError:
        rel = str(path)
    if path.is_file():
        count, size = 1, path.stat().st_size
    else:
        files = [item for item in path.rglob("*") if item.is_file()]
        count, size = len(files), sum(item.stat().st_size for item in files)
    return {"kind": kind, "path": rel, "files": count, "bytes": size}


def privacy_audit(project: Path | None = None) -> dict:
    root = repo_root()
    targets: list[tuple[Path, str]] = []
    projects = [project] if project else [p for p in (root / "projects").glob("*") if p.is_dir()]
    for proj in projects:
        if proj is None:
            continue
        for rel in RUNTIME_RELATIVE:
            path = proj / rel
            if path.exists():
                targets.append((path, "project-runtime"))
    runtime = home_dir() / "runtime"
    if runtime.exists():
        targets.append((runtime, "global-runtime"))
    mock_runs = home_dir() / "mock-runs"
    if mock_runs.exists():
        targets.append((mock_runs, "mock-runtime"))
    telemetry = telemetry_db_path()
    if telemetry.exists():
        targets.append((telemetry, "metrics-telemetry"))
    entries = [_entry(path, root, kind) for path, kind in targets]
    return {
        "schema": "rolenavi-privacy-audit-v1",
        "entries": entries,
        "totals": {"files": sum(e["files"] for e in entries),
                   "bytes": sum(e["bytes"] for e in entries)},
        "notes": [
            "Global telemetry is metrics-only; raw provider logging is off by default.",
            "Run `rolenavi clean --runtime` for a dry-run deletion manifest.",
        ],
    }


def clean_runtime(project: Path | None = None, *, apply: bool = False,
                  older_than_days: int = 30) -> dict:
    root = repo_root()
    cutoff = time.time() - max(0, older_than_days) * 86400
    candidates: list[Path] = []
    projects = [project] if project else [p for p in (root / "projects").glob("*") if p.is_dir()]
    for proj in projects:
        if proj is None:
            continue
        for rel in RUNTIME_RELATIVE:
            path = proj / rel
            if path.exists() and (older_than_days == 0 or path.stat().st_mtime < cutoff):
                candidates.append(path)
    runtime = home_dir() / "runtime"
    if runtime.exists() and (older_than_days == 0 or runtime.stat().st_mtime < cutoff):
        candidates.append(runtime)
    mock_runs = home_dir() / "mock-runs"
    if mock_runs.exists() and (older_than_days == 0 or mock_runs.stat().st_mtime < cutoff):
        candidates.append(mock_runs)
    entries = [_entry(path, root, "delete") for path in candidates]
    if apply:
        for path in candidates:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink(missing_ok=True)
    return {"schema": "rolenavi-clean-manifest-v1", "dry_run": not apply,
            "older_than_days": older_than_days, "entries": entries}


def delete_person(person: str, *, apply: bool = False) -> dict:
    root = repo_root()
    person = str(person or "").strip()
    if not person or any(ch not in "abcdefghijklmnopqrstuvwxyz0123456789-" for ch in person):
        raise ValueError("person must be a lowercase slug")
    candidates: list[Path] = []
    profile = root / "profiles" / person
    if profile.is_dir():
        candidates.append(profile)
    for project in (root / "projects").glob("*"):
        meta = project / "project.json"
        if not meta.exists():
            continue
        try:
            doc = json.loads(meta.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if str(doc.get("person", "")) == person:
            candidates.append(project)
    entries = [_entry(path, root, "delete-person") for path in candidates]
    if apply:
        for path in candidates:
            shutil.rmtree(path)
    return {"schema": "rolenavi-delete-person-manifest-v1", "person": person,
            "dry_run": not apply, "entries": entries,
            "note": "Metrics telemetry contains no person/project identifiers."}


def print_manifest(manifest: dict) -> None:
    print(json.dumps(manifest, indent=2, ensure_ascii=False))
