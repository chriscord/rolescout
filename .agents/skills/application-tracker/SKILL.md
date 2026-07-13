---
name: application-tracker
description: Maintain the local tracker store — move chosen jobs into the pipeline, update application statuses from user-provided information, record next actions, and show pipeline views. Use whenever the user reports application news, asks where things stand, or wants to record a manual next step.
---

# Application Tracker

Keep the `tracker` store as the single accurate record of what the user is pursuing and where each application stands. Read `references/recruiting-sheet-schema.md` first — it defines the tracker columns, the status enum, allowed transitions, the storage backend (SQLite source of truth + generated views), and write discipline. Never invent statuses or skip validation.

Work in the recruiting repo root; resolve the active search project via `active-project.json` per `references/project-structure.md` — `<project>` below means that directory, `<person>` the profile dir in its `project.json`.

## Adding roles to the pipeline

When the user picks jobs to act on:

1. Confirm the `job_id` exists in `job_list` (add it via `job-opening-research` if not — the tracker never contains jobs the list doesn't know about).
2. Build tracker rows: `application_id` = `app--<job_id>`, entry status `to_apply` (or `applied` if the user already applied outside RoleNavi), `resume_version` pointing at the chosen variant, `next_action` + `next_action_due` set to something concrete.
3. Validate then write: `python3 scripts/upsert_rows.py tracker rows.json` (validates linkage/transitions, upserts into the private pipeline SQLite store, and verifies full field equality). Exports are explicit via `rolenavi export --private`; never create a combined public/private workbook.

## Status updates

Map what the user tells you onto the enum (`to_apply, applied, to_interview_1..3, offer, accepted, rejected, withdrawn, paused`). The validator enforces legal transitions; if the real world skipped steps (e.g. recruiter jumped straight to onsite), confirm with the user, then write the intermediate transition(s) or record the exception in `notes`. Always set `last_updated`; on terminal statuses set `outcome`.

With every update, recommend the next local action and due date — a tracker row without a next action is a stalled record. Do not check email, create calendar events, send messages, or submit applications.

## Pipeline views

When asked for status, read the store and summarize: counts by status, items with overdue `next_action_due`, stale rows (no update in 14+ days), and recommended focus. Flag pipeline-health patterns honestly (e.g. many applications but no interviews → positioning or targeting issue; suggest revisiting `target-job-group-strategy`).

## Boundaries

This skill records decisions and statuses only. It never submits applications, sends messages, checks email, schedules interviews, or withdraws applications.
