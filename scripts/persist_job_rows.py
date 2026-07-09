#!/usr/bin/env python3
"""Normalize, validate, and upsert job rows with one deterministic command.

Usage:
  python scripts/persist_job_rows.py <rows.json> [--project projects/person--focus] [--keep]

The input may be a JSON list, {"rows": [...]}, {"job_rows": [...]}, or a
research-log-like object with kept candidates that already use job_list fields.
Temporary files are written under <project>/data/ and removed unless --keep is
passed.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from location_normalize import normalize_job_rows
from normalize_job_url import build as build_job_url
from schema_defs import JOB_LIST_COLUMNS


ROOT = Path(__file__).resolve().parents[1]


def _configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def _active_project() -> Path:
    env = os.environ.get("RECRUITING_PROJECT_DIR")
    if env:
        return Path(env)
    active = ROOT / "active-project.json"
    if active.exists():
        data = json.loads(active.read_text(encoding="utf-8"))
        value = data.get("active") or data.get("project") or data.get("code")
        if value:
            return _resolve_project_arg(Path(str(value)))
    raise FileNotFoundError("project not specified and no active project found")


def _resolve_project_arg(value: Path) -> Path:
    if value.is_absolute():
        return value
    candidate = (ROOT / value)
    if (candidate / "project.json").exists():
        return candidate
    return ROOT / "projects" / str(value)


def _extract_rows(payload) -> list[dict]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("rows", "job_rows"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
        candidates = payload.get("candidates")
        if isinstance(candidates, list):
            rows = []
            for item in candidates:
                if isinstance(item, dict) and item.get("decision") == "kept":
                    if isinstance(item.get("row"), dict):
                        rows.append(item["row"])
                    else:
                        rows.append(item)
            return rows
    raise ValueError("input must contain a list of job row objects")


def _normalize_rows(rows: list[dict]) -> list[dict]:
    normalized: list[dict] = []
    allowed = set(JOB_LIST_COLUMNS)
    for row in rows:
        item = dict(row)
        source_url = str(item.get("source_url", "")).strip()
        if source_url:
            info = build_job_url(source_url, item.get("company", ""),
                                 item.get("title", ""))
            item["source_url"] = info["canonical_url"]
            item.setdefault("job_id", info["job_id"])
        item = {key: value for key, value in item.items() if key in allowed}
        normalized.append(item)
    return normalize_job_rows(normalized)


def _run(cmd: list[str], project: Path) -> subprocess.CompletedProcess:
    env = {**os.environ, "RECRUITING_PROJECT_DIR": str(project),
           "PYTHONIOENCODING": "utf-8"}
    return subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace",
                          env=env, cwd=str(ROOT))


def persist(rows_path: Path, project: Path, keep: bool = False) -> int:
    payload = json.loads(rows_path.read_text(encoding="utf-8"))
    rows = _normalize_rows(_extract_rows(payload))
    data_dir = project / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    normalized_path = data_dir / "_persist_job_rows_normalized.json"
    normalized_path.write_text(
        json.dumps(rows, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    validate_cmd = [
        sys.executable,
        str(ROOT / "scripts" / "validate_job_rows.py"),
        str(normalized_path),
    ]
    existing = data_dir / "job_list.csv"
    if existing.exists():
        validate_cmd += ["--existing", str(existing)]
    validate = _run(validate_cmd, project)
    print(validate.stdout, end="")
    if validate.stderr:
        print(validate.stderr, end="", file=sys.stderr)
    if validate.returncode != 0:
        if not keep:
            normalized_path.unlink(missing_ok=True)
        return validate.returncode

    upsert = _run([
        sys.executable,
        str(ROOT / "scripts" / "upsert_rows.py"),
        "job_list",
        str(normalized_path),
    ], project)
    print(upsert.stdout, end="")
    if upsert.stderr:
        print(upsert.stderr, end="", file=sys.stderr)
    if not keep:
        normalized_path.unlink(missing_ok=True)
    return upsert.returncode


def main(argv: list[str] | None = None) -> int:
    _configure_stdio()
    ap = argparse.ArgumentParser(description="Persist validated job rows.")
    ap.add_argument("rows_json", type=Path)
    ap.add_argument("--project", type=Path, default=None)
    ap.add_argument("--keep", action="store_true",
                    help="keep normalized intermediate rows under project/data")
    args = ap.parse_args(argv)
    try:
        project = _resolve_project_arg(args.project) if args.project else _active_project()
        return persist(args.rows_json, project, keep=args.keep)
    except (OSError, ValueError, json.JSONDecodeError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
