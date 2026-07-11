"""Preflight readiness checks for live workflow runs.

Profile intake is person-scoped. Search/score may proceed provisionally while
profile-intake is missing or running; focused prep workflows consume the
candidate profile/evidence map and never rebuild it themselves.

  blocking  — missing inputs the workflow cannot work without (refuse + guide)
  warning   — quality risks the user may knowingly accept (print + continue)

Mock runs skip preflight: the disposable fixture project is self-contained.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

from ..paths import repo_root

MATERIAL_SUFFIXES = {".md", ".txt", ".pdf", ".docx", ".doc", ".html"}


def profile_dir(project: Path) -> Path | None:
    """Resolve the project's profile dir (project.json `profile_dir`, prototype
    convention: relative to the repo root; fixtures may use project-local)."""
    pj = project / "project.json"
    rel = ""
    if pj.exists():
        try:
            rel = json.loads(pj.read_text(encoding="utf-8")).get("profile_dir", "") or ""
        except (json.JSONDecodeError, OSError):
            pass
    candidates = [project / "profile"]
    if rel:
        rel_path = Path(rel)
        if rel_path.is_absolute():
            candidates.append(rel_path)
        else:
            candidates += [repo_root() / rel_path, project / rel_path]
    for c in candidates:
        if c.is_dir():
            return c
    return None


def _looks_like_profile_dir(path: Path) -> bool:
    return path.is_dir() and (path / "profile-meta.json").exists()


def _has_profile(pdir: Path | None) -> tuple[bool, bool]:
    """(candidate-profile.md exists, evidence-map.md exists)"""
    if pdir is None:
        return False, False
    return ((pdir / "candidate-profile.md").exists(),
            (pdir / "evidence-map.md").exists())


def _has_materials(pdir: Path | None) -> bool:
    if pdir is None:
        return False
    generated = {
        "candidate-profile.md",
        "evidence-map.md",
        "linkedin-current.md",
        "linkedin-analysis.md",
        "story-bank.md",
        "story-bank.json",
    }
    return any(f.suffix.lower() in MATERIAL_SUFFIXES and f.name not in generated
               for f in pdir.iterdir() if f.is_file())


def _stale_profile(pdir: Path | None) -> str:
    """Name of the newest source material that is newer than candidate-profile.md
    ('' when the profile is fresh or absent). Replacing a resume file does not
    invalidate the generated profile/evidence map by itself — this is the
    deterministic staleness signal every profile-consuming workflow checks."""
    if pdir is None:
        return ""
    prof = pdir / "candidate-profile.md"
    if not prof.exists():
        return ""
    prof_m = prof.stat().st_mtime
    newest, name = prof_m, ""
    # generated artifacts are not user materials: these are rewritten by
    # profile/prep workflows and must not create a profile rebuild loop.
    generated = {
        "candidate-profile.md",
        "evidence-map.md",
        "linkedin-current.md",
        "linkedin-analysis.md",
        "story-bank.md",
        "story-bank.json",
    }
    for f in pdir.iterdir():
        if (f.is_file() and f.suffix.lower() in MATERIAL_SUFFIXES
                and f.name not in generated):
            m = f.stat().st_mtime
            if m > newest:
                newest, name = m, f.name
    return name


def _csv_rows(path: Path) -> int:
    if not path.exists():
        return 0
    with open(path, newline="", encoding="utf-8") as f:
        return sum(1 for _ in csv.DictReader(f))


def check(workflow: str, project: Path) -> tuple[list[str], list[str]]:
    """Returns (blocking, warnings) — human-readable, with the fix in the message."""
    blocking: list[str] = []
    warnings: list[str] = []
    pdir = project if workflow == "profile-intake" and _looks_like_profile_dir(project) else profile_dir(project)
    has_prof, has_ev = _has_profile(pdir)
    from ..profile_meta import linkedin_url
    has_linkedin = bool(linkedin_url(pdir))
    has_materials = _has_materials(pdir)
    has_source = has_materials or has_linkedin
    where = pdir if pdir is not None else f"{project}/profile (or project.json profile_dir)"

    if workflow == "profile-intake":
        if not has_source:
            blocking.append(
                f"no source materials in {where} — add a resume/material file or "
                "LinkedIn URL before profile-intake can build candidate-profile.md "
                "+ evidence-map.md")
        elif not has_prof:
            warnings.append("candidate-profile.md missing — profile-intake will build "
                            "candidate-profile.md + evidence-map.md from available "
                            "materials and will leave unsupported claims as open questions")
        elif not has_ev:
            warnings.append("evidence-map.md missing — profile-intake will rebuild it")
    else:
        if not has_prof:
            if workflow in ("search", "score"):
                warnings.append(
                    f"candidate-profile.md missing in {where} — proceeding with project "
                    "targets and available LinkedIn/source hints only; grouping and scoring "
                    "are provisional until profile-intake builds candidate-profile.md "
                    "+ evidence-map.md")
            elif workflow == "apply":
                warnings.append(
                    f"candidate-profile.md missing in {where} — '{workflow}' may proceed "
                    "with local instructions/status work only; do not invent candidate facts")
            else:
                blocking.append(
                    f"candidate-profile.md missing in {where} — the '{workflow}' workflow needs "
                    "a truthful profile to ground decisions. Fix: add a resume/materials or "
                    "LinkedIn URL and run `rolescout run profile-intake --person <person>` first")
        elif not has_ev:
            if workflow in ("search", "score", "apply"):
                warnings.append("evidence-map.md missing — claims can't be evidence-checked; "
                                "run profile-intake to rebuild it")
            else:
                blocking.append("evidence-map.md missing — focused prep requires an "
                                "evidence map. Run `rolescout run profile-intake "
                                "--person <person>` first")

    stale = _stale_profile(pdir)
    if stale and has_prof:
        if workflow == "profile-intake":
            warnings.append(
                f"'{stale}' is newer than candidate-profile.md — the profile/evidence map "
                "are stale; profile-intake must rebuild them from the updated materials")
        elif workflow in ("search", "score"):
            warnings.append(
                f"'{stale}' is newer than candidate-profile.md — proceeding provisionally "
                "from project targets and current job evidence; run profile-intake to "
                "refresh candidate-profile.md + evidence-map.md, then rerun score")
        elif workflow in ("prep", "prep-strategy", "prep-resume", "prep-linkedin",
                          "prep-interview", "story-bank"):
            blocking.append(
                f"'{stale}' is newer than candidate-profile.md — '{workflow}' cannot "
                "rebuild the profile and would ground on the OLD materials. Fix: run "
                "`rolescout run profile-intake --person <person>` first; profile-intake "
                "rebuilds candidate-profile.md + evidence-map.md from the new materials")
        else:
            warnings.append(
                f"'{stale}' is newer than candidate-profile.md — the profile/evidence map "
                f"are STALE; run profile-intake before relying on candidate facts")

    if workflow in ("prep", "prep-strategy", "prep-resume", "prep-linkedin", "prep-interview"):
        fj = project / "data" / "focused-jobs.json"
        n_focused = 0
        try:
            n_focused = len(json.loads(fj.read_text(encoding="utf-8")).get("job_ids", []))
        except (OSError, json.JSONDecodeError):
            pass
        if n_focused == 0:
            blocking.append(
                f"'{workflow}' operates on FOCUSED positions only, and none are registered — "
                "open the web UI list tab and star the positions to focus "
                "(or POST /api/project/<code>/job-focus), then rerun")

    if workflow == "prep-linkedin":
        if not has_linkedin:
            blocking.append(
                "prep-linkedin requires the candidate LinkedIn profile URL so it can "
                "capture the current LinkedIn profile fresh before generating review "
                "artifacts — set the URL in the web UI profile form or with "
                "`rolescout init --linkedin-url ...`")

    jobs = _csv_rows(project / "data" / "job_list.csv")
    if workflow == "apply" and jobs == 0:
        warnings.append(f"job_list is empty — '{workflow}' usually follows "
                        "`rolescout run search`; the agent will have no saved openings "
                        "to work from unless your --task names one explicitly")
    return blocking, warnings
