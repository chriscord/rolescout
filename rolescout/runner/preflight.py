"""Preflight readiness checks for live workflow runs (profile intake first!).

The workflow order matters (AGENTS.md: profile → research → … ). A live `run
search` without a candidate profile produces ungrounded garbage, so the CLI
checks prerequisites BEFORE spending the user's tokens/subscription:

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
        candidates += [Path(rel), repo_root() / rel, project / rel]
    for c in candidates:
        if c.is_dir():
            return c
    return None


def _has_profile(pdir: Path | None) -> tuple[bool, bool]:
    """(candidate-profile.md exists, evidence-map.md exists)"""
    if pdir is None:
        return False, False
    return ((pdir / "candidate-profile.md").exists(),
            (pdir / "evidence-map.md").exists())


def _has_materials(pdir: Path | None) -> bool:
    if pdir is None:
        return False
    return any(f.suffix.lower() in MATERIAL_SUFFIXES and f.name != "candidate-profile.md"
               and f.name != "evidence-map.md"
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
    # generated artifacts are not user materials: linkedin-current.md is the
    # runner's fresh capture handoff, rewritten on every prep-linkedin run
    generated = ("candidate-profile.md", "evidence-map.md", "linkedin-current.md")
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
    pdir = profile_dir(project)
    has_prof, has_ev = _has_profile(pdir)
    from ..profile_meta import linkedin_url
    has_linkedin = bool(linkedin_url(pdir))
    has_materials = _has_materials(pdir)
    has_source = has_materials or has_linkedin
    where = pdir if pdir is not None else f"{project}/profile (or project.json profile_dir)"

    if workflow == "prep":
        if not has_prof and not has_source:
            blocking.append(
                f"no candidate profile AND no source materials in {where} — drop your "
                "resume/notes (md/pdf/docx…) there or add a LinkedIn URL; the prep "
                "workflow's candidate-profile-builder will build the profile from them")
        elif not has_prof:
            source = "LinkedIn URL" if has_linkedin and not has_materials else "materials"
            warnings.append(f"no candidate-profile.md yet — prep will build it from your "
                            f"{source} before tailoring anything")
        if not has_linkedin:
            warnings.append("no LinkedIn source URL — the prep-linkedin step captures "
                            "fresh from the profile URL every run")
    else:
        if not has_prof:
            if workflow in ("search", "score"):
                warnings.append(
                    f"candidate-profile.md missing in {where} — proceeding with project "
                    "targets and available LinkedIn/source hints only; grouping and scoring "
                    "are provisional until `rolescout run prep` builds the evidence map")
            elif workflow in ("prep-resume", "prep-linkedin") and has_source:
                warnings.append(
                    f"candidate-profile.md missing in {where} — '{workflow}' may proceed "
                    "from the available LinkedIn/source material, but all candidate claims "
                    "must stay sourced or labeled as open questions")
            elif workflow == "apply":
                warnings.append(
                    f"candidate-profile.md missing in {where} — '{workflow}' may proceed "
                    "with local instructions/status work only; do not invent candidate facts")
            else:
                blocking.append(
                    f"candidate-profile.md missing in {where} — the '{workflow}' workflow needs "
                    "a truthful profile to ground decisions. Fix: add a resume/materials or "
                    "LinkedIn URL and run `rolescout run prep` (profile intake) first")
        elif not has_ev:
            warnings.append("evidence-map.md missing — claims can't be evidence-checked; "
                            "`rolescout run prep` builds it")

    stale = _stale_profile(pdir)
    if stale and has_prof:
        if workflow in ("prep", "prep-resume"):
            warnings.append(
                f"'{stale}' is newer than candidate-profile.md — the profile/evidence map "
                "are stale; this run's profile-builder step MUST rebuild them from the "
                "updated materials before tailoring (the runner verifies this after the run)")
        elif workflow in ("search", "score", "prep-strategy", "prep-linkedin",
                          "prep-interview"):
            # these chains cannot rebuild the profile — running them would
            # silently ground every output on the OLD resume (observed failure)
            blocking.append(
                f"'{stale}' is newer than candidate-profile.md — '{workflow}' cannot "
                "rebuild the profile and would ground on the OLD materials. Fix: run "
                "`rolescout run prep-resume` (or prep) first; its profile-builder step "
                "rebuilds candidate-profile.md + evidence-map.md from the new resume")
        else:
            warnings.append(
                f"'{stale}' is newer than candidate-profile.md — the profile/evidence map "
                f"are STALE; run `rolescout run prep` (or prep-resume) first so '{workflow}' "
                "grounds on the updated materials")

    if workflow in ("prep-strategy", "prep-resume", "prep-linkedin", "prep-interview"):
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
