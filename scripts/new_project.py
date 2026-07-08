#!/usr/bin/env python3
"""Create or activate a search project (person x target industry focus).

Usage:
  python3 scripts/new_project.py --person ck --focus ai-infra   # create + activate
  python3 scripts/new_project.py --activate ck--ai-infra        # switch active project
  python3 scripts/new_project.py --list                         # list projects

Creates profiles/<person>/ if missing, scaffolds projects/<person>--<focus>/ with
project.json, seeds strategy/scoring-config.json from references/scoring-config.default.json,
initializes the project's SQLite store, and sets it active in active-project.json.
See references/project-structure.md for the model.
"""
import argparse
import json
import re
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SUBDIRS = ["data", "targets/jobs", "targets/job-groups", "strategy",
           "resumes", "linkedin", "applications", "interviews"]


def slug_ok(s):
    return bool(re.fullmatch(r"[a-z0-9][a-z0-9-]*", s))


def set_active(code):
    json.dump({"active": f"projects/{code}"},
              open(ROOT / "active-project.json", "w", encoding="utf-8"), indent=2)
    print(f"ACTIVE: projects/{code}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--person")
    ap.add_argument("--focus")
    ap.add_argument("--activate", metavar="CODE", help="switch to existing project <person>--<focus>")
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()

    if args.list:
        active = ""
        apf = ROOT / "active-project.json"
        if apf.exists():
            with open(apf, encoding="utf-8") as f:
                active = json.load(f).get("active", "")
        for p in sorted((ROOT / "projects").glob("*/project.json")):
            with open(p, encoding="utf-8") as f:
                meta = json.load(f)
            mark = " *active*" if f"projects/{p.parent.name}" == active else ""
            print(f"{p.parent.name}: person={meta['person']} focus={meta['focus']} "
                  f"status={meta.get('status','active')}{mark}")
        return 0

    if args.activate:
        proj = ROOT / "projects" / args.activate
        if not (proj / "project.json").exists():
            print(f"FAIL: {proj} does not exist. Existing projects:")
            main_args = sys.argv[:]  # noqa
            subprocess.run([sys.executable, __file__, "--list"])
            return 1
        set_active(args.activate)
        return 0

    if not (args.person and args.focus):
        ap.error("provide --person and --focus (or --activate / --list)")
    if not (slug_ok(args.person) and slug_ok(args.focus)):
        print("FAIL: person/focus must be lowercase slugs (a-z, 0-9, hyphen)")
        return 1

    profile_dir = ROOT / "profiles" / args.person
    profile_dir.mkdir(parents=True, exist_ok=True)

    code = f"{args.person}--{args.focus}"
    proj = ROOT / "projects" / code
    if (proj / "project.json").exists():
        print(f"NOTE: {code} already exists — activating it.")
        set_active(code)
        return 0

    for d in SUBDIRS:
        (proj / d).mkdir(parents=True, exist_ok=True)
    json.dump({
        "person": args.person,
        "focus": args.focus,
        "profile_dir": f"profiles/{args.person}",
        "created_at": date.today().isoformat(),
        "status": "active",
        "external_sheet": None,
    }, open(proj / "project.json", "w", encoding="utf-8"), indent=2)

    default_cfg = ROOT / "references" / "scoring-config.default.json"
    shutil.copy(default_cfg, proj / "strategy" / "scoring-config.json")

    set_active(code)
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "init_db.py")])
    if r.returncode != 0:
        return r.returncode
    print(f"CREATED: projects/{code} (profile: profiles/{args.person})")
    print("Reminder: present the default scoring criteria/weights to the user for tuning.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
