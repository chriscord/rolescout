from __future__ import annotations

import json
import os
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace

from rolescout import core
from rolescout.llm.codex import CodexProvider
from rolescout.llm.prompts import workflow_prompt
from rolescout.runner import workflows
from rolescout.runner.workflows import _canonical_story_bank, _story_bank_markdown
from rolescout.web import server as web_server


def linkedin_review(*, extra_score_row: str = "") -> str:
    examples = {
        "Headline": ("current headline", "proposed headline"),
        "About": ("current about", "proposed about"),
        "Experience entries": (
            "Role — Company — 2020 - Present\n- Current responsibility",
            "Role — Company — 2020 - Present\n- Proposed achievement",
        ),
        "Skills": ("Strategy\nAnalytics\nOperations", "AI Strategy\nProduct Management\nAnalytics"),
        "Education": ("University\nDegree", "University\nDegree"),
    }
    proposals = "\n\n".join(
        f"### {section}\n\n**Current**\n```text\n{examples[section][0]}\n```\n\n"
        f"**Proposed**\n```text\n{examples[section][1]}\n```"
        for section in examples
    )
    return f"""# LinkedIn Review

## Scorecard

| Section | Score | Strengths | Gaps | Missing |
|---|---:|---|---|---|
| Headline | 3/5 | clear | narrow | none |
| About | 3/5 | clear | narrow | none |
| Experience entries | 4/5 | clear | narrow | none |
| Skills | 3/5 | clear | narrow | none |
| Education | 3/5 | clear | narrow | none |
{extra_score_row}

Overall score: 3.4/5 (weighted: Experience x3).

1. Lead with relevant proof.
2. Tighten the About section.
3. Pin target skills.

## Recommendations

{proposals}
"""


def test_linkedin_validator_needs_no_part_wrappers_and_scores_five_sections(tmp_path: Path):
    validator = core.load("validate_linkedin_review")
    path = tmp_path / "linkedin-review.md"
    path.write_text(linkedin_review(), encoding="utf-8")
    assert validator.validate_file(path) == []


def test_linkedin_validator_rejects_activity_and_featured_score_rows(tmp_path: Path):
    validator = core.load("validate_linkedin_review")
    for section in ("Activity", "Featured"):
        path = tmp_path / f"{section}.md"
        path.write_text(linkedin_review(extra_score_row=f"| {section} | 2/5 | x | y | z |"),
                        encoding="utf-8")
        errors = validator.validate_file(path)
        assert any("must not be a scored" in error for error in errors)


def test_linkedin_validator_requires_copy_ready_proposed_blocks(tmp_path: Path):
    validator = core.load("validate_linkedin_review")
    path = tmp_path / "linkedin-review.md"
    broken = linkedin_review().replace(
        "**Proposed**\n```text\nAI Strategy\nProduct Management\nAnalytics\n```",
        "**Change**\n```text\nPrioritize visible skills near the top: AI Strategy, Product Management.\n```",
    )
    path.write_text(broken, encoding="utf-8")
    errors = validator.validate_file(path)
    assert any("Skills' missing fenced Proposed block" in error for error in errors)


def test_linkedin_validator_rejects_advisory_prose_inside_proposed(tmp_path: Path):
    validator = core.load("validate_linkedin_review")
    path = tmp_path / "linkedin-review.md"
    broken = linkedin_review().replace(
        "AI Strategy\nProduct Management\nAnalytics",
        "Prioritize visible skills near the top: AI Strategy, Product Management, Analytics.",
    )
    path.write_text(broken, encoding="utf-8")
    errors = validator.validate_file(path)
    assert any("advisory prose" in error for error in errors)
    assert any("one concise skill name per line" in error for error in errors)


def test_linkedin_validator_accepts_plain_semantic_labels(tmp_path: Path):
    validator = core.load("validate_linkedin_review")
    path = tmp_path / "linkedin-review.md"
    plain = linkedin_review().replace("**Current**", "Current").replace(
        "**Proposed**", "Proposed"
    )
    path.write_text(plain, encoding="utf-8")
    assert validator.validate_file(path) == []


def test_linkedin_validator_accepts_semantic_fence_info_strings(tmp_path: Path):
    validator = core.load("validate_linkedin_review")
    path = tmp_path / "linkedin-review.md"
    inline = linkedin_review().replace("**Current**\n```text", "```Current").replace(
        "**Proposed**\n```text", "```Proposed"
    )
    path.write_text(inline, encoding="utf-8")
    assert validator.validate_file(path) == []


def test_linkedin_ui_keeps_guidance_outside_mockups():
    ui = web_server.UI_PATH.read_text(encoding="utf-8")
    assert "proposedText:''" in ui
    assert "else if (mode === 'change') sec.changeGuidance.push(blk);" in ui
    assert "const proposed = sec.proposedText;" in ui
    assert "Change guidance" in ui
    assert "sec.addText" not in ui


def test_linkedin_capture_rejects_top_card_only_and_accepts_full_profile():
    capture = core.load("capture_linkedin_profile")
    base = {"url": "https://www.linkedin.com/in/example", "text": "A" * 350}
    assert capture.useful_profile(base) is False
    full = dict(base)
    full["text"] += "\nExperience\nRole\nSkills\nStrategy\nEducation\nUniversity\n"
    assert capture.useful_profile(full) is True


def test_linkedin_capture_accepts_numbered_section_headings_and_nested_scroller():
    capture = core.load("capture_linkedin_profile")
    payload = {
        "url": "https://www.linkedin.com/in/example/",
        "text": "A" * 350 + "\nExperience (8)\nRole\nSkills (25)\nStrategy\n"
                "Education (3)\nUniversity\n",
    }
    assert capture.useful_profile(payload) is True
    assert "main#workspace" in capture.CAPTURE_JS
    assert "scroller.scrollTop" in capture.CAPTURE_JS


def test_linkedin_browser_backends_reuse_legacy_authenticated_session(tmp_path: Path,
                                                                      monkeypatch):
    capture = core.load("capture_linkedin_profile")
    monkeypatch.setenv("ROLESCOUT_HOME", str(tmp_path))
    legacy = tmp_path / "browser" / "linkedin-playwright"
    legacy.mkdir(parents=True)
    assert capture.browser_session_dir() == legacy


def test_linkedin_surface_urls_include_complete_recruiter_sections():
    capture = core.load("capture_linkedin_profile")
    surfaces = capture.profile_surface_urls(
        "https://linkedin.com/in/example/?trk=public_profile"
    )
    assert [label for label, _ in surfaces] == [
        "Profile", "Experience", "Skills", "Education"
    ]
    assert surfaces[-1][1] == "https://linkedin.com/in/example/details/education/"


def test_linkedin_publish_gate_preserves_validator_feedback_for_repair(tmp_path: Path):
    ctx = workflows.RunContext("prep-linkedin", tmp_path, "mock")
    ok = workflows._prevalidate_linkedin_payload(ctx, [{
        "path": "linkedin/group/linkedin-review.md",
        "text": "# incomplete review\n",
    }], "prep-linkedin-001-group")
    assert ok is False
    error = ctx.publish_errors["prep-linkedin-001-group"]
    assert error["code"] == "linkedin-review-validation"
    assert "missing scored section" in error["detail"]
    assert ctx.partial_reasons == []


def test_linkedin_group_jobs_enable_bounded_publish_repair(tmp_path: Path, monkeypatch):
    captured = {}
    monkeypatch.setattr(workflows, "_prepare_processed_jds", lambda ctx: None)
    monkeypatch.setattr(workflows, "_focused_groups",
                        lambda project: [{"slug": "group", "jobs": []}])

    def collect(ctx, provider, jobs, on_stream, **kwargs):
        captured["jobs"] = jobs
        return {"events": [], "usage": {}}

    monkeypatch.setattr(workflows, "_run_parallel_named_subagents", collect)
    ctx = workflows.RunContext("prep-linkedin", tmp_path, "mock")
    workflows._run_linkedin_workflow(
        ctx, object(), lambda *args: {"runner_context_packet": {}}, "", lambda text: None
    )
    assert captured["jobs"][0]["repair_on_publish_fail"] is True
    assert captured["jobs"][0]["publish_repair_attempts"] == 2


def test_aggregate_linkedin_sends_only_pursue_and_conditional_groups(
        tmp_path: Path, monkeypatch):
    captured = {}
    monkeypatch.setattr(workflows, "_prepare_processed_jds", lambda ctx: None)
    monkeypatch.setattr(workflows, "_focused_groups", lambda project: [
        {"slug": "pursue", "jobs": [{"job_id": "p"}]},
        {"slug": "conditional", "jobs": [{"job_id": "c"}]},
        {"slug": "parked", "jobs": [{"job_id": "x"}]},
    ])
    monkeypatch.setattr(workflows, "_strategy_dispositions", lambda project: {
        "p": {"disposition": "pursue", "reason": "fit"},
        "c": {"disposition": "conditional", "reason": "verify"},
        "x": {"disposition": "parked", "reason": "gap"},
    })

    def collect(ctx, provider, jobs, on_stream, **kwargs):
        captured["labels"] = [job["label"] for job in jobs]
        return {"events": [], "usage": {}, "published_labels": [],
                "validation_failed_labels": []}

    monkeypatch.setattr(workflows, "_run_parallel_named_subagents", collect)
    ctx = workflows.RunContext("prep", tmp_path, "mock")
    workflows._run_linkedin_workflow(
        ctx, object(), lambda *args: {"runner_context_packet": {}}, "",
        lambda text: None, aggregate=True,
    )
    assert captured["labels"] == [
        "prep-linkedin-001-pursue", "prep-linkedin-002-conditional"
    ]


def test_linkedin_normalizer_enforces_one_to_five_and_weighted_average():
    raw = linkedin_review().replace(
        "| About | 3/5 |", "| About | 0/5 |"
    ).replace(
        "Overall score: 3.4/5 (weighted: Experience x3).",
        "Overall score: 22/35 with Experience weighted x3",
    )
    normalized = workflows._normalize_linkedin_review_text(raw)
    assert "| About | 1/5 |" in normalized
    assert "Overall score: 3.1/5 (weighted: Experience x3)." in normalized
    assert "22/35" not in normalized


def test_story_bank_legacy_list_is_normalized_with_stable_shape():
    canonical, errors = _canonical_story_bank([{
        "id": "story-001", "title": "Launch", "source": "Role bullet 1",
        "situation": "A launch needed a plan.", "task": "Build the plan.",
        "action": "I aligned the teams.", "result": "The launch completed.",
        "best_for": ["leadership"], "ev_refs": "EV-001",
    }])
    assert errors == []
    assert canonical is not None
    assert canonical["entries"][0]["id"] == "ST-01"
    assert "| ST-01 |" in _story_bank_markdown(canonical)


def test_story_bank_rejects_semantically_incomplete_entry():
    canonical, errors = _canonical_story_bank([{"id": "ST-01", "title": "Thin"}])
    assert canonical is None
    assert errors


def test_strategy_and_interview_enable_public_web_search_only(tmp_path: Path):
    provider = object.__new__(CodexProvider)
    provider.exe = "codex"
    profile = {"model": "test", "effort": "low", "settings_file": "test"}
    strategy = " ".join(provider._exec_command("prep-strategy", str(tmp_path), profile))
    interview = " ".join(provider._exec_command("prep-interview", str(tmp_path), profile))
    score = " ".join(provider._exec_command("score", str(tmp_path), profile))
    assert 'web_search="live"' in strategy
    assert 'web_search="live"' in interview
    assert 'web_search="disabled"' in score


def test_strategy_prompt_has_quality_contract_without_sentence_geometry():
    prompt = workflow_prompt("prep-strategy", {"runner_context_packet": {}})
    assert "same-company multi-role strategy" in prompt
    assert "no fixed sentence, paragraph" in prompt
    assert "or length\nrule" in prompt
    assert "never log in" in prompt


def test_strategy_group_limit_rejects_near_one_role_per_group():
    assert workflows._max_strategy_groups(5) == 5
    assert workflows._max_strategy_groups(15) == 9


def test_resume_packet_contract_is_exact_and_prompt_requires_typed_json():
    contract = workflows._resume_artifact_contract("product-leadership")
    assert contract["schema"] == "rolescout-resume-group-artifacts-v1"
    assert contract["target_brief"]["schema"] == "rolescout-resume-target-brief-v1"
    assert contract["target_brief"]["priority_enum"] == ["must", "preferred"]
    prompt = workflow_prompt("prep-resume", {
        "runner_context_packet": {"artifact_contract": contract},
    })
    assert "artifact_contract" in prompt
    assert "artifact's `json` field" in prompt
    assert "must_have_requirements" in prompt
    assert "at most 16 experience bullets and 360" in prompt
    assert "never add a seventeenth bullet" in prompt


def test_resume_schema_gate_accepts_contract_and_rejects_alternate_arrays():
    brief = {
        "schema": "rolescout-resume-target-brief-v1",
        "group": "product-leadership",
        "source_job_ids": ["job-1"],
        "positioning_angle": "Product-adjacent operator",
        "requirements": [{
            "id": "REQ-1", "priority": "must", "text": "Product leadership",
            "keywords": ["product"], "source_job_ids": ["job-1"],
        }],
        "gaps": [{"requirement_id": "REQ-1", "gap": "Limited direct tenure"}],
    }
    assert workflows._validate_resume_target_brief(brief) == []
    wrong = dict(brief)
    wrong.pop("requirements")
    wrong["must_have_requirements"] = []
    assert any("requirements" in error
               for error in workflows._validate_resume_target_brief(wrong))


def test_resume_reason_mapping_feedback_names_exact_unmatched_content():
    draft = "# Resume\n\n## Experience\n\n- Led a complete mapped program.\n- Built the omitted workflow.\n\n## Education\n\n- University\n"
    reasons = [{"bullet_prefix": "Led a complete mapped program"}]
    diagnostics = workflows._resume_reason_mapping_diagnostics(draft, reasons)
    assert diagnostics["bullet_count"] == 2
    assert diagnostics["unmatched_bullets"] == [
        {"index": 1, "text": "Built the omitted workflow."}
    ]
    assert diagnostics["unmatched_reason_prefixes"] == []


def test_generated_artifacts_are_preserved_in_run_staging(tmp_path: Path):
    ctx = workflows.RunContext("prep", tmp_path, "mock")
    ctx.run_id = "20260712-test"
    staged = workflows._stage_generated_artifacts(ctx, "prep-resume-001-group",
                                                   "prep-resume", [{
        "path": "resumes/group/target-brief.json",
        "json": {"schema": "test"},
    }])
    physical = workflows._run_staging_dir(ctx, "prep-resume-001-group").name
    assert staged == [
        f"runtime/runs/20260712-test/staging/{physical}/artifacts/"
        "resumes/group/target-brief.json"
    ]
    status = web_server._load_json(
        tmp_path / "runtime" / "runs" / "20260712-test" / "staging" /
        physical / "status.json", {})
    assert status["state"] == "generated"
    assert (tmp_path / staged[0]).is_file()


def test_prep_staging_exposes_only_latest_repair_state(tmp_path: Path):
    run = tmp_path / "runtime" / "runs" / "20260712-test" / "staging"
    for label, state, stamp in (
        ("prep-resume-001-group", "validation_failed", "2026-07-12T01:00:00Z"),
        ("prep-resume-001-group-repair-1", "published", "2026-07-12T01:01:00Z"),
    ):
        folder = run / label
        folder.mkdir(parents=True)
        (folder / "status.json").write_text(json.dumps({
            "run_id": "20260712-test", "label": label, "workflow": "prep-resume",
            "state": state, "updated_at": stamp, "artifacts": [],
        }), encoding="utf-8")
    statuses = web_server._prep_staging(tmp_path)
    assert len(statuses) == 1
    assert statuses[0]["label"].endswith("repair-1")
    assert statuses[0]["state"] == "published"


def test_repairing_attempt_is_not_a_final_generated_failure(tmp_path: Path):
    ctx = workflows.RunContext("prep", tmp_path, "mock")
    ctx.run_id = "20260712-test"
    label = "prep-resume-001-group"
    workflows._stage_generated_artifacts(ctx, label, "prep-resume", [{
        "path": "resumes/group/resume-draft.md", "text": "draft",
    }])
    workflows._mark_staging_repairing(ctx, label, "prep-resume")
    statuses = web_server._prep_staging(tmp_path)
    assert statuses[0]["state"] == "repairing"
    ui = web_server.UI_PATH.read_text(encoding="utf-8")
    assert "['validation_failed','publish_failed'].includes(a.state)" in ui


def test_resume_second_repair_receives_cumulative_validator_feedback(
        tmp_path: Path, monkeypatch):
    calls = []

    def result_envelope():
        payload = {"schema": "rolescout-artifact-output-v1", "artifacts": [],
                   "store_writes": [], "notes": []}
        return {"events": [{"type": "result", "content":
                            "ROLESCOUT_ARTIFACT_OUTPUT_JSON:" + json.dumps(payload)}],
                "usage": {}}

    def provider_run(provider, workflow, context, on_stream, model_workflow):
        calls.append(json.loads(json.dumps(context)))
        return result_envelope()

    outcomes = iter([False, False, True])

    def materialize(ctx, envelope, allowed_paths=None, stage_workflow=None, label=""):
        outcome = next(outcomes)
        if not outcome:
            ctx.publish_errors[label] = {
                "code": "resume-content-validation",
                "detail": ("budget max 16" if "repair-1" not in label
                           else "coverage too low: missing REQ-006"),
            }
        return outcome

    monkeypatch.setattr(workflows, "_provider_run", provider_run)
    monkeypatch.setattr(workflows, "_materialize_runner_artifact_output", materialize)
    monkeypatch.setattr(workflows, "_append_agent_result_log", lambda *args: None)
    ctx = workflows.RunContext("prep", tmp_path, "mock")
    ctx.run_id = "run"
    result = workflows._run_parallel_named_subagents(
        ctx, object(), [{
            "label": "prep-resume-001-group", "workflow": "prep-resume",
            "model_workflow": "prep-resume", "repair_model_workflow": "prep-resume-repair",
            "context": {"runner_context_packet": {}},
            "repair_on_publish_fail": True, "publish_repair_attempts": 2,
        }], lambda text: None, hard_fail_when_all_fail=False,
    )
    feedback = calls[2]["runner_context_packet"]["repair"]["validator_feedback"]
    assert "budget max 16" in feedback
    assert "coverage too low: missing REQ-006" in feedback
    assert result["published_labels"] == ["prep-resume-001-group"]


def test_resume_repair_payload_is_merged_as_path_patch():
    def envelope(artifacts):
        payload = {"schema": "rolescout-artifact-output-v1", "artifacts": artifacts,
                   "store_writes": [], "notes": []}
        return {"events": [{"type": "result", "content":
                            "ROLESCOUT_ARTIFACT_OUTPUT_JSON:" + json.dumps(payload)}]}

    previous = envelope([
        {"path": "resumes/g/resume-draft.md", "text": "old draft"},
        {"path": "resumes/g/target-brief.json", "json": {"old": True}},
    ])
    repair = envelope([
        {"path": "resumes/g/target-brief.json", "json": {"fixed": True}},
    ])
    merged = workflows._merge_repair_artifact_envelope(previous, repair)
    payload = workflows._extract_runner_artifact_payload(workflows._result_text(merged))
    assert payload is not None
    by_path = {item["path"]: item for item in payload["artifacts"]}
    assert by_path["resumes/g/resume-draft.md"]["text"] == "old draft"
    assert by_path["resumes/g/target-brief.json"]["json"] == {"fixed": True}


def test_long_repair_labels_get_distinct_staging_directories(tmp_path: Path):
    ctx = workflows.RunContext("prep-resume", tmp_path, "mock")
    ctx.run_id = "run"
    prefix = "prep-resume-002-ai-strategy-gtm-analytics"
    one = workflows._run_staging_dir(ctx, prefix + "-repair-1")
    two = workflows._run_staging_dir(ctx, prefix + "-repair-2")
    assert one != two
    assert len(one.name) == 18
    assert len(two.name) == 18
    assert one.name.startswith("s-")
    assert two.name.startswith("s-")


def test_interview_staging_path_stays_within_windows_legacy_budget(tmp_path: Path):
    # Use a representative installed-workspace root. pytest's own tmp_path is
    # much deeper than a real project and would test an unrelated temp-path
    # budget instead of the staging layout that caused WinError 206.
    project = Path(
        "C:/Users/candidate/Documents/_WorkTools/rolescout/"
        "projects/candidate--search-project"
    )
    ctx = workflows.RunContext("prep-interview", project, "mock")
    ctx.run_id = "20260712-161926-7f79c2"
    label = (
        "prep-interview-001-google-forward-deployed-engineering-manager-"
        "7862dfe2-company-research-path-retry"
    )
    target = (workflows._run_staging_dir(ctx, label) / "artifacts" /
              "interviews" / "google-forward-deployed-engineering-manager" /
              "_stages" / "company-research.md")
    assert len(str(target)) < 260


def test_interview_role_slug_distinguishes_same_company_and_title():
    shared = {"company": "Grab", "title": "Lead Technical Program Manager"}
    one = workflows._interview_role_slug({
        **shared, "job_id": "grab--lead-technical-program-manager--7e6846b2",
    })
    two = workflows._interview_role_slug({
        **shared, "job_id": "grab--lead-technical-program-manager--940b4471",
    })
    assert one != two
    assert one.endswith("-7e6846b2")
    assert two.endswith("-940b4471")
    assert len(one) <= 48
    assert len(two) <= 48


def test_same_day_interview_pack_cache_reuses_complete_valid_set(
        tmp_path: Path, monkeypatch):
    role = {
        "company": "Grab", "title": "Lead Technical Program Manager",
        "job_id": "grab--lead-technical-program-manager--7e6846b2",
    }
    (tmp_path / "data").mkdir()
    (tmp_path / "interviews").mkdir()
    (tmp_path / "data" / "focused-jobs.json").write_text(
        json.dumps({"job_ids": [role["job_id"]]}), encoding="utf-8")
    (tmp_path / "interviews" / "interview-context.json").write_text(
        json.dumps({"roles": [role]}), encoding="utf-8")
    (tmp_path / "interviews" / "story-bank.json").write_text(
        json.dumps({"entries": [{"id": "ST-01"}]}), encoding="utf-8")
    output = tmp_path / workflows._interview_expected_artifact(role)
    output.parent.mkdir(parents=True)
    output.write_text("valid", encoding="utf-8")
    now = time.time()
    for path in (
        tmp_path / "data" / "focused-jobs.json",
        tmp_path / "interviews" / "interview-context.json",
        tmp_path / "interviews" / "story-bank.json",
    ):
        os.utime(path, (now - 10, now - 10))
    os.utime(output, (now, now))

    monkeypatch.setattr(workflows.core, "run_script", lambda *args, **kwargs:
                        SimpleNamespace(returncode=0, stdout="PASS", stderr=""))
    monkeypatch.setattr("rolescout.runner.preflight.profile_dir", lambda project: None)
    ctx = workflows.RunContext("prep-interview", tmp_path, "live")
    assert workflows._reuse_current_interview_packs(ctx, [role])
    assert workflows._interview_expected_artifact(role) in ctx.artifacts_written
    assert "Reused 1" in ctx.summary


def test_interview_why_validator_accepts_semantic_label_variants():
    from scripts import validate_interview_prep as validator

    text = """## The Whys

| Area | Version | Answer |
|---|---|---|
| Why industry | V1 | one |
| Industry | V2 | two |
| Why this industry | V3 | three |
| Company | V1 | one |
| Why company | V2 | two |
| Why this company | V3 | three |
| Position | V1 | one |
| Role | V2 | two |
| Why this position | V3 | three |
| Candidate | V1 | one |
| Why candidate | V2 | two |
| Why you | V3 | three |
"""
    rows = validator._why_rows(text)
    assert {(question, version) for question, version, _answer in rows} == {
        (question.lower(), version)
        for question in validator.REQUIRED_WHY_QUESTIONS
        for version in ("V1", "V2", "V3")
    }

    horizontal = """## The Whys

| Version | Why Industry | Why Google | Why Position | Why Candidate |
|---|---|---|---|---|
| V1 | i1 | c1 | p1 | y1 |
| V2 | i2 | c2 | p2 | y2 |
| V3 | i3 | c3 | p3 | y3 |
"""
    horizontal_rows = validator._why_rows(horizontal)
    assert len(horizontal_rows) == 12
    assert {question for question, _version, _answer in horizontal_rows} == {
        question.lower() for question in validator.REQUIRED_WHY_QUESTIONS
    }

    transposed = """## The Whys

| Topic | V1 | V2 | V3 | Evidence |
|---|---|---|---|---|
| Why industry | i1 | i2 | i3 | e |
| Why Google | c1 | c2 | c3 | e |
| Why this position | p1 | p2 | p3 | e |
| Why me / candidate | y1 | y2 | y3 | e |
"""
    transposed_rows = validator._why_rows(transposed)
    assert len(transposed_rows) == 12

    length_versions = """## The Whys

| Prompt | Version | Answer |
|---|---|---|
| Why this industry V1 | Short | i1 |
| Why this industry V2 | Medium | i2 |
| Why this industry V3 | Long | i3 |
| Why this company V1 | Short | c1 |
| Why this company V2 | Medium | c2 |
| Why this company V3 | Long | c3 |
| Why this position V1 | Short | p1 |
| Why this position V2 | Medium | p2 |
| Why this position V3 | Long | p3 |
| Why you V1 | Short | y1 |
| Why you V2 | Medium | y2 |
| Why you V3 | Long | y3 |
"""
    assert len(validator._why_rows(length_versions)) == 12

    grounded = (
        "Enterprise GenAI deployment is the next version of strategy and operations: "
        "the market now depends on adoption, governance, and business impact."
    )
    assert not any(
        "job function" in issue
        for issue in validator._quality_issues(Path("prep-notes.md"),
            "## The Whys\n\n| Why question | Version | Answer |\n"
            "|---|---|---|\n"
            f"| Why this industry | V1 | {grounded} |\n"
            f"| Why this industry | V2 | {grounded} |\n"
            f"| Why this industry | V3 | {grounded} |\n"
            "| Why this company | V1 | company |\n| Why this company | V2 | company |\n"
            "| Why this company | V3 | company |\n| Why this position | V1 | role |\n"
            "| Why this position | V2 | role |\n| Why this position | V3 | role |\n"
            "| Why you | V1 | me |\n| Why you | V2 | me |\n| Why you | V3 | me |\n"
            "\n## Glossary\n\n| Term | Meaning |\n|---|---|\n| Enterprise GenAI | market |\n")
    )


def test_prep_artifacts_marks_non_active_groups_stale(tmp_path: Path):
    (tmp_path / "data").mkdir()
    (tmp_path / "strategy").mkdir()
    (tmp_path / "data" / "focused-jobs.json").write_text(
        '{"job_ids":["job-1"]}\n', encoding="utf-8")
    (tmp_path / "strategy" / "group-assignments.json").write_text(
        '{"assignments":[{"job_id":"job-1","job_group":"active-group"}]}\n',
        encoding="utf-8")
    for group in ("active-group", "old-group"):
        folder = tmp_path / "resumes" / group
        folder.mkdir(parents=True)
        (folder / "resume-draft.md").write_text("# Resume\n", encoding="utf-8")
    states = {item["target"]: item["state"]
              for item in web_server._prep_artifacts(tmp_path)}
    assert states == {"active-group": "current", "old-group": "stale"}


def test_prep_artifacts_marks_parked_group_stale(tmp_path: Path):
    (tmp_path / "data").mkdir()
    (tmp_path / "strategy").mkdir()
    (tmp_path / "data" / "focused-jobs.json").write_text(
        '{"job_ids":["job-1","job-2"]}\n', encoding="utf-8")
    (tmp_path / "strategy" / "group-assignments.json").write_text(json.dumps({
        "assignments": [
            {"job_id": "job-1", "job_group": "active", "disposition": "pursue"},
            {"job_id": "job-2", "job_group": "parked", "disposition": "parked"},
        ]
    }), encoding="utf-8")
    for group in ("active", "parked"):
        folder = tmp_path / "resumes" / group
        folder.mkdir(parents=True)
        (folder / "resume-draft.md").write_text("# Resume\n", encoding="utf-8")
    states = {item["target"]: item["state"]
              for item in web_server._prep_artifacts(tmp_path)}
    assert states == {"active": "current", "parked": "stale"}


def test_validator_feedback_puts_blocker_before_length_warnings():
    output = """  WARN: bullet 1: 19 words
  WARN: bullet 2: 18 words
FAIL: 1 issue(s) across 12 bullet(s)
  - bullet 12: weak opener 'Worked on'
"""
    detail = workflows._validator_failure_first(output)
    assert detail.startswith("FAIL:")
    assert "weak opener 'Worked on'" in detail
    assert detail.index("weak opener") < detail.index("Non-blocking warnings")


def test_resume_bullet_validator_enforces_one_page_content_budget(tmp_path: Path):
    draft = tmp_path / "resume.md"
    bullets = [
        f"- Led strategy analysis for regional product launch and aligned stakeholders "
        f"across operations, partnerships, finance, and delivery workstream {index}."
        for index in range(17)
    ]
    draft.write_text("# Candidate\n\n## Experience\n\n" + "\n".join(bullets),
                     encoding="utf-8")
    result = core.run_script("validate_resume_bullets", str(draft))
    assert result.returncode == 1
    assert "one-page budget max 16" in result.stdout


def test_prep_state_exposes_durable_progress_and_revision(tmp_path: Path, monkeypatch):
    (tmp_path / "project.json").write_text('{"person":"p","focus":"f"}', encoding="utf-8")
    run = tmp_path / "runtime" / "runs" / "run-1"
    run.mkdir(parents=True)
    (run / "prep-progress.json").write_text(json.dumps({
        "schema": "rolescout-prep-progress-v1", "run_id": "run-1",
        "current_phase": "resume", "state": "running", "revision": 3,
        "phases": {"strategy": {"state": "published"},
                   "resume": {"state": "running", "completed": 1, "total": 2}},
    }), encoding="utf-8")
    monkeypatch.setattr(web_server, "_project_or_raise", lambda code: tmp_path)
    state = web_server.prep_state("project")
    assert state["prep_progress"]["current_phase"] == "resume"
    assert state["prep_progress"]["phases"]["resume"]["completed"] == 1
    assert len(state["revision"]) == 16


def test_parallel_prep_publishes_fast_sibling_before_slow_sibling(
        tmp_path: Path, monkeypatch):
    published = []

    def provider_run(provider, workflow, context, on_stream, model_workflow):
        time.sleep(context["delay"])
        return {"events": [], "usage": {}, "result": "ok"}

    def materialize(ctx, envelope, allowed_paths=None, stage_workflow=None, label=""):
        published.append(label)
        return True

    monkeypatch.setattr(workflows, "_provider_run", provider_run)
    monkeypatch.setattr(workflows, "_materialize_runner_artifact_output", materialize)
    monkeypatch.setattr(workflows, "_append_agent_result_log", lambda *args: None)
    ctx = workflows.RunContext("prep", tmp_path, "mock")
    ctx.run_id = "run"
    result = workflows._run_parallel_named_subagents(
        ctx, object(), [
            {"label": "slow", "workflow": "prep-resume",
             "model_workflow": "prep-resume", "context": {"delay": 0.08}},
            {"label": "fast", "workflow": "prep-resume",
             "model_workflow": "prep-resume", "context": {"delay": 0.0}},
        ], lambda text: None, hard_fail_when_all_fail=False,
    )
    assert published == ["fast", "slow"]
    assert set(result["published_labels"]) == {"fast", "slow"}


def test_prep_artifacts_hides_legacy_interview_paths_when_context_is_available(
        tmp_path: Path):
    role = {
        "company": "Grab", "title": "Lead Technical Program Manager",
        "job_id": "grab--lead-technical-program-manager--7e6846b2",
    }
    (tmp_path / "interviews").mkdir()
    (tmp_path / "interviews" / "interview-context.json").write_text(
        json.dumps({"roles": [role]}), encoding="utf-8")
    current = tmp_path / workflows._interview_expected_artifact(role)
    current.parent.mkdir(parents=True)
    current.write_text("current", encoding="utf-8")
    legacy = (tmp_path / "interviews" / "grab-lead-technical-program-manager" /
              "prep-notes.md")
    legacy.parent.mkdir(parents=True)
    legacy.write_text("legacy", encoding="utf-8")

    interviews = [item for item in web_server._prep_artifacts(tmp_path)
                  if item["kind"] == "interview"]
    assert [item["target"] for item in interviews] == [current.parent.name]


def test_interview_validator_honors_aggregate_pursue_scope(tmp_path: Path):
    from scripts import validate_interview_prep as validator

    roles = [
        {"job_id": "pursue", "company": "OpenAI", "title": "Deployment"},
        {"job_id": "conditional", "company": "OpenAI", "title": "Sales"},
    ]
    folder = tmp_path / "interviews"
    folder.mkdir()
    (folder / "interview-context.json").write_text(json.dumps({
        "roles": roles, "prep_scope_job_ids": ["pursue"],
    }), encoding="utf-8")
    files = validator._focused_prep_files(tmp_path)
    assert files is not None
    assert len(files) == 1
    assert "deployment" in files[0].as_posix()


def test_chat_running_indicator_is_updated_in_place_without_poll_teardown():
    ui = web_server.UI_PATH.read_text(encoding="utf-8")
    assert "log.insertBefore(d,anchor)" in ui
    assert "d.dataset.activityText!==text" in ui
    poll_body = ui.split("async function poll(){", 1)[1].split(
        "async function refreshPrepState", 1)[0]
    assert "clearRunActivity();" not in poll_body


def test_full_prep_downgrades_resume_failure_and_continues_interview(
        tmp_path: Path, monkeypatch):
    ctx = workflows.RunContext("prep", tmp_path, "mock")
    (tmp_path / "interviews").mkdir()
    (tmp_path / "interviews" / "story-bank.json").write_text(
        '{"meta":"valid prior bank","entries":[]}\n', encoding="utf-8")
    called = []

    monkeypatch.setattr(workflows, "_run_strategy_workflow",
                        lambda *args, **kwargs: {"events": [], "usage": {}})

    def fail_resume(*args, **kwargs):
        ctx.failure_class = "prep_resume_publish_gate_failure"
        return {"events": [], "usage": {}}

    monkeypatch.setattr(workflows, "_run_resume_workflow", fail_resume)
    monkeypatch.setattr(workflows, "_story_bank_needs_refresh", lambda *args: False)
    monkeypatch.setattr("rolescout.profile_meta.linkedin_url", lambda pdir: "")

    def interview(*args, **kwargs):
        called.append("interview")
        return {"events": [], "usage": {}}

    monkeypatch.setattr(workflows, "_run_interview_workflow", interview)
    workflows._run_prep_orchestration(
        ctx, object(), lambda *args: {}, "", lambda text: None, None, None)
    assert called == ["interview"]
    assert ctx.failure_class == ""
    assert ctx.run_status() == "partial"


def test_strategy_assignment_hydrates_complete_rows_before_upsert(tmp_path: Path, monkeypatch):
    full = {
        "job_id": "example--strategy-manager--1234abcd",
        "captured_at": "2026-07-12",
        "company": "Example",
        "title": "Strategy Manager",
        "source_url": "https://example.com/jobs/123",
        "job_group": "old-group",
    }
    monkeypatch.setattr("rolescout.repositories.job_rows",
                        lambda project, job_ids=None: [dict(full)])
    rows = workflows._hydrate_strategy_group_assignments(tmp_path, [{
        "job_id": full["job_id"], "job_group": "strategy-operations",
    }])
    assert rows == [{**full, "job_group": "strategy-operations"}]
    assert rows[0]["captured_at"] == "2026-07-12"
    assert rows[0]["source_url"].startswith("https://")


def test_stdlib_resume_builder_uses_real_numbering_and_tnr(tmp_path: Path):
    source = tmp_path / "resume.md"
    output = tmp_path / "resume.docx"
    source.write_text(
        "# Candidate Name\n\n## Work Experience\n\n- Led a strategy project with a measured outcome.\n\n"
        "## Education\n\nUniversity, Degree\n\n## Skills\n\nStrategy, Partnerships\n",
        encoding="utf-8",
    )
    result = core.run_script("build_resume_docx", str(source), str(output))
    assert result.returncode == 0, result.stdout + result.stderr
    with zipfile.ZipFile(output) as archive:
        document = archive.read("word/document.xml").decode("utf-8")
        styles = archive.read("word/styles.xml").decode("utf-8")
        numbering = archive.read("word/numbering.xml").decode("utf-8")
        assert "w:numId" in document
        assert "Times New Roman" in styles
        assert 'w:sz w:val="17"' in styles
        assert 'w:val="174"' not in document + styles
        assert 'w:ascii="Times New Roman"' in numbering
        assert 'w:w="11906" w:h="16838"' in document
