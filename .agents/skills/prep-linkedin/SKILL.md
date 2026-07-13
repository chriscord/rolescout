---
name: prep-linkedin
description: Analyze and score the user's current LinkedIn profile against each target group of FOCUSED positions — per-section strengths, gaps, and empty fields — then propose concrete add/remove/change edits per group as a copyable review document. Use whenever the user asks to review, score, optimize, or align their LinkedIn with target roles, or asks how recruiters would read their profile.
---

# Prep: LinkedIn

Follow the shared publish and quality contract in `references/prep-quality-contract.md`.

Scope contract: target groups come from focused positions (`<project>/data/focused-jobs.json`; `prep-strategy` defines groups). Empty focus -> stop and ask. Inputs: the user's LinkedIn URL in `profile-meta.json`, a freshly captured `profiles/<person>/linkedin-current.md` handoff from this run, canonical `profiles/<person>/candidate-profile.md`, `profiles/<person>/evidence-map.md`, and `profiles/<person>/decision-policy.json`, group files, and the strategy doc's "LinkedIn direction". These paths are fixed; do not search for substitutes. Apply decision-policy constraints and output preferences. Truthfulness and local-only rules: `references/local-boundaries.md` -- this skill produces a review document; it never saves edits to LinkedIn itself.

**Output language.** Write the review in the user's language — take it from the user's standing instructions / profile locale; **default to English** when unspecified. Never hard-code a specific language: this skill and `prep-interview` must resolve output language the same way. All section/column labels below are the English defaults; translate them when the resolved language is not English.

## Current LinkedIn Source Gate

Do not generate reviews from local profile/resume/evidence alone. Those files are support evidence only, not the current LinkedIn surface recruiters will see.

Accepted current-source inputs:

- `profiles/<person>/linkedin-current.md` produced fresh by the RoleNavi runner during this same run.
- `profiles/<person>/linkedin-current.md` imported from an official/user-provided LinkedIn export or paste during profile intake.

The runner performs fresh capture before the agent starts. The agent must not perform capture again:

1. Read `profiles/<person>/linkedin-current.md`.
2. Confirm the file contains a capture timestamp, source URL, capture method, and visible LinkedIn profile text.
3. Analyze the captured profile text, score it against each focused target job group, and write detailed suggestions.
4. Do not run browser automation, local capture helpers, Codex Chrome Extension, chrome-devtools MCP, or a user manual text handoff.

If the fresh handoff file is missing, empty, or clearly not LinkedIn profile text, stop before writing `linkedin/<group>/linkedin-review.md`. Print:

`ERROR: LinkedIn capture handoff missing after runner capture`

For focused prep, do not use a bare LinkedIn URL as a substitute for current content. If current content is missing, ask the user to complete the supported LinkedIn import/capture path first.

Do not compare or validate the displayed LinkedIn name against the resume/profile name. People commonly use different legal, preferred, English, local-language, or professional names. Continue analysis from the captured current source when the LinkedIn source URL and visible profile text are valid. Hard fail only when the handoff is missing, empty, not a LinkedIn profile surface, or clearly the wrong URL class.

Never type credentials, never change LinkedIn fields, never save edits, never message, and never continue with a "local evidence proxy" review.

## Output — `linkedin/<group>/linkedin-review.md` per group

**Scorecard.** Scored sections are exactly: Headline, About, Experience entries, Skills, Education. **Activity, Licenses, and Featured are NOT score items** — never give them a score row; mention them only as optional recommendations when a concrete add/remove is warranted. Score each section 1–5 *for this group's target audience*, with three columns of findings: **Strengths** (what already works), **Gaps** (what underperforms and why), **Missing** (empty/missing sections and what belongs there). (Use the resolved output language for these labels; the words here are the English defaults.)

End with an overall score computed as a **weighted average: Experience counts ×3, every other scored section ×1** (Experience is what recruiters weigh 2–3x more than anything else). Show the arithmetic on the `Overall score:` line, e.g. `Overall score: 2.1/5 (weighted: Experience ×3).` Then list the 3 highest-leverage fixes. Do not require or manufacture `Part 1` / `Part 2` wrapper headings; the semantic scorecard and proposal sections are the contract.

**Current / Proposed / advisory proposals.** Per `### <Section>`, concrete edits:

- **Current** (required — this is what the web UI's left "Current" pane renders): reproduce the section's PRESENT LinkedIn content verbatim from the captured source, in a copyable block. For **Experience**, list every current role as `Title — Company — dates` on its own line followed by that role's current bullets (`•`/`-`, one per line), a blank line between roles — this exact shape is what the UI parses into LinkedIn-style entries. For single-value sections (Headline, About) a plain block is fine. If the section is empty/absent on the current profile, write `(not present on the current profile)` — never leave **Current** off, or multi-entry sections (Experience, Education) render blank on the left. Do not invent current content: it must come from the capture.
- **Proposed** (required): exact proposed LinkedIn content in one fenced, copyable block (headline ≤220 chars; About front-loads the first ~300 chars; Skills are one skill name per line with the desired top skills first), each tied to EV- refs — no claim without evidence. For Experience use the same `Title — Company — dates` + bullets shape as **Current** so the right pane mirrors the left. For Education, reproduce the recommended final entries; when no factual change is recommended, copy the current entries unchanged. The Web UI renders only this **Proposed** block inside its LinkedIn mockup.
- **Remove**: items that dilute this group's positioning (e.g. off-target skills, stale entries) with the reason.
- **Change / Add / Guidance**: advisory actions, rationale, caveats, or current → proposed mapping. Keep these outside the **Proposed** copy block. Never place sentences such as “prioritize…”, “keep as-is…”, “consider adding…”, or “for the current role…” inside **Proposed**; those are guidance and the UI must not render them as roles, skill tags, or education entries.

Consistency check before finishing: proposals must not contradict the group's tailored resume (`resumes/<group>/`) — recruiters cross-read; dates/titles/claims identical everywhere; flag any tension found.

Before reporting completion, run:

```bash
python scripts/validate_linkedin_review.py <project>
```

Fix every FAIL. Do not replace this with one-off PowerShell or Python structural checks; the validator is the UI/parser contract.

**Cross-source recency & discrepancy check (mandatory).** Compare the captured LinkedIn against the latest baseline resume in `profiles/<person>/` (and the group's tailored resume when it exists). Decide which source is more current by the most recent experience entry (role + dates); the newer source is the reference for current-state facts. Then:

- Content present in the newer source but missing on LinkedIn (e.g. the latest work experience) → generate it as a concrete **Add** proposal, marked `discrepancy: missing on LinkedIn`, with text drafted from the newer source (still evidence-traced; confirm ambiguous dates/titles with the user rather than guessing). The section's **Current** block must still reflect what IS on LinkedIn today (older roles, or `(not present on the current profile)`) so the left/right comparison stays truthful.
- Content present on LinkedIn but missing from the resume → do not silently drop it; list it in a short `## Discrepancies` section at the end of the review so `prep-resume` can pick it up on its next run.
- Conflicting titles/dates for the same role → flag in `## Discrepancies` with both versions and which source wins (newer), never averaging or inventing a third version.

## Regeneration semantics

Rerun regenerates per-group reviews from the latest focus set and latest LinkedIn content; stamp the source ("LinkedIn content as of <date>, via <export|paste|public page>"). If the user applied earlier proposals, diff first and score the improved state — don't re-propose what's done.
