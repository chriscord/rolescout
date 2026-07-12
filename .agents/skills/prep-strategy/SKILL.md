---
name: prep-strategy
description: Build the overall application strategy for the user's FOCUSED positions — group them, analyze strengths/weaknesses, set application priorities, and lay out portfolio, resume-emphasis, LinkedIn, and same-company multi-position strategy in one document. Use whenever the user asks for an application strategy, "how should I approach these roles", wants focused openings grouped and prioritized, or after new positions are focus-registered.
---

# Prep: Application Strategy

Turn the user's **focused positions** into one coherent application strategy document. Work in the recruiting repo root; resolve the active project (`active-project.json`). Scope contract: operate ONLY on positions registered in `<project>/data/focused-jobs.json` — if it's empty or missing, stop and tell the user to register focus in the list UI (star toggle) or name positions explicitly. Never silently widen scope to the whole job_list. When `strategy/score-freshness.json` exists, the runner further limits strategy scope to the intersection of focused positions and `current_job_ids`; exclude unresolved, unscored, and stale positions, report the excluded count, and block when that intersection is empty.

Follow the shared publish and quality contract in `references/prep-quality-contract.md`.

## Inputs

`data/focused-jobs.json` (scope), focused rows from the public SQLite repository + `targets/jobs/*.json` snapshots for those job_ids, the runner-minimized contents of the canonical `profiles/<person>/candidate-profile.md`, `profiles/<person>/evidence-map.md`, and `profiles/<person>/decision-policy.json`, `strategy/job-scores.json`/`job-ratings.json` if present, and `references/prioritization-model.md`. Do not search for alternate profile/policy files. Apply every decision-policy constraint before prioritizing. For same-company multi-position questions, apply the audit approach from the `application-strategy` skill (web-verify the company's current recruiting process before recommending all-in vs staggered).

## Output — `strategy/prep-strategy.md` (and UI refresh)

One document, exactly these sections:

1. **Executive summary** — synthesize what the focused set looks like, the candidate's strongest advantages and material gaps, the recommended play, and expected sequencing. It is surfaced verbatim at the top of the web UI's Strategy tab. Use the prose shape that communicates this clearly; there is no paragraph-count or sentence-count requirement.
2. **Strengths / weaknesses vs this set** — evidence-backed (EV- refs): where the profile beats typical competition for these specific roles; honest gaps and how to mitigate or frame truthfully.
3. **Application priority** — ranked focused positions with one-line why each (fit, likelihood, timing, network); deviations from script-suggested priority go to `strategy/overrides.json`.
4. **Portfolio strategy** — what work artifacts/writing/talks to surface or create for this set; where they matter (which applications/interviews).
5. **Resume emphasis direction** — per group: which experiences/metrics to lead with, what to trim (direction only — `prep-resume` executes).
6. **LinkedIn direction** — positioning angle, headline direction, activity posture for this set (direction only — `prep-linkedin` executes).
7. **Same-company multi-position strategy** — for every company with 2+ focused positions: apply-all vs top-1 staggered vs referral-first, with the web-audited rationale and sequencing dates.

## Grouping & store write-back

**Reuse before regroup**: when `target-job-group-strategy` already assigned groups (job_group column / targets/job-groups/), treat those slugs as hints and preserve a slug when the positioning remains coherent. Do not mechanically preserve a fragmented one-role-per-group taxonomy: consolidate roles whenever one truthful positioning and resume variant can serve them. For a focused set of roughly 15 mixed roles, 3-6 groups is normally enough. Keep a singleton only when its positioning is genuinely incompatible with every other role, and explain that incompatibility in its group file. This skill is authoritative for the FOCUSED set; whole-list triage stays with target-job-group-strategy.

Cluster the focused set into groups one positioning can serve (slug per group); write `job_group` back to job_list for focused rows (validate → `python scripts/upsert_rows.py job_list ...`) and create/update `targets/job-groups/<slug>.md` per the existing group-file template. Refresh `strategy/target-priorities.md` (the web UI's strategy view reads it) so the UI reflects this run. The UI's Strategy tab shows the `## Executive summary` section of `strategy/prep-strategy.md` as its lead paragraph — keep that section current on every rerun.

`strategy/group-assignments.json` is also the machine-readable downstream scope contract. Every
assignment must include `disposition` (`pursue`, `conditional`, or `parked`) and a concise
`disposition_reason`. The values must match the narrative recommendation: aggregate `prep`
generates resume/LinkedIn material only for pursue or conditional groups, and interview packs only
for pursue roles. A parked role remains focused and visible; it is not silently deleted.

## Regeneration semantics

Focused set changes between runs. On rerun: reload `focused-jobs.json`, diff against the strategy doc's position list, and REGENERATE the affected sections (or the whole doc if grouping shifted) — stamp `> Focus set as of <date>, N positions` at the top. Never leave stale positions in the doc; note removed ones under a short "Dropped since last version" line.

## Boundaries

Strategy only — no applications submitted, nothing sent, no LinkedIn edits (local-only rules: `references/local-boundaries.md`). Suggest `prep-resume` / `prep-linkedin` / `prep-interview` as next steps per group.

Public, read-only web research is allowed and required for the same-company multi-position audit. Cite the URLs used. Never authenticate, submit forms, or disclose candidate-private context to a website.
