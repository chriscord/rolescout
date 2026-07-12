#!/usr/bin/env python3
"""Finalize a RoleScout score run with runner-owned deterministic writes.

The score agent owns qualitative judgment: job groups and per-criterion
`strategy/job-ratings.json`. This script owns the mechanical steps that must not
depend on an agent sandbox:

  1. run scripts/score_jobs.py to compute strategy/job-scores.json
  2. write fit_score/priority/job_group updates back to job_list
  3. rebuild the SQLite job_visibility selection

Exit codes:
  0 = all visible rows have ratings+scores and updates were applied
  2 = mechanically succeeded, but visible scoring coverage is incomplete
  1 = mechanical failure
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))
import store_io  # noqa: E402
from schema_defs import JOB_LIST_COLUMNS  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SUMMARY_NAME = "score-finalize-summary.json"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _resolve_project(value: Path | None) -> Path:
    if value is None:
        active = ROOT / "active-project.json"
        if not active.exists():
            raise FileNotFoundError("no project provided and active-project.json missing")
        data = json.loads(active.read_text(encoding="utf-8"))
        value = Path(str(data.get("active") or data.get("project") or data.get("code") or ""))
    if value.is_absolute():
        project = value
    else:
        direct = ROOT / value
        project = direct if (direct / "project.json").exists() else ROOT / "projects" / str(value)
    if not (project / "project.json").exists():
        raise FileNotFoundError(f"project not found: {value}")
    return project


def _run(cmd: list[str], project: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        cwd=str(ROOT),
        env={**os.environ, "RECRUITING_PROJECT_DIR": str(project), "PYTHONIOENCODING": "utf-8"},
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return default


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _rating_group(entry: dict[str, Any]) -> str:
    for key in ("job_group", "group", "target_group", "group_slug"):
        value = str(entry.get(key, "")).strip()
        if value:
            return value
    return ""


def _rating_fit_score(entry: dict[str, Any]) -> str:
    ratings = entry.get("ratings", {})
    if not isinstance(ratings, dict):
        return ""
    value = ratings.get("role_fit")
    return str(value) if isinstance(value, int) and 1 <= value <= 5 else ""


def _rating_note(entry: dict[str, Any]) -> str:
    reason = str(entry.get("reason") or entry.get("summary") or "").strip()
    rationale = entry.get("rationale", {})
    if isinstance(rationale, dict):
        parts = [str(v).strip() for v in rationale.values() if str(v).strip()]
        if parts:
            reason = reason or "; ".join(parts[:2])
    return (reason or "Scored by RoleScout score workflow.")[:900]


def _criteria_names(project: Path) -> list[str]:
    cfg = _read_json(project / "strategy" / "scoring-config.json", {})
    criteria = cfg.get("criteria", []) if isinstance(cfg, dict) else []
    names = [str(item.get("name", "")).strip() for item in criteria if isinstance(item, dict)]
    return [name for name in names if name]


def _fallback_rating(job_id: str, row: dict[str, str], criteria: list[str]) -> dict[str, Any]:
    ratings = {name: 1 for name in criteria}
    if "company_quality" in ratings:
        ratings["company_quality"] = 2
    if "timing" in ratings:
        ratings["timing"] = 2
    title = str(row.get("title", "")).strip() or "Untitled role"
    company = str(row.get("company", "")).strip() or "Unknown company"
    reason = (
        "Deterministic fallback: no batch evaluator rating was returned for this "
        "visible row, so RoleScout parked it low instead of leaving the UI unscored."
    )
    return {
        "job_id": job_id,
        "job_group": "parked",
        "ratings": ratings,
        "rationale": {name: reason for name in criteria},
        "reason": f"{company} - {title}: {reason}",
    }


def _is_deterministic_fallback(entry: dict[str, Any]) -> bool:
    text = " ".join([
        str(entry.get("reason", "")),
        json.dumps(entry.get("rationale", {}), ensure_ascii=False),
    ]).lower()
    return "deterministic fallback: no batch evaluator rating" in text


def _normalize_rating_entry(entry: dict[str, Any], criteria: list[str]) -> dict[str, Any]:
    ratings = entry.get("ratings", {})
    if not isinstance(ratings, dict):
        ratings = {}
    nested_rationale: dict[str, str] = {}
    normalized: dict[str, Any] = {}
    for name in criteria:
        value = ratings.get(name)
        if isinstance(value, dict):
            nested_rationale[name] = str(value.get("rationale", "")).strip()
            value = value.get("score")
        # Accept the legacy/nested shape emitted by some evaluator runs, but do
        # not manufacture a score for genuinely missing or invalid output.
        # score_jobs.py remains the final strict validation boundary.
        normalized[name] = value
    entry = dict(entry)
    entry["ratings"] = normalized
    if not _rating_group(entry):
        entry["job_group"] = "parked"
    rationale = entry.get("rationale", {})
    if not isinstance(rationale, dict):
        rationale = {}
    for name in criteria:
        if nested_rationale.get(name) and not str(rationale.get(name, "")).strip():
            rationale[name] = nested_rationale[name]
    entry["rationale"] = {name: str(rationale.get(name, "")) for name in criteria}
    return entry


def _ensure_visible_rating_coverage(
    project: Path,
    ratings_path: Path,
    ratings: list[Any],
    visible_rows: list[dict[str, str]],
) -> tuple[list[dict[str, Any]], int]:
    criteria = _criteria_names(project)
    clean = [
        item for item in ratings
        if isinstance(item, dict) and not _is_deterministic_fallback(item)
    ]
    if not criteria:
        return clean, 0
    unnormalized = clean
    clean = [_normalize_rating_entry(item, criteria) for item in clean]
    removed = len([item for item in ratings if isinstance(item, dict)]) - len(clean)
    if removed or clean != unnormalized:
        _write_json(ratings_path, clean)
    return clean, 0


def _priority_overrides(project: Path) -> dict[str, str]:
    overrides = _read_json(project / "strategy" / "overrides.json", [])
    out: dict[str, str] = {}
    if not isinstance(overrides, list):
        return out
    for item in overrides:
        if not isinstance(item, dict):
            continue
        job_id = str(item.get("job_id", "")).strip()
        final = str(item.get("final", "")).strip()
        if job_id and final in {"high", "medium", "low"}:
            out[job_id] = final
    return out


def _summary_payload(project: Path, **items: Any) -> dict[str, Any]:
    payload = {
        "schema": "rolescout-score-finalize-summary-v1",
        "generated_at": _now(),
        "project": project.name,
    }
    payload.update(items)
    return payload


def finalize(project: Path) -> tuple[int, dict[str, Any]]:
    ratings_path = project / "strategy" / "job-ratings.json"
    if not ratings_path.exists():
        return 2, _summary_payload(
            project,
            status="partial",
            reason="strategy/job-ratings.json missing; score agent did not produce ratings",
            ratings_rows=0,
            scores_rows=0,
            visible_rows=0,
            rated_visible_rows=0,
            missing_visible_rows=0,
            updated_rows=0,
        )

    init = _run([sys.executable, str(ROOT / "scripts" / "init_db.py")], project)
    if init.returncode != 0:
        return 1, _summary_payload(
            project,
            status="failed",
            reason="init_db failed before score finalization",
            output=(init.stdout + init.stderr).strip()[:1200],
        )

    visible_rows = store_io.read_visible_job_rows()
    ratings = _read_json(ratings_path, [])
    if not isinstance(ratings, list):
        ratings = []
    fallback_ids = {
        str(item.get("job_id", "")).strip()
        for item in ratings
        if isinstance(item, dict) and _is_deterministic_fallback(item)
        and str(item.get("job_id", "")).strip()
    }
    ratings, fallback_ratings = _ensure_visible_rating_coverage(
        project, ratings_path, ratings, visible_rows
    )

    criteria = _criteria_names(project)
    visible_ids = [row.get("job_id", "") for row in visible_rows if row.get("job_id")]
    rating_by_id = {
        str(item.get("job_id", "")).strip(): item
        for item in ratings if isinstance(item, dict) and str(item.get("job_id", "")).strip()
    }
    invalid_visible: list[str] = []
    for job_id in visible_ids:
        item = rating_by_id.get(job_id)
        if item is None:
            continue
        values = item.get("ratings", {}) if isinstance(item, dict) else {}
        if (
            not isinstance(values, dict)
            or set(values) != set(criteria)
            or any(not isinstance(value, int) or not 1 <= value <= 5 for value in values.values())
        ):
            invalid_visible.append(job_id)
    if invalid_visible:
        return 2, _summary_payload(
            project,
            status="partial",
            reason=(
                "evaluator coverage is incomplete or invalid; deterministic scoring "
                "was not run and no score updates were written"
            ),
            ratings_rows=len(rating_by_id),
            visible_rows=len(visible_ids),
            rated_visible_rows=len(rating_by_id) - len(invalid_visible),
            missing_visible_rows=len(invalid_visible),
            missing_visible_examples=invalid_visible[:20],
            updated_rows=0,
        )

    score = _run([sys.executable, str(ROOT / "scripts" / "score_jobs.py"), str(ratings_path)], project)
    score_out = (score.stdout + score.stderr).strip()
    if score.returncode != 0:
        return 1, _summary_payload(
            project,
            status="failed",
            reason="score_jobs failed",
            output=score_out[:1200],
        )

    ratings = _read_json(ratings_path, [])
    scores = _read_json(project / "strategy" / "job-scores.json", [])
    if not isinstance(ratings, list):
        ratings = []
    if not isinstance(scores, list):
        scores = []
    rating_by_id = {
        str(item.get("job_id", "")).strip(): item
        for item in ratings if isinstance(item, dict) and str(item.get("job_id", "")).strip()
    }
    score_by_id = {
        str(item.get("job_id", "")).strip(): item
        for item in scores if isinstance(item, dict) and str(item.get("job_id", "")).strip()
    }

    raw_rows = store_io.read_rows("job_list")
    cleared_fallback_scores = 0
    if fallback_ids:
        repair_rows: list[dict[str, Any]] = []
        for row in raw_rows:
            if row.get("job_id") not in fallback_ids:
                continue
            patch: dict[str, Any] = dict(row)
            for field in ("fit_score", "priority", "job_group", "notes"):
                patch[field] = {"$clear": True}
            repair_rows.append(patch)
        if repair_rows:
            repair_path = project / "data" / "score-fallback-repair.json"
            _write_json(repair_path, repair_rows)
            repair = _run(
                [sys.executable, str(ROOT / "scripts" / "upsert_rows.py"),
                 "job_list", str(repair_path)],
                project,
            )
            try:
                repair_path.unlink()
            except OSError:
                pass
            if repair.returncode != 0:
                return 1, _summary_payload(
                    project,
                    status="failed",
                    reason="failed to clear legacy deterministic fallback scores",
                    output=(repair.stdout + repair.stderr).strip()[:1200],
                )
            cleared_fallback_scores = len(repair_rows)
            raw_rows = store_io.read_rows("job_list")
    raw_by_id = {row.get("job_id", ""): row for row in raw_rows if row.get("job_id")}
    visible_id_set = set(visible_ids)
    final_priority = _priority_overrides(project)

    rated_visible = sorted(visible_id_set & set(rating_by_id) & set(score_by_id))
    missing_before_write = sorted(visible_id_set - set(rated_visible))
    updates: list[dict[str, str]] = []
    for job_id in rated_visible:
        source = dict(raw_by_id.get(job_id) or {})
        if not source:
            continue
        rating = rating_by_id[job_id]
        computed = score_by_id[job_id]
        fit_score = _rating_fit_score(rating)
        if fit_score:
            source["fit_score"] = fit_score
        priority = final_priority.get(job_id) or str(computed.get("suggested_priority", "")).strip()
        if priority in {"high", "medium", "low"}:
            source["priority"] = priority
        group = _rating_group(rating)
        if group:
            source["job_group"] = group
        source["notes"] = _rating_note(rating)
        updates.append({key: str(source.get(key, "")) for key in JOB_LIST_COLUMNS})

    if not updates:
        summary = _summary_payload(
            project,
            status="partial",
            reason="no current visible job rows have both ratings and computed scores",
            score_jobs_output=score_out[:1200],
            ratings_rows=len(rating_by_id),
            scores_rows=len(score_by_id),
            fallback_ratings=fallback_ratings,
            visible_rows=len(visible_ids),
            rated_visible_rows=0,
            missing_visible_rows=len(visible_ids),
            updated_rows=0,
        )
        return 2, summary

    updates_path = project / "data" / "score-updates.json"
    _write_json(updates_path, updates)
    upsert = _run(
        [sys.executable, str(ROOT / "scripts" / "upsert_rows.py"), "job_list", str(updates_path)],
        project,
    )
    upsert_out = (upsert.stdout + upsert.stderr).strip()
    try:
        updates_path.unlink()
    except OSError:
        pass
    if upsert.returncode != 0:
        return 1, _summary_payload(
            project,
            status="failed",
            reason="upsert_rows refused score updates",
            score_jobs_output=score_out[:1200],
            output=upsert_out[:1200],
        )

    view = _run([sys.executable, str(ROOT / "scripts" / "build_search_view.py"), str(project)], project)
    view_out = (view.stdout + view.stderr).strip()
    if view.returncode != 0:
        return 1, _summary_payload(
            project,
            status="failed",
            reason="build_search_view failed after score finalization",
            score_jobs_output=score_out[:1200],
            upsert_output=upsert_out[:1200],
            output=view_out[:1200],
        )

    partial = bool(missing_before_write)
    summary = _summary_payload(
        project,
        status="partial" if partial else "ok",
        reason=(
            "validated current ratings were updated atomically; unresolved rows retain "
            "their prior database scores and are listed as stale"
            if partial else ""
        ),
        score_jobs_output=score_out[:1200],
        upsert_output=upsert_out[:1200],
        view_output=view_out[:1200],
        ratings_rows=len(rating_by_id),
        scores_rows=len(score_by_id),
        fallback_ratings=fallback_ratings,
        cleared_fallback_scores=cleared_fallback_scores,
        visible_rows=len(visible_ids),
        rated_visible_rows=len(rated_visible),
        missing_visible_rows=len(missing_before_write),
        missing_visible_examples=missing_before_write[:20],
        updated_rows=len(updates),
    )
    return (2 if partial else 0), summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Finalize RoleScout score artifacts and store updates.")
    parser.add_argument("project", type=Path, nargs="?")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        project = _resolve_project(args.project)
        rc, summary = finalize(project)
        _write_json(project / "strategy" / SUMMARY_NAME, summary)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(summary, indent=2, ensure_ascii=False))
    else:
        label = "OK" if rc == 0 else ("PARTIAL" if rc == 2 else "FAIL")
        print(
            f"{label}: finalized score updates for "
            f"{summary.get('rated_visible_rows', 0)}/{summary.get('visible_rows', 0)} "
            f"visible row(s); updated={summary.get('updated_rows', 0)}"
        )
        reason = str(summary.get("reason", "")).strip()
        if reason:
            print(f"  {reason}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
