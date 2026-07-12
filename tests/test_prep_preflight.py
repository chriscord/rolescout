from __future__ import annotations

import json
from pathlib import Path

from rolescout.runner import preflight, workflows


def _project(tmp_path: Path) -> tuple[Path, Path]:
    project = tmp_path / "projects" / "person--focus"
    profile = tmp_path / "profiles" / "person"
    project.mkdir(parents=True)
    profile.mkdir(parents=True)
    (project / "project.json").write_text(
        json.dumps({"person": "person", "focus": "focus", "profile_dir": str(profile)}),
        encoding="utf-8",
    )
    return project, profile


def test_profile_repair_candidate_requires_source_material(tmp_path: Path):
    project, profile = _project(tmp_path)
    assert preflight.profile_repair_candidate("prep", project) is None

    (profile / "resume.pdf").write_bytes(b"resume")
    repair = preflight.profile_repair_candidate("prep", project)
    assert repair is not None
    assert repair["person"] == "person"

    (profile / "candidate-profile.md").write_text("profile", encoding="utf-8")
    (profile / "evidence-map.md").write_text("evidence", encoding="utf-8")
    assert preflight.profile_repair_candidate("prep", project) is None


def test_prep_auto_repairs_profile_once_then_highlights_missing_focus(
    tmp_path: Path, monkeypatch,
):
    project, profile = _project(tmp_path)
    (profile / "resume.pdf").write_bytes(b"resume")
    calls: list[str] = []
    events: list[tuple[str, str]] = []

    def fake_check(workflow: str, checked_project: Path):
        blockers = []
        if not (profile / "candidate-profile.md").exists():
            blockers.append("candidate-profile.md missing")
        blockers.append("no focused positions")
        return blockers, []

    def fake_profile_intake(person: str, **kwargs):
        calls.append(person)
        (profile / "candidate-profile.md").write_text("profile", encoding="utf-8")
        (profile / "evidence-map.md").write_text("evidence", encoding="utf-8")
        return {"status": "ok", "summary": "profile ready"}

    monkeypatch.setattr(workflows.llm, "mode", lambda _: "live")
    monkeypatch.setattr(preflight, "check", fake_check)
    monkeypatch.setattr(preflight, "focused_job_count", lambda _: 0)
    monkeypatch.setattr(workflows, "run_profile_intake", fake_profile_intake)

    rec = workflows.run_workflow(
        "prep",
        project=project,
        telemetry_path=tmp_path / "telemetry.db",
        on_event=lambda kind, text, extra=None: events.append((kind, text)),
    )

    assert calls == ["person"]
    assert rec["status"] == "blocked"
    assert any(
        kind == "attention" and "Select focused roles first" in text
        for kind, text in events
    )
    assert any(
        item["validator"] == "profile_intake_auto_repair"
        and item["returncode"] == 0
        for item in rec["validator_results"]
    )


def test_prep_interview_preflight_requires_nonempty_story_bank(tmp_path: Path):
    (tmp_path / "interviews").mkdir()
    missing = preflight.story_bank_readiness_error(tmp_path)
    assert "story bank is missing" in missing

    (tmp_path / "interviews" / "story-bank.json").write_text(
        '{"entries": []}\n', encoding="utf-8")
    empty = preflight.story_bank_readiness_error(tmp_path)
    assert "no usable entries" in empty

    (tmp_path / "interviews" / "story-bank.json").write_text(
        '{"entries": [{"id": "ST-01"}]}\n', encoding="utf-8")
    assert preflight.story_bank_readiness_error(tmp_path) == ""
