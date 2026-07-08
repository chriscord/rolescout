"""SQLite store layer for the recruiting pipeline (per search project).

Each search project (projects/<person>--<focus>/) has its own store:
  Source of truth: <project>/data/recruiting.db
  Generated views (never hand-edit; regenerated on every write):
    - <project>/data/job_list.csv, tracker.csv  (mirrors: backup + validator input)
    - <project>/data/recruiting-pipeline.xlsx   (human view incl. pipeline summary)

Active project resolution: env RECRUITING_PROJECT_DIR, else repo-root
active-project.json (see references/project-structure.md).

Schema constraints enforced by the DB itself: primary keys, status/priority/
remote_policy/posting_status enums (CHECK), fit_score range, tracker->job_list FK.
Status *transition* rules stay in validate_tracker_rows.py (they depend on prior state).
"""
import csv
import json
import os
import sqlite3
import sys
from datetime import date
from pathlib import Path

from schema_defs import (JOB_LIST_COLUMNS, TRACKER_COLUMNS, STATUSES,
                         REMOTE_POLICIES, POSTING_STATUSES, OUTCOMES)

ROOT = Path(__file__).resolve().parent.parent
KEYS = {"job_list": "job_id", "tracker": "application_id"}
COLS = {"job_list": JOB_LIST_COLUMNS, "tracker": TRACKER_COLUMNS}


def project_dir() -> Path:
    """Resolve the active search project directory. Fails loudly if unresolved."""
    env = os.environ.get("RECRUITING_PROJECT_DIR")
    if env:
        p = Path(env).resolve()
    else:
        ap = ROOT / "active-project.json"
        if not ap.exists():
            sys.exit("FAIL: no active project. Create one with "
                     "'python3 scripts/new_project.py --person <code> --focus <slug>' "
                     "or set env RECRUITING_PROJECT_DIR.")
        with open(ap, encoding="utf-8") as f:
            rel = json.load(f).get("active", "")
        p = (ROOT / rel).resolve()
    if not (p / "project.json").exists():
        sys.exit(f"FAIL: {p} is not a search project (missing project.json). "
                 "Fix active-project.json or create it via scripts/new_project.py.")
    return p


def db_path() -> Path:
    return project_dir() / "data" / "recruiting.db"


def xlsx_path() -> Path:
    return project_dir() / "data" / "recruiting-pipeline.xlsx"


def mirrors() -> dict:
    d = project_dir() / "data"
    return {"job_list": d / "job_list.csv", "tracker": d / "tracker.csv"}


def _enum(vals):
    return ", ".join(f"'{v}'" for v in sorted(vals))


DDL = f"""
CREATE TABLE IF NOT EXISTS job_list (
  job_id TEXT PRIMARY KEY,
  captured_at TEXT NOT NULL,
  company TEXT NOT NULL,
  title TEXT NOT NULL,
  job_group TEXT DEFAULT '',
  location TEXT DEFAULT '',
  remote_policy TEXT DEFAULT '' CHECK (remote_policy IN ('', {_enum(REMOTE_POLICIES)})),
  source_url TEXT NOT NULL,
  job_page_url TEXT DEFAULT '',
  posting_status TEXT DEFAULT '' CHECK (posting_status IN ('', {_enum(POSTING_STATUSES)})),
  seniority TEXT DEFAULT '',
  must_have_requirements TEXT DEFAULT '',
  nice_to_have_requirements TEXT DEFAULT '',
  jd_summary TEXT DEFAULT '',
  fit_score TEXT DEFAULT '' CHECK (fit_score IN ('', '1', '2', '3', '4', '5')),
  priority TEXT DEFAULT '' CHECK (priority IN ('', 'high', 'medium', 'low')),
  notes TEXT DEFAULT '',
  last_seen_at TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS tracker (
  application_id TEXT PRIMARY KEY,
  job_id TEXT NOT NULL REFERENCES job_list(job_id),
  company TEXT NOT NULL,
  title TEXT NOT NULL,
  job_group TEXT DEFAULT '',
  status TEXT NOT NULL CHECK (status IN ({_enum(set(STATUSES))})),
  applied_at TEXT DEFAULT '',
  resume_version TEXT DEFAULT '',
  linkedin_version TEXT DEFAULT '',
  contact TEXT DEFAULT '',
  next_action TEXT DEFAULT '',
  next_action_due TEXT DEFAULT '',
  last_updated TEXT NOT NULL,
  outcome TEXT DEFAULT '' CHECK (outcome IN ('', {_enum(set(o for o in OUTCOMES if o))})),
  notes TEXT DEFAULT ''
);
"""


def connect():
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    # Synced/mounted folders (Cowork, cloud drives) often lack the file locking
    # SQLite's rollback journal needs -> "disk I/O error". In-memory journaling
    # avoids that; the CSV mirrors + init_db reimport serve as recovery if a
    # crash ever corrupts this small single-user DB.
    con.execute("PRAGMA journal_mode = MEMORY")
    con.execute("PRAGMA foreign_keys = ON")
    con.executescript(DDL)
    _migrate_removed_posting_status(con)
    return con


def _migrate_removed_posting_status(con):
    """Rebuild older job_list tables whose CHECK enum predates `removed`."""
    row = con.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='job_list'"
    ).fetchone()
    if not row or "'removed'" in (row[0] or ""):
        return
    cols = ", ".join(JOB_LIST_COLUMNS)
    con.commit()
    con.execute("PRAGMA foreign_keys = OFF")
    con.execute("PRAGMA legacy_alter_table = ON")
    try:
        with con:
            con.execute("ALTER TABLE job_list RENAME TO job_list_old")
            con.executescript(DDL)
            con.execute(f"INSERT INTO job_list ({cols}) SELECT {cols} FROM job_list_old")
            con.execute("DROP TABLE job_list_old")
    finally:
        con.execute("PRAGMA legacy_alter_table = OFF")
        con.execute("PRAGMA foreign_keys = ON")


def read_rows(store, con=None):
    own = con is None
    con = con or connect()
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute(f"SELECT * FROM {store}")]
    if own:
        con.close()
    return rows


def upsert(store, rows, con):
    """Upsert rows (list of dicts). Empty incoming values never overwrite non-empty."""
    cols, key = COLS[store], KEYS[store]
    updated = appended = 0
    for row in rows:
        cur = con.execute(f"SELECT * FROM {store} WHERE {key} = ?", (row.get(key),))
        cur.row_factory = None
        existing = cur.fetchone()
        if existing:
            sets = {c: str(row[c]).strip() for c in cols
                    if c != key and str(row.get(c, "")).strip()}
            if sets:
                assign = ", ".join(f"{c} = ?" for c in sets)
                con.execute(f"UPDATE {store} SET {assign} WHERE {key} = ?",
                            [*sets.values(), row[key]])
            updated += 1
        else:
            vals = [str(row.get(c, "")) for c in cols]
            ph = ",".join("?" * len(cols))
            con.execute(f"INSERT INTO {store} ({','.join(cols)}) VALUES ({ph})", vals)
            appended += 1
    return appended, updated


def export_views(con):
    """Regenerate CSV mirrors and the xlsx pipeline view from the DB."""
    for store, path in mirrors().items():
        rows = read_rows(store, con)
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=COLS[store], lineterminator="\n")
            w.writeheader()
            w.writerows(rows)
    try:
        _export_xlsx(con)
    except ImportError:
        print("NOTE: openpyxl not installed — skipped xlsx view "
              "(pip install openpyxl --break-system-packages)")


def _export_xlsx(con):
    from openpyxl import Workbook
    from openpyxl.styles import Font
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    wb.remove(wb.active)
    for store in ("job_list", "tracker"):
        ws = wb.create_sheet(store)
        cols = COLS[store]
        ws.append(cols)
        for c in ws[1]:
            c.font = Font(bold=True)
        for row in read_rows(store, con):
            ws.append([row.get(c, "") for c in cols])
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions
        for i, c in enumerate(cols, 1):
            ws.column_dimensions[get_column_letter(i)].width = min(max(len(c) + 2, 12), 40)

    ws = wb.create_sheet("pipeline")
    ws.append(["status", "count"]); ws["A1"].font = ws["B1"].font = Font(bold=True)
    for status in STATUSES:
        n = con.execute("SELECT COUNT(*) FROM tracker WHERE status=?", (status,)).fetchone()[0]
        if n:
            ws.append([status, n])
    ws.append([])
    ws.append(["overdue next actions (as of", date.today().isoformat() + ")"])
    ws[f"A{ws.max_row}"].font = Font(bold=True)
    q = ("SELECT application_id, company, title, status, next_action, next_action_due "
         "FROM tracker WHERE next_action_due != '' AND next_action_due < ? "
         "AND status NOT IN ('accepted','rejected','withdrawn')")
    for r in con.execute(q, (date.today().isoformat(),)):
        ws.append(list(r))
    wb.save(xlsx_path())
