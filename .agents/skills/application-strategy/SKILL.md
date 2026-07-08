---
name: application-strategy
description: Build local application instructions from selected job-list rows, verify the current application route, create step-by-step instructions, and add local tracker rows. Use when the user asks how to apply, wants application preparation, or asks about multiple roles at one company.
---

# Application Strategy

Turn selected jobs into a current, evidence-backed application plan. The
instruction file is the primary artifact: it tells the user exactly how to
apply, what materials to use, which fields require judgment, and what to record
afterward.

Public RoleScout does not submit applications, send messages, upload files,
create accounts, accept terms, schedule anything, or click final submit. This
skill produces instructions and tracker rows only.

Work in the recruiting repo root; resolve the active search project via
`active-project.json` (`references/project-structure.md`). Read
`references/recruiting-sheet-schema.md` before writing tracker rows.

## Scope

Use the user's explicit request first. If the user names roles, companies, or
job IDs, operate only on those. Otherwise use focused jobs from
`<project>/data/focused-jobs.json` when present. If neither exists, prepare the
highest-priority open rows in `job_list` and say which rows were selected.

Never invent an application target. Every tracker row must link to a real
`job_id` already present in `job_list`.

## Execution-Time Application Check

This is the "audit" step. It is not a broad company report; it is a current,
per-position check before instructions:

1. Re-open the posting URL or canonical source URL.
2. Verify the posting appears open, or clearly label the status as unverified,
   closed, redirected, or unavailable.
3. Identify the application route: ATS/vendor, direct careers form, email route,
   account requirement, referral path, and whether referral must happen before
   submission.
4. Capture required materials and visible screening questions before login.
5. Flag sensitive fields that require the user to answer manually: compensation,
   visa/work authorization, demographics, address, phone, references, or legal
   attestations.
6. For multiple roles at one company, check visible company guidance about
   multiple applications. If nothing reliable is visible, make a conservative
   sequencing recommendation and label it as such.
7. Note what confirmation artifact the user should save after manual submission.

Do not sign in, bypass access controls, create accounts, upload files, accept
terms, or click final submit during this check.

## Artifacts

Write one instruction file per position:

`<project>/applications/<job_id>/application-instructions.md`

Use this structure:

- Position summary: company, title, location, posting URL, last checked date.
- Current posting state and any uncertainty.
- Application route and account/login requirement.
- Required materials and local file paths.
- Field-by-field guidance for visible questions, using only evidence-backed
  profile facts.
- Sensitive fields the user must answer manually.
- Step-by-step user instructions.
- What to save after submission.
- Tracker update recommendation.

If several openings at the same company are in scope, add a short sequencing
section: apply to all, pick one, stagger, referral-first, or park. Ground this in
visible company guidance when available; otherwise say the recommendation is
conservative.

## Tracker Rows

For each prepared application, upsert a tracker row with:

- `application_id`: `app--<job_id>`
- `job_id`, `company`, `title`
- `status`: `to_apply` unless the user already applied outside RoleScout
- `resume_version`: recommended local resume path when available
- `next_action`: manual submission using the instruction file
- `next_action_due`: a concrete date chosen from urgency and user context
- `last_updated`: today
- `notes`: instruction file path and any uncertainty

Validate and write with:

```bash
python3 scripts/upsert_rows.py tracker rows.json
```

## Done When

Every selected position has a current instruction file, tracker rows are
validated and upserted, uncertainty is labeled, and the user knows the manual
next step. No external action is performed.
