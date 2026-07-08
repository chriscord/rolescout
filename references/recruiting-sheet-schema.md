# Recruiting Data Schema

Single source of truth for the `job_list` and `tracker` schemas. All skills that read or write pipeline data must follow this file. Do not copy these schemas into skill files — link here instead.

## Storage backends

Each search project has its own store (see `references/project-structure.md`); scripts resolve the active project via `active-project.json`.

1. **SQLite — source of truth**: `<project>/data/recruiting.db`, tables `job_list` and `tracker`. Create/verify with `python scripts/init_db.py` (or `ensure_recruiting_sheet.py`, which delegates). The DB itself enforces primary keys, enums, fit_score range, and the tracker→job_list foreign key.
2. **Generated views — never hand-edit** (regenerated on every write by `scripts/upsert_rows.py`):
   - `<project>/data/job_list.csv`, `tracker.csv` — diff-able mirrors, also used as validator inputs.
   - `<project>/data/recruiting-pipeline.xlsx` — human view: both tables plus a `pipeline` summary sheet (status counts, overdue next actions).
3. **External sheet (optional, read-only)** — the user may keep a Google Sheet copy (ID/URL in the project's `project.json` `external_sheet` field). With only read access (Drive connector), use it to reconcile the user's manual edits before writing locally; offer changed rows in paste-ready form afterward. If a write-capable Sheets connector appears, verify headers with `ensure_recruiting_sheet.py --check-headers` before any write.

### Write discipline

- **Validate before write**: run the relevant validator (`scripts/validate_job_rows.py` / `scripts/validate_tracker_rows.py`) on the candidate rows. `upsert_rows.py` re-runs it and refuses to write on FAIL.
- **Upsert, don't append blindly**: `job_list` keys on `job_id`; `tracker` keys on `application_id`. `scripts/upsert_rows.py <table> rows.json` handles this transactionally.
- **Read after write**: `upsert_rows.py` verifies from the DB and regenerates all views automatically. For external sheet writes (connector case), re-read the written range yourself.

## `job_list` columns (exact order)

```text
job_id
captured_at
company
title
job_group
location
remote_policy
source_url
job_page_url
posting_status
seniority
must_have_requirements
nice_to_have_requirements
jd_summary
fit_score
priority
notes
last_seen_at
```

Field rules:

| Field | Rule |
|---|---|
| `job_id` | Required. Stable dedupe key: `<company-slug>--<title-slug>--<8-char-hash-of-canonical-url>`. Generate with `scripts/normalize_job_url.py`. |
| `captured_at`, `last_seen_at` | ISO date `YYYY-MM-DD`. `captured_at` required. |
| `company`, `title`, `source_url` | Required, non-empty. |
| `location` | Normalize before every write. Use semicolon-separated location tags for multi-location roles. `Singapore` stays `Singapore` (city-state). Other city locations use `{City}, {Country}`; use `USA` for the United States. Examples: `SG - Singapore` → `Singapore`; `Singapore, , Singapore` → `Singapore`; `US - San Francisco` → `San Francisco, USA`; `Seoul; Singapore` → `Singapore; Seoul, South Korea`. `scripts/upsert_rows.py` normalizes job_list writes; direct validation rejects unnormalized values. |
| `source_url`, `job_page_url` | Must be `http(s)` URLs. Canonicalize with `scripts/normalize_job_url.py` (strips tracking params, fragments). |
| `posting_status` | One of: `open`, `closed`, `removed`, `unknown`. Use `removed` for duplicate or out-of-scope rows retained for auditability. |
| `remote_policy` | One of: `onsite`, `hybrid`, `remote`, `unknown`. |
| `fit_score` | Integer 1–5 or empty (empty until scored). |
| `priority` | One of: `high`, `medium`, `low`, or empty. |
| `job_group` | Slug matching a file in `<project>/targets/job-groups/`, or empty until grouped. |
| `must_have_requirements`, `nice_to_have_requirements`, `jd_summary` | Plain text; separate multiple requirements with `; `. |

## `tracker` columns (exact order)

```text
application_id
job_id
company
title
job_group
status
applied_at
resume_version
linkedin_version
contact
next_action
next_action_due
last_updated
outcome
notes
```

Field rules:

| Field | Rule |
|---|---|
| `application_id` | Required, unique: `app--<job_id>`. |
| `job_id` | Required; must exist in `job_list`. |
| `status` | Required; see enum below. |
| `applied_at`, `next_action_due`, `last_updated` | ISO date `YYYY-MM-DD`; `last_updated` required, set on every write. |
| `resume_version` | Path or label of the approved resume variant, e.g. `<project>/resumes/ml-platform/resume.md@2026-07-02`. |
| `outcome` | Empty until terminal; then `offer_accepted`, `offer_declined`, `rejected`, or `withdrawn`. |

### `status` enum and allowed transitions

```text
to_apply -> applied | rejected | withdrawn | paused
applied -> to_interview_1 | rejected | withdrawn | paused
to_interview_1 -> to_interview_2 | rejected | withdrawn | paused
to_interview_2 -> to_interview_3 | offer | rejected | withdrawn | paused
to_interview_3 -> offer | rejected | withdrawn | paused
offer -> accepted | rejected | withdrawn
paused -> to_apply | applied | to_interview_1 | to_interview_2 | to_interview_3 | withdrawn
accepted, rejected, withdrawn -> terminal (no transitions out)
```

A row may also enter at `to_apply` (default) or `applied` (user already applied on their own). Any other transition requires explicit user confirmation and a note explaining why.
