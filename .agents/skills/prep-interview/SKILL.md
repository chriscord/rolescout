---
name: prep-interview
description: Build a per-position interview pack for FOCUSED positions — a timed self-introduction table, a JD-requirement↔story mapping table, adversarial (red-flag) questions with truthful answer plans, a "The Whys" table, a behavioral question↔story mapping table, a researched industry/company glossary, recent company news — plus an independent, editable STAR story bank derived from every resume bullet — grounded in the resume, the JD, and MANDATORY web research of the company's actual past interview questions with cited sources. Use whenever the user mentions an interview, screen, or onsite, or asks for likely questions or practice.
---

# Prep: Interview

Scope: focused positions (`<project>/data/focused-jobs.json`), or the specific position the user names. Inputs per position: the tracker row (**which interview stage?** — a screen, an onsite, and a final round need different packs), the resume variant **actually submitted** (tracker `resume_version`; interviewers probe the resume they received — fall back to the group resume/baseline only if nothing was submitted yet), user's standing instructions, the JD snapshot (`targets/jobs/<job_id>.json`) — requirements and responsibilities drive everything — plus the resume scoring/validation outputs (`resumes/<group>/`) and strategy group analysis (`strategy/`) for the weakness-driven sections. **Output language.** Write all questions and answers in the user's language — take it from the user's standing instructions / profile locale; **default to English** when unspecified. Never hard-code a specific language: this skill and `prep-linkedin` must resolve output language the same way. Evidence discipline: every answer draws on `profiles/<person>/evidence-map.md`; thin story coverage → ask the user for a real example, never script fiction.

## Step 0 — MANDATORY research pass (before drafting anything)

Web-search, collecting every URL used:

1. **Past interview questions** — Glassdoor/Blind/Reddit/prep sites/candidate blogs, `"<company>" interview questions "<role family>"`, regional-language sources where relevant. Never invent a question and attribute it to a source; researched items are marked `[reported]` with their source, JD-derived ones `[inferred from JD]`.
2. **Glossary material** — keywords, jargon, product names, metrics, and bite-size domain knowledge an interviewer at this company/role would use naturally (industry terms, the company's own vocabulary, regulatory/technical terms the JD implies).
3. **Recent news** — company news from the **last 2–4 weeks**; collect enough to pick the ~3 most position-relevant items (funding/investment, M&A, layoffs/restructuring, new products/launches, stock-price events, leadership changes, partnerships). Record publication date and source URL for each.
4. **Current company context** — products, leadership, positioning; unverifiable claims stay labeled.

## Step 1 — Industry thesis context → `<project>/interviews/interview-context.json`

Run `python scripts/build_interview_context.py <project>` and read the generated context before drafting. For every focused position, use its concrete `{company}`, `{business arm}` (when present), role title, JD summary, and scaffolded `web_search_queries` to web-search the company's actual product market and business model. Do **not** hard-code a company→industry mapping in the skill or generated content.

Build a short industry thesis for each position before writing `## The Whys`:

- `industry`: the company/product market, customer or user system, and economic model.
- `business arm`: the specific org, product line, partner segment, or role-facing market named by the posting.
- `current market tension`: what is changing now in that market, supported by sources.
- `candidate bridge`: which story IDs and EV refs explain the user's genuine pull toward that market.

`Why this industry` must answer why the user is interested in that **industry/market**, not the job function, role family, or job_group. Phrases such as "strategy/GTM operations", "strategic finance/corpdev", or "business development" are functions, not industries. If the web search cannot identify the market confidently, write `[inferred — confirm]` and name the evidence gap instead of substituting a function label.

## Step 2 — Story bank → `<project>/interviews/story-bank.json` (+ `story-bank.md` mirror) — independent, canonical, editable

The story bank is its **own artifact**, generated and managed independently of any single interview pack (it derives from the person's resume, so it is shared across all positions). Store it canonically as `<project>/interviews/story-bank.json` (schema: `{ "meta": "...", "entries": [ {id, title, source, situation, task, action, result, best_for, ev_refs} ... ] }`) and write a human-readable `story-bank.md` mirror alongside it. The web UI renders it once at the **bottom of the Interview tab** from the JSON and lets the user edit S/T/A/R/Best-for inline (saves write straight back to the JSON). Do **not** embed the story-bank table inside `prep-notes.md`.

Build or refresh the story bank from the submitted resume variant:

- **One row per resume bullet.** For every work experience, each bullet yields one story entry: **S**ituation, **T**ask, **A**ction, **R**esult. (No Reflection field.) Each experience produces as many rows as it has bullets.
- **Flesh keywords into short sentences.** Do not leave S/T/A/R as bare keyword fragments plucked from the bullet. Extract the keywords, then add just enough connective tissue to make each of S/T/A/R a short, natural sentence — no invented facts, metrics, names, or outcomes beyond the resume/evidence map. Use a strong model (**gpt-5.5, high reasoning**) for this fleshing so the sentences read naturally while staying truthful. Good example: `ST-06 — [S] The company was raising capital and needed to reach more investors; [T] Reach out to and hold conversations with as many high-value investors as possible; [A] I identified and aggressively attended meetups for Korean investors; [R] Landed conversations with otherwise-inaccessible investors.` `ST-03 — [S] At EthGlobal NYC 2023; [T] Build something with blockchain; [A] I built CharStat6551, a blockchain-powered backend system for game devs; [R] Won a $3K award.`
- **CAR-grouping exception**: when adjacent bullets were written as a split CAR/STAR arc, merge that bullet group into a **single row**, recording the merged bullet refs.
- Each row gets a stable **story ID** (`ST-01`, `ST-02`, … — append new, never renumber), a short **title**, a `source` (experience + bullet index/es), `ev_refs` from the evidence map, and `best_for` (question types it answers).
- Derivation is bounded by truth: S/T/A/R come from the bullet, the evidence map, or reasonable inference from the resume; anything inferred is tagged `[inferred — confirm]` for the user to validate. Never fabricate a story.
- **Table column order (mirror + UI):** `| ID | Title | Source | S | T | A | R | Best for | EV refs |`.

## Answer-crafting principles (apply to every answer)

**Ban the generic-praise template.** Never structure a "why" answer as "[subject] is attractive because [facts anyone can recite] + my experience fits". The test for every sentence: *could any other candidate say this word-for-word?* If yes, cut or personalize it. "Why" answers are personal narratives: a concrete moment (from the evidence map) → what it sparked → what the user actually did about it → why now, this company/position. **Truthfulness under personalization**: every narrative beat backed by evidence or tagged `[inferred — confirm]`. **2–3 labeled versions per why-question** (V1 personal-narrative — default lead, V2 strategic/analytical, V3 concise 30-second); behavioral answers vary by *story choice* (cite story IDs), not by rephrasing.

## Output — `interviews/<company>-<role>/prep-notes.md` per position

Short metadata header (position, stage, resume version, date), then **exactly these H2 sections, in this order** — the web app's Prep tab renders this file directly, so the titles and order are a UI contract:

1. `## Self Introduction` — a two-column table: left **Seconds** (`10s`, `30s`, `60s`), right **Content** (the script). 10s = who + sharpest hook; 30s = arc: past → relevant proof → why here; 60s = adds 1–2 quantified proof points + position-specific close. Written to be spoken, not read.
2. `## Job Requirements` — split the JD's requirements **one by one** (each must-have and nice-to-have is its own row) and link each to the story bank: `| # | Requirement | Must/Nice | Matching story (ID — title) | Fit | Prep note |`. Fit ∈ strong / partial / none. `none` is flagged, never hidden: add the truthful gap plan, and if a real experience could become the missing story, propose it to the user and append it to the bank after confirmation.
3. `## Adversarial Questions` — **4–5 red-flag/adversarial questions** an interviewer would actually ask, derived from the resume scoring/validation results, the strategy group's weaknesses, and this JD's unfit points (gaps, seniority stretch, domain switch, short stints, defensibility of claimed metrics…): `| # | Question | Why they'll ask (weakness source) | How to answer | Story refs |`. Answers are truthful framing — mitigation, adjacent evidence, honest learning plan — never denial or spin. Every number on the resume must be defensible here.
4. `## The Whys` — its **own top-level section** (NOT a subsection of Behavioral Questions): the four mandatory why-questions (*Why this industry · Why this company · Why this position · Why you*), each with 2–3 labeled versions, as a **table**: `| Why-question | Version | Answer | Refs |` — one row per question×version (V1 personal-narrative default, V2 strategic/analytical, V3 concise 30s), Answer written per the answer-crafting principles (keep any `[inferred — confirm]` tag), Refs = story IDs + EV refs separated with `·` **never a raw `|`** (e.g. `ST-02, ST-01 · EV-020, EV-001`). `Why this industry` must use the Step 1 industry thesis and cite company/product-market context; it fails the quality bar if it merely says the user likes the role function. `Why this company` must use company-specific product, market, news, or business-model context. `Why this position` must name the posting's actual scope or must-have requirements.
5. `## Behavioral Questions` — the expected behavioral/experience questions (conflict, failure, leadership, prioritization, resume deep-dives — tuned to seniority and the researched patterns, `[reported]` items included with sources) as a **mapping table only, no subsection heading**: `| Question | Story (ID — title) | Angle | Tag |`.
6. `## Glossary` — 8–15 researched terms: `| Term | Meaning | Why it matters here |` — company/role/industry keywords, jargon, and domain trivia from Step 0; plain-English meanings, no invented facts.
7. `## News` — ~3 items from the last 2–4 weeks, **sorted by relevance to this position** (investment, acquisition, layoffs, new product, stock-price events rank by how much they touch the role): each row has **news date, source URL**, a 2–3 sentence summary, and a one-line "so what for this interview". If fewer than 3 exist in-window, widen the window and label it.
8. `## Questions to Ask` — 6–10, position- and company-specific (team scope, success metrics, current initiatives from research), as a **table**: `| # | Question to ask | What it signals |`; never generic filler.
9. `## Sources` — every URL from Step 0 as a **table**: `| # | Source | What it informed |` (Source = a markdown link to the URL; "What it informed" = one-line note on what it contributed). No sources table = the research pass didn't happen = incomplete output.

The story bank is **not** part of this file — it is the independent `interviews/story-bank.json` artifact (Step 1), rendered separately at the bottom of the Interview tab. Consistency rule: every section that lists questions/sources is a **table**, matching the tables above — no prose/bullet sections in the pack.

`## Negotiation` is appended **only at offer stage**: market context clearly labeled as estimate, the user's floor from profile constraints, current compensation never disclosed without explicit approval. Omit at earlier stages.

Before reporting completion, run:

```bash
python scripts/validate_interview_prep.py <project>
```

Fix every FAIL. If the validator prints `QUALITY`, treat it as a retryable content issue: keep the generated artifacts, rewrite the flagged `The Whys` rows from the per-position industry thesis, and rerun the validator. If a bounded retry still leaves QUALITY issues, report the remaining scope as partial instead of deleting or withholding the artifacts. Do not use `validate_research_artifacts.py` or resume validators as substitutes for interview-pack validation; they check different contracts and produce misleading errors for this workflow.

## Regeneration & follow-through

Rerun refreshes research (question reports and news go stale fast — News is regenerated every run) and regenerates per latest focus/resume version, keeping story IDs stable. Offer mock-practice mode (one question at a time; feedback on STAR structure, specificity, evidence use, and length — honest scoring beats cheerleading; walk the user through every `[inferred — confirm]` tag and mark their preferred answer version per question) and a tracker update (`application-tracker`) for the interview stage (next_action e.g. thank-you-note draft — drafting is free, sending requires approval) — scheduling or messaging anyone stays behind explicit approval.
