# Prioritization Model

How jobs in `job_list` get scored and prioritized. The model is explicit and user-tunable: criteria and weights live in `<project>/strategy/scoring-config.json`; the math lives in `scripts/score_jobs.py`. The agent rates, the script computes — never hand-compute weighted scores. In the RoleNavi runner, the script/upsert step is runner-owned so agent sandbox or approval settings cannot silently prevent score propagation.

## Two numbers, different meanings

- **`fit_score` (1–5)**: evidence-based must-have coverage only. "Could the candidate credibly do this job?" Set during grouping from the evidence map.
- **`priority` (high/medium/low)**: "Should we spend effort on this now?" Derived from the weighted score below. Fit is one input among several — a fit-5 job can be low priority (posting stale, terrible comp) and a fit-3 job high priority (referral available, perfect timing).

## Default criteria and weights

Defined in `<project>/strategy/scoring-config.json` (weights sum to 100). Defaults:

| Criterion | Weight | Rating guide (1–5) |
|---|---|---|
| `role_fit` | 25 | Must-have coverage from evidence map; mirror fit_score |
| `comp_potential` | 15 | Expected comp vs user's target range (estimate → label as estimate) |
| `company_quality` | 15 | Stability, trajectory, engineering reputation, user's stated interest |
| `location_remote` | 10 | Compatibility with user's location/remote constraints (1 = violates) |
| `growth_path` | 10 | Career trajectory value: scope growth, learning, brand |
| `likelihood` | 10 | Realistic odds: seniority match, posting age, competition, market |
| `network_access` | 5 | Referral or warm contact available |
| `interview_cost` | 5 | Inverted effort: 5 = light process, 1 = months-long gauntlet |
| `timing` | 5 | Urgency fit: posting freshness vs user's timeline |

Weighted score = Σ(weight × rating) / 5 → 0–100. Priority mapping: ≥ 70 high, 50–69 medium, < 50 low. A rating of 1 on `location_remote` or any user-declared dealbreaker forces priority `low` (or park) regardless of score.

Before semantic ratings, the runner normalizes each JD into requirement atoms cached at
`<project>/targets/requirements/<job_id>.json`. Each atom records category, obligation
(`minimum_required`, `required`, `preferred`, or `responsibility`), and importance
(`central`, `supporting`, or `eligibility`). The model must evaluate every injected atom
against the capability ledger and evidence map, separating direct tenure from adjacent
exposure. Deterministic post-processing caps both `role_fit` and `likelihood` at 2 when a
central minimum or eligibility atom is unmet, and at 3 when it is unknown. Preferred atoms
can distinguish otherwise similar jobs but never trigger a hard minimum cap.

All explicit minimum/required atoms are retained; there is no fixed eight-item truncation.
The runner batches by total requirement count, asks the model only for semantic criteria,
and derives `minimum_requirement` and `essential_qualification` itself. A failed row is
repaired alone with exact expected IDs. Valid current rows commit atomically while unresolved
rows retain their previous database score and are recorded as stale. Dependency fingerprints
over the JD, profile/evidence ledger, policy, criteria, and contract allow later runs to
evaluate only new or changed rows.

Each completed batch is durably checkpointed by the single runner coordinator in
`<project>/strategy/score-staging.db`. Validated payloads and validation failures are written
in one SQLite transaction. A compatible rerun resumes those rows instead of calling the
model again. The canonical `job-ratings.json` and job store are promoted only after the run
has exhausted initial batches and one-row repairs.

## Workflow

1. Read `<project>/strategy/scoring-config.json`. If missing, seed it from `references/scoring-config.default.json` (new_project.py does this automatically) and **show the user the criteria/weights, inviting adds/removes/reweights** — the model belongs to the user; these defaults are just a starting proposal.
2. Rate each job 1–5 per criterion with a one-line rationale. Distinguish evidence-based ratings from estimates.
3. Write ratings to `<project>/strategy/job-ratings.json`. In the RoleNavi runner, `scripts/finalize_score.py` then runs `python scripts/score_jobs.py <project>/strategy/job-ratings.json`, validates criteria names/ratings/weight sum, writes computed scores, upserts `fit_score`/`priority`/`job_group` back to `job_list`, and rebuilds the visible view. In a standalone/manual run, run those deterministic scripts yourself.
4. Show the user the ranked table with per-criterion rationale before treating the score run as complete.

## Priority overrides

If the written `priority` deviates from the script's suggestion (closed posting, user veto, information the criteria don't capture), log it in `<project>/strategy/overrides.json` with job_id, suggested, final, reason, date. QA treats an unlogged deviation as a defect.

## User overrides (criteria/weights)

When the user adds/removes criteria or changes weights: update `<project>/strategy/scoring-config.json` (weights must re-sum to 100 — rescale and confirm), re-run scoring, and show what moved and why. Keep a dated note of config changes in the file's `changelog` field.
