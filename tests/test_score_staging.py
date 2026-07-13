from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from rolenavi import score_staging
from rolenavi.runner import preflight, workflows


def _rating(job_id: str, fingerprint: str) -> dict:
    return {
        "job_id": job_id,
        "ratings": {"role_fit": 2},
        "rationale": {"role_fit": "Validated."},
        "policy_evaluations": [],
        "requirement_evaluations": [],
        "score_meta": {
            "contract_version": "contract-v1",
            "dependency_fingerprint": fingerprint,
        },
    }


def test_batch_checkpoints_survive_reopen_and_filter_stale_dependencies(tmp_path: Path):
    project = tmp_path / "project"
    dependencies = {"job-1": "dep-1", "job-2": "dep-2"}
    score_staging.begin(
        project,
        checkpoint_key="score-snapshot",
        run_id="run-1",
        contract_version="contract-v1",
        snapshot_fingerprint="snapshot",
        total_jobs=2,
    )
    score_staging.checkpoint_batch(
        project,
        checkpoint_key="score-snapshot",
        batch_id="batch-001",
        dependency_fingerprints=dependencies,
        validated={"job-1": _rating("job-1", "dep-1")},
        invalid={"job-2": ["missing requirement ID"]},
    )

    resumed = score_staging.load_validated(
        project,
        checkpoint_key="score-snapshot",
        dependency_fingerprints=dependencies,
    )
    assert set(resumed) == {"job-1"}

    stale = score_staging.load_validated(
        project,
        checkpoint_key="score-snapshot",
        dependency_fingerprints={"job-1": "changed", "job-2": "dep-2"},
    )
    assert stale == {}


def test_repair_atomically_replaces_invalid_checkpoint(tmp_path: Path):
    project = tmp_path / "project"
    dependencies = {"job-1": "dep-1"}
    score_staging.begin(
        project,
        checkpoint_key="score-snapshot",
        run_id="run-1",
        contract_version="contract-v1",
        snapshot_fingerprint="snapshot",
        total_jobs=1,
    )
    score_staging.checkpoint_batch(
        project,
        checkpoint_key="score-snapshot",
        batch_id="batch-001",
        dependency_fingerprints=dependencies,
        validated={},
        invalid={"job-1": ["invalid output"]},
    )
    score_staging.checkpoint_batch(
        project,
        checkpoint_key="score-snapshot",
        batch_id="repair-001",
        dependency_fingerprints=dependencies,
        validated={"job-1": _rating("job-1", "dep-1")},
        invalid={},
    )
    score_staging.finish(project, checkpoint_key="score-snapshot", unresolved=0)

    resumed = score_staging.load_validated(
        project,
        checkpoint_key="score-snapshot",
        dependency_fingerprints=dependencies,
    )
    status = score_staging.summary(project, "score-snapshot")
    latest = score_staging.latest_summary(project)
    assert set(resumed) == {"job-1"}
    assert status["status"] == "complete"
    assert status["validated_jobs"] == 1
    assert status["invalid_jobs"] == 0
    assert latest["checkpoint_key"] == "score-snapshot"


def test_score_is_visible_as_a_separate_web_workflow():
    ui = (Path(__file__).parents[1] / "rolenavi" / "web" / "ui.html").read_text(
        encoding="utf-8"
    )
    assert "['search','score','prep'" in ui
    assert "'opportunity-plan','score'" not in ui
    workflow_source = (
        Path(__file__).parents[1] / "rolenavi" / "runner" / "workflows.py"
    ).read_text(encoding="utf-8")
    assert "ROLENAVI_SEARCH_AUTO_SCORE" not in workflow_source


def test_normalization_failure_quarantines_one_row_without_failing_score(monkeypatch, tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "project.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(workflows, "_ensure_capability_ledger", lambda *args, **kwargs: None)
    monkeypatch.setattr(workflows, "_score_rows", lambda _project: [{
        "job_id": "bad-job",
        "company": "Example",
        "title": "Broken normalized row",
        "requirement_coverage_issues": ["explicit years signal missing"],
        "requirement_contract": [],
        "preferred_requirement_contract": [],
        "requirement_source_fingerprint": "source-v1",
    }])
    monkeypatch.setattr(workflows, "_score_criteria", lambda _project: [{
        "name": "role_fit", "weight": 100,
    }])
    ctx = workflows.RunContext("score", project, "live")
    ctx.run_id = "run-quarantine"

    result = workflows._run_score_batches(ctx, object(), {}, lambda _text: None)

    assert result["ratings"] == 0
    assert result["unresolved_ratings"] == 1
    assert ctx.failure_class == ""
    assert ctx.run_status() == "partial"
    latest = score_staging.latest_summary(project)
    assert latest["invalid_jobs"] == 1
    assert latest["status"] == "partial"


def test_cancel_salvage_promotes_validated_checkpoint_to_canonical_ratings(
    monkeypatch, tmp_path: Path
):
    project = tmp_path / "project"
    project.mkdir()
    dependencies = {"job-1": "dep-1", "job-2": "dep-2"}
    score_staging.begin(
        project,
        checkpoint_key="score-snapshot",
        run_id="run-cancelled",
        contract_version=workflows.SCORE_CONTRACT_VERSION,
        snapshot_fingerprint="snapshot",
        total_jobs=2,
    )
    score_staging.checkpoint_batch(
        project,
        checkpoint_key="score-snapshot",
        batch_id="batch-001",
        dependency_fingerprints=dependencies,
        validated={"job-1": _rating("job-1", "dep-1")},
        invalid={},
    )
    ctx = workflows.RunContext("score", project, "live")
    ctx.score_checkpoint_key = "score-snapshot"
    ctx.score_dependency_fingerprints = dependencies
    ctx.score_jobs_by_id = {
        "job-1": {"job_id": "job-1", "requirements": []},
        "job-2": {"job_id": "job-2", "requirements": []},
    }
    ctx.score_requirements_by_job = {"job-1": [], "job-2": []}
    ctx.score_criteria_names = {"role_fit"}
    finalized: list[bool] = []
    monkeypatch.setattr(
        workflows,
        "_finalize_score",
        lambda _ctx, *, complete_on_cancel=False: finalized.append(complete_on_cancel),
    )

    saved = workflows._salvage_score_checkpoint(ctx)

    ratings = json.loads((project / "strategy" / "job-ratings.json").read_text())
    freshness = json.loads((project / "strategy" / "score-freshness.json").read_text())
    assert saved == 1
    assert [item["job_id"] for item in ratings] == ["job-1"]
    assert freshness["current_job_ids"] == ["job-1"]
    assert freshness["unresolved_job_ids"] == ["job-2"]
    assert score_staging.summary(project, "score-snapshot")["status"] == "partial"
    assert finalized == [True]


def test_score_cancel_cancels_queued_batches_instead_of_waiting_for_all(
    monkeypatch, tmp_path: Path
):
    project = tmp_path / "project"
    project.mkdir()
    (project / "project.json").write_text("{}", encoding="utf-8")
    jobs = [{
        "job_id": f"job-{index}", "company": "Example", "title": f"Role {index}",
        "requirement_contract": [], "preferred_requirement_contract": [],
        "requirement_source_fingerprint": f"source-{index}",
    } for index in range(20)]
    monkeypatch.setattr(workflows, "_ensure_capability_ledger", lambda *args, **kwargs: None)
    monkeypatch.setattr(workflows, "_score_rows", lambda _project: jobs)
    monkeypatch.setattr(workflows, "_score_criteria", lambda _project: [{
        "name": "role_fit", "weight": 100,
    }])
    monkeypatch.setattr(workflows, "SCORE_BATCH_WORKERS", 1)
    calls: list[int] = []
    cancelled = threading.Event()

    def provider_run(provider, workflow, context, on_stream, **kwargs):
        calls.append(context["score_batch"]["index"])
        cancelled.set()
        on_stream("provider stopped")
        return {"events": []}

    monkeypatch.setattr(workflows, "_provider_run", provider_run)
    ctx = workflows.RunContext("score", project, "live")
    ctx.run_id = "run-cancel"
    ctx.cancel_event = cancelled

    with pytest.raises(workflows.RunCancelled):
        workflows._run_score_batches(ctx, object(), {}, lambda _text: None)

    assert len(calls) < 5


def test_prep_strategy_scope_excludes_unscored_focused_rows(monkeypatch, tmp_path: Path):
    project = tmp_path / "project"
    (project / "strategy").mkdir(parents=True)
    (project / "data").mkdir()
    (project / "data" / "focused-jobs.json").write_text(
        '{"job_ids":["job-1","job-2"]}', encoding="utf-8"
    )
    (project / "strategy" / "score-freshness.json").write_text(
        '{"current_job_ids":["job-1"],"unresolved_job_ids":["job-2"]}',
        encoding="utf-8",
    )
    monkeypatch.setattr(workflows, "_focused_job_rows", lambda _project: [
        {"job_id": "job-1", "fit_score": "4"},
        {"job_id": "job-2", "fit_score": ""},
    ])

    assert [row["job_id"] for row in workflows._strategy_focused_job_rows(project)] == [
        "job-1"
    ]
    assert preflight.strategy_score_scope(project) == (1, 2)
