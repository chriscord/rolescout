from __future__ import annotations

import json
import time
from pathlib import Path

from rolenavi.web import server as web_server


def _project(root: Path) -> Path:
    project = root / "projects" / "person--focus"
    project.mkdir(parents=True)
    (project / "project.json").write_text(
        json.dumps({"person": "person", "focus": "focus"}), encoding="utf-8"
    )
    return project


def _wait(manager: web_server.UniverseRunManager, code: str) -> dict:
    deadline = time.time() + 2
    while time.time() < deadline:
        value = manager.summary(code)
        if value["status"] != "running":
            return value
        time.sleep(0.01)
    raise AssertionError("universe worker did not finish")


def test_universe_manager_streams_progress_and_ready_event(monkeypatch, tmp_path: Path):
    _project(tmp_path)
    monkeypatch.setattr(web_server, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(
        web_server.project_meta,
        "universe_status",
        lambda project: {"ready": True, "reason": "", "state": "ready"},
    )

    def run_workflow(workflow: str, **kwargs):
        assert workflow == "opportunity-plan"
        kwargs["on_event"]("progress", "Expanding a broad company descriptor.")
        kwargs["on_event"]("stream", 'UNIVERSE_PROPOSAL_JSON:{"large":"payload"}')
        kwargs["on_event"]("stream", "more provider stream")
        return {"status": "ok", "summary": "Expanded 12 employers."}

    monkeypatch.setattr(web_server.workflows, "run_workflow", run_workflow)
    manager = web_server.UniverseRunManager()

    assert manager.start("person--focus") is True
    assert _wait(manager, "person--focus")["status"] == "done"
    view = manager.run_view("person--focus", since=1)

    assert view["n_events"] == 4
    assert [event["kind"] for event in view["events"]] == [
        "progress", "progress", "result"
    ]
    assert view["events"][1]["text"] == (
        "Employer expansion proposal received; validating and merging."
    )
    assert "UNIVERSE_PROPOSAL_JSON" not in str(view)
    assert view["events"][-1]["text"] == (
        "Search universe ready — deterministic search can now start."
    )


def test_universe_manager_reports_incomplete_coverage_as_partial(
    monkeypatch, tmp_path: Path
):
    _project(tmp_path)
    monkeypatch.setattr(web_server, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(
        web_server.project_meta,
        "universe_status",
        lambda project: {"ready": False, "reason": "category targets have not been expanded"},
    )
    monkeypatch.setattr(
        web_server.workflows,
        "run_workflow",
        lambda workflow, **kwargs: {"status": "ok", "summary": ""},
    )
    manager = web_server.UniverseRunManager()

    manager.start("person--focus")
    summary = _wait(manager, "person--focus")
    view = manager.run_view("person--focus")

    assert summary["status"] == "partial"
    assert summary["summary"] == "category targets have not been expanded"
    assert view["events"][-1]["kind"] == "attention"


def test_project_ui_live_updates_universe_chat_badge_and_search_button():
    ui = web_server.UI_PATH.read_text(encoding="utf-8")

    assert "/universe-run?since=" in ui
    assert "function pollUniverse()" in ui
    assert 'id="universeBadge"' in ui
    assert "const universeBuilding = P?.universe_run?.status === 'running'" in ui
    assert "Run unlocks automatically when ready" in ui
    assert "!!S.active_run || universeBuilding || universeBlocked" in ui
    assert "Search universe ready — Search is enabled." in ui
