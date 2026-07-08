#!/usr/bin/env python3
"""Deterministic run grader — Layers 1 & 2 of references/evaluation-rubric.md.

Usage: python3 scripts/grade_run.py projects/<person>--<focus> [--report]

Grades a search project's current state: invariants (schema, scoring integrity,
override discipline) and trace presence/completeness (research log, grouping
artifacts, snapshots). Layer 3 (coverage vs dynamic reference, regression replay,
judgment rubric) is not part of the public runtime.

--report writes <project>/qa-reports/<date>-<project>-grade.{json,md}.
Exit 1 if any P0 finding.
"""
import argparse
import csv
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except AttributeError:
    pass


def load_csv(p):
    if not p.exists():
        return None
    with open(p, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _log_runs(raw):
    return raw if isinstance(raw, list) else [raw]


def _query_text(q) -> str:
    if isinstance(q, dict):
        return " ".join(str(q.get(k, "")) for k in ("scope", "q", "url"))
    return str(q)


def _candidate_text(c) -> str:
    if not isinstance(c, dict):
        return str(c)
    keys = ("url", "source_url", "job_page_url", "company", "title",
            "decision", "reason_code", "reason")
    return " ".join(str(c.get(k, "")) for k in keys)


def _mentions_linkedin_jobs(text: str) -> bool:
    lower = text.lower()
    return "linkedin" in lower and ("jobs" in lower or "/jobs/" in lower)


def _mentions_linkedin_auth_block(text: str) -> bool:
    lower = text.lower()
    return ("linkedin" in lower and
            any(term in lower for term in (
                "sign in", "signed out", "login", "authwall",
                "authentication", "verification")))


def latest_run_has_linkedin_jobs_coverage(runs: list[dict]) -> tuple[bool, bool]:
    """Return (has_jobs_pass, has_auth_block) for the latest research-log run."""
    if not runs:
        return False, False
    latest = runs[-1] if isinstance(runs[-1], dict) else {}
    queries = latest.get("queries", []) if isinstance(latest, dict) else []
    cands = latest.get("candidates", []) if isinstance(latest, dict) else []
    texts = [_query_text(q) for q in queries] + [_candidate_text(c) for c in cands]
    has_jobs_pass = any(_mentions_linkedin_jobs(t) for t in texts)
    has_auth_block = any(_mentions_linkedin_auth_block(t) for t in texts)
    return has_jobs_pass, has_auth_block


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("project")
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args()
    proj = (ROOT / args.project) if not Path(args.project).is_absolute() else Path(args.project)
    if not (proj / "project.json").exists():
        print(f"FAIL: {proj} is not a search project")
        return 1

    findings = []  # (severity, layer, check, detail)
    ok = []        # (layer, check, evidence)

    def add(sev, layer, check, detail):
        findings.append((sev, layer, check, detail))

    jl = load_csv(proj / "data" / "job_list.csv") or []
    tr = load_csv(proj / "data" / "tracker.csv") or []

    # ---- Layer 1: invariants ----
    if jl:
        _jlf = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                           encoding="utf-8")
        json.dump(jl, _jlf); _jlf.close()
        r = subprocess.run([sys.executable, str(ROOT / "scripts" / "validate_job_rows.py"),
                            _jlf.name], capture_output=True, text=True,
                           encoding="utf-8", errors="replace",
                           env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        if r.returncode == 0:
            ok.append(("L1", "job_list schema", f"{len(jl)} rows validator-clean"))
        else:
            add("P0", "L1", "job_list schema", r.stdout.strip()[:500])
        tracked = [row["job_id"] for row in jl
                   if any(t in row["source_url"] for t in ("utm_", "gh_src", "fbclid"))]
        if tracked:
            add("P1", "L1", "URL canonicalization", f"tracking params in {tracked}")
        else:
            ok.append(("L1", "URL canonicalization", "no tracking params"))
    else:
        add("P1", "L1", "job_list", "empty or missing")

    if tr:
        _trf = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                           encoding="utf-8")
        json.dump(tr, _trf); _trf.close()
        r = subprocess.run([sys.executable, str(ROOT / "scripts" / "validate_tracker_rows.py"),
                            _trf.name, "--job-list", str(proj / "data" / "job_list.csv")],
                           capture_output=True, text=True,
                           encoding="utf-8", errors="replace",
                           env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        if r.returncode == 0:
            ok.append(("L1", "tracker schema+linkage", f"{len(tr)} rows clean"))
        else:
            add("P0", "L1", "tracker schema+linkage", r.stdout.strip()[:500])
        na = [t["application_id"] for t in tr
              if t["status"] not in ("accepted", "rejected", "withdrawn") and not t["next_action"].strip()]
        if na:
            add("P1", "L1", "next_action discipline", f"active rows without next_action: {na}")

    # scoring integrity
    ratings_p = proj / "strategy" / "job-ratings.json"
    scores_p = proj / "strategy" / "job-scores.json"
    overrides_p = proj / "strategy" / "overrides.json"
    prioritized = [r_ for r_ in jl if r_.get("priority", "").strip()]
    if prioritized:
        if not ratings_p.exists():
            add("P0", "L1", "scoring integrity", "priorities written but no job-ratings.json")
        else:
            with open(ratings_p, encoding="utf-8") as f:
                ratings = {e.get("job_id"): e for e in json.load(f)}
            unrated = [r_["job_id"] for r_ in prioritized if r_["job_id"] not in ratings]
            if unrated:
                add("P1", "L1", "scoring coverage", f"{len(unrated)} prioritized rows unrated: {unrated[:3]}...")
            else:
                ok.append(("L1", "scoring coverage", f"{len(prioritized)} prioritized rows all rated"))
            no_rat = [j for j, e in ratings.items() if not e.get("rationale")]
            if no_rat:
                add("P1", "L1", "rating rationale", f"{len(no_rat)} entries without rationale")
            else:
                ok.append(("L1", "rating rationale", "all rating entries carry rationale"))
        if scores_p.exists():
            with open(scores_p, encoding="utf-8") as f:
                scores = {s["job_id"]: s for s in json.load(f)}
            overrides = {}
            if overrides_p.exists():
                with open(overrides_p, encoding="utf-8") as f:
                    overrides = {o["job_id"]: o for o in json.load(f)}
            unlogged = []
            for r_ in prioritized:
                s = scores.get(r_["job_id"])
                if s and s["suggested_priority"] != r_["priority"] and r_["job_id"] not in overrides:
                    unlogged.append((r_["job_id"][:40], s["suggested_priority"], r_["priority"]))
            if unlogged:
                add("P1", "L1", "override discipline",
                    f"{len(unlogged)} priority deviations without overrides.json entry: {unlogged[:5]}")
            else:
                ok.append(("L1", "override discipline", "all deviations logged (or none)"))

    # ---- Layer 2: trace presence/completeness ----
    rl_p = proj / "targets" / "research-log.json"
    if not rl_p.exists():
        add("P1", "L2", "research log", "targets/research-log.json missing — coverage not auditable")
    else:
        try:
            with open(rl_p, encoding="utf-8") as f:
                rl = json.load(f)
            runs = _log_runs(rl)
            cands = [c for run in runs for c in run.get("candidates", [])]
            from schema_defs import RESEARCH_DECISIONS
            bad = [c for c in cands if c.get("decision") not in RESEARCH_DECISIONS
                   or (c.get("decision") != "kept" and not c.get("reason"))]
            kept_ids = {c.get("job_id") for c in cands if c.get("decision") == "kept"}
            orphans = [r_["job_id"] for r_ in jl if r_["job_id"] not in kept_ids]
            if bad:
                add("P1", "L2", "research log completeness", f"{len(bad)} candidates missing decision/reason")
            if len(cands) < len(jl):
                add("P1", "L2", "research log completeness",
                    f"log has {len(cands)} candidates < {len(jl)} saved rows")
            if orphans and cands:
                add("P2", "L2", "research log linkage", f"{len(orphans)} saved rows not in log (pre-log runs?)")
            if not bad and len(cands) >= len(jl):
                ok.append(("L2", "research log", f"{len(cands)} candidates, all with decisions+reasons"))

            has_linkedin_jobs, has_linkedin_auth_block = latest_run_has_linkedin_jobs_coverage(runs)
            if not has_linkedin_jobs:
                add("P0", "L2", "LinkedIn Jobs coverage",
                    "latest research-log run has no LinkedIn Jobs query/candidate; "
                    "search runs must use LinkedIn Jobs or stop for login/browser setup")
            elif has_linkedin_auth_block:
                add("P0", "L2", "LinkedIn login gate",
                    "latest research-log run appears to hit a LinkedIn login/authwall; "
                    "the run should stop with APPROVAL_REQUIRED for user login")
            else:
                ok.append(("L2", "LinkedIn Jobs coverage",
                           "latest run includes a LinkedIn Jobs query/candidate"))
        except (json.JSONDecodeError, KeyError) as e:
            add("P1", "L2", "research log", f"unparseable: {e}")

    if prioritized:
        grouped = [r_ for r_ in jl if r_.get("job_group", "").strip()]
        gfiles = list((proj / "targets" / "job-groups").glob("*.md"))
        if not grouped and not gfiles:
            add("P1", "L2", "grouping artifacts",
                "scoring done but no job_group values and no group files — downstream skills have no target")
        else:
            slugs = {r_["job_group"] for r_ in grouped}
            missing = [s for s in slugs if not (proj / "targets" / "job-groups" / f"{s}.md").exists()]
            if missing:
                add("P1", "L2", "grouping artifacts", f"job_group slugs without group files: {missing}")
            else:
                ok.append(("L2", "grouping artifacts", f"{len(gfiles)} group files, slugs consistent"))
        if not (proj / "strategy" / "target-priorities.md").exists():
            add("P2", "L2", "strategy file", "strategy/target-priorities.md missing")

    snaps = list((proj / "targets" / "jobs").glob("*.json")) if (proj / "targets" / "jobs").exists() else []
    if jl and len(snaps) == 0:
        add("P2", "L2", "JD snapshots", "no snapshots in targets/jobs/ — requirements lost if postings die")
    elif jl:
        ok.append(("L2", "JD snapshots", f"{len(snaps)}/{len(jl)} rows snapshotted"))

    # search-workflow artifacts (references/search-workflow.md) — mechanical layer
    tdir = proj / "targets"
    artifact_names = ["opportunity-thesis.md", "company-universe.json",
                      "source-plan.json", "research-log.json", "coverage-audit.md"]
    missing_art = [n for n in artifact_names if not (tdir / n).exists()]
    if jl and missing_art:
        add("P1", "L2", "search artifacts",
            f"missing {missing_art} — discovery not auditable (see search-workflow.md)")
    elif jl:
        r = subprocess.run([sys.executable, str(ROOT / "scripts" / "validate_research_artifacts.py"),
                            str(proj)], capture_output=True, text=True,
                           encoding="utf-8", errors="replace",
                           env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        if r.returncode == 0:
            ok.append(("L2", "search artifacts", "validate_research_artifacts PASS"))
        else:
            add("P0", "L2", "search artifacts", r.stdout.strip()[-400:])

    # ---- report ----
    # run-state classification: a run that stopped before producing artifacts must
    # never read as a plain PASS in runner/telemetry summaries.
    artifacts_present = sum(1 for n in ["opportunity-thesis.md", "company-universe.json",
                                        "source-plan.json", "research-log.json",
                                        "coverage-audit.md"] if (proj / "targets" / n).exists())
    audit_p = proj / "targets" / "coverage-audit.md"
    pending_approval = (audit_p.exists() and
                        "approval_required" in audit_p.read_text(
                            encoding="utf-8").lower().replace(" ", "_"))
    if artifacts_present == 0 and not jl:
        run_state = "INCOMPLETE (no search artifacts, no rows — run stopped before Phase 1)"
    elif pending_approval:
        run_state = "AWAITING_APPROVAL (partial run persisted; LinkedIn pass pending per coverage-audit)"
    elif artifacts_present < 5:
        run_state = f"PARTIAL ({artifacts_present}/5 artifacts)"
    else:
        run_state = "COMPLETE (5/5 artifacts)"

    p0 = [f for f in findings if f[0] == "P0"]
    print(f"\n=== grade_run: {proj.name} ({date.today().isoformat()}) ===")
    print(f"run_state: {run_state}")
    print(f"rows: job_list={len(jl)}, tracker={len(tr)} | PASS checks: {len(ok)} | findings: "
          f"{len(p0)} P0, {sum(1 for f in findings if f[0]=='P1')} P1, "
          f"{sum(1 for f in findings if f[0]=='P2')} P2")
    for layer, check, ev in ok:
        print(f"  PASS [{layer}] {check}: {ev}")
    for sev, layer, check, detail in sorted(findings):
        print(f"  {sev}  [{layer}] {check}: {detail}")
    print("\nNOTE: Layer 3 (coverage vs dynamic reference, regression replay, judgment rubric)"
          "\nis not part of the public runtime — this script grades Layers 1-2 only.")

    if args.report:
        outdir = proj / "qa-reports"
        outdir.mkdir(parents=True, exist_ok=True)
        base = f"{date.today().isoformat()}-{proj.name}-grade"
        with open(outdir / f"{base}.json", "w", encoding="utf-8") as f:
            json.dump({"project": proj.name, "date": date.today().isoformat(),
                       "run_state": run_state,
                       "pass": [{"layer": l, "check": c, "evidence": e} for l, c, e in ok],
                       "findings": [{"severity": s, "layer": l, "check": c, "detail": d}
                                    for s, l, c, d in findings]},
                      f, indent=2)
        print(f"report: {outdir / (base + '.json')}")
    return 1 if p0 else 0


if __name__ == "__main__":
    sys.exit(main())
