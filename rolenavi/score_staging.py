"""Durable, resumable score-batch checkpoints owned by the runner."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def path_for(project: Path) -> Path:
    return project / "strategy" / "score-staging.db"


def connect(project: Path) -> sqlite3.Connection:
    path = path_for(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=30)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=FULL")
    con.execute("PRAGMA foreign_keys=ON")
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS score_runs (
          checkpoint_key TEXT PRIMARY KEY,
          latest_run_id TEXT NOT NULL,
          contract_version TEXT NOT NULL,
          snapshot_fingerprint TEXT NOT NULL,
          status TEXT NOT NULL CHECK(status IN ('running','partial','complete')),
          total_jobs INTEGER NOT NULL,
          validated_jobs INTEGER NOT NULL DEFAULT 0,
          invalid_jobs INTEGER NOT NULL DEFAULT 0,
          started_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS score_staging (
          checkpoint_key TEXT NOT NULL REFERENCES score_runs(checkpoint_key) ON DELETE CASCADE,
          job_id TEXT NOT NULL,
          dependency_fingerprint TEXT NOT NULL,
          batch_id TEXT NOT NULL,
          status TEXT NOT NULL CHECK(status IN ('validated','invalid')),
          rating_json TEXT NOT NULL DEFAULT '{}',
          validation_errors TEXT NOT NULL DEFAULT '[]',
          completed_at TEXT NOT NULL,
          PRIMARY KEY (checkpoint_key, job_id)
        );
        CREATE INDEX IF NOT EXISTS idx_score_staging_status
          ON score_staging(checkpoint_key, status);
        """
    )
    con.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    con.commit()
    return con


def begin(
    project: Path,
    *,
    checkpoint_key: str,
    run_id: str,
    contract_version: str,
    snapshot_fingerprint: str,
    total_jobs: int,
) -> None:
    con = connect(project)
    now = _now()
    try:
        con.execute("BEGIN IMMEDIATE")
        con.execute(
            """
            INSERT INTO score_runs (
              checkpoint_key, latest_run_id, contract_version, snapshot_fingerprint,
              status, total_jobs, started_at, updated_at
            ) VALUES (?, ?, ?, ?, 'running', ?, ?, ?)
            ON CONFLICT(checkpoint_key) DO UPDATE SET
              latest_run_id=excluded.latest_run_id,
              status='running', total_jobs=excluded.total_jobs,
              updated_at=excluded.updated_at
            """,
            (checkpoint_key, run_id, contract_version, snapshot_fingerprint,
             total_jobs, now, now),
        )
        con.commit()
    finally:
        con.close()


def load_validated(
    project: Path,
    *,
    checkpoint_key: str,
    dependency_fingerprints: dict[str, str],
) -> dict[str, dict[str, Any]]:
    con = connect(project)
    try:
        rows = con.execute(
            """SELECT job_id, dependency_fingerprint, rating_json
               FROM score_staging
               WHERE checkpoint_key=? AND status='validated'""",
            (checkpoint_key,),
        ).fetchall()
    finally:
        con.close()
    out: dict[str, dict[str, Any]] = {}
    for job_id, fingerprint, raw in rows:
        if dependency_fingerprints.get(job_id) != fingerprint:
            continue
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            out[str(job_id)] = value
    return out


def checkpoint_batch(
    project: Path,
    *,
    checkpoint_key: str,
    batch_id: str,
    dependency_fingerprints: dict[str, str],
    validated: dict[str, dict[str, Any]],
    invalid: dict[str, list[str]],
) -> None:
    """Commit one completed batch in one durable transaction."""
    con = connect(project)
    now = _now()
    try:
        con.execute("BEGIN IMMEDIATE")
        for job_id, rating in validated.items():
            con.execute(
                """
                INSERT INTO score_staging (
                  checkpoint_key, job_id, dependency_fingerprint, batch_id,
                  status, rating_json, validation_errors, completed_at
                ) VALUES (?, ?, ?, ?, 'validated', ?, '[]', ?)
                ON CONFLICT(checkpoint_key, job_id) DO UPDATE SET
                  dependency_fingerprint=excluded.dependency_fingerprint,
                  batch_id=excluded.batch_id, status='validated',
                  rating_json=excluded.rating_json, validation_errors='[]',
                  completed_at=excluded.completed_at
                """,
                (checkpoint_key, job_id, dependency_fingerprints[job_id], batch_id,
                 json.dumps(rating, ensure_ascii=False, separators=(",", ":")), now),
            )
        for job_id, errors in invalid.items():
            if job_id in validated or job_id not in dependency_fingerprints:
                continue
            con.execute(
                """
                INSERT INTO score_staging (
                  checkpoint_key, job_id, dependency_fingerprint, batch_id,
                  status, rating_json, validation_errors, completed_at
                ) VALUES (?, ?, ?, ?, 'invalid', '{}', ?, ?)
                ON CONFLICT(checkpoint_key, job_id) DO UPDATE SET
                  dependency_fingerprint=excluded.dependency_fingerprint,
                  batch_id=excluded.batch_id, status='invalid', rating_json='{}',
                  validation_errors=excluded.validation_errors,
                  completed_at=excluded.completed_at
                """,
                (checkpoint_key, job_id, dependency_fingerprints[job_id], batch_id,
                 json.dumps(errors, ensure_ascii=False), now),
            )
        counts = con.execute(
            """SELECT status, COUNT(*) FROM score_staging
               WHERE checkpoint_key=? GROUP BY status""",
            (checkpoint_key,),
        ).fetchall()
        by_status = {str(status): int(count) for status, count in counts}
        con.execute(
            """UPDATE score_runs SET validated_jobs=?, invalid_jobs=?, updated_at=?
               WHERE checkpoint_key=?""",
            (by_status.get("validated", 0), by_status.get("invalid", 0),
             now, checkpoint_key),
        )
        con.commit()
    finally:
        con.close()


def finish(project: Path, *, checkpoint_key: str, unresolved: int) -> None:
    con = connect(project)
    try:
        con.execute(
            "UPDATE score_runs SET status=?, updated_at=? WHERE checkpoint_key=?",
            ("complete" if unresolved == 0 else "partial", _now(), checkpoint_key),
        )
        con.commit()
    finally:
        con.close()


def summary(project: Path, checkpoint_key: str) -> dict[str, Any]:
    con = connect(project)
    con.row_factory = sqlite3.Row
    try:
        row = con.execute(
            "SELECT * FROM score_runs WHERE checkpoint_key=?", (checkpoint_key,)
        ).fetchone()
        return dict(row) if row else {}
    finally:
        con.close()


def latest_summary(project: Path) -> dict[str, Any]:
    if not path_for(project).exists():
        return {}
    con = connect(project)
    con.row_factory = sqlite3.Row
    try:
        row = con.execute(
            "SELECT * FROM score_runs ORDER BY updated_at DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else {}
    finally:
        con.close()
