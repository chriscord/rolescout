---
name: candidate-profile-builder
description: Build a truthful, evidence-backed candidate profile and evidence map from a resume, LinkedIn content, portfolio, and notes. Use this whenever the user shares career materials, asks to start a job search, asks "what am I qualified for", wants their background analyzed, or before any resume tailoring, job research, or LinkedIn work when no profile exists yet OR the source materials have changed since the profile was built — even if they don't say "profile".
---

# Candidate Profile Builder

Convert the user's raw career materials into two artifacts that every downstream recruiting skill depends on:

- `profiles/<person>/candidate-profile.md` — stable background, strengths, constraints, positioning facts
- `profiles/<person>/evidence-map.md` — every claim mapped to its source

Work in the recruiting repo root (the folder containing `references/` and `scripts/`); if you can't find it, ask the user. This skill is **person-scoped**: `<person>` is the profile directory under `profiles/`. It must work even when no search project exists yet. Do **not** create a project just to build a profile.

## Why this matters

Downstream skills (resume tailoring, LinkedIn positioning, interview prep) are forbidden from inventing facts. They can only use what the profile and evidence map contain. A thin or unsourced profile forces them to either stall or fabricate — so build this carefully and honestly. Read `references/local-boundaries.md` for the truthfulness and data-sensitivity rules; they govern everything here.

## Person vs project

The profile is **person-scoped and shared**: one person (folder code like `ck` under `profiles/`) may run several search projects with different industry focuses, and all of them read this same profile. So keep industry-specific positioning OUT of the profile — it belongs in each project's `targets/`, `strategy/`, and resume variants. If the person has no folder yet, propose a short code (e.g. initials) and confirm it; raw source files the user dropped into `profiles/<person>/` are your inputs. When a fact is corrected in any session, fix it here so every project inherits the correction.

## Process

1. **Gather inputs.** Resume (any format — extract text), LinkedIn profile export/paste/current-source handoff, portfolio links, notes, raw files already in `profiles/<person>/`, and the conversation itself. A resume is helpful but not required: if LinkedIn current content is the only source, build the profile from `linkedin-current.md`; leave unavailable facts as Open Questions. A bare LinkedIn URL is only a pointer, not evidence. Never create a fictional resume or infer missing employers, dates, metrics, immigration facts, compensation facts, or credentials. Do not log in to LinkedIn, automate LinkedIn browsing, or edit anything.
2. **Extract and normalize.** Employers, titles, date ranges (normalize to `YYYY-MM`), locations, education, skills, metrics, links. Flag gaps and overlaps in the timeline rather than smoothing them over.
3. **Interview for the gaps.** Ask the user the *smallest* set of questions that unblocks downstream work — typically: target roles/geography, remote preference, compensation range, work authorization, timeline, and any claim in their materials that is ambiguous. Don't interrogate; batch questions.
4. **Write the profile** using the template below.
5. **Write the evidence map.** Every material claim gets an ID (`EV-001`, `EV-002`, …), the claim text, the source (`resume`, `linkedin`, `user-2026-07-02`, portfolio URL), and a confidence note. Downstream skills cite these IDs.
6. **Verify**: every major claim in the profile either cites an `EV-` ID or is explicitly labeled `[assumption]` or `[open question]`. If you can't source a claim, it goes in Open Questions — not in the narrative.

## `profiles/<person>/candidate-profile.md` template

```markdown
# Candidate Profile — <Name>
> Updated: YYYY-MM-DD | Sources: resume vX, linkedin YYYY-MM-DD, user notes

## Snapshot
(2–3 sentences: who they are professionally, seniority, core value)

## Career History
(reverse-chronological table: period | employer | title | scope | key evidence IDs)

## Strengths & Differentiators
(each with evidence IDs)

## Transferable Capabilities
## Gaps & Risks
(honest: missing skills for stated targets, timeline gaps, weak evidence areas)

## Constraints & Preferences
(geography, remote, compensation range, visa/authorization, timeline, dealbreakers)

## Open Questions
(unresolved ambiguities to confirm with the user)
```

## Refresh semantics — rebuild when materials change

Both files existing is NOT "done" by itself. If any source material in `profiles/<person>/` is **newer than `candidate-profile.md`** (mtime, or clearly different content — e.g. a replaced resume version), the profile and evidence map are STALE and this skill must refresh them, not skip:

1. Re-extract from the updated materials, treating the newest resume as the current baseline.
2. Rewrite BOTH artifacts: bump the `> Updated:` stamp and the sources line (e.g. `resume v3`).
3. Evidence map discipline on refresh: keep stable IDs for unchanged claims (**never renumber existing EV- IDs** — downstream reasons.json/reviews cite them), add new EV- entries for new claims, and mark superseded claims `[superseded YYYY-MM-DD]` instead of silently deleting them.
4. Reconcile the career history against the new baseline; new gaps/conflicts go to Open Questions, not smoothed over.

The runner's preflight prints a staleness warning naming the newer file — treat that warning as a rebuild instruction.

## Sensitive data

Compensation history, visa/work-authorization status, contact details, and demographics stay local-only and are stripped from model packets by default. Target compensation is a search preference and may be used by relevant search/strategy model stages (see `references/local-boundaries.md`).

## Done when

Both files exist, are at least as new as every source material in the profile folder, every claim is sourced or labeled, and Open Questions contains nothing that blocks judging job fit. Then tell the user what you found — strengths, gaps, and the one or two questions most worth answering — and suggest the next step (usually job research via the `job-opening-research` skill).
