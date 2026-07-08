---
name: prep-resume
description: Score and rewrite the user's resume for focused target job groups, producing evidence-backed, JD-mapped, professional 1-page DOCX variants. Use whenever the user asks to tailor, update, score, review, or generate resumes/CVs for target roles or groups.
---

# Prep: Resume

Produce resume variants that are truthful, targeted, and materially rewritten for the focused job groups. The baseline resume is raw material, not the deliverable.

Scope contract: target groups come from the **focused positions** (`<project>/data/focused-jobs.json` + their `job_group` values; run `prep-strategy` first if ungrouped). Empty focus -> stop and ask the user to register focus. Truthfulness rules are absolute: every claim traces to `profiles/<person>/evidence-map.md` or explicit user input; missing metrics are asked, never invented (`references/resume-bullet-rules.md`, `references/local-boundaries.md`).

Do not generate an application resume for parked/constraint groups by default. For groups whose strategy file says not to apply, write `resumes/<group>/resume-not-generated.md` explaining the blocker and required user override; only generate a resume after explicit role-specific override.

## Inputs

Read before drafting:

- `profiles/<person>/candidate-profile.md`
- `profiles/<person>/evidence-map.md`
- baseline resume in `profiles/<person>/` (latest user-provided resume)
- `profiles/<person>/linkedin-current.md` (latest captured LinkedIn, when present)
- `<project>/strategy/prep-strategy.md`
- `<project>/targets/job-groups/<group>.md`
- JD snapshots for every focused job in the group under `<project>/targets/jobs/*.json`

If candidate profile or evidence map is missing, run `candidate-profile-builder` first. Tailoring without an evidence map is a fabrication risk.

## Step 0 - Cross-source recency & discrepancy check

When a captured LinkedIn (`linkedin-current.md`) exists alongside the baseline resume, compare them before drafting. Decide which source is more current by the most recent experience entry (role + dates); the **newer source is the reference** for current-state facts. Then:

- Experience present in the newer source but missing from the baseline resume (e.g. the latest role is only on LinkedIn) → include it in the tailored drafts, drafting bullets from the newer source's content as user-authored evidence; confirm ambiguous dates/titles/scope with the user instead of guessing.
- Experience on the resume but missing from LinkedIn → note it so `prep-linkedin` proposes the add on its side.
- Conflicting titles/dates for the same role → the newer source wins; never average or invent a third version.

Record every finding in `resume-validation.md` under a `## Cross-source discrepancies` section (source, direction, resolution). No captured LinkedIn → skip and note "no LinkedIn capture available".

## Step 1 - Build the target brief

For each active group, write `resumes/<group>/target-brief.json` before writing any resume content:

```json
{
  "group": "strategy-ops-gtm",
  "source_job_ids": ["..."],
  "positioning_angle": "one sentence from the strategy doc, adjusted to the JDs",
  "requirements": [
    {
      "id": "REQ-gtm-planning",
      "priority": "must",
      "text": "recurring JD requirement",
      "keywords": ["GTM", "operating cadence"],
      "source_job_ids": ["..."]
    }
  ],
  "gaps": [
    {"requirement_id": "REQ-sql", "gap": "No direct evidence; do not claim"}
  ]
}
```

Extract requirements from the representative JDs: must-haves, nice-to-haves, recurring language, seniority markers, and domain terms. This is the relevance filter; generic role-family intuition is not enough.

## Step 2 - Score the baseline resume per group

Produce `resumes/<group>/resume-score.md` after the target brief:

- Per-dimension 1-5 with evidence: **requirement coverage** (vs `target-brief.json`), **impact quantification**, **seniority signaling**, **keyword/domain alignment**, **structure & scannability**.
- Verdict line: what the baseline already wins on for this group, and the 2-3 highest-leverage rewrites.
- No score delta may be claimed until the tailored draft exists and has passed validation.

## Step 3 - Select evidence, then write

Write `resumes/<group>/resume-draft.md` first. Generate DOCX only after the draft passes content validation.

Method:

1. Map each must-have requirement to the strongest evidence-map entries. Draft from evidence, never from aspiration.
2. Rewrite bullets for the target group. A tailored bullet should usually combine role language + evidence + result/scope. Selecting an original bullet unchanged is allowed only sparingly for exact metrics or legal/truthfulness precision.
   Baseline bullets may be rough notes in **any language** (Korean memos, fragments, keyword dumps) — that is expected input, not a blocker. Always produce polished, professional **English** bullets tailored to the target group. Target length per bullet: **20–27 words and 200–250 characters** (hard cap 250 — the validator enforces it); shorter is acceptable only for exact-metric bullets that lose precision when padded.
3. Cut ruthlessly. A bullet without a defensible reason for this group goes, even if impressive.
4. Reorder content so the most target-relevant proof leads within the resume constraints. Chronological employer order can remain, but the top third must carry the target story.
5. Preserve truth and attribution. Do not inflate scope, seniority, metrics, AI/product claims, languages, visa status, or employment continuity.

`reasons.json` must use the enriched schema:

```json
[
  {
    "bullet_prefix": "Led APAC GTM planning",
    "reason": "req_match",
    "evidence": "EV-008, EV-012",
    "requirement_ids": ["REQ-gtm-planning"],
    "source_job_ids": ["<companyslug>--<hash>", "<companyslug>--<hash>"],
    "rewrite_type": "substantial_rewrite",
    "baseline_source_bullet_id": "B08"
  }
]
```

Valid `rewrite_type`: `new`, `substantial_rewrite`, `compressed`, `reframed`, `selected`. `selected` means mostly unchanged from baseline and should be rare.

## Step 4 - Validate content

Extract the original resume bullets to `resumes/baseline-extracted.md` (one bullet per line); use that file as the baseline input. **Re-extract on every run from the CURRENT latest baseline resume** — never reuse a `baseline-extracted.md` that is older than the baseline resume file (a replaced resume would otherwise be validated against the old baseline).

Run both validators before generating DOCX:

```bash
python scripts/validate_resume_bullets.py resumes/<group>/resume-draft.md --reasons resumes/<group>/reasons.json
python scripts/validate_resume_tailoring.py resumes/<group>/resume-draft.md \
  --baseline resumes/baseline-extracted.md \
  --target-brief resumes/<group>/target-brief.json \
  --reasons resumes/<group>/reasons.json
```

Also compare active variants against each other when more than one group exists:

```bash
python scripts/validate_resume_tailoring.py resumes/<group>/resume-draft.md \
  --baseline resumes/baseline-extracted.md \
  --target-brief resumes/<group>/target-brief.json \
  --reasons resumes/<group>/reasons.json \
  --other-resume resumes/<other-group>/resume-draft.md
```

Fix failures before DOCX generation. If a validator flags a truthful bullet that must remain close to the baseline, document the reason in `resume-validation.md`; do not silently ship around the warning.

## Step 5 - Generate the DOCX

File: `resumes/<group>/resume_{UserName}_{groupSlug}.docx` (UserName = person's name CamelCase). Hard format requirements:

- **Exactly 1 page** (verify by rendering to PDF and checking page count when tooling is available; content must also fill the page, not half of it).
- Times New Roman throughout; bullet points (real Word list formatting, never unicode bullet characters); horizontal section dividers (bottom-border paragraphs). A proven generator pattern exists at `benchmarks/eval-set/generation/build_resumes.js` (docx-js, A4/narrow/TNR/dividers/typography presets) — reuse the approach.
- **If a previous version of the group resume exists, preserve its format/layout and update content only** (docx skill: unpack → edit XML → repack), rather than regenerating from scratch.

DOCX content must come from the validated `resume-draft.md`. Do not create or edit bullets only inside DOCX; that bypasses content validation.

## Step 6 - Write validation report

Write `resumes/<group>/resume-validation.md` with:

- validator outputs
- bullet -> evidence ref -> requirement id -> source job id -> reason code -> rewrite type
- must-have coverage summary from `target-brief.json`
- baseline similarity summary and any accepted exceptions
- what changed vs baseline, grounded in actual content changes
- unresolved gaps/questions, especially missing metrics or unevidenced JD requirements

## Regeneration semantics

Rerun regenerates per-group artifacts **in place** (same filenames) from the latest focus set, latest baseline, and latest evidence map — never append variant copies. Groups that are no longer in the focused set: move their `resumes/<group>/` folder to `resumes/_retired/<group>-<YYYY-MM-DD>/` and say so in the run summary — stale variants must not keep appearing as current in the UI. If the baseline resume changed since the last run, say what changed at the top of each regenerated `resume-validation.md`.

## Done when

Every active focused group has: `target-brief.json`, `resume-score.md`, validator-clean `resume-draft.md`, `reasons.json`, the 1-page DOCX (verified when tooling is available), and `resume-validation.md`. Parked groups have `resume-not-generated.md` unless explicitly overridden. Present score deltas only after validation. Do not send or upload resumes.
