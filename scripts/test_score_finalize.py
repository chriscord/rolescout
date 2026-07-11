#!/usr/bin/env python3
"""Self-test for runner-owned score finalization.

Creates a temporary project with one visible job and one rating, then verifies
finalize_score computes job-scores and writes fit_score/priority back through
the normal store pipeline.
"""

from __future__ import annotations

import csv
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from rolescout.runner import workflows  # noqa: E402


class FakeCtx:
    def __init__(self, project: Path) -> None:
        self.project = project
        self.validator_results = []
        self.partial_reasons = []
        self.pending_reasons = {}
        self.failure_class = ""
        self.artifacts_written = []

    def emit(self, kind: str, text: str, extra=None) -> None:
        del kind, text, extra

    def mark_partial(self, scope: str, reason: str) -> None:
        self.partial_reasons.append({"scope": scope, "reason": reason})


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


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _write_job_list(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "job_id": "exampleco--lead-strategy-manager--1234abcd",
        "captured_at": "2026-07-10",
        "company": "ExampleCo",
        "title": "Lead Strategy Manager",
        "job_group": "",
        "location": "San Francisco, USA",
        "remote_policy": "hybrid",
        "source_url": "https://jobs.lever.co/example/12345678-1234-1234-1234-123456789abc",
        "job_page_url": "https://jobs.lever.co/example/12345678-1234-1234-1234-123456789abc",
        "posting_status": "open",
        "seniority": "manager",
        "must_have_requirements": "Strategy, partnerships, business development",
        "nice_to_have_requirements": "AI platform experience",
        "jd_summary": "Lead strategy role with partnership and business development scope.",
        "fit_score": "",
        "priority": "",
        "notes": "",
        "last_seen_at": "2026-07-10",
    }
    missing = dict(row)
    missing.update({
        "job_id": "exampleco--operations-analyst--5678abcd",
        "title": "Operations Analyst",
        "location": "San Francisco, USA",
        "must_have_requirements": "Operations analysis and reporting",
        "jd_summary": "Operations role with limited evidence returned by the evaluator.",
    })
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(row))
        writer.writeheader()
        writer.writerow(row)
        writer.writerow(missing)


def _minimal_interview_notes() -> str:
    whys = []
    for question in [
        "Why this industry",
        "Why this company",
        "Why this position",
        "Why you",
    ]:
        for version in ["V1", "V2", "V3"]:
            whys.append(
                f"| {question} | {version} | Cloud platform market context and ExampleCo "
                "product signals make this answer specific. |"
            )
    return (
        "# Interview Prep\n\n"
        "## Self-Introduction\n\n"
        "| Time | Script |\n|---|---|\n| 60s | I build operating systems. |\n\n"
        "## Requirements\n\n"
        "| Requirement | Evidence |\n|---|---|\n| Strategy | ST-01 |\n\n"
        "## Red Flags\n\n"
        "| # | Question | Why they'll ask | How to answer | Story refs |\n"
        "|---|---|---|---|---|\n| 1 | Gap? | Seniority | Truthfully | ST-01 |\n\n"
        "## Whys\n\n"
        "| Why Question | Version | Answer |\n|---|---|---|\n"
        + "\n".join(whys)
        + "\n\n"
        "## Behavioral\n\n"
        "| Question | Story | Angle | Tag |\n|---|---|---|---|\n"
        "| Tell me about execution | ST-01 | cadence | ops |\n\n"
        "## Company Glossary\n\n"
        "| Term | Meaning |\n|---|---|\n| Cloud platform market | ExampleCo product market |\n\n"
        "## Recent News\n\n"
        "| Date | News | Source |\n|---|---|---|\n| 2026-07-01 | Product update | Example source |\n\n"
        "## Questions\n\n"
        "| Question | Why |\n|---|---|\n| How is success measured? | Role clarity |\n\n"
        "## Citations\n\n"
        "| Source | URL |\n|---|---|\n| Example source | https://example.com |\n"
    )


def main() -> int:
    compact_jobs = [
        {"job_id": f"job-{i}", "jd_summary": "x" * 5000}
        for i in range(8)
    ]
    batches = workflows._make_score_batches(compact_jobs, max_jobs=20, max_chars=12000)
    if len(batches) < 4:
        print(f"FAIL: adaptive score batches are too large: {[len(b) for b in batches]}", file=sys.stderr)
        return 1
    accepted, missing, issues = workflows._validate_score_batch_ratings(
        [
            {"job_id": "job-1", "ratings": {"role_fit": 4}},
            {"job_id": "unknown", "ratings": {"role_fit": 4}},
        ],
        {"job-1", "job-2"},
        {"role_fit"},
    )
    if set(accepted) != {"job-1"} or missing != {"job-2"} or not issues:
        print("FAIL: score batch validation did not detect missing/unknown rows", file=sys.stderr)
        return 1

    with tempfile.TemporaryDirectory(prefix="rolescout-score-test-") as td:
        project = Path(td) / "projects" / "tester--score"
        shutil.copytree(ROOT / "rolescout" / "fixtures" / "mock-project", project)
        artifact_payload = {
            "schema": "rolescout-artifact-output-v1",
            "artifacts": [{
                "path": "linkedin/example/linkedin-review.md",
                "text": (
                    "# LinkedIn Review\n\n"
                    "## Part 1 - Scorecard\n\n"
                    "| Section | Score | Strengths | Gaps | Missing |\n"
                    "|---|---:|---|---|---|\n"
                    "| Headline | 2 | ok | gap | missing |\n"
                    "| About | 1 | ok | gap | missing |\n"
                    "| Experience entries | 1 | ok | gap | missing |\n"
                    "| Skills | 1 | ok | gap | missing |\n"
                    "| Education | 1 | ok | gap | missing |\n"
                    "| Activity | 1 | ok | gap | missing |\n\n"
                    "Overall score: 1.1/5 (weighted: Experience x3).\n\n"
                    "## Part 2 - Current / Add / Remove / Change Proposals\n\n"
                    "### Headline\n\n**Current**\n```text\nold\n```\n\n**Proposed**\n```text\nnew\n```\n\n"
                    "### About\n\n**Current**\n```text\nold\n```\n\n**Proposed**\n```text\nnew\n```\n\n"
                    "### Experience\n\n**Current**\n```text\nold\n```\n\n**Add**\n```text\nnew\n```\n\n"
                    "### Skills\n\n**Current**\n```text\nold\n```\n\n**Add**\n```text\nnew\n```\n\n"
                    "### Education\n\n**Current**\n```text\nold\n```\n\n**Add**\n```text\nnew\n```\n\n"
                    "### Activity\n\n**Current**\n```text\nold\n```\n\n**Proposed**\n```text\nnew\n```\n"
                ),
            }],
            "store_writes": [],
        }
        if not workflows._materialize_runner_artifact_output(FakeCtx(project), {
            "events": [{
                "type": "result",
                "content": "ROLESCOUT_ARTIFACT_OUTPUT_JSON:\n" + json.dumps(artifact_payload),
            }],
        }):
            print("FAIL: runner artifact output was not materialized", file=sys.stderr)
            return 1
        if not (project / "linkedin" / "example" / "linkedin-review.md").exists():
            print("FAIL: runner artifact file was not written", file=sys.stderr)
            return 1
        linkedin_validation = _run([
            sys.executable,
            str(SCRIPTS / "validate_linkedin_review.py"),
            str(project / "linkedin" / "example" / "linkedin-review.md"),
        ], project)
        if linkedin_validation.returncode != 0:
            print(linkedin_validation.stdout + linkedin_validation.stderr, file=sys.stderr)
            return 1
        interview_payload = {
            "schema": "rolescout-artifact-output-v1",
            "artifacts": [{
                "path": "interviews/story-bank.json",
                "json": {
                    "entries": [{
                        "id": "ST-01",
                        "title": "Built operating cadence",
                        "source": "resume",
                        "situation": "A team needed clearer execution rhythm.",
                        "task": "Create a repeatable process.",
                        "action": "Built dashboards and reviews.",
                        "result": "Leaders made faster decisions.",
                        "best_for": ["operations"],
                        "ev_refs": ["EV-001"],
                    }],
                },
            }, {
                "path": "interviews/example-role/prep-notes.md",
                "text": _minimal_interview_notes(),
            }],
            "store_writes": [],
        }
        interview_ctx = FakeCtx(project)
        if not workflows._materialize_runner_artifact_output(interview_ctx, {
            "events": [{
                "type": "result",
                "content": "ROLESCOUT_ARTIFACT_OUTPUT_JSON:\n" + json.dumps(interview_payload),
            }],
        }):
            print("FAIL: runner interview artifact output was not materialized", file=sys.stderr)
            return 1
        interview_validation = _run([
            sys.executable,
            str(SCRIPTS / "validate_interview_prep.py"),
            str(project),
        ], project)
        if interview_validation.returncode != 0:
            print(interview_validation.stdout + interview_validation.stderr, file=sys.stderr)
            return 1
        scoped_ctx = FakeCtx(project)
        scoped_payload = {
            "schema": "rolescout-artifact-output-v1",
            "artifacts": [{
                "path": "interviews/wrong-role/prep-notes.md",
                "text": _minimal_interview_notes(),
            }, {
                "path": "interviews/expected-role/prep-notes.md",
                "text": _minimal_interview_notes(),
            }],
            "store_writes": [],
        }
        workflows._materialize_runner_artifact_output(scoped_ctx, {
            "events": [{
                "type": "result",
                "content": "ROLESCOUT_ARTIFACT_OUTPUT_JSON:\n" + json.dumps(scoped_payload),
            }],
        }, allowed_paths={"interviews/expected-role/prep-notes.md"})
        if (project / "interviews" / "wrong-role" / "prep-notes.md").exists():
            print("FAIL: out-of-scope interview artifact was written", file=sys.stderr)
            return 1
        if not (project / "interviews" / "expected-role" / "prep-notes.md").exists():
            print("FAIL: expected scoped interview artifact was not written", file=sys.stderr)
            return 1
        _write_json(project / "project-meta.json", {
            "target_locations": ["San Francisco"],
            "target_level": "Manager",
            "target_companies": ["ExampleCo"],
            "search_view_filter_mode": "deterministic",
        })
        shutil.copy(
            ROOT / "references" / "scoring-config.default.json",
            project / "strategy" / "scoring-config.json",
        )
        _write_job_list(project / "data" / "job_list.csv")
        score_payload = {
            "schema": "rolescout-score-output-v1",
            "job_groups": [{
                "slug": "strategy-bd",
                "markdown": "# Target Group: Strategy BD (strategy-bd)\n\n## Roles in group\nexample",
            }],
            "target_priorities_md": "# Target Priorities\n\n1. Strategy BD",
            "job_ratings": [{
            "job_id": "exampleco--lead-strategy-manager--1234abcd",
            "job_group": "strategy-bd",
            "ratings": {
                "role_fit": 4,
                "comp_potential": 4,
                "company_quality": 4,
                "location_remote": 5,
                "growth_path": 4,
                "likelihood": 3,
                "network_access": 1,
                "interview_cost": 3,
                "timing": 5,
            },
            "rationale": {
                "role_fit": "Evidence-backed strategy/BD fit with one domain gap.",
                "location_remote": "Target city match.",
            },
            }],
        }
        materialized = workflows._materialize_score_output(FakeCtx(project), {
            "events": [{
                "type": "result",
                "content": "SCORE_OUTPUT_JSON:\n" + json.dumps(score_payload),
            }],
        })
        if not materialized:
            print("FAIL: score output JSON was not materialized", file=sys.stderr)
            return 1

        init = _run([sys.executable, str(SCRIPTS / "init_db.py")], project)
        if init.returncode != 0:
            print(init.stdout + init.stderr, file=sys.stderr)
            return 1
        view = _run([sys.executable, str(SCRIPTS / "build_search_view.py"), str(project)], project)
        if view.returncode != 0:
            print(view.stdout + view.stderr, file=sys.stderr)
            return 1
        final = _run([sys.executable, str(SCRIPTS / "finalize_score.py"), str(project)], project)
        if final.returncode != 0:
            print(final.stdout + final.stderr, file=sys.stderr)
            return 1

        scores = json.loads((project / "strategy" / "job-scores.json").read_text(encoding="utf-8"))
        if len(scores) != 2:
            print(f"FAIL: unexpected scores {scores}", file=sys.stderr)
            return 1
        first_score = next(
            (s for s in scores if s.get("job_id") == "exampleco--lead-strategy-manager--1234abcd"),
            None,
        )
        if not first_score or first_score.get("suggested_priority") != "high":
            print(f"FAIL: expected high score for rated row: {scores}", file=sys.stderr)
            return 1
        rows = list(csv.DictReader((project / "data" / "job_list.visible.csv").open(
            newline="", encoding="utf-8"
        )))
        if len(rows) != 2:
            print(f"FAIL: expected two visible rows, got {len(rows)}", file=sys.stderr)
            return 1
        row = next(r for r in rows if r.get("job_id") == "exampleco--lead-strategy-manager--1234abcd")
        if row.get("fit_score") != "4" or row.get("priority") != "high":
            print(f"FAIL: score fields not finalized: {row}", file=sys.stderr)
            return 1
        if row.get("job_group") != "strategy-bd":
            print(f"FAIL: job_group not finalized: {row}", file=sys.stderr)
            return 1
        fallback = next(r for r in rows if r.get("job_id") == "exampleco--operations-analyst--5678abcd")
        if fallback.get("fit_score") != "1" or fallback.get("priority") != "low":
            print(f"FAIL: fallback row not parked low: {fallback}", file=sys.stderr)
            return 1
        if fallback.get("job_group") != "parked" or "Deterministic fallback" not in fallback.get("notes", ""):
            print(f"FAIL: fallback rationale not visible: {fallback}", file=sys.stderr)
            return 1

    print("PASS: score finalization self-test")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
