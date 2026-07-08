# Search Workflow — Structured Discovery with Auditable Artifacts

Normative workflow for `job-opening-research`. Goal: not "search every job", but build a **logical opportunity universe** from profile + target focus + location, explore it through **trusted sources** with enforced fallbacks, and leave a trace that lets QA distinguish *unseen* from *seen-but-skipped*. Every phase produces a mandatory artifact under `<project>/targets/`.

**Division of labor (non-negotiable):** the LLM agent interprets the profile, invents the thesis, expands the universe, chooses sources, and judges every posting. Scripts own repeatable mechanics: `scripts/resolve_company_sources.py` gives an official-first source skeleton, `scripts/fetch_url.py` handles public fetch/cache summaries, `scripts/normalize_job_url.py` canonicalizes URL/job_id fields, `scripts/location_eligibility.py` classifies exact-city vs same-metro location ambiguity, `scripts/merge_research_parts.py` merges shard logs, `scripts/generate_coverage_audit.py` writes deterministic coverage audit scaffolding, `scripts/probe_linkedin_jobs.py` records the runner-owned LinkedIn Jobs observation, `scripts/persist_job_rows.py` normalizes/validates/upserts rows, `scripts/validate_research_artifacts.py` verifies search artifacts, `scripts/validate_linkedin_review.py` verifies LinkedIn prep artifacts, and `scripts/validate_interview_prep.py` verifies interview prep artifacts. Do not write one-off Python for these common tasks; if a deterministic adapter is missing, report the capability gap. Do not read external helper skills; use RoleScout skills/references plus the deterministic scripts. A script must never decide *which company to search*.

Before Phase 2, read `references/search-source-registry.yaml`. Use matching curated company sets and direct careers URLs as seeds when they fit the user's thesis (for example ex-consulting strategy/investing/fintech/AI searches), but never as a hardcoded universal target list. Every registry URL must be verified at runtime before relying on it; a stale URL becomes a discovery hint, not evidence that a company has no openings.

## Phase 1 — Opportunity Thesis → `targets/opportunity-thesis.md`

Extract from profile evidence + project focus + location, before any searching:

- **Role families** (not title keywords): e.g. strategy & ops, BD/partnerships, GTM, product strategy, chief of staff, ecosystem/alliances, AI deployment/solutions. Same work hides under many titles — families are what you search, titles are what you find.
- **Seniority floor/ceiling** (evidence-backed: years, scope), **hard constraints** (location, visa, language, dealbreakers), **preferred domain buckets**, **strengths/gaps** with EV- refs.
- One-line thesis, e.g.: "APAC strategy/BD/product-partnership roles in AI, cloud/data, marketplaces, developer platforms; senior-manager-to-director band; Singapore-based."

## Phase 2 — Company Universe → `targets/company-universe.json`

Seeds (user-named companies) are a starting point, never the boundary. **Treat each seed as an *example of a market map*, not only a literal target.** For each seed, first infer its **archetype** along the axes that make it a useful exemplar — scale/maturity, business model, product category, talent pool, location market — then expand that archetype into **adjacent buckets** via web research and reasoning: competitor sets, same-talent-pool employers, adjacent product categories, ecosystem partners, funded entrants, and location-relevant employers. The relationship *types* are fixed; the company names are always discovered at runtime, never hardcoded. Name-brand seeds (large platforms, hyperscalers, frontier labs, category leaders) are the ones most often collapsed back to just the seed — treating a name-brand seed as a single literal target rather than as an exemplar of its peer set is the exact defect this phase exists to prevent; the same generalization you already apply to a category seed like `fintech startup` applies to a name-brand seed. Every entry carries its *why*:

```json
{"buckets": [
  {"bucket": "ai-labs-and-platforms", "why_relevant": "thesis: AI deployment + product strategy; hires ex-consulting strategists",
   "expansion_note": "optional — required only when this archetype/bucket ends with <5 searched companies: say why (no more peers under the constraints, thesis narrows here, …)",
   "companies": [
     {"name": "…", "seed": false, "rationale": "competes with seed X in APAC; SG hub; hires strategy/BD",
      "evidence": "careers page shows SG strategy roles / news ref", "priority": "high"}]}],
 "excluded": [{"name_or_bucket": "…", "reason": "no APAC presence / violates constraint"}]}
```

Rules: every seed must appear; every company has a rationale tied to thesis (never "well-known company"); record **excluded** buckets/companies with reasons — exclusion without a reason is a future unexplainable miss. **Per-archetype expansion floor**: each seed's archetype/bucket should hold **≥5 searched companies**, or the universe must record why expansion is inappropriate (genuinely no peers under the constraints, the thesis narrows to that one firm, etc.) as an `excluded` entry or a bucket `expansion_note`. **Omissions self-critique (run once before saving)**: ask — "given these seeds, role families, and locations, what obvious peer employers is this universe missing?" — and either add each with a rationale or exclude it with a reason; carry the result into `coverage-audit.md` as an obvious-omissions note. This is a reasoning pass, not a hardcoded list: for name-brand seeds it is what surfaces the rest of the peer set (other hyperscalers, other frontier labs, adjacent marketplaces) that a literal reading would miss. Target scale (defaults, thesis may override): seeds + 25–50 adjacent across 5–8 buckets.

## Phase 3 — Source Plan → `targets/source-plan.json`

Per company (or per bucket for the tail): the trusted source path and its fallback ladder. Use `python scripts/resolve_company_sources.py "<Company>" --json` as the deterministic official-first starting point, then adapt it with your judgment.

```json
{"companies": [{"name": "…", "sources": [
   {"type": "ats", "url": "https://boards.greenhouse.io/<token>", "status": "planned|ok|blocked|empty"},
   {"type": "official_careers", "url": "…", "status": "planned"}],
  "fallbacks_used": [], "notes": ""}]}
```

### Source matrix (trust order)

1. **Official/registered careers source first when known.** If the resolver or registry has a branded/self-hosted careers source, start there. The official source may be server HTML, JSON API, or JS-rendered. Registered entries from `self_hosted_careers` or `major_company_careers` are discovery seeds requiring runtime verification; they are not proof of role existence. ATS slug probes are fallback/discovery signals after official discovery or a verified ATS token, not the first move.
2. **ATS pages — reliable when the token is verified, static, ToS-safe.** Recognize by URL pattern; verify the token before relying on it. **Enumerate, don't filter**: on a structured board you control the whole listing cheaply — pull ALL postings for the target location(s) and judge each one, logging every skip. Keyword-narrowed source queries are a coverage defect on boards (a live run captured only 6 of a seed company's postings this way); keywords belong to web discovery, not board reads. Record the enumeration as a query entry `{scope: "board_enumeration", q: "<company> <ats> board, <location>", results_seen: <total postings at location>}`:
   - Greenhouse: `boards.greenhouse.io/<token>`, `job-boards.greenhouse.io/<token>`, or `job-boards.eu.greenhouse.io/<token>` (JSON: `boards-api.greenhouse.io/v1/boards/<token>/jobs`; a redirect to `?error=true` = posting closed)
   - Lever: `jobs.lever.co/<token>` (JSON: `api.lever.co/v0/postings/<token>?mode=json`)
   - Ashby: `jobs.ashbyhq.com/<org>` (public posting API pattern: `api.ashbyhq.com/posting-api/job-board/<org>`)
   - Workable: `apply.workable.com/<account>` (also check Workable-hosted indexed pages via `jobs.workable.com`; enumerate public account pages when visible)
   - SmartRecruiters: `careers.smartrecruiters.com/<Company>` (JSON: `api.smartrecruiters.com/v1/companies/<Company>/postings`)
   - Workday: `<company>.wd<N>.myworkdayjobs.com/<site>` (public CXS endpoint pattern in the registry; often JS-heavy → also use search-engine indexing of postings or direct job-ID URLs)
   - **Extended ATS families — full patterns in `search-source-registry.yaml`**: Recruitee, Teamtailor, Personio, BambooHR, Breezy HR, Pinpoint, Rippling, iCIMS/JibeApply, Jobvite, SuccessFactors, Comeet. Same enumeration rule applies; most expose zero-auth per-tenant feeds (`/api/offers/`, `/jobs.rss`, `/postings.json`, `/careers/list`, `/api/jobs?page=N`).
   - **Branded-URL rule**: when a company has both a branded careers page and a raw ATS board, record the branded page as `source_url`; raw ATS URLs can return false 410/closed states when a branded page exists. Check the registry's `posting_expiry_signals` before persisting and set `posting_status` accordingly.
3. **Official / self-hosted careers site** — authoritative; when a company self-hosts or brands its careers site this is the **PRIMARY** source, not a fallback. A large share of employers publish **no** third-party ATS board — that absence is **expected**, never a `failed_capture` on its own. Detect it (official careers/source resolver first, ATS-slug probes and `site:` checks only as fallback), don't assume it from a company's name. Then consult the registry's `self_hosted_careers` and `major_company_careers` sections — **curated examples, not exhaustive lists**: if the company is listed, use its adapter (careers **search endpoint**, `posting_url` pattern, source kind, and `render` type are recorded there); if it is **not** listed, web-discover the official careers site and apply the same method. Method (identical either way): hit the careers **search endpoint** (its JSON API when one exists, else the listing page), **enumerate** postings for the target location(s), collect **each posting's own official URL** (`posting_url`), then **fetch every posting URL and parse its JD**, snapshotting to `targets/jobs/<job_id>.json`. Render handling comes from the registry's `render` field or is inferred at runtime: `server_html` / `json_api` careers sites need **no browser**; a client-rendered (`render: js`) site needs supported local browser tooling, otherwise record the connector/browser gap and continue other sources. Verify every registry URL/API at runtime before relying on it.
4. **Web/search-engine discovery** — `site:` queries against ATS domains and careers paths; role-family × location × company queries; finds direct posting URLs including job IDs. Instantiate the query shapes in the registry's `discovery_query_catalog` across ALL ATS domains above (not just Greenhouse/Ashby/Workable), plus multi-title-synonym queries per role family.
5. **LinkedIn Jobs — a mandatory discovery pass, run LAST, with a strict login contract.** Every search run must include at least one LinkedIn Jobs query (semantic search: target locations + role families + seed/neighbor companies) — it surfaces title families per-company scans miss. The runner records the navigation observation through `python scripts/probe_linkedin_jobs.py <project>` so this does not depend on the Codex agent's own sandbox. The contract, identical across skill/runner/prompts:
   - **Order**: Phases 1–3 and all non-login sources (ATS, careers, web discovery) complete BEFORE the LinkedIn pass. A blocked LinkedIn pass costs one source, never the run.
   - **Evidence before stopping**: declare LinkedIn blocked only after actually navigating to `linkedin.com/jobs` and observing the state; record the attempt in research-log (`{scope: "LinkedIn Jobs", attempt: "navigation", observed: authwall|signed_out|verification_prompt|connector_error:<msg>|jobs_page_ok}`). `jobs_page_ok` ⇒ proceed. Assuming blockage without navigating is a defect (a live run failed exactly this way).
   - **Partial progress then handoff**: when blockage is observed, finish everything else, write all five artifacts, persist validated rows, note the pending pass in coverage-audit.md — THEN stop with `APPROVAL_REQUIRED: LinkedIn login - open the browser connector, sign in to linkedin.com/jobs, then rerun`. The *user* performs the login; the agent never enters credentials. The rerun is **incremental**: LinkedIn pass only, appended to existing artifacts.
   - Store the canonical posting form `https://linkedin.com/jobs/view/<jobId>` as source_url; prefer capturing full JDs from the company/ATS source when one exists.
   - **A suspiciously low result count (≤1) is a coverage-failure signal to log and retry, never evidence of absence.**
6. **Trusted boards/portfolio pages** — the registry's `job_boards_and_aggregators` (RemoteOK, Remotive, We Work Remotely, Working Nomads, NoDesk, Arbeitnow, The Hub, Landing.jobs, 4 Day Week, HN Who's Hiring, YC/Work at a Startup, Wellfound, VC portfolio boards) plus regional boards matching the thesis. Same rule: **discover here, verify and capture at the company/ATS source** whenever one exists; feed rows alone enter the log as discovery signals, not captures.

### Fallback ladder (enforced)

A company may be marked `failed_capture` **only after ≥3 distinct source types failed**, recorded in order. JS-blocked official page → try ATS patterns → search-engine discovery of direct posting URLs → LinkedIn Jobs pass (per contract above) / board mirror for existence-evidence + partial capture with `posting_status: unknown`. "Static HTML was empty" alone is never a terminal state.

**Self-hosted-careers seeds (any seed with no third-party ATS board — registry `self_hosted_careers` entries and any others detected at runtime):** `failed_capture` requires that BOTH (a) the careers **JSON API / search endpoint** read (rendered with a browser for client-rendered sites) AND (b) a **web-discovery pass for direct `posting_url`s** came up empty. "No third-party ATS board", "static HTML was a JS shell", or "no browser installed" are **never** sufficient — a client-rendered site with no available browser means *install Playwright and render*, or capture via the mirror + web-discovery, not give up. The validator checks that a `failed_capture` seed actually recorded an official-careers/web-discovery attempt, not just an ATS miss.

### Deterministic helper usage

- Fetch/cache: `python scripts/fetch_url.py <url> --out <project>/data/<source>.json --json` for JSON APIs, or `--out <project>/data/<source>.html` for HTML. JSON stdout is a compact summary; read the saved file for full content. Do not stream full ATS feeds through stdout.
- URL/job id: `python scripts/normalize_job_url.py --url <url> --company <company> --title <title>`, forgiving positional `python scripts/normalize_job_url.py <url> <company> <title>`, or batch `python scripts/normalize_job_url.py --json <rows.json>`.
- Location ambiguity: before excluding a nearby non-exact city, run `python scripts/location_eligibility.py <location> --target <city> ...`. Same-metro non-exact results are `review` unless the project explicitly states exact-city only.
- Location-first capture: use the source's native location filters before worldwide enumeration when they exist; then judge every returned posting and log bulk roll-ups for out-of-family skips. Fetch full JD bodies only for location/role candidates or when lightweight listing metadata cannot support a decision.

## Phase 4 — Candidate Log → `targets/research-log.json`

Unchanged principle, tightened: **every candidate seen at any source enters the log** — `kept | skipped | failed_capture | pending_fallback` (plus company-level `no_postings_found` records when a source was clean but empty) with reason codes (`constraint_violation, seniority_mismatch, low_fit, duplicate, closed, off_focus, capture_error, run_interrupted`). Queries recorded with `results_seen` and, for board reads, a `company` field; a query with `results_seen ≤ 1` gets a note (coverage signal, see Phase 5). Rule of thumb stands: "N found, M saved" ⇒ log holds N entries.

- **`failed_capture` = attempts, not plans.** `fallbacks_attempted: [...]` lists ≥3 source types **actually tried**, matching the source-plan. Entries reading like intentions ("web discovery follow-up", "LinkedIn pending") are a validator FAIL. If the ladder could not finish because the run stopped (e.g. the LinkedIn `APPROVAL_REQUIRED` handoff), the decision is **`pending_fallback`** (reason `run_interrupted`) — legal only while the interruption is unresolved, listed in coverage-audit follow-ups, and converted to a real decision by the follow-up pass. A live run marked several name-brand self-hosted-careers seeds `failed_capture` with pending-style fallbacks exactly this way; that state is now unrepresentable as a pass.
- **Board-enumeration completeness (validator-enforced).** A `board_enumeration` query with `results_seen: N` obliges the log to account for ≥N entries for that company. In-family postings get per-candidate entries; clearly out-of-family blocks may be rolled up into a single bulk entry with a `count` and a collective reason (e.g. `{company: "<company>", title: "bulk: engineering/site-ops postings", decision: "skipped", reason: "off_focus", count: 180}`). Enumerating 77 and logging only 9 — as a live run once did for a seed company, silently dropping its Senior Manager roles — is the defect this check exists to catch.

### Parallel capture with subagents (plan centrally, capture in parallel, merge centrally)

When the universe holds **>~15 companies**, shard Phase 4 across parallel subagents instead of walking companies serially. The protocol keeps every auditability guarantee:

- **Lead agent owns Phases 1–3 and 5** — thesis, universe, and source plan are built centrally (global coherence), then sharded. Default sharding: one subagent per bucket; split buckets larger than ~12 companies, merge buckets smaller than ~4. **3–6 concurrent subagents** is the normal band (more parallelism than that mostly buys rate-limit noise); tiny universes (<10 companies) run inline with no subagents.
- **Each subagent receives**: the thesis, the registry rules (branded-URL rule, expiry signals, enumeration rule), its company shard from the source plan, and the per-company budgets. It executes only Phase 4 capture for its shard — same fallback ladder, same reason codes, same JD snapshots to `targets/jobs/<job_id>.json` (job_ids are URL-derived, so shard writes don't collide).
- **Shard output, not shared state**: each subagent writes `targets/research-log.parts/<shard>.json` (same schema as research-log entries) and touches **nothing else** — no `research-log.json`, no DB writes, no store scripts, no tracker. Parts files are kept after the run as audit trail.
- **Subagents never do**: the LinkedIn Jobs pass (login contract — lead only, still LAST, after all shards return), user-approval interactions (an `APPROVAL_REQUIRED` condition inside a shard is returned to the lead as a shard result, and the lead issues the single handoff), or persistence (Persisting-rows steps run once, in the lead, over the merged log).
- **Lead/runner merges**: concatenate parts into `targets/research-log.json` with `python scripts/merge_research_parts.py <project>`, dedupe cross-shard by canonical URL keeping the richer entry and logging the loser as `duplicate`, then run/record the LinkedIn pass with `python scripts/probe_linkedin_jobs.py <project>`, generate the mechanical audit scaffold with `python scripts/generate_coverage_audit.py <project>`, then validate (`validate_research_artifacts.py`) and persist rows with `python scripts/persist_job_rows.py <rows.json> --project <project>`. A subagent that dies or returns malformed parts = rerun that shard only; report it, never silently absorb it.
- Stopping criteria and budgets apply **per shard**; the lead's coverage-audit reads the merged log plus each shard's own thin-bucket notes.

### Stopping criteria (coverage goals, not quotas)

Stop a bucket/company when ANY: (a) every universe company has ≥1 successful source capture or a completed fallback ladder; (b) every role-family × location combination ran ≥1 discovery query; (c) marginal yield decays — last 3 queries produced <2 new candidates each. Budgets (defaults, overridable in thesis): ≤4 queries per company, ≤60 total captures per run. Running out of budget is *reportable state*, not silent truncation.

## Phase 5 — Coverage Self-Audit → `targets/coverage-audit.md`

Before reporting to the user, the agent audits itself:

- Which buckets/companies are **thin** (0–1 candidates) and why; which sources failed and where the fallback ladder ended.
- Remaining `failed_capture` list with next-step recommendation per item.
- Low-coverage signals (≤1-result queries, empty ATS tokens) and whether they were retried.
- Prioritized follow-up search list ("what I would search next with more budget").

The user-facing summary quotes this audit — never report "done" while the audit lists unexplained thin buckets.
