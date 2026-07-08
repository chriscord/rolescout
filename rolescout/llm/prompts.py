"""Provider-neutral prompt composition for headless workflow runs.

The skills are markdown instructions and the validators are plain scripts, so the
same prompt drives any capable local agent CLI backend (Codex CLI, OpenCode, etc.).
The public runtime is local-output only; the runner rejects external-action events.
"""

from __future__ import annotations

from ..paths import repo_root

RUN_CONTRACT = """
HEADLESS RUN CONTRACT (rolescout CLI):
- Work ONLY on the search project at: {project}
  (env RECRUITING_PROJECT_DIR is set; scripts resolve it automatically.)
- Follow AGENTS.md and the relevant skill exactly; run validators before writes.
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
                   "literal targets. Infer each seed's archetype and expand it into peers; "
                   "a name-brand seed like a large platform or frontier lab stands for its "
                   "whole peer set, never just itself. Excludes and locations are hard "
                   "constraints — violations need an explicit override note.)")
    if context.get("instructions"):
        extras += ("\n\n## User's standing instructions\n" + context["instructions"]
                   + "\n(These never override local-only or truthfulness rules.)")
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
    extras += ("\n\n## Research tooling\n"
               "- For plain public JSON/HTML fetches use `python scripts/fetch_url.py "
               "<url>` (stdlib urllib, UTF-8 output). Do NOT assume `requests` is "
               "installed — RoleScout ships zero runtime dependencies. Add `--json` to "
               "pretty-print API responses. You do not need to write your own fetch "
               "script.\n"
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
               "and you detect it (ATS-slug probes come back empty), never assume it from a "
               "company's name. When there is no ATS board, treat the official careers site "
               "as a first-class source: consult `references/search-source-registry.yaml` -> "
               "`self_hosted_careers` (a curated set of examples) and use the matching "
               "adapter (careers JSON API / search endpoint, then fetch EACH posting's own "
               "URL and parse the JD); if the company is NOT in that registry, web-discover "
               "its careers site and apply the same method. Never mark such a seed failed "
               "just because it has no ATS board or its page is a JS shell.\n"
               "- If a careers page renders its listings only via client-side JavaScript, do "
               "not settle for empty raw HTML. Use a browser runtime (Chrome DevTools or "
               "Playwright); if neither is installed, INSTALL Playwright: `python -m pip "
               "install playwright && python -m playwright install chromium`, then render. "
               "Server-rendered or JSON-API careers pages need no browser. Reading public "
               "pages is allowed, but never enter credentials, save profile edits, submit "
               "applications, schedule events, or send messages for the user.\n"
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
                   "full board for target locations and judge every posting, logging each "
                   "skip. Never keyword-filter a board at the source - that hides postings "
                   "from the log entirely. Record a board_enumeration query with the total "
                   "count per seed company.\n"
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
                   "peers via competitors, same-talent-pool employers, adjacent product "
                   "categories, and location-relevant employers. Fix the relationship TYPES; "
                   "never hardcode company names. Name-brand seeds (large platforms, "
                   "hyperscalers, frontier labs, category leaders) are the ones most often "
                   "under-expanded — a universe of just the seeds plus one or two neighbors is "
                   "a coverage defect. Per-archetype floor: if a seed's archetype yields fewer "
                   "than ~5 searched companies, expand it or record in company-universe.json "
                   "why expansion is inappropriate (excluded entry or a bucket expansion_note). "
                   "Then run one omissions self-critique before saving — 'given these seeds, "
                   "role families, and locations, what obvious peer employers are missing?' — "
                   "and add each with a rationale or exclude it with a reason (noted in "
                   "coverage-audit.md). (Example only: a Singapore/APAC strategy/product/BD "
                   "search typically surfaces adjacent AI, data/cloud, consumer-platform, "
                   "marketplace, media, and gaming employers — an illustration of archetype "
                   "expansion, not a fixed list.)\n"
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
