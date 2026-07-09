"""Provider-neutral prompt composition for headless workflow runs.

The skills are markdown instructions and the validators are plain scripts, so the
same prompt drives any capable local agent CLI backend (Codex CLI, OpenCode, etc.).
The public runtime is local-output only; the runner rejects external-action events.
"""

from __future__ import annotations

import json

from ..paths import repo_root

RUN_CONTRACT = """
HEADLESS RUN CONTRACT (rolescout CLI):
- Work ONLY on the search project at: {project}
  (env RECRUITING_PROJECT_DIR is set; scripts resolve it automatically.)
- Follow AGENTS.md and the relevant skill exactly; run validators before writes.
- Do not read external helper skills. Use only the RoleScout skill text included
  in this prompt plus the deterministic `scripts/` commands named here.
- NEVER perform or request an external action. Do not submit applications, send
  messages, save LinkedIn edits, upload files, schedule events, accept terms, or
  share sensitive data. If the task appears to require one, produce local
  instructions or tracker notes only.
- Task focus: {task}
"""

PROFILE_RUN_CONTRACT = """
HEADLESS PROFILE-INTAKE CONTRACT (rolescout CLI):
- Work ONLY on the candidate profile directory at: {profile_dir}
- This workflow is person-scoped. Do not create or require a search project,
  job_list, focused-jobs.json, strategy artifacts, resumes, LinkedIn review
  packets, interview packs, or application tracker rows.
- Produce or refresh `candidate-profile.md` and `evidence-map.md` in that
  profile directory from the available local materials and accepted LinkedIn
  current-source handoff when present.
- NEVER invent employers, dates, degrees, credentials, metrics, compensation,
  work authorization, or preferences. Unsupported facts become Open Questions.
- Task focus: {task}
"""


def workflow_prompt(workflow: str, context: dict) -> str:
    root = repo_root()
    skills = context.get("skills", [])
    skill_text = "\n\n".join(
        (root / ".agents" / "skills" / s / "SKILL.md").read_text(encoding="utf-8")
        for s in skills if (root / ".agents" / "skills" / s / "SKILL.md").exists())
    extras = ""
    if context.get("linkedin_url"):
        extras += (f"\n- Candidate's LinkedIn profile URL: {context['linkedin_url']}"
                   "\n  (read-only current profile source target. For LinkedIn review "
                   "runs, fresh capture is handled by the RoleScout runner before the "
                   "agent starts; the user logs in manually in that visible local browser "
                   "if needed. NEVER type credentials, save edits, post, upload, "
                   "or message.)")
    if context.get("targets"):
        extras += ("\n\n## Declared project targets (user-set; current as of this run)\n"
                   + context["targets"]
                   + "\n(Target companies are SEEDS — examples of a market map, not only "
                   "literal targets. Infer each seed's archetype and expand it into close "
                   "peers, but keep default expansion conservative: no arbitrary company "
                   "count target, no loose profile-history domain expansion, and no weak "
                   "adjacency without a clear rationale. Excludes and locations are hard "
                   "constraints — violations need an explicit override note.)")
    if context.get("instructions"):
        extras += ("\n\n## User's standing instructions\n" + context["instructions"]
                   + "\n(These never override local-only or truthfulness rules.)")
    if context.get("run_intent") and context["run_intent"].get("raw_instruction"):
        extras += ("\n\n## Run-level custom instruction\n"
                   "- This instruction applies to this run only. Treat it as higher "
                   "priority than broad project preferences, while preserving project "
                   "hard constraints such as target locations, negatives, privacy, "
                   "truthfulness, local-only execution, and approval boundaries.\n"
                   "- Do not permanently rewrite project targets unless the user "
                   "explicitly asks.\n"
                   "```json\n"
                   f"{json.dumps(context['run_intent'], indent=2, ensure_ascii=False)}\n"
                   "```")
    if context.get("profile_ready") is False:
        extras += ("\n\n## Candidate profile status\n"
                   "- candidate-profile.md is not available yet.\n"
                   "- For search, score, and apply workflows, proceed from declared "
                   "project targets, LinkedIn/source hints, and user instructions only.\n"
                   "- Do not invent candidate credentials, employers, dates, achievements, "
                   "metrics, immigration facts, compensation facts, or preferences. Label "
                   "profile-dependent fit, grouping, and scoring as provisional until "
                   "`rolescout run profile-intake --person <person>` builds "
                   "candidate-profile.md and evidence-map.md.\n"
                   "- Focused prep workflows must not build the profile. If this is a "
                   "prep workflow, stop and tell the user to run profile-intake first.")
    if context.get("profile_stale"):
        extras += ("\n\n## PROFILE REBUILD REQUIRED FIRST (hard prerequisite)\n"
                   f"- `{context['profile_stale']}` in the profile folder is NEWER than "
                   "candidate-profile.md — the profile and evidence map are STALE.\n"
                   "- BEFORE any tailoring or scoring, rebuild BOTH candidate-profile.md "
                   "and evidence-map.md with `rolescout run profile-intake --person "
                   "<person>` from the updated materials per the "
                   "candidate-profile-builder refresh semantics (treat the newest resume "
                   "as the current baseline; keep stable EV- IDs; mark superseded claims; "
                   "bump the Updated stamp).\n"
                   "- Then regenerate downstream artifacts from the REFRESHED evidence "
                   "map — never reuse content that only exists in previous run outputs.\n"
                   "- Search/score may continue provisionally; focused prep must stop "
                   "until profile-intake has refreshed the profile.")
    if workflow == "profile-intake":
        extras += ("\n\n## Profile intake lane\n"
                   "- Build or refresh only person-scoped artifacts in the profile dir: "
                   "`candidate-profile.md`, `evidence-map.md`, and a baseline "
                   "`linkedin-analysis.md` when `linkedin-current.md` exists.\n"
                   "- Use `linkedin-current.md` only when it exists and contains visible "
                   "profile content; a bare LinkedIn URL is a pointer, not evidence.\n"
                   "- Do not create a project if none exists. Do not touch job search, "
                   "focused prep, applications, or tracker artifacts.\n"
                   "- If LinkedIn content is missing, continue from resume/materials and "
                   "list LinkedIn-dependent facts as Open Questions.")
    if workflow == "search" and context.get("search_phase"):
        phase = context["search_phase"]
        if phase == "plan":
            extras += ("\n\n## Search orchestration phase: lead plan\n"
                       "- Build ONLY Phases 1-3: `targets/opportunity-thesis.md`, "
                       "`targets/company-universe.json`, and `targets/source-plan.json`.\n"
                       "- Use `python scripts/resolve_company_sources.py <company> --json` "
                       "as the deterministic official-first source resolver input when "
                       "building per-company source plans.\n"
                       "- If the run-level custom instruction names requested companies "
                       "or titles, treat this as a targeted incremental search: keep "
                       "project hard constraints, ensure requested companies have source "
                       "plans, and do not broaden into an unrelated full-market refresh "
                       "unless the user asked for it.\n"
                       "- Do not capture postings, do not write `research-log.json`, do not "
                       "persist rows, do not run LinkedIn Jobs, and do not score. The "
                       "RoleScout runner will spawn capture shards after this phase.")
        elif phase == "plan_repair":
            extras += ("\n\n## Search orchestration phase: plan repair\n"
                       "- The runner rejected the plan as poorly scoped or mechanically "
                       "incomplete. Revise ONLY `targets/company-universe.json` and "
                       "`targets/source-plan.json` as needed; keep the opportunity thesis "
                       "unless it directly causes the defect.\n"
                       "- Adjacent company expansion is default behavior. A user-named "
                       "company is a seed/archetype example, not the whole search. Add "
                       "close competitors, same-talent-pool employers, tightly adjacent "
                       "product/category companies, ecosystem partners, and location-"
                       "relevant employers when the relationship is clear. Default "
                       "search is conservative: remove weak adjacency and profile-history "
                       "domain expansion unless explicitly requested. Do not create a "
                       "separate mode for this.\n"
                       "- Every universe company must appear in source-plan with "
                       "non-LinkedIn sources. Do not capture postings, do not write "
                       "`research-log.json`, and do not persist rows.\n"
                       "- Rerun/consider this gate output while repairing:\n"
                       "```text\n"
                       f"{context.get('search_plan_gate', '')}\n"
                       "```")
        elif phase in {"capture_shard", "capture_repair"}:
            shard = json.dumps(context.get("search_shard", {}), indent=2, ensure_ascii=False)
            repair_note = ""
            if phase == "capture_repair":
                repair_note = ("\n- This is a targeted repair pass after the coverage gate "
                               "found unfinished companies. Do NOT repeat the same blocked "
                               "source first. If a Google/official careers API was DNS-blocked "
                               "in shell, move to direct official results pages, browser-rendered "
                               "career search if available, direct posting URL web discovery, "
                               "verified ATS/mirror sources, and then the next source family. "
                               "After recording the unresolved state for one company, continue "
                               "to the next assigned company.\n"
                               "- Coverage gate context:\n"
                               "```text\n"
                               f"{context.get('search_coverage_gate', '')}\n"
                               "```\n")
            extras += ("\n\n## Search orchestration phase: "
                       f"{'capture repair' if phase == 'capture_repair' else 'capture shard'}\n"
                       "- Operate ONLY on the assigned non-LinkedIn company shard below.\n"
                       "- If run-level requested_titles are present, capture/include "
                       "matching postings for the assigned company even when title/fit "
                       "would normally be low; project location and exclusion constraints "
                       "still apply.\n"
                       f"- Write ONLY `{context.get('search_part_path')}` and JD snapshots "
                       "under `targets/jobs/`. Do not write `targets/research-log.json`, "
                       "coverage audit, source plan, job_list rows, tracker rows, profile "
                       "files, or any focused prep artifact.\n"
                       "- Do not run LinkedIn Jobs, do not ask the user for approval, and "
                       "do not persist rows. The lead/runner merges and persists once.\n"
                       "- Use deterministic scripts for fetch/source mechanics. Do not "
                       "write one-off Python for source resolution, fetch, browser probe, "
                       "merge, or persistence. If a needed parser/adapter is missing, "
                       "record the gap as an unresolved fallback state and continue the "
                       "deep-search ladder through official careers, verified ATS, direct "
                       "posting URL web discovery, and supported browser rendering where "
                       "available. Raw fetch failures, DNS errors, JS shells, and missing "
                       "browser tooling are NEVER terminal `failed_capture` evidence by "
                       "themselves.\n"
                       "- Direct URL discipline: listing/search URLs are query evidence "
                       "only. For every `kept` candidate, resolve the listing/API/card "
                       "item to the posting's own URL, fetch or render that posting, "
                       "canonicalize with `scripts/normalize_job_url.py`, and write a "
                       "JD snapshot under `targets/jobs/` with full posting text in "
                       "`raw_text`, `jd_text`, `job_description`, `description`, "
                       "`content`, `body_text`, or `html` (not only a summary). "
                       "Google Careers rows must use "
                       "`/about/careers/applications/jobs/results/<posting-id>-<slug>`, "
                       "not `/jobs/results?q=...`; ServiceNow rows must use "
                       "`/jobs/<posting-id>/<slug>`, not `/jobs/`. If a user-forced or "
                       "interesting role cannot be verified to a direct posting URL and "
                       "JD, record `pending_fallback` instead of `kept` and do not emit "
                       "placeholder `Not verified` requirements.\n"
                       "- `failed_capture` is legal only after the shard actually tries "
                       ">=3 distinct source types and none ended in an unresolved blocker. "
                       "If the ladder is interrupted by DNS/network/browser/connector "
                       "limits or still needs another pass, write `pending_fallback` with "
                       "reason `run_interrupted` and explain the exact next source to try.\n"
                       "- A blocker at one company must not stop the shard. Record that "
                       "company's state and continue to the next assigned company.\n"
                       + repair_note +
                       "- Use only the shard-minimal skill context provided here; do not "
                       "do strategy scoring or target-priority modeling inside a capture "
                       "shard.\n"
                       "Assigned shard JSON:\n"
                       f"```json\n{shard}\n```")
        elif phase == "finalize":
            failed_shards = context.get("search_failed_shards") or []
            partial_reasons = context.get("search_partial_reasons") or []
            partial_note = ""
            if failed_shards or partial_reasons:
                partial_note = ("\n- PARTIAL RUN CONTEXT: the runner recorded failed/"
                                "skipped sub-work. You MUST still persist valid kept "
                                "rows, and you MUST list the failed scope in "
                                "`targets/coverage-audit.md` and the user summary. "
                                f"failed_shards={json.dumps(failed_shards, ensure_ascii=False)}; "
                                f"partial_reasons={json.dumps(partial_reasons, ensure_ascii=False)}")
            extras += ("\n\n## Search orchestration phase: finalize\n"
                       "- The runner has already merged shard parts with "
                       "`scripts/merge_research_parts.py` and attempted the runner-owned "
                       "LinkedIn Jobs probe with `scripts/probe_linkedin_jobs.py`.\n"
                       "- Read `targets/research-log.json`; honor any LinkedIn Jobs "
                       "`observed` value already recorded there. Do not spawn browser "
                       "automation from inside Codex and do not write one-off browser code.\n"
                       "- Finish `targets/coverage-audit.md`, prepare validated job row "
                       "JSON under `<project>/data/`, then persist with "
                       "`python scripts/persist_job_rows.py <rows.json> --project <project>`.\n"
                       "- Persist only verified rows with a direct posting URL and JD "
                       "snapshot. Do not include listing/search URLs or placeholder "
                       "`Not verified` requirement text in job_list rows; keep those as "
                       "`pending_fallback` coverage gaps until a follow-up source pass "
                       "resolves the posting URL/JD.\n"
                       "- If `coverage-audit.md` is missing or stale, run "
                       "`python scripts/generate_coverage_audit.py <project>` before "
                       "adding any human-readable final notes.\n"
                       "- Run `python scripts/validate_research_artifacts.py <project>` "
                       "before reporting. Then run "
                       "`python scripts/analyze_search_coverage.py <project>`; if it "
                       "returns PARTIAL/BLOCKED, do not describe the run as complete. "
                       "Persist valid rows, summarize the coverage gap, and name the "
                       "next concrete fallback/source pass."
                       + partial_note)
        elif phase == "legacy":
            extras += ("\n\n## Search orchestration phase: legacy fallback\n"
                       "- The lead phase did not produce a usable source plan. Run the "
                       "standard end-to-end search, but still use deterministic scripts "
                       "for source resolution, merge, LinkedIn probe, and row persistence.")
    extras += ("\n\n## Research tooling\n"
               "- For plain public JSON/HTML fetches use `python scripts/fetch_url.py "
               "<url> --out <project>/data/source-cache.json --json` for JSON APIs or "
               "`python scripts/fetch_url.py <url> --out <project>/data/source-cache.html` "
               "for HTML. The script prints only a bounded summary for JSON; read the "
               "`--out` file when full content is needed. Do NOT pipe huge ATS JSON "
               "through stdout, and do NOT assume `requests` is installed — RoleScout "
               "ships zero runtime dependencies.\n"
               "- Do not write one-off Python for common RoleScout mechanics. Use the "
               "product scripts instead: `scripts/resolve_company_sources.py` for "
               "official-first source planning, `scripts/build_location_search_urls.py` "
               "for registry-driven location-filtered careers URL candidates, "
               "`scripts/fetch_url.py` for public "
               "fetches, `scripts/merge_research_parts.py` for shard merge, "
               "`scripts/generate_coverage_audit.py` for deterministic coverage "
               "audit scaffolding, "
               "`scripts/probe_linkedin_jobs.py` for the runner-owned LinkedIn Jobs "
               "observation, and `scripts/persist_job_rows.py` for normalize/validate/"
               "upsert. Use `scripts/analyze_search_coverage.py` to classify whether "
               "the search is complete, partial, or blocked after capture/finalize. "
               "Use `scripts/validate_linkedin_review.py` and "
               "`scripts/build_interview_context.py` plus "
               "`scripts/validate_interview_prep.py` for prep-interview context "
               "and artifact quality/structure. "
               "Use `scripts/validate_application_packets.py` for apply packet "
               "structure and tracker linkage. "
               "Use `scripts/render_docx_gate.py` before attempting DOCX render QA. "
               "If a deterministic adapter is missing, do not generate throwaway "
               "Python inside the run; record the unresolved fallback state, continue "
               "with other trusted source families, and let the coverage gate report "
               "partial rather than converting a tooling gap into `failed_capture`.\n"
               "- For URL canonicalization/job IDs, use "
               "`python scripts/normalize_job_url.py --url <url> --company <company> "
               "--title <title>` or the forgiving positional form "
               "`python scripts/normalize_job_url.py <url> <company> <title>`. For row "
               "sets, prefer `python scripts/normalize_job_url.py --json <rows.json>`.\n"
               "- For ambiguous nearby locations, use "
               "`python scripts/location_eligibility.py <location> --target <city> ...` "
               "before excluding. Same-metro but non-exact cities should be `review` "
               "unless the project explicitly says exact-city only.\n"
               "- Encoding discipline (critical on Windows): every Python file/subprocess "
               "read or write MUST pass `encoding=\"utf-8\"`; add `errors=\"replace\"` when "
               "reading fetched pages, user files, or OS-command output. Never rely on the "
               "platform default — on Windows it is the locale's legacy ANSI codepage "
               "(cp932 Japanese, cp936/cp950 Chinese, cp949 Korean, cp1252 Western/Indian), "
               "which writes artifacts the validators/UI then fail to read "
               "(UnicodeDecodeError). All "
               "RoleScout artifacts (row JSON, JD snapshots, logs, research-log) are UTF-8; "
               "write them the same way.\n"
               "- Scratch/intermediate files (row JSON for validate/upsert, ratings JSON, "
               "merged shard logs) MUST be written INSIDE the active project directory — put "
               "them under `<project>/data/` (guaranteed writable) and delete them after the "
               "upsert. NEVER write to `/tmp`, `C:\\tmp`, `~`, or any absolute OS path: on "
               "Windows `/tmp` resolves to `C:\\tmp`, which usually does not exist and is not "
               "writable (FileNotFoundError / PermissionError), and a persist step that "
               "fails there silently drops the rows you captured.\n"
               "- Not every employer uses a third-party ATS; many self-host their careers "
               "site and publish no ATS board at all — that absence is expected, not a miss, "
               "and you detect it (official careers/source resolver first, ATS-slug probes "
               "only as fallback), never assume it from a "
               "company's name. When there is no ATS board, treat the official careers site "
               "as a first-class source: consult `references/search-source-registry.yaml` -> "
               "`self_hosted_careers` (a curated set of examples) and use the matching "
               "adapter (registry-provided location search URL template when present; "
               "use `scripts/build_location_search_urls.py --company <company> "
               "--location <target> --json` rather than hand-formatting values), "
               "careers JSON API / search endpoint, then fetch EACH posting's own "
               "URL and parse the JD); if the company is NOT in that registry, web-discover "
               "its careers site and apply the same method. Never mark such a seed failed "
               "just because it has no ATS board or its page is a JS shell.\n"
               "- If a careers page renders its listings only via client-side JavaScript, do "
               "not settle for empty raw HTML. Use supported local browser tooling when "
               "available; if it is not available in the current environment, record the "
               "connector/browser gap as `pending_fallback`, continue the rest of the "
               "source plan, and do not mark the company `failed_capture` solely from "
               "the JS shell. "
               "Server-rendered or JSON-API careers pages need no browser. Reading public "
               "pages is allowed, but never enter credentials, save profile edits, submit "
               "applications, schedule events, or send messages for the user.\n"
               "- Location filter discipline: employer career sites use inconsistent "
               "location labels. A zero-result or ignored-location page is a format "
               "suspicion, not absence evidence. For registry sources with "
               "`location_search_url_template`, generate several candidate URLs with "
               "`scripts/build_location_search_urls.py`; try the primary multi-location "
               "URL first, then single-location/canonical/raw variants when the result "
               "count looks implausibly low. Record which variant was observed in "
               "research-log queries.\n"
               "- Before any job_list write, normalize `location`: use semicolon-separated "
               "location tags for multi-location roles; `Singapore` stays `Singapore`; "
               "other city locations use `{City}, {Country}`; use `USA` for the United "
               "States. Examples: `SG - Singapore` -> `Singapore`; `Singapore, , "
               "Singapore` -> `Singapore`; `US - San Francisco` -> `San Francisco, "
               "USA`; `Seoul; Singapore` -> `Singapore; Seoul, South Korea`. The "
               "upsert pipeline enforces this, but the agent should write normalized "
               "values directly.")
    if context.get("focused_jobs") is not None:
        fj = context["focused_jobs"]
        if fj:
            lines = "\n".join(f"- {j['job_id']} | {j['company']} | {j['title']}"
                               f"{' | group:' + j['job_group'] if j.get('job_group') else ''}"
                               for j in fj)
            extras += ("\n\n## FOCUS SCOPE (hard boundary)\n"
                       "Operate ONLY on these focus-registered positions - never widen "
                       "scope to the rest of job_list without the user explicitly asking:\n"
                       + lines)
        else:
            extras += ("\n\n## FOCUS SCOPE\n"
                       "No positions are focus-registered. STOP and tell the user to star "
                       "positions in the list UI (focused-jobs.json) or name positions "
                       "explicitly - do not run against the whole job_list.")

    if workflow == "prep-linkedin":
        extras += ("\n\n## CURRENT LINKEDIN CONTENT GATE\n"
                   "- This workflow requires the user's current LinkedIn profile "
                   "content before any `linkedin/<group>/linkedin-review.md` write.\n"
                   "- Do not generate LinkedIn reviews from local profile/resume/evidence alone; "
                   "those files may support proposed edits only after the current LinkedIn "
                   "surface has been captured.\n"
                   "- Fresh LinkedIn capture has already been completed by the runner "
                   "before this agent prompt. Do not perform capture again.\n"
                   f"- Read the fresh handoff file: `{context.get('linkedin_source_path') or 'profiles/<person>/linkedin-current.md'}`.\n"
                   "- Treat that file as the only current LinkedIn source for analysis, "
                   "scoring, and group-specific suggestions.\n"
                   "- Do not run browser automation, do not open LinkedIn, do not run "
                   "local capture helpers, do not use the Codex Chrome Extension, and "
                   "do not use chrome-devtools MCP.\n"
                   "- If the handoff file is missing or empty, STOP before writing review "
                   "artifacts and print `ERROR: LinkedIn capture handoff missing after "
                   "runner capture`.\n"
                   "- Never type credentials, save LinkedIn edits, post, upload, message, "
                   "or continue with a local-evidence proxy review.")

    if workflow == "score":
        extras += ("\n\n## Score runs (grouping + scoring only)\n"
                   "- This is NOT a discovery run: do not build a company universe or "
                   "search for new openings. Work the existing job_list.\n"
                   "- ENRICH-THEN-SCORE: rows missing jd_summary/must_have_requirements "
                   "(e.g. manually added URL-only rows) must be enriched FIRST - fetch "
                   "each row's posting URL, capture JD fields, snapshot to targets/jobs/, "
                   "upsert - then group and score. Scoring a row with no JD content is "
                   "fabrication.\n"
                   "- Order: focused positions first (data/focused-jobs.json), then the "
                   "rest of the list. Dead URLs: mark posting_status per what you "
                   "observe and score only on available evidence, flagging the gap.")

    if workflow == "prep-interview":
        extras += ("\n\n## Prep-interview industry thesis contract\n"
                   "- The runner has already attempted "
                   "`python scripts/build_interview_context.py <project>`; read "
                   "`interviews/interview-context.json` before drafting.\n"
                   "- For each focused position, web-search from the scaffolded "
                   "`web_search_queries` using the concrete {company} and "
                   "{business arm}/role-title terms. Fill an industry thesis in "
                   "your working notes before writing `## The Whys`.\n"
                   "- Industry means the company/product market, customer/user "
                   "system, business model, and current market tension. It is not "
                   "the job function, role family, or job_group.\n"
                   "- `Why this industry`, `Why this company`, and `Why this "
                   "position` must each use at least one position-specific signal "
                   "from web research, JD context, glossary/news, or official "
                   "company/product context.\n"
                   "- Run `python scripts/validate_interview_prep.py <project>`. "
                   "If it returns `QUALITY`, retry The Whys from the industry "
                   "thesis instead of deleting or withholding the artifacts. If a "
                   "bounded retry still leaves quality issues, keep the artifacts "
                   "and report the remaining scope as partial.")
        if context.get("prep_interview_quality_retry"):
            extras += ("\n\n## Prep-interview quality retry\n"
                       "The previous prep-interview validator returned retryable "
                       "QUALITY issues. Edit only the flagged `## The Whys` rows "
                       "and any necessary source/glossary support; keep valid "
                       "story-bank and prep sections intact. Validator output:\n"
                       "```text\n"
                       f"{context['prep_interview_quality_retry']}\n"
                       "```")

    if workflow == "search":
        extras += ("\n\n## Job sources (search runs)\n"
                   "- RUN ORDER IS FIXED: complete Phases 1-3 (opportunity thesis, company "
                   "universe, source plan) and ALL non-login sources (ATS boards, official "
                   "careers, web discovery) BEFORE the LinkedIn Jobs pass. LinkedIn is the "
                   "LAST source pass. Never abort the whole run because LinkedIn is blocked - "
                   "a blocked LinkedIn pass costs one source, not the run.\n"
                   "- Before building the company universe and source plan, read "
                   "`references/search-source-registry.yaml`. Use relevant curated "
                   "company sets/direct careers URLs as seeds when they match the thesis; "
                   "verify every registry URL at runtime before relying on it.\n"
                   "- LinkedIn Jobs pass is mandatory for every search run. A search run is "
                   "not complete until the latest `targets/research-log.json` run contains "
                   "at least one query with `scope: \"LinkedIn Jobs\"` or a LinkedIn Jobs "
                   "candidate URL.\n"
                   "- EVIDENCE BEFORE STOPPING: you may only declare LinkedIn blocked after "
                   "actually navigating to linkedin.com/jobs in the connected browser and "
                   "observing the state. Record the attempt in research-log as a query "
                   "entry: {scope: \"LinkedIn Jobs\", attempt: \"navigation\", observed: "
                   "\"authwall\" | \"signed_out\" | \"verification_prompt\" | "
                   "\"connector_error: <msg>\" | \"jobs_page_ok\"}. If observed is "
                   "jobs_page_ok, PROCEED with the pass - assuming blockage without "
                   "navigating is a defect (a live run failed exactly this way: connector "
                   "connected, user logged in, agent stopped anyway).\n"
                   "- ATS boards (Greenhouse including job-boards.greenhouse.io, Lever, "
                   "Ashby, Workable, SmartRecruiters, Workday): ENUMERATE the "
                   "board with source-supported target-location filters first, then judge "
                   "every posting returned and log each skip. Do not fetch full worldwide "
                   "JD bodies unless the source lacks usable filters; use lightweight "
                   "listing metadata and bulk roll-ups for out-of-location/out-of-family "
                   "postings. Never keyword-filter a board at the source - that hides "
                   "postings from the log entirely. Record a board_enumeration query with "
                   "the total count per seed company.\n"
                   "- Only when blockage is OBSERVED: finish everything else first (write "
                   "all artifacts, persist validated rows, and note the pending LinkedIn "
                   "pass in coverage-audit.md). Tell the user what manual login or rerun "
                   "step is needed. Do not emit external-action events or attempt login.\n"
                   "- Company careers pages/ATS boards are still required as the primary "
                   "canonical source, but LinkedIn Jobs semantic search must run too "
                   "(location filter from target locations; keywords from company seeds, "
                   "neighbor companies, focus role, and title variants). LinkedIn surfaces "
                   "title families that per-company scans miss (partnerships/BD/solutions) "
                   "— log every seen result with a decision like any other candidate.\n"
                   "- MARKET-MAP EXPANSION (before scanning individual companies): treat each "
                   "seed company as an EXAMPLE of a market map, not only a literal target. For "
                   "each seed, infer its archetype (scale/maturity, business model, product "
                   "category, talent pool, location market), then expand that archetype into "
                   "close peers via competitors, same-talent-pool employers, tightly adjacent "
                   "product categories, and location-relevant employers. Fix the relationship "
                   "TYPES; never hardcode company names. Name-brand seeds (large platforms, "
                   "hyperscalers, frontier labs, category leaders) should not collapse to "
                   "literal-only search, but default breadth is conservative: roughly 1-3 "
                   "strong adjacent employers per seed when the relationship is clear, fewer "
                   "when the thesis is narrow, and more only when the user asks for broad "
                   "search or `expansion_mode: broad` is explicit. Candidate profile/history "
                   "is mainly for fit scoring and role interpretation; it may add at most "
                   "1-2 exceptionally close employers, not whole new domains. Then run one "
                   "omissions self-critique before saving — 'given these seeds, role families, "
                   "and locations, what obvious close peer employers are missing?' — and add "
                   "each with a rationale or exclude it with a reason (noted in "
                   "coverage-audit.md). Weak adjacency is excluded by default unless "
                   "explicitly requested: for an AI/platform/SEA superapp seed set, consumer "
                   "community apps, music platforms, general e-commerce retailers, travel "
                   "apps, and gaming publishers need a very specific seed-level rationale. "
                   "FAANG-like platforms and close social-platform peers may still qualify "
                   "when tied directly to the seed thesis.\n"
                   "- LinkedIn URL discipline: store the canonical posting form "
                   "https://linkedin.com/jobs/view/<jobId> as source_url (NEVER a "
                   "/jobs/search-results/ page URL; strip refId/trackingId/eBP/origin junk). "
                   "When the posting links out to the company's own ATS page, put that in "
                   "job_page_url and dedupe against rows already found via the company scan.\n"
                   "- Compliance: browse at human pace inside the user's own logged-in "
                   "session, only what the user could see themselves; no bulk "
                   "scraping/exports; respect site terms.")
    if workflow == "profile-intake":
        contract = PROFILE_RUN_CONTRACT.format(
            profile_dir=context.get("profile_dir") or context["project"],
            task=context.get("task") or "(default per skill)")
    else:
        contract = RUN_CONTRACT.format(project=context["project"],
                                       task=context.get("task") or "(default per skill)")
    return (
        f"Execute the '{workflow}' recruiting workflow using the skill(s) below.\n"
        + contract
        + extras
        + "\n--- SKILLS ---\n" + skill_text)
