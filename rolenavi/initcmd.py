"""`rolenavi init` — person/project intake wrapping scripts/new_project.py.

The prototype script owns project scaffolding; this wrapper owns INTAKE:
  person  (must) person code + display name; (opt) linkedin url, resume files
          (drop into profiles/<person>/ — the web UI uploads them for you),
          (opt) standing instructions for all agent runs
  project (must) target locations; (opt) focus role, level, company seeds,
          comp range, negatives
All values live in profile-meta.json / project-meta.json, are injected into
every live run, and can be updated any time (re-run init with flags, or web UI).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from . import core, profile_meta, project_meta
from .paths import RoleNaviError, repo_root

_SLUG = r"[a-z0-9][a-z0-9-]*"


def _passthrough(*script_args: str) -> int:
    r = core.run_script("new_project", *script_args, capture=False)
    return r.returncode


def _save_person_meta(person: str, args) -> None:
    pdir = repo_root() / "profiles" / person
    fields: dict = {}
    if getattr(args, "name", None):
        fields["name"] = args.name.strip()
    if getattr(args, "instructions", None):
        fields["instructions"] = args.instructions.strip()
    url = getattr(args, "linkedin_url", None)
    if url:
        try:
            fields["linkedin_url"] = profile_meta.normalize_linkedin_url(url)
        except ValueError as e:
            raise RoleNaviError(str(e)) from e
    if fields:
        p = profile_meta.save(pdir, **fields)
        print(f"profile meta saved -> {p.relative_to(repo_root())}")


def _save_project_meta(person: str, focus: str, args) -> Path | None:
    proj = repo_root() / "projects" / f"{person}--{focus}"
    if not (proj / "project.json").exists():
        return None
    fields = {"target_locations": getattr(args, "locations", None),
              "focus_role": getattr(args, "role", None),
              "target_level": getattr(args, "level", None),
              "target_companies": getattr(args, "companies", None),
              "comp_range": getattr(args, "comp_range", None),
              "negatives": getattr(args, "negatives", None)}
    changed = False
    if any(v is not None for v in fields.values()):
        before = project_meta.preference_fingerprint(proj)
        project_meta.update(proj, **fields)
        changed = project_meta.preference_fingerprint(proj) != before
        print(f"project targets saved -> projects/{person}--{focus}/project-meta.json")
    meta = project_meta.load(proj)
    if not meta["target_locations"]:
        print("WARN: no target locations declared — searches will be ungrounded; "
              "set with `rolenavi init --person … --focus … --locations 'Seoul, Remote-KR'`")
    return proj if changed or not project_meta.universe_status(proj)["ready"] else None


def _build_universe(project: Path | None) -> int:
    if project is None:
        return 0
    from .runner import workflows
    print("building target employer universe from current project preferences...")
    rec = workflows.run_workflow("opportunity-plan", project=project)
    status = str(rec.get("status", "failed"))
    if status != "ok" or not project_meta.universe_status(project)["ready"]:
        print(f"FAIL: target employer universe update ended {status}", file=sys.stderr)
        return 1
    print("target employer universe ready")
    return 0


def main(args) -> int:
    if args.list:
        return _passthrough("--list")
    if args.activate:
        return _passthrough("--activate", args.activate)

    if args.person and args.focus:
        rc = _passthrough("--person", args.person, "--focus", args.focus)
        if rc == 0:
            _save_person_meta(args.person, args)
            project = _save_project_meta(args.person, args.focus, args)
            if _build_universe(project) != 0:
                return 1
        return rc
    if args.person:  # profile-meta-only update (no project touched)
        _save_person_meta(args.person, args)
        return 0

    # ---- interactive wizard ----
    if not sys.stdin.isatty():
        print("usage: rolenavi init --person <slug> --focus <slug> --locations '…' "
              "[--name --linkedin-url --instructions --role --level --companies "
              "--comp-range --negatives] | --activate CODE | --list\n"
              "(interactive wizard needs a TTY)", file=sys.stderr)
        return 1

    root = repo_root()
    print("RoleNavi setup — a *person* holds the profile; a *project* is one search\n"
          "focus (own store, targets, resumes, strategy). Existing projects:")
    _passthrough("--list")

    print("\n[1/2] Person")
    person = input("  person code (must; lowercase slug, e.g. initials): ").strip()
    if not re.fullmatch(_SLUG, person or ""):
        print("FAIL: person must be a lowercase slug (a-z, 0-9, hyphen)")
        return 1
    name = input("  display name (must, used on documents): ").strip()
    while not name:
        name = input("  display name is required: ").strip()
    linkedin = ""
    while not linkedin:
        raw = input("  LinkedIn URL (optional, Enter to skip): ").strip()
        if not raw:
            break
        try:
            linkedin = profile_meta.normalize_linkedin_url(raw)
        except ValueError as e:
            print(f"    {e} — try again or Enter to skip")
    instructions = input("  standing instructions (optional, one line; e.g. tone, "
                         "priorities — editable later in the web UI): ").strip()
    print(f"  resume files: copy them into profiles/{person}/ "
          "(pdf/docx/md; or upload via `rolenavi web` → Profile)")

    print("\n[2/2] Project")
    focus = input("  search focus (must; lowercase slug, e.g. ai-product): ").strip()
    if not re.fullmatch(_SLUG, focus or ""):
        print("FAIL: focus must be a lowercase slug")
        return 1
    locations = input("  target locations (MUST; comma-separated, e.g. "
                      "'Seoul, Remote-KR, Singapore'): ").strip()
    while not locations:
        locations = input("  target locations are required — they ground every search: ").strip()
    role = input("  focus role (optional, e.g. 'AI product manager'): ").strip()
    level = input("  target level (optional, e.g. senior/staff/director): ").strip()
    companies = input("  target companies (optional seeds, comma-separated — similar "
                      "companies get explored too): ").strip()
    comp = input("  target comp range (optional search preference; model-allowed): ").strip()
    negatives = input("  excludes (optional, comma-separated companies/titles/industries): ").strip()

    print(f"\nAbout to create/activate projects/{person}--{focus}"
          f"\n  profile: profiles/{person}/ ({name})"
          f"\n  targets: {locations}"
          + (f" · {role}" if role else "") + (f" · {level}" if level else "")
          + f"\n  repo: {root}")
    if input("proceed? [y/N] ").strip().lower() not in ("y", "yes"):
        print("aborted — nothing created")
        return 1

    rc = _passthrough("--person", person, "--focus", focus)
    if rc != 0:
        return rc

    class _A:  # adapt wizard answers to the flag-shaped savers
        pass
    a = _A()
    a.name, a.linkedin_url, a.instructions = name, linkedin, instructions
    a.locations, a.role, a.level = locations, role, level
    a.companies, a.comp_range, a.negatives = companies, comp, negatives
    _save_person_meta(person, a)
    project = _save_project_meta(person, focus, a)
    if _build_universe(project) != 0:
        return 1
    print(f"\nNext: put your resume in profiles/{person}/ then "
          "`rolenavi run prep` (profile intake) → `rolenavi run search` — "
          "or do it all from `rolenavi web`.")
    return 0
