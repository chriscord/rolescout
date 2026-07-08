---
name: prep-strategy
description: Build the overall application strategy for the user's FOCUSED positions — group them, analyze strengths/weaknesses, set application priorities, and lay out portfolio, resume-emphasis, LinkedIn, and same-company multi-position strategy in one document. Use whenever the user asks for an application strategy, "how should I approach these roles", wants focused openings grouped and prioritized, or after new positions are focus-registered.
---

# Prep: Application Strategy

Turn the user's **focused positions** into one coherent application strategy document. Work in the recruiting repo root; resolve the active project (`active-project.json`). Scope contract: operate ONLY on positions registered in `<project>/data/focused-jobs.json` — if it's empty or missing, stop and tell the user to register focus in the list UI (star toggle) or name positions explicitly. Never silently widen scope to the whole job_list.

## Inputs

`data/focused-jobs.json` (scope), `data/job_list.csv` rows + `targets/jobs/*.json` snapshots for those job_ids, `profiles/<person>/candidate-profile.md` + `evidence-map.md`, `strategy/job-scores.json`/`job-ratings.json` if present, and `references/prioritization-model.md`. For same-company multi-position questions, apply the audit approach from the `application-strategy` skill (web-verify the company's current recruiting process before recommending all-in vs staggered).

## Output — `strategy/prep-strategy.md` (and UI refresh)

One document, exactly these sections:

1. **Executive summary** — exactly one paragraph of ~3 sentences (max 4): what the focused set looks like, the recommended play, expected sequencing. No bullets, no sub-headings — this paragraph is surfaced verbatim at the top of the web UI's Strategy tab.
2. **Strengths / weaknesses vs this set** — evidence-backed (EV- refs): where the profile beats typical competition for these specific roles; honest gaps and how to mitigate or frame truthfully.
3. **Application priority** — ranked focused positions with one-line why each (fit, likelihood, timing, network); deviations from script-suggested priority go to `strategy/overrides.json`.
4. **Portfolio strategy** — what work artifacts/writing/talks to surface or create for this set; where they matter (which applications/interviews).
5. **Resume emphasis direction** — per group: which experiences/metrics to lead with, what to trim (direction only — `prep-resume` executes).
6. **LinkedIn direction** — positioning angle, headline direction, activity posture for this set (direction only — `prep-linkedin` executes).
7. **Same-company multi-position strategy** — for every company with 2+ focused positions: apply-all vs top-1 staggered vs referral-first, with the web-audited rationale and sequencing dates.

## Grouping & store write-back

**Reuse before regroup**: when `target-job-group-strategy` already assigned groups (job_group column / targets/job-groups/), start from those slugs and refine only where the focused subset demands it — forking parallel group taxonomies breaks resumes/linkedin folder continuity. This skill is authoritative for the FOCUSED set; whole-list triage stays with target-job-group-strategy.

Cluster the focused set into groups one positioning can serve (slug per group); write `job_group` back to job_list for focused rows (validate → `python scripts/upsert_rows.py job_list ...`) and create/update `targets/job-groups/<slug>.md` per the existing group-file template. Refresh `strategy/target-priorities.md` (the web UI's strategy view reads it) so the UI reflects this run. The UI's Strategy tab shows the `## Executive summary` section of `strategy/prep-strategy.md` as its lead paragraph — keep that section current on every rerun.

## Regeneration semantics

Focused set changes between runs. On rerun: reload `focused-jobs.json`, diff against the strategy doc's position list, and REGENERATE the affected sections (or the whole doc if grouping shifted) — stamp `> Focus set as of <date>, N positions` at the top. Never leave stale positions in the doc; note removed ones under a short "Dropped since last version" line.

## Boundaries

Strategy only — no applications submitted, nothing sent, no LinkedIn edits (local-only rules: `references/local-boundaries.md`). Suggest `prep-resume` / `prep-linkedin` / `prep-interview` as next steps per group.
