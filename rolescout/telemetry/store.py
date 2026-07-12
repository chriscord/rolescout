"""Local telemetry store (SQLite, schema-versioned via PRAGMA user_version).

v1 (M1): runs table — run id, workflow, mode, model config, cost, latency,
validator results, approval decisions, failure class.
v2: adds legacy events/corrections/share-ledger tables.
v3: adds prompt size/fingerprint/data-class metrics.
v4: purges legacy content-bearing telemetry; new writes are metrics-only.

Same journal_mode=MEMORY mitigation as the project store (synced folders).
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path

from ..paths import telemetry_db_path

SCHEMA_VERSION = 4

_DDL_V1 = """
CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY,
  started_at TEXT NOT NULL,
  finished_at TEXT DEFAULT '',
  workflow TEXT NOT NULL,
  mode TEXT NOT NULL CHECK (mode IN ('mock','live')),
  project TEXT DEFAULT '',
  model_config TEXT DEFAULT '{}',
  cost_usd REAL DEFAULT 0,
  tokens_in INTEGER DEFAULT 0,
  tokens_out INTEGER DEFAULT 0,
  latency_s REAL DEFAULT 0,
  validator_results TEXT DEFAULT '[]',
  approvals TEXT DEFAULT '[]',
  failure_class TEXT DEFAULT '',
  status TEXT DEFAULT 'ok',
  summary TEXT DEFAULT ''
);
"""

_DDL_V2 = """
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL REFERENCES runs(run_id),
  seq INTEGER NOT NULL,
  type TEXT NOT NULL,
  payload TEXT DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS corrections (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT DEFAULT '',
  created_at TEXT NOT NULL,
  kind TEXT NOT NULL,
  detail TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS shares (
  share_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  redaction_report TEXT DEFAULT '{}',
  user_approved_at TEXT DEFAULT '',
  destination TEXT DEFAULT '',
  bundle_path TEXT DEFAULT ''
);
"""

_DDL_V3 = """
ALTER TABLE runs ADD COLUMN input_bytes INTEGER DEFAULT 0;
ALTER TABLE runs ADD COLUMN prompt_fingerprint TEXT DEFAULT '';
ALTER TABLE runs ADD COLUMN data_classes TEXT DEFAULT '[]';
"""

_DDL_V4 = """
UPDATE runs SET project='', summary='', validator_results='[]', approvals='[]';
DELETE FROM events;
DELETE FROM corrections;
DELETE FROM shares;
"""

MIGRATIONS: dict[int, str] = {1: _DDL_V1, 2: _DDL_V2, 3: _DDL_V3, 4: _DDL_V4}


def connect(path: Path | None = None) -> sqlite3.Connection:
    p = path or telemetry_db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(p)
    con.execute("PRAGMA journal_mode = MEMORY")
    con.execute("PRAGMA foreign_keys = ON")
    migrate(con)
    return con


def migrate(con: sqlite3.Connection) -> int:
    """Apply pending migrations; returns the resulting schema version."""
    current = con.execute("PRAGMA user_version").fetchone()[0]
    for version in sorted(MIGRATIONS):
        if version > current:
            con.executescript(MIGRATIONS[version])
            con.execute(f"PRAGMA user_version = {version}")
            current = version
    con.commit()
    return current


def new_run_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]


def record_run(rec: dict, path: Path | None = None) -> str:
    """Persist metrics only.

    The caller may retain a richer in-memory record for the live UI, but global
    telemetry never stores prompt/model output, project/person identifiers,
    URLs, validator excerpts, or event payloads.
    """
    con = connect(path)
    try:
        run_id = rec.get("run_id") or new_run_id()
        con.execute(
            "INSERT OR REPLACE INTO runs (run_id, started_at, finished_at, workflow, mode,"
            " project, model_config, cost_usd, tokens_in, tokens_out, latency_s,"
            " validator_results, approvals, failure_class, status, summary, input_bytes,"
            " prompt_fingerprint, data_classes)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, rec.get("started_at", ""), rec.get("finished_at", ""),
             rec["workflow"], rec["mode"], "",
             json.dumps({k: v for k, v in rec.get("model_config", {}).items()
                         if k in {"provider", "model", "effort", "auth"}}),
             rec.get("cost_usd", 0), rec.get("tokens_in", 0), rec.get("tokens_out", 0),
             rec.get("latency_s", 0),
             json.dumps([{
                 "validator": str(item.get("validator", ""))[:100],
                 "returncode": int(item.get("returncode", 0) or 0),
             } for item in rec.get("validator_results", []) if isinstance(item, dict)]),
             "[]",
             rec.get("failure_class", ""), rec.get("status", "ok"),
             "", int(rec.get("input_bytes", 0) or 0),
             str(rec.get("prompt_fingerprint", ""))[:64],
             json.dumps(sorted(set(rec.get("data_classes", []))))))
        con.execute("DELETE FROM events WHERE run_id = ?", (run_id,))
        con.commit()
        return run_id
    finally:
        con.close()


def list_runs(limit: int = 20, path: Path | None = None) -> list[dict]:
    con = connect(path)
    try:
        con.row_factory = sqlite3.Row
        rows = [dict(r) for r in con.execute(
            "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,))]
        return rows
    finally:
        con.close()


def get_run(run_id: str, path: Path | None = None) -> dict | None:
    con = connect(path)
    try:
        con.row_factory = sqlite3.Row
        r = con.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if r is None:
            return None
        rec = dict(r)
        rec["events"] = []
        return rec
    finally:
        con.close()
