---
name: target-job-group-strategy
description: Cluster researched jobs into target job groups, score fit, and prioritize what to pursue. Use whenever the user asks "what should I target/apply to", wants jobs ranked, grouped, compared, or prioritized, asks which of the saved roles fit best, or after job research produces enough job_list rows to warrant strategy — even if they don't use the word "group".
---

# Target Job Group Strategy

Turn raw `job_list` rows into a small set of coherent target job groups with honest fit analysis and clear priorities. **Boundary**: this skill is whole-list triage after research — its scores/priorities/groups power the list UI and help the user CHOOSE what to focus. Once positions are focus-registered, `prep-strategy` owns the focused-set application strategy and may refine group assignments; never overwrite its `strategy/prep-strategy.md`. A "job group" is a cluster of roles a single positioning (one resume variant, one LinkedIn angle) can credibly serve.

Work in the recruiting repo root; resolve the active search project via `active-project.json` per `references/project-structure.md` — `<project>` below means that directory, `<person>` the profile dir in its `project.json`. Inputs: `profiles/<person>/candidate-profile.md` and `profiles/<person>/evidence-map.md` when available, plus the UI-visible job list (`<project>/data/job_list.visible.csv` when present, otherwise `job_list.csv`; see `references/recruiting-sheet-schema.md` for backends and columns). If job rows are missing, route to `job-opening-research` first. If the profile is missing, still group and prioritize from project targets, LinkedIn/source hints, and JD evidence; mark `role_fit`, group rationale, and priorities as provisional, do not invent candidate evidence, and tell the user to run `prep` before resume/LinkedIn tailoring.

## Enrichment pre-pass (thin rows)

Manually added rows often carry only a URL. Before grouping/scoring, enrich every row missing `jd_summary`/`must_have_requirements`: fetch its posting URL (browser for JS pages), capture title/location/seniority/must-haves/nice-to-haves/summary, snapshot JD text to `targets/jobs/<job_id>.json`, and upsert. This is row enrichment, not discovery — the search workflow's five artifacts are not required for an enrich-and-score run. Scoring a row with no JD content is fabrication; if the URL is dead, record `posting_status` accordingly and score only on available evidence with the gap flagged in the rating rationale. When focused positions exist (`data/focused-jobs.json`), enrich and score those first.

## Clustering

Group by what changes the *positioning*, not surface titles: function, seniority band, domain, and the dominant must-have capabilities. Titles lie; requirements don't. Aim for 2–4 groups — more than that dilutes effort, and a group with fewer than ~3 roles is usually a stray unless it's strategically special. Split a group when its roles would need different resumes; merge groups when one resume serves both.

Give each group a slug (e.g. `platform-eng`, `ml-infra-lead`) — this exact slug goes in the `job_group` column and names folders in `resumes/` and `linkedin/`.

## Scoring

Two numbers, defined in `references/prioritization-model.md` — read it before scoring:

- `fit_score` 1–5: evidence-based must-have coverage only (5 = fully evidenced; 3 = one real gap; 1 = aspirational).
- `priority`: computed, not vibes. Rate each job 1–5 on every criterion in `<project>/strategy/scoring-config.json` (if missing, seed it from the model's defaults and **present the criteria/weights to the user for adds/removes/reweights** — it's their model). Write ratings + one-line rationales to `<project>/strategy/job-ratings.json`. In the RoleScout runner, stop there for deterministic math/store writes: the runner will call `scripts/score_jobs.py`, validate/upsert score updates, and rebuild the visible view. In a standalone/manual run outside the runner, run `python scripts/score_jobs.py <project>/strategy/job-ratings.json` yourself before writing priority back. Show the user the ranked table with rationale before writing back.

Note where you're uncertain rather than faking precision — a guessed 4 is worse than a "3–4, depends on whether EV-012 covers X". Label estimated ratings (comp, likelihood) as estimates.

**Overrides are logged, not silent.** When the final `priority` you write differs from the script's suggestion (e.g. demoting a high-scoring job because the posting is closed), append an entry to `<project>/strategy/overrides.json`: `[{"job_id": "...", "suggested": "high", "final": "low", "reason": "posting closed on careers page; mirror data unverified", "date": "YYYY-MM-DD"}]`. A prose note in the row is for humans; the override log is what lets QA verify that every deviation was deliberate.

**Even when the user only asks for a ranking**, persist the full artifact set below — a scored list without group files leaves the next skills (resume tailoring, LinkedIn) with nothing to target, which is how pipelines silently stall between sessions.

## Artifacts

Write `<project>/targets/job-groups/<slug>.md` for each group:

```markdown
# Target Group: <name> (<slug>)
## Roles in group        (job_id | company | title | fit_score | priority)
## Why this group        (fit rationale tied to evidence IDs)
## Ideal role shape
## Fit strengths         (with EV- refs)
## Gaps & concerns       (honest; how to mitigate or spin truthfully)
## Positioning angle     (the one-sentence pitch for this group)
## Next action
## Confidence            (what could change this assessment)
```

Write `<project>/strategy/target-priorities.md` ranking the groups with rationale and recommended sequencing.

## Updating the store

When running inside `rolescout run score` or the web UI, do not write `job_group`, `fit_score`, or `priority` back to `job_list` yourself and do not run `score_jobs.py`, `validate_job_rows.py`, or `upsert_rows.py`; the runner owns CSV/SQLite reads, compact batch construction, deterministic score math, validation, upsert, and visible-view rebuild. If the runner gives you an injected score batch, evaluate only that batch and return structured ratings rather than reading files. Keep batch output compact: no markdown/prose outside JSON, `reason` <= 180 characters, each criterion rationale <= 80 characters, and one rating object per input `job_id`. For a standalone/manual run outside the runner, write `job_group`, `fit_score`, and `priority` back to `job_list` by building a JSON list of partial rows (must include `job_id`, `captured_at`, `company`, `title`, `source_url` plus the updated fields) **as a file under `<project>/data/`** (e.g. `<project>/data/score-updates.json`) — never `/tmp`, `C:\tmp`, or an absolute OS path, which fail on Windows (FileNotFoundError/PermissionError). Then validate with `python scripts/validate_job_rows.py <project>/data/score-updates.json` and upsert (`python scripts/upsert_rows.py job_list <project>/data/score-updates.json` locally, or header-verified upsert via the Sheets connector — follow the write discipline in the schema reference). Delete the intermediate file after the upsert.

If the agent workspace is read-only or local shell/file writes are blocked, return a final JSON payload instead of failing:

```text
SCORE_OUTPUT_JSON:
{"schema":"rolescout-score-output-v1","job_ratings":[...],"job_groups":[{"slug":"...","markdown":"..."}],"target_priorities_md":"..."}
```

The RoleScout runner will materialize that payload into artifacts and run deterministic finalization.

## Done when

Every UI-visible job is either in a group/rating set or explicitly parked with a reason; each group file has all sections filled; the user has been shown the ranking and picked (or been asked to pick) which group to pursue first. Then suggest `prep-resume` for the chosen group.
