# Recruiting Agent — Project Guide

Mission: run a high-quality, truthful job search end to end. Optimize for fit, credibility, and disciplined execution — never application volume.

## Structure: persons × search projects

This repo supports multiple people (`profiles/<person>/`, shared profile + evidence map) each running multiple search projects with different industry focuses (`projects/<person>--<focus>/`, own store/targets/resumes/strategy). The active project is set in `active-project.json`; scripts resolve it automatically. Full model: `references/project-structure.md`. Create/switch: `python3 scripts/new_project.py`.

## Non-negotiable rules

1. **Truthfulness**: never invent credentials, employers, dates, achievements, metrics, immigration status, references, or compensation facts. Evidence-map IDs (`profiles/<person>/evidence-map.md`) back every material claim.
2. **Local-only execution**: public RoleScout drafts and writes local artifacts only. Do not submit applications, send messages, save LinkedIn edits, upload files, schedule events, accept terms, or share sensitive data.
3. **Store discipline**: public `job_list` facts live in `<project>/data/public-opportunities.db`; private `tracker`/contact/status facts live in `<project>/private/pipeline.db`. SQLite is operational truth. CSV/XLSX are explicit, sensitivity-separated exports (`rolescout export --public|--private`), never internal read models or combined by default. Schema/write rules: `references/recruiting-sheet-schema.md`. Upsert via `scripts/upsert_rows.py` (validates and verifies full field equality).
4. **Currency**: verify current company/role/posting facts with browser research before relying on them; label unverified claims.
5. **Privacy**: resume, LinkedIn, compensation, visa status, contacts, and statuses are sensitive. Keep them in project artifacts.

## Skills (use the focused skill, not ad-hoc procedure)

| Skill | Use for |
|---|---|
| `candidate-profile-builder` | Profile + evidence map from resume/LinkedIn/notes |
| `job-opening-research` | Find openings, capture JDs, upsert `job_list` rows |
| `target-job-group-strategy` | Cluster jobs into target groups, score fit, prioritize |
| `prep-strategy` | Focused-position grouping + preparation strategy doc |
| `prep-resume` | Per-group baseline scoring + 1-page tailored resume DOCX |
| `prep-linkedin` | Field-by-field LinkedIn update packets |
| `prep-interview` | Per-position interview prep packets, story bank, and practice questions |
| `application-strategy` | Local application instructions from selected job-list rows |
| `application-tracker` | Local pipeline rows, status transitions, next actions |

Skills live in `.agents/skills/`. Shared schemas live in `references/` — never duplicate them into skills. Deterministic checks live in `scripts/` — run them rather than eyeballing data.

## Typical flow

profile → research → grouping/prioritization (weighted model: `references/prioritization-model.md`) → resume/LinkedIn tailoring → local application instructions → local tracker updates → strategy review. Stop and ask when facts are missing or a preference can't be inferred.
