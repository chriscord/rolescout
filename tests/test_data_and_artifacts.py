from __future__ import annotations

import importlib
import json
import subprocess
import sys
from pathlib import Path

from rolenavi.repositories import artifacts

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
store_io = importlib.import_module("store_io")
reconcile_job_lifecycle = importlib.import_module("reconcile_job_lifecycle")


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "projects" / "sample--test"
    project.mkdir(parents=True)
    (project / "project.json").write_text('{"person":"sample","focus":"test"}\n')
    return project


def _job(job_id: str = "job--1") -> dict:
    return {"job_id": job_id, "captured_at": "2026-07-11", "company": "Example Co",
            "title": "Product Lead", "source_url": "https://example.com/jobs/1",
            "notes": "keep"}


def test_split_stores_revision_and_clear_semantics(tmp_path: Path, monkeypatch):
    project = _project(tmp_path)
    monkeypatch.setenv("RECRUITING_PROJECT_DIR", str(project))
    con = store_io.connect()
    try:
        assert store_io.public_db_path().exists()
        assert store_io.private_db_path().exists()
        assert store_io.upsert("job_list", [_job()], con) == (1, 0, 0)
        con.commit()
        rev = store_io.revision("job_list", con)
        assert store_io.upsert("job_list", [{"job_id": "job--1", "notes": ""}], con) == (0, 0, 1)
        assert store_io.upsert("job_list", [{"job_id": "job--1", "notes": {"$clear": True}}], con) == (0, 1, 0)
        con.commit()
        assert store_io.revision("job_list", con) == rev + 1
        assert store_io.read_rows("job_list", con)[0]["notes"] == ""
    finally:
        con.close()


def test_job_upsert_preserves_first_capture_date(tmp_path: Path, monkeypatch):
    project = _project(tmp_path)
    monkeypatch.setenv("RECRUITING_PROJECT_DIR", str(project))
    con = store_io.connect()
    try:
        store_io.upsert("job_list", [_job()], con)
        newer = _job()
        newer["captured_at"] = "2026-08-01"
        newer["last_seen_at"] = "2026-08-01"
        store_io.upsert("job_list", [newer], con)
        con.commit()
        row = store_io.read_rows("job_list", con)[0]
        assert row["captured_at"] == "2026-07-11"
        assert row["last_seen_at"] == "2026-08-01"
    finally:
        con.close()


def test_authoritative_lifecycle_removes_and_reopens_without_deleting(
        tmp_path: Path, monkeypatch):
    project = _project(tmp_path)
    monkeypatch.setenv("RECRUITING_PROJECT_DIR", str(project))
    con = store_io.connect()
    try:
        store_io.upsert("job_list", [_job("job--1"), _job("job--2")], con)
        con.commit()
    finally:
        con.close()

    def manifest(run_id: str, status: str, authoritative: bool, seen: list[str]) -> dict:
        return {"run_id": run_id, "sources": [{
            "source_key": "example:greenhouse", "company": "Example Co",
            "provider": "greenhouse", "source_url": "https://example.test/jobs",
            "scan_status": status, "authoritative": authoritative, "seen_job_ids": seen,
        }]}

    reconcile_job_lifecycle.reconcile(manifest("run-1", "scanned", True,
                                                ["job--1", "job--2"]))
    reconcile_job_lifecycle.reconcile(manifest("run-2", "failed_retryable", False, []))
    con = store_io.connect()
    try:
        assert {r["job_id"]: r["posting_status"] for r in store_io.read_rows("job_list", con)} == {
            "job--1": "open", "job--2": "open"}
    finally:
        con.close()

    reconcile_job_lifecycle.reconcile(manifest("run-3", "scanned", True, ["job--1"]))
    con = store_io.connect()
    try:
        rows = {r["job_id"]: r["posting_status"] for r in store_io.read_rows("job_list", con)}
        assert rows == {"job--1": "open", "job--2": "removed"}
        assert len(rows) == 2
    finally:
        con.close()

    reconcile_job_lifecycle.reconcile(manifest("run-4", "scanned", True,
                                                ["job--1", "job--2"]))
    con = store_io.connect()
    try:
        assert {r["job_id"]: r["posting_status"] for r in store_io.read_rows("job_list", con)} == {
            "job--1": "open", "job--2": "open"}
    finally:
        con.close()


def test_public_export_has_manifest_and_no_tracker(tmp_path: Path, monkeypatch):
    project = _project(tmp_path)
    monkeypatch.setenv("RECRUITING_PROJECT_DIR", str(project))
    con = store_io.connect()
    try:
        store_io.upsert("job_list", [_job()], con)
        con.commit()
        paths = store_io.export_views(con, public=True, private=False)
    finally:
        con.close()
    assert len(paths) == 1
    text = paths[0].read_text(encoding="utf-8")
    assert "contact" not in text and "application_id" not in text
    manifest = json.loads(paths[0].with_suffix(".csv.manifest.json").read_text())
    assert manifest["sensitivity"] == "public"


def test_visibility_is_sqlite_backed_and_can_be_empty(tmp_path: Path, monkeypatch):
    project = _project(tmp_path)
    monkeypatch.setenv("RECRUITING_PROJECT_DIR", str(project))
    con = store_io.connect()
    try:
        store_io.upsert("job_list", [_job("job--1"), _job("job--2")], con)
        con.commit()
        store_io.replace_visible_job_ids(["job--2"], con)
        assert [row["job_id"] for row in store_io.read_visible_job_rows(con)] == ["job--2"]
        store_io.replace_visible_job_ids([], con)
        assert store_io.read_visible_job_rows(con) == []
    finally:
        con.close()


def test_impossible_date_rejected(tmp_path: Path):
    rows = tmp_path / "rows.json"
    bad = _job()
    bad["captured_at"] = "2026-02-31"
    rows.write_text(json.dumps([bad]), encoding="utf-8")
    result = subprocess.run([sys.executable, str(SCRIPTS / "validate_job_rows.py"), str(rows)],
                            capture_output=True, text=True)
    assert result.returncode == 1
    assert "real YYYY-MM-DD date" in result.stdout


def test_artifact_manifest_detects_dependency_change(tmp_path: Path):
    root = tmp_path / "project"
    root.mkdir()
    dep = root / "project.json"
    dep.write_text("one")
    artifact = root / "strategy.md"
    artifact.write_text("result")
    artifacts.record(root, "strategy.md", artifact, "prep-strategy", [dep])
    assert artifacts.fresh(root, "strategy.md")
    dep.write_text("two")
    assert not artifacts.fresh(root, "strategy.md")
