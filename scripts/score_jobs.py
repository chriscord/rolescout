#!/usr/bin/env python3
"""Compute weighted priority scores for jobs from per-criterion ratings.

Usage:
  python3 scripts/score_jobs.py strategy/job-ratings.json [--config strategy/scoring-config.json]

ratings JSON: [{"job_id": "...", "ratings": {"role_fit": 4, ...}, "rationale": {"role_fit": "..."}}, ...]
Every criterion in the config must be rated (1-5 integers). Weights must sum to 100.

Output: ranked table + strategy/job-scores.json with {job_id, score, suggested_priority,
dealbreaker_hit}. A rating of 1 on any dealbreaker criterion forces priority "low".
Exit 1 on any validation error — fix inputs rather than hand-computing scores.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import store_io

ROOT = store_io.ROOT


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("ratings_json")
    ap.add_argument("--config", default=None,
                    help="defaults to <active project>/strategy/scoring-config.json")
    args = ap.parse_args()
    proj = store_io.project_dir()
    if args.config is None:
        args.config = proj / "strategy" / "scoring-config.json"
        if not Path(args.config).exists():
            sys.exit(f"FAIL: {args.config} missing — seed it from "
                     "references/scoring-config.default.json (present criteria/weights "
                     "to the user first).")

    with open(args.config, encoding="utf-8") as f:
        cfg = json.load(f)
    criteria = {c["name"]: c["weight"] for c in cfg["criteria"]}
    total_w = sum(criteria.values())
    if total_w != 100:
        print(f"FAIL: weights sum to {total_w}, must be 100. Rescale strategy/scoring-config.json.")
        return 1
    thresholds = cfg.get("priority_thresholds", {"high": 70, "medium": 50})
    dealbreakers = set(cfg.get("dealbreaker_criteria", []))

    with open(args.ratings_json, encoding="utf-8") as f:
        entries = json.load(f)
    errors, results = [], []
    for i, e in enumerate(entries):
        jid = e.get("job_id", f"<row {i}>")
        ratings = e.get("ratings", {})
        missing = set(criteria) - set(ratings)
        extra = set(ratings) - set(criteria)
        if missing:
            errors.append(f"{jid}: missing ratings for {sorted(missing)}")
        if extra:
            errors.append(f"{jid}: unknown criteria {sorted(extra)} (add to config first)")
        bad = {k: v for k, v in ratings.items()
               if not isinstance(v, int) or not 1 <= v <= 5}
        if bad:
            errors.append(f"{jid}: ratings must be integers 1-5, got {bad}")
        if missing or extra or bad:
            continue
        score = round(sum(criteria[k] * v for k, v in ratings.items()) / 5, 1)
        hit = sorted(k for k in dealbreakers if ratings.get(k) == 1)
        if hit:
            prio = "low"
        elif score >= thresholds["high"]:
            prio = "high"
        elif score >= thresholds["medium"]:
            prio = "medium"
        else:
            prio = "low"
        results.append({"job_id": jid, "score": score, "suggested_priority": prio,
                        "dealbreaker_hit": hit})

    if errors:
        print(f"FAIL: {len(errors)} error(s)")
        for e in errors:
            print(f"  - {e}")
        return 1

    results.sort(key=lambda r: -r["score"])
    out = proj / "strategy" / "job-scores.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)
    width = max((len(r["job_id"]) for r in results), default=10)
    print(f"{'job_id':<{width}}  score  priority  dealbreaker")
    for r in results:
        db = ",".join(r["dealbreaker_hit"]) or "-"
        print(f"{r['job_id']:<{width}}  {r['score']:>5}  {r['suggested_priority']:<8}  {db}")
    # Projects may live outside the repo (RECRUITING_PROJECT_DIR) — don't crash on that.
    try:
        shown = out.relative_to(ROOT)
    except ValueError:
        shown = out
    print(f"\nOK: {len(results)} job(s) scored -> {shown}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
