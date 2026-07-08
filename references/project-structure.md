# Project Structure — Persons and Search Projects

This repo supports multiple people and, per person, multiple parallel job-search campaigns ("search projects") with different target industry focuses. Two axes, two scopes:

## Person scope — `profiles/<person>/` (shared)

One folder per person, keyed by a short code (e.g. `ck`). Contains what is true about the person regardless of which industry they're targeting:

```
profiles/<person>/
  candidate-profile.md    # built by candidate-profile-builder
  evidence-map.md         # claim -> source mapping (EV- ids)
  <raw source files>      # resumes, exports the user dropped in
```

The profile is written once and *shared* by every search project of that person. Industry-specific positioning does NOT belong here — it belongs to the project.

## Project scope — `projects/<person>--<focus>/` (campaign)

One folder per (person × target industry focus), e.g. `projects/ck--ai-infra/`, `projects/ck--fintech-product/`. Everything competitive/strategic/stateful about that campaign:

```
projects/<person>--<focus>/
  project.json            # {person, focus, profile_dir, created_at, status, external_sheet}
  data/                   # recruiting.db (source of truth) + generated views
  targets/jobs/           # JD snapshots
  targets/job-groups/     # group files
  strategy/               # scoring-config.json, job-ratings.json, job-scores.json, target-priorities.md
  resumes/<group>/        # resume variants (tailored per project focus)
  linkedin/<group>/       # update packets
  applications/<company>/ # application strategies and local tracker notes
  interviews/<company>-<role>/
```

Each project has its OWN job_list/tracker database — pipelines from different industry focuses never mix.

## Active project resolution

Root file `active-project.json`:

```json
{"active": "projects/ck--ai-infra"}
```

- All `scripts/` (init_db, upsert_rows, score_jobs) resolve their data/strategy paths from this file automatically; override with env `RECRUITING_PROJECT_DIR` or by editing the file.
- Skills must resolve the active project at the start of any run, and confirm with the user when the request obviously belongs to a different project ("switch to your fintech search?"). Switching = editing `active-project.json`.
- No active project / no projects yet → create one first (below). Scripts fail with a clear message rather than guessing.

## Creating a person or project

```
python scripts/new_project.py --person ck --focus ai-infra
```

Creates `profiles/ck/` if missing, scaffolds `projects/ck--ai-infra/`, writes `project.json`, seeds `strategy/scoring-config.json` from `references/scoring-config.default.json`, initializes the DB, and sets the project active. Switch later with `--activate ck--ai-infra`.

Focus codes: short slugs describing the target market (`ai-infra`, `fintech-product`, `strategy-product-growth`) — the agent proposes one from the user's stated focus; user confirms.

## What stays repo-global

`references/`, `scripts/`, `dist/`, `AGENTS.md` — shared machinery, no per-person state. Never write person or campaign data into these.

## Cross-project rules

- The person profile is the single truth source: a fact fixed in one project's session (e.g. corrected metric) is fixed in `profiles/<person>/` so all projects see it.
- Resume variants, positioning, scoring configs, trackers are per-project — never reuse across projects without explicit tailoring.
- When the user mentions applying to the same company from two projects, flag it: overlapping applications from parallel campaigns at one company is exactly the multi-opening risk `application-strategy` audits.
