"""Package-level repository API; CSV/XLSX are export formats, never authority."""

from __future__ import annotations

import sqlite3
from pathlib import Path


def _db_for(project: Path, store: str) -> Path:
    preferred = (project / "data" / "public-opportunities.db" if store == "job_list"
                 else project / "private" / "pipeline.db")
    legacy = project / "data" / "recruiting.db"
    return preferred if preferred.exists() else legacy


def _rows(project: Path, store: str, *, ids: list[str] | None = None,
          visible: bool = False) -> list[dict]:
    path = _db_for(project, store)
    if not path.exists():
        return []
    key = "job_id" if store == "job_list" else "application_id"
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    try:
        if visible and store == "job_list":
            has_visibility = con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='job_visibility'"
            ).fetchone()
            meta = con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='job_visibility_meta'"
            ).fetchone()
            initialized = (con.execute(
                "SELECT initialized FROM job_visibility_meta WHERE singleton=1").fetchone()[0]
                if has_visibility and meta else 0)
            if initialized:
                query = ("SELECT j.* FROM job_visibility v JOIN job_list j "
                         "ON j.job_id=v.job_id ORDER BY v.position")
                params = ()
            else:
                query, params = "SELECT * FROM job_list", ()
        elif ids is None:
            query, params = f"SELECT * FROM {store}", ()
        elif not ids:
            return []
        else:
            placeholders = ",".join("?" for _ in ids)
            query, params = f"SELECT * FROM {store} WHERE {key} IN ({placeholders})", tuple(ids)
        return [dict(row) for row in con.execute(query, params)]
    except sqlite3.Error:
        return []
    finally:
        con.close()


def job_rows(project: Path, *, job_ids: list[str] | None = None,
             visible: bool = False) -> list[dict]:
    return _rows(project, "job_list", ids=job_ids, visible=visible)


def tracker_rows(project: Path, *, application_ids: list[str] | None = None) -> list[dict]:
    return _rows(project, "tracker", ids=application_ids)


def database_revision(project: Path, store: str = "job_list") -> int:
    path = _db_for(project, store)
    if not path.exists():
        return 0
    con = sqlite3.connect(path)
    try:
        row = con.execute("PRAGMA data_version").fetchone()
        return int(row[0]) if row else 0
    finally:
        con.close()
