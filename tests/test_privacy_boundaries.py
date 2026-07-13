from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from rolenavi import decision_policy
from rolenavi.llm.codex import CodexProvider
from rolenavi.llm.prompts import workflow_prompt_with_audit
from rolenavi.llm.runtime import provider_environment, staged_working_directory
from rolenavi.privacy.prompt_gateway import prepare_prompt_context
from rolenavi.telemetry import store as telemetry


def test_prompt_gateway_deny_by_default_and_target_comp_allowed():
    sentinel = "LOCAL_ONLY_SENTINEL_9b7d"
    context = {
        "project": "C:/private/person/project",
        "linkedin_url": f"https://linkedin.com/in/{sentinel}",
        "instructions": f"visa status: {sentinel}",
        "targets": f"Target comp range preference: SGD 200k\ncontact: {sentinel}",
        "runner_context_packet": {
            "candidate_profile_md": f"Strength: systems\nEmail: {sentinel}@example.com",
            "evidence_map_md": "EV-001 systems evidence",
            "application_state": sentinel,
        },
    }
    clean, audit = prepare_prompt_context("prep-strategy", context)
    encoded = json.dumps(clean)
    assert sentinel not in encoded
    assert "SGD 200k" in encoded
    assert "project" in audit.removed_fields
    assert "target_compensation" in audit.data_classes


def test_linkedin_content_is_workflow_specific():
    packet = {"runner_context_packet": {"linkedin_current_md": "LINKEDIN_CONTENT_SENTINEL"}}
    resume, _ = prepare_prompt_context("prep-resume", packet)
    linkedin, _ = prepare_prompt_context("prep-linkedin", packet)
    # The resume packet builder no longer adds this field; the gateway still
    # proves LinkedIn content is available to the one workflow that needs it.
    assert "LINKEDIN_CONTENT_SENTINEL" in json.dumps(linkedin)
    assert "linkedin_content" not in prepare_prompt_context("prep-resume", packet)[1].data_classes


def test_canonical_decision_policy_is_model_allowed_without_profile_block(tmp_path: Path):
    profile = tmp_path / "profile"
    profile.mkdir()
    payload = decision_policy.build(profile, """
<user_profile>private biographical material</user_profile>
<preference>Dealbreaker: must not be a backward career progression.</preference>
""")
    assert payload["constraints"]["no_backward_career_progression"] is True
    assert "private biographical material" not in payload["policy_text"]
    clean, _ = prepare_prompt_context(
        "prep-strategy", {"runner_context_packet": {"decision_policy": payload}}
    )
    assert clean["runner_context_packet"]["decision_policy"]["constraints"][
        "no_backward_career_progression"
    ] is True


def test_provider_environment_does_not_inherit_secret(monkeypatch):
    monkeypatch.setenv("ROLENAVI_SECRET_SENTINEL", "do-not-inherit")
    env = provider_environment()
    assert "ROLENAVI_SECRET_SENTINEL" not in env
    assert env["PYTHONUTF8"] == "1"


def test_staging_directory_contains_no_workspace_files():
    with staged_working_directory("test") as stage:
        assert {path.name for path in stage.iterdir()} == {"README.txt"}


def test_codex_command_is_content_only_and_read_only(tmp_path: Path):
    provider = object.__new__(CodexProvider)
    provider.exe = "codex"
    command = provider._exec_command("score", cwd=str(tmp_path), profile={
        "model": "test-model", "effort": "low", "settings_file": "test",
    })
    joined = " ".join(command)
    assert "--sandbox read-only" in joined
    assert "--skip-git-repo-check" in joined
    assert "--ephemeral" in joined
    assert "features.shell_tool=false" in joined
    assert "features.unified_exec=false" in joined
    assert "features.apps=false" in joined
    assert 'history.persistence="none"' in joined
    assert str(tmp_path) in command


def test_invariant_prompt_contract_is_small():
    prompt, audit = workflow_prompt_with_audit("prep-strategy", {
        "targets": "Target locations: Singapore",
        "runner_context_packet": {"focused_jobs": []},
    })
    assert len(prompt.encode("utf-8")) < 15_000
    assert audit.input_bytes == len(prompt.encode("utf-8"))


def test_global_telemetry_is_metrics_only(tmp_path: Path):
    db = tmp_path / "telemetry.db"
    secret = "RAW_MODEL_OUTPUT_SENTINEL"
    telemetry.record_run({
        "run_id": "r1", "started_at": "2026-01-01T00:00:00Z",
        "finished_at": "2026-01-01T00:00:01Z", "workflow": "score", "mode": "live",
        "project": "private-project", "summary": secret,
        "validator_results": [{"validator": "v", "returncode": 1, "output": secret}],
        "events": [{"type": "result", "content": secret}],
        "input_bytes": 123, "prompt_fingerprint": "a" * 64,
        "data_classes": ["job"],
    }, path=db)
    assert secret.encode() not in db.read_bytes()
    con = sqlite3.connect(db)
    try:
        row = con.execute("SELECT project, summary, input_bytes FROM runs").fetchone()
        assert row == ("", "", 123)
        assert con.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
    finally:
        con.close()
