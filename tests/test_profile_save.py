from __future__ import annotations

from pathlib import Path

from rolenavi import profile_meta
from rolenavi.web import server as web_server


class _ProfileManager:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def start(self, person: str, **kwargs) -> bool:
        self.calls.append((person, kwargs))
        return True

    def active(self, person: str) -> bool:
        return False

    def summaries(self) -> dict:
        return {}


def _install(monkeypatch, tmp_path: Path) -> _ProfileManager:
    (tmp_path / "profiles").mkdir()
    manager = _ProfileManager()
    monkeypatch.setattr(web_server, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(web_server, "PROFILE_MANAGER", manager)
    return manager


def _payload(**updates) -> dict:
    value = {
        "person": "test-person",
        "name": "Test Person",
        "linkedin_url": "https://linkedin.com/in/test-person",
        "instructions": "Be concise.",
    }
    value.update(updates)
    return value


def _mark_current(root: Path, *, resume: bytes = b"resume-v1") -> Path:
    pdir = root / "profiles" / "test-person"
    pdir.mkdir(parents=True)
    profile_meta.replace(
        pdir,
        name="Test Person",
        linkedin_url="https://linkedin.com/in/test-person",
        instructions="Be concise.",
    )
    (pdir / "resume.pdf").write_bytes(resume)
    (pdir / "linkedin-current.md").write_text(
        "# LinkedIn Capture\nCaptured: yesterday\n\n"
        "## Visible LinkedIn Profile Text\nSame stable content\n",
        encoding="utf-8",
    )
    (pdir / "candidate-profile.md").write_text("# Candidate\n", encoding="utf-8")
    (pdir / "evidence-map.md").write_text("# Evidence\n", encoding="utf-8")
    profile_meta.mark_profile_built(pdir)
    return pdir


def test_new_profile_commits_resume_and_linkedin_in_one_save(monkeypatch, tmp_path: Path):
    manager = _install(monkeypatch, tmp_path)

    result = web_server.save_profile(
        _payload(), {"file-resume": ("resume.pdf", b"resume-v1")}
    )

    pdir = tmp_path / "profiles" / "test-person"
    assert (pdir / "resume.pdf").read_bytes() == b"resume-v1"
    assert profile_meta.linkedin_url(pdir) == _payload()["linkedin_url"]
    assert result["profile_intake_started"] is True
    assert result["resume_changed"] is True
    assert result["linkedin_changed"] is True
    assert manager.calls == [
        (
            "test-person",
            {
                "capture_linkedin": True,
                "task": "Reconcile the candidate profile from newly committed profile sources.",
            },
        )
    ]


def test_unchanged_sources_save_without_model_call(monkeypatch, tmp_path: Path):
    manager = _install(monkeypatch, tmp_path)
    _mark_current(tmp_path)

    result = web_server.save_profile(
        _payload(instructions="Updated standing instruction."),
        {"file-resume": ("copy-of-resume.pdf", b"resume-v1")},
    )

    assert result["llm_required"] is False
    assert result["profile_intake_started"] is False
    assert result["skipped_duplicate_files"] == ["copy-of-resume.pdf"]
    assert manager.calls == []
    assert profile_meta.instructions(tmp_path / "profiles" / "test-person") == (
        "Updated standing instruction."
    )


def test_unchanged_legacy_profile_establishes_hash_baseline_without_model(
    monkeypatch, tmp_path: Path
):
    manager = _install(monkeypatch, tmp_path)
    pdir = _mark_current(tmp_path)
    profile_meta.replace(pdir, profile_build_fingerprint="")

    result = web_server.save_profile(_payload(), {})

    assert result["llm_required"] is False
    assert profile_meta.profile_is_current(pdir)
    assert manager.calls == []


def test_changed_resume_rebuilds_without_recapturing_unchanged_linkedin(
    monkeypatch, tmp_path: Path
):
    manager = _install(monkeypatch, tmp_path)
    pdir = _mark_current(tmp_path)

    result = web_server.save_profile(
        _payload(), {"file-resume": ("resume.pdf", b"resume-v2")}
    )

    assert (pdir / "resume.pdf").read_bytes() == b"resume-v2"
    assert result["resume_changed"] is True
    assert result["capture_linkedin"] is False
    assert manager.calls[0][1]["capture_linkedin"] is False


def test_rename_of_identical_resume_does_not_rebuild(monkeypatch, tmp_path: Path):
    manager = _install(monkeypatch, tmp_path)
    pdir = _mark_current(tmp_path)

    result = web_server.save_profile(
        _payload(remove_materials='["resume.pdf"]'),
        {"file-resume": ("renamed-resume.pdf", b"resume-v1")},
    )

    assert not (pdir / "resume.pdf").exists()
    assert (pdir / "renamed-resume.pdf").exists()
    assert result["resume_changed"] is False
    assert result["llm_required"] is False
    assert profile_meta.profile_is_current(pdir)
    assert manager.calls == []


def test_changed_linkedin_url_invalidates_snapshot_and_requests_capture(
    monkeypatch, tmp_path: Path
):
    manager = _install(monkeypatch, tmp_path)
    pdir = _mark_current(tmp_path)

    result = web_server.save_profile(
        _payload(linkedin_url="https://www.linkedin.com/in/new-url/"), {}
    )

    assert not (pdir / "linkedin-current.md").exists()
    assert result["linkedin_changed"] is True
    assert result["capture_linkedin"] is True
    assert manager.calls[0][1]["capture_linkedin"] is True


def test_explicit_resync_is_only_path_that_forces_same_url_capture(
    monkeypatch, tmp_path: Path
):
    manager = _install(monkeypatch, tmp_path)
    _mark_current(tmp_path)

    result = web_server.resync_linkedin({"person": "test-person"})

    assert result["profile_intake_started"] is True
    assert manager.calls == [
        (
            "test-person",
            {
                "capture_linkedin": True,
                "skip_if_unchanged": True,
                "task": "Explicit LinkedIn resync requested by the user.",
            },
        )
    ]


def test_linkedin_fingerprint_ignores_capture_metadata(tmp_path: Path):
    pdir = tmp_path / "profile"
    pdir.mkdir()
    path = pdir / "linkedin-current.md"
    path.write_text(
        "Captured: first\n\n## Visible LinkedIn Profile Text\nStable body\n",
        encoding="utf-8",
    )
    first = profile_meta.linkedin_content_fingerprint(pdir)
    path.write_text(
        "Captured: second\nURL: different header\n\n"
        "## Visible LinkedIn Profile Text\nStable body\n",
        encoding="utf-8",
    )
    assert profile_meta.linkedin_content_fingerprint(pdir) == first


def test_profile_ui_uses_one_commit_and_explicit_linkedin_resync():
    ui = web_server.UI_PATH.read_text(encoding="utf-8")

    assert "/api/profile/save" in ui
    assert "Resync LinkedIn profile" in ui
    assert "/api/profile/linkedin/resync" in ui
    assert "function uploadResume" not in ui
    assert ">Add</button>" not in ui
