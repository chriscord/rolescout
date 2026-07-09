#!/usr/bin/env python3
"""Mechanical validation of search-workflow artifacts (references/search-workflow.md).

Usage: python3 scripts/validate_research_artifacts.py [projects/<person>--<focus>]
       (defaults to the active project)

Checks ONLY mechanics — never which companies should have been searched:
  1. All five artifacts exist (opportunity-thesis.md, company-universe.json,
     source-plan.json, research-log.json, coverage-audit.md).
  2. company-universe: every company has non-empty rationale; every bucket has
     why_relevant; excluded entries have reasons; seed flag present.
  3. source-plan: every universe company appears; every source has type+status;
     statuses valid.
  4. research-log: every candidate has decision in {kept,skipped,failed_capture,
     no_postings_found,pending_fallback} and non-empty reason; failed_capture
     lists >=3 fallbacks_attempted that are ATTEMPTS (entries reading like plans
     — "pending"/"follow-up"/"deferred" — fail: use pending_fallback instead);
     pending_fallback is legal only while the log records an unresolved run
     interruption (e.g. LinkedIn APPROVAL_REQUIRED handoff); every kept with
     job_id exists in job_list; queries carry results_seen; low-coverage
     queries (results_seen<=1) must appear in coverage-audit text.
  5. Every universe company has >=1 research-log entry (candidate or an explicit
     company-level 'no_postings_found' record) OR a source-plan status of
     blocked/empty with fallbacks noted.
  6. Board-enumeration completeness: for every query with scope
     board_enumeration, the log must account for >= results_seen entries for
     that company (per-candidate entries and/or bulk roll-up entries carrying a
     `count`). Enumerating N and logging fewer means postings were seen but
     never judged — the exact defect the log exists to prevent.
  7. Market-map expansion (WARN, name-free): each seed should be expanded into
     peers, not searched literally. A seed with almost no non-seed peers in its
     bucket(s) is flagged unless silenced by an `excluded` entry, a bucket
     `expansion_note`, or a top-level `expansion_exceptions` entry. Structural
     only — never asserts which companies belong.
Exit 1 on any FAIL. Warnings don't fail.
"""
import csv
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).parent))
from job_url_policy import row_has_direct_posting_url, unverified_jd_placeholder
import store_io

from schema_defs import RESEARCH_DECISIONS as VALID_DECISIONS
from schema_defs import RESEARCH_REASON_CODES as VALID_REASONS
VALID_SOURCE_STATUS = {"planned", "ok", "blocked", "empty", "failed"}
JD_TEXT_FIELDS = ("raw_text", "jd_text", "job_description", "description",
                  "content", "body_text", "html")
MIN_JD_TEXT_CHARS = 200
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except AttributeError:
    pass
# Below this many non-seed peers, a seed looks searched literally rather than
# expanded as a market-map exemplar (search-workflow.md Phase 2). WARN only —
# structural, name-free; the ~5 target lives in the instructions, this flags the
# clear under-expansion (0-1 peers, e.g. "name-brand seed -> only the seed plus one neighbor").
MIN_ARCHETYPE_PEERS_WARN = 2


def norm(name: str) -> str:
    return "".join(c for c in name.lower() if c.isalnum())


def _snapshot_has_jd_text(path: Path) -> bool:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(data, dict):
        return False
    texts = []
    for field in JD_TEXT_FIELDS:
        value = data.get(field)
        if isinstance(value, str):
            texts.append(value)
    return len(" ".join(texts).strip()) >= MIN_JD_TEXT_CHARS


def _clean_yaml_scalar(value: str) -> str:
    value = value.split("#", 1)[0].strip().strip("'\"")
    return value


def _urls_in_text(value: str) -> list[str]:
    return re.findall(r"https?://[^\\s,'\"\]\)]+", value)


def _registry_registered_careers() -> dict[str, dict]:
    """Tiny parser for maintained company careers registry entries.

    PyYAML is intentionally not a runtime dependency. The registry shape we need
    here is simple: company name plus authoritative careers/search/posting/mirror
    URLs. `json_api` is deliberately not treated as authoritative by itself
    because some entries record stale/optional APIs next to the real careers
    listing path.
    """
    reg_path = Path(__file__).resolve().parents[1] / "references" / "search-source-registry.yaml"
    try:
        lines = reg_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    out, current = {}, None
    active_section = ""
    supported_sections = {"self_hosted_careers", "major_company_careers"}
    for raw in lines:
        stripped = raw.strip()
        if not raw.startswith(" ") and stripped.endswith(":"):
            if current:
                out.setdefault(norm(current["name"]), current)
                current = None
            section = stripped[:-1]
            active_section = section if section in supported_sections else ""
            continue
        if not active_section:
            continue
        if stripped.startswith("- name:"):
            if current:
                out.setdefault(norm(current["name"]), current)
            current = {"name": _clean_yaml_scalar(stripped.split(":", 1)[1]),
                       "urls": []}
            continue
        if not current:
            continue
        for key in ("careers_search_url", "listing_url", "posting_url",
                    "detail_prefix", "detail_pattern", "raw_ats_url",
                    "evidence_url"):
            if stripped.startswith(key + ":"):
                val = _clean_yaml_scalar(stripped.split(":", 1)[1])
                if val.startswith("http"):
                    current["urls"].append(val)
        if stripped.startswith("alternate_detail_prefixes:"):
            current["urls"].extend(_urls_in_text(stripped))
        if stripped.startswith("mirrors:"):
            current["urls"].extend(_urls_in_text(stripped))
    if current:
        out.setdefault(norm(current["name"]), current)
    return out


def _url_tokens(urls: list[str]) -> list[str]:
    tokens = []
    for url in urls:
        parsed = urlparse(url)
        if not parsed.netloc:
            continue
        path = parsed.path.rstrip("/")
        tokens.append((parsed.netloc + path).lower())
        tokens.append(parsed.netloc.lower())
    # Check longest tokens first; host-only tokens are fallback for mirrors.
    return sorted({t for t in tokens if t}, key=len, reverse=True)


def _has_registry_source(haystack: str, entry: dict) -> bool:
    hay = haystack.lower().replace("\\", "/")
    return any(token and token in hay for token in _url_tokens(entry.get("urls", [])))


def main() -> int:
    proj = Path(sys.argv[1]) if len(sys.argv) > 1 else store_io.project_dir()
    if not proj.is_absolute():
        proj = store_io.ROOT / proj
    t = proj / "targets"
    fails, warns = [], []

    paths = {n: t / n for n in ["opportunity-thesis.md", "company-universe.json",
                                "source-plan.json", "research-log.json", "coverage-audit.md"]}
    for n, p in paths.items():
        if not p.exists():
            fails.append(f"missing artifact: targets/{n}")
    if fails:
        print("FAIL:", len(fails), "issue(s)")
        for f in fails:
            print(" -", f)
        return 1

    with open(paths["company-universe.json"], encoding="utf-8") as f:
        universe = json.load(f)
    companies = []
    for b in universe.get("buckets", []):
        if not str(b.get("why_relevant", "")).strip():
            fails.append(f"universe bucket '{b.get('bucket')}' missing why_relevant")
        for c in b.get("companies", []):
            companies.append(c)
            if not str(c.get("rationale", "")).strip():
                fails.append(f"universe company '{c.get('name')}' missing rationale")
            if "seed" not in c:
                fails.append(f"universe company '{c.get('name')}' missing seed flag (schema requires seed: true/false)")
    for ex in universe.get("excluded", []):
        if not str(ex.get("reason", "")).strip():
            fails.append(f"excluded entry '{ex.get('name_or_bucket')}' missing reason")
    if not companies:
        fails.append("company-universe has no companies")

    # seed coverage: every user-declared target company must appear in the universe
    # (as a company) or in `excluded` with a reason. Source of truth: project-meta.json
    # target_companies (fallback: project.json external fields). Negatives kept = warning.
    seeds_declared, negatives = [], []
    for meta_name in ("project-meta.json", "project.json"):
        mp = proj / meta_name
        if mp.exists():
            try:
                with open(mp, encoding="utf-8") as f:
                    meta = json.load(f)
                seeds_declared = meta.get("target_companies", []) or seeds_declared
                negatives = meta.get("negatives", []) or negatives
                if seeds_declared:
                    break
            except json.JSONDecodeError:
                warns.append(f"{meta_name} unparseable — seed coverage not checked")
    if seeds_declared:
        universe_names = {norm(c.get("name", "")) for c in companies}
        excluded_names = {norm(str(e.get("name_or_bucket", ""))) for e in universe.get("excluded", [])}
        for s in seeds_declared:
            ns = norm(s)
            if ns in universe_names:
                continue
            if ns in excluded_names:
                warns.append(f"declared seed '{s}' is in `excluded` — legal only with user-visible justification")
                continue
            fails.append(f"declared seed '{s}' (project-meta target_companies) absent from "
                         "company-universe.json — seeds are a floor, never droppable silently")
    else:
        warns.append("no target_companies found in project-meta.json/project.json — seed coverage not checked")

    # market-map expansion (name-free WARN): each seed should be an EXAMPLE of a
    # market map, expanded into peers — not searched literally. For every seed in
    # the universe, count distinct non-seed peers sharing its bucket(s); a seed
    # left with almost none is the "name-brand seed -> only the seed plus one neighbor" under-expansion
    # defect. Escape hatch (silences the WARN): the seed/bucket is in `excluded`,
    # its bucket carries an `expansion_note`, or a top-level `expansion_exceptions`
    # entry covers it. Checks structure only — never which companies belong.
    seed_declared_norms = {norm(s) for s in seeds_declared}
    exclusion_norms = {norm(str(e.get("name_or_bucket", ""))) for e in universe.get("excluded", [])}
    exception_norms = set()
    for ee in universe.get("expansion_exceptions", []) or []:
        if isinstance(ee, dict):
            for k in ("seed", "seed_or_archetype", "bucket", "archetype", "name"):
                if ee.get(k):
                    exception_norms.add(norm(str(ee[k])))
        else:
            exception_norms.add(norm(str(ee)))
    note_bucket_norms = {norm(str(b.get("bucket", ""))) for b in universe.get("buckets", [])
                         if str(b.get("expansion_note", "")).strip()}
    seed_bucket_map, bucket_nonseed = {}, {}
    for b in universe.get("buckets", []):
        bn = norm(str(b.get("bucket", "")))
        bcomps = b.get("companies", [])
        bucket_nonseed[bn] = sum(
            1 for c in bcomps
            if not c.get("seed") and norm(c.get("name", "")) not in seed_declared_norms)
        for c in bcomps:
            cn = norm(c.get("name", ""))
            if c.get("seed") or cn in seed_declared_norms:
                seed_bucket_map.setdefault(cn, set()).add(bn)
    escaped = exclusion_norms | exception_norms
    for sn, bns in seed_bucket_map.items():
        peers = max((bucket_nonseed.get(bn, 0) for bn in bns), default=0)
        if peers >= MIN_ARCHETYPE_PEERS_WARN or sn in escaped or (bns & (note_bucket_norms | escaped)):
            continue
        disp = next((c.get("name") for b in universe.get("buckets", [])
                     for c in b.get("companies", []) if norm(c.get("name", "")) == sn), sn)
        warns.append(f"seed '{disp}' expanded into only {peers} non-seed peer(s) — treat it as a "
                     "market-map exemplar and expand its archetype (competitors / same-talent-pool "
                     "/ adjacent categories / location peers), or record a bucket `expansion_note` "
                     "or `excluded` reason (search-workflow.md Phase 2)")

    with open(paths["source-plan.json"], encoding="utf-8") as f:
        plan = json.load(f)
    plan_companies = {norm(c.get("name", "")): c for c in plan.get("companies", [])}
    for c in companies:
        if norm(c.get("name", "")) not in plan_companies:
            fails.append(f"source-plan missing universe company '{c.get('name')}'")
    for name, pc in plan_companies.items():
        for s in pc.get("sources", []):
            if s.get("status") not in VALID_SOURCE_STATUS:
                fails.append(f"source-plan {pc.get('name')}: invalid source status '{s.get('status')}'")
            if not s.get("type"):
                fails.append(f"source-plan {pc.get('name')}: source missing type")

    with open(paths["research-log.json"], encoding="utf-8") as f:
        rl = json.load(f)
    runs = rl if isinstance(rl, list) else [rl]
    cands, queries = [], []
    for run in runs:
        cands += run.get("candidates", [])
        queries += run.get("queries", [])

    job_ids = set()
    jl = proj / "data" / "job_list.csv"
    if jl.exists():
        with open(jl, newline="", encoding="utf-8") as f:
            job_ids = {r["job_id"] for r in csv.DictReader(f)}

    audit_text_early = paths["coverage-audit.md"].read_text(encoding="utf-8").lower()
    run_interrupted = ("approval_required" in audit_text_early.replace(" ", "_")
                       or any("blocked" in str(run.get("linkedin_status", "")).lower()
                              for run in runs))
    PLAN_TOKENS = ("pending", "follow-up", "follow up", "followup", "deferred",
                   "todo", "next run", "next pass", "not yet")
    UNRESOLVED_CAPTURE_TOKENS = (
        "outbound_dns_blocked",
        "dns_error",
        "temporary failure in name resolution",
        "name or service not known",
        "nodename nor servname",
        "connection timed out",
        "network is unreachable",
        "js shell",
        "javascript shell",
        "requires javascript",
        "static html",
        "browser unavailable",
        "playwright unavailable",
        "browser runtime unavailable",
        "connector_error",
        "approval_required",
        "authwall",
        "signed_out",
        "verification_prompt",
        "captcha",
        "rate limit",
        "429",
        "403 forbidden",
    )

    seen_companies = set()
    kept_companies = set()
    for i, c in enumerate(cands):
        seen_companies.add(norm(c.get("company", "")))
        if c.get("decision") == "kept":
            kept_companies.add(norm(c.get("company", "")))
        if c.get("decision") not in VALID_DECISIONS:
            fails.append(f"log candidate {i}: invalid decision '{c.get('decision')}'")
        if not str(c.get("reason", "")).strip() and c.get("decision") != "kept":
            fails.append(f"log candidate {i} ({c.get('company')}/{c.get('title')}): missing reason")
        if c.get("decision") == "failed_capture":
            fb = c.get("fallbacks_attempted", [])
            if len(fb) < 3:
                fails.append(f"log candidate {i} ({c.get('company')}): failed_capture with "
                             f"{len(fb)} fallbacks (need >=3 source types attempted)")
            plans = [x for x in fb if any(t in str(x).lower() for t in PLAN_TOKENS)]
            if plans:
                fails.append(f"log candidate {i} ({c.get('company')}): failed_capture lists "
                             f"planned-not-attempted fallbacks {plans} — attempts only; an "
                             "interrupted ladder is decision 'pending_fallback', not failed_capture")
            qtext = " ".join(
                (str(q.get("scope", "")) + " " + str(q.get("q", "")) + " "
                 + str(q.get("observed", "")) + " " + str(q.get("note", ""))).lower()
                for q in queries
                if norm(str(q.get("company", ""))) == norm(str(c.get("company", "")))
            )
            blocker_text = " ".join([
                str(c.get("reason", "")),
                str(c.get("reason_code", "")),
                str(c.get("notes", "")),
                " ".join(str(x) for x in fb),
                qtext,
            ]).lower()
            blockers = [t for t in UNRESOLVED_CAPTURE_TOKENS if t in blocker_text]
            if blockers:
                fails.append(f"log candidate {i} ({c.get('company')}): failed_capture contains "
                             f"unresolved capture/tooling blocker(s) {blockers[:4]} — finish "
                             "the fallback ladder, use pending_fallback for interrupted work, "
                             "or record a source-plan blocked/empty state with evidence")
        if c.get("decision") == "pending_fallback":
            pending_text = " ".join([
                str(c.get("reason", "")),
                str(c.get("reason_code", "")),
                str(c.get("notes", "")),
                str(c.get("fallbacks_attempted", "")),
            ]).lower()
            unresolved_blocker = any(t in pending_text for t in UNRESOLVED_CAPTURE_TOKENS)
            if not run_interrupted and not unresolved_blocker:
                fails.append(f"log candidate {i} ({c.get('company')}): pending_fallback without an "
                             "unresolved run interruption or capture/tooling blocker — "
                             "finish the fallback ladder or record failed_capture with real attempts")
            if norm(c.get("company", "")) not in audit_text_early.replace(" ", ""):
                warns.append(f"pending_fallback company '{c.get('company')}' not discussed in "
                             "coverage-audit follow-ups")
        if c.get("decision") == "kept":
            if not row_has_direct_posting_url(c):
                fails.append(f"log candidate {i} ({c.get('company')}/{c.get('title')}): "
                             "kept candidate must include a direct posting URL in "
                             "source_url or job_page_url; listing/search URLs belong in "
                             "queries or pending_fallback, not job_list rows")
            placeholder = unverified_jd_placeholder(c)
            if placeholder:
                fails.append(f"log candidate {i} ({c.get('company')}/{c.get('title')}): "
                             f"kept candidate contains unverified JD placeholder "
                             f"'{placeholder}' — record pending_fallback until the "
                             "posting URL and JD are verified")
            jid = c.get("job_id", "")
            if not jid:
                fails.append(f"log candidate {i} ({c.get('company')}): kept without job_id")
            elif job_ids and jid not in job_ids:
                fails.append(f"log candidate {i}: kept job_id '{jid}' not in job_list store")
            if jid:
                snapshot = t / "jobs" / f"{jid}.json"
                if not snapshot.exists():
                    fails.append(f"log candidate {i} ({c.get('company')}/{c.get('title')}): "
                                 f"missing JD snapshot targets/jobs/{jid}.json")
                elif not _snapshot_has_jd_text(snapshot):
                    fails.append(f"log candidate {i} ({c.get('company')}/{c.get('title')}): "
                                 f"JD snapshot targets/jobs/{jid}.json lacks verified JD "
                                 f"snapshot text (need one of {', '.join(JD_TEXT_FIELDS)} "
                                 f"with >= {MIN_JD_TEXT_CHARS} characters)")

    # Self-hosted careers registry enforcement for declared seeds: if the
    # maintained registry says a seed's canonical source is self-hosted, final
    # artifacts must show that source family was actually used. A guessed ATS
    # token or stale API alone cannot prove absence.
    if seeds_declared:
        registry_self_hosted = _registry_registered_careers()
        for s in seeds_declared:
            ns = norm(s)
            pc = plan_companies.get(ns)
            if not pc:
                continue
            reg_entry = registry_self_hosted.get(ns)
            plan_hay = " ".join(
                (str(src.get("type", "")) + " " + str(src.get("url", ""))).lower()
                for src in pc.get("sources", []))
            fallback_hay = " ".join(str(x).lower() for x in pc.get("fallbacks_used", []))
            query_hay = " ".join(
                (str(q.get("scope", "")) + " " + str(q.get("q", "")) + " "
                 + str(q.get("source_url", ""))).lower()
                for q in queries
                if norm(str(q.get("company", ""))) == ns or ns in norm(str(q.get("q", ""))))
            hay = f"{plan_hay} {fallback_hay} {query_hay}"
            if reg_entry and not _has_registry_source(hay, reg_entry):
                fails.append(
                    f"declared seed '{s}' is listed in the self-hosted careers registry "
                    "or maintained careers registry, "
                    "but source-plan/research-log do not show its registry careers/search/"
                    "posting source family — do not conclude absence from guessed ATS "
                    "tokens, stale APIs, or generic search plans")
            planned = [src for src in pc.get("sources", [])
                       if src.get("status") == "planned"
                       and "linkedin" not in str(src.get("type", "")).lower()]
            if ns not in kept_companies and planned:
                labels = [f"{src.get('type')} {src.get('url')}" for src in planned]
                fails.append(
                    f"declared seed '{s}' has 0 kept rows while non-LinkedIn source(s) "
                    f"remain planned-not-attempted: {labels} — finish the fallback ladder "
                    "or record an evidence-backed exclusion before PASS")

    # board-enumeration completeness: enumerated postings must all be accounted
    # for in the log (per-candidate entries and/or bulk roll-ups with `count`).
    def _accounted(company_norm: str) -> int:
        total = 0
        for c in cands:
            if norm(c.get("company", "")) == company_norm:
                try:
                    total += max(1, int(c.get("count", 1) or 1))
                except (TypeError, ValueError):
                    total += 1
        return total
    universe_norms = {norm(c.get("name", "")): c.get("name", "") for c in companies}
    for q in queries:
        if "board_enumeration" not in str(q.get("scope", "")).lower():
            continue
        qn = norm(str(q.get("company", "")))
        if not qn:  # fall back: longest universe name prefixing the query text
            qtext = norm(str(q.get("q", "")))
            matches = [n for n in universe_norms if n and qtext.startswith(n)]
            qn = max(matches, key=len) if matches else ""
        if not qn:
            warns.append(f"board_enumeration query '{str(q.get('q'))[:50]}' has no company field "
                         "and matches no universe company — completeness not checkable")
            continue
        seen_n = q.get("results_seen")
        if not isinstance(seen_n, int) or seen_n <= 0:
            continue
        acc = _accounted(qn)
        if acc < seen_n:
            fails.append(f"board_enumeration '{universe_norms.get(qn, qn)}': results_seen="
                         f"{seen_n} but log accounts for only {acc} — every posting seen must "
                         "be logged (use bulk roll-up entries with `count` for out-of-family skips)")

    # enumerate-don't-filter: seed companies with a working ATS source should show
    # a board_enumeration query (full-board read), not only keyword-filtered pulls.
    if seeds_declared:
        enum_scopes = {norm(str(q.get("q", ""))): q for q in queries
                       if "board_enumeration" in str(q.get("scope", "")).lower()}
        for s in seeds_declared:
            ns = norm(s)
            pc = plan_companies.get(ns)
            if not pc:
                continue
            has_ok_ats = any(x.get("type", "").lower() in ("ats", "official_careers")
                             and x.get("status") == "ok" for x in pc.get("sources", []))
            has_enum = any(ns in k for k in enum_scopes)
            if has_ok_ats and not has_enum:
                warns.append(f"seed '{s}': ATS source ok but no board_enumeration query "
                             "recorded — coverage may be keyword-filtered at source")

    # self-hosted-careers guard: a declared seed marked failed_capture must have
    # actually attempted its OFFICIAL CAREERS SITE and/or WEB DISCOVERY (where
    # big-tech postings live), not only a third-party ATS lookup. An ATS-only miss
    # for a self-hosted-careers company (self-hosted-careers seeds) is the exact defect
    # that keeps dropping them.
    if seeds_declared:
        seed_norms = {norm(s) for s in seeds_declared}
        CAREERS_HINT = ("official", "careers", "self_hosted", "self-hosted", "web_discovery",
                        "web discovery", "site:", "search-engine", "search engine", "posting_url")
        for i, c in enumerate(cands):
            if c.get("decision") != "failed_capture":
                continue
            cn = norm(c.get("company", ""))
            if cn not in seed_norms:
                continue
            fb = " ".join(str(x).lower() for x in c.get("fallbacks_attempted", []))
            pc = plan_companies.get(cn, {})
            plan_types = " ".join((str(s.get("type", "")) + " " + str(s.get("url", ""))).lower()
                                  for s in pc.get("sources", []))
            qtext = " ".join((str(q.get("scope", "")) + " " + str(q.get("q", ""))).lower()
                             for q in queries
                             if norm(str(q.get("company", ""))) == cn or cn in norm(str(q.get("q", ""))))
            hay = f"{fb} {plan_types} {qtext}"
            if not any(h in hay for h in CAREERS_HINT):
                fails.append(f"seed '{c.get('company')}' marked failed_capture but no "
                             "official-careers or web-discovery attempt is recorded (ATS-only?) "
                             "— big-tech postings live on the self-hosted careers site; capture "
                             "the direct posting URLs there before declaring absence "
                             "(see search-source-registry.yaml `self_hosted_careers`)")

    if negatives:
        neg_norm = {norm(n) for n in negatives}
        for c in cands:
            if c.get("decision") == "kept" and norm(c.get("company", "")) in neg_norm:
                warns.append(f"kept candidate at negative-listed company '{c.get('company')}' — "
                             "verify an explicit override note exists")

    audit_text = paths["coverage-audit.md"].read_text(encoding="utf-8").lower()
    # evidence-before-stopping: a pending LinkedIn handoff requires a recorded navigation attempt
    if "approval_required" in audit_text.replace(" ", "_"):
        li_attempts = [q for q in queries
                       if "linkedin" in str(q.get("scope", "")).lower() and q.get("observed")]
        if not li_attempts:
            fails.append("coverage-audit declares APPROVAL_REQUIRED (LinkedIn pending) but "
                         "research-log has no LinkedIn navigation attempt with an `observed` "
                         "state — stopping without evidence is a defect")

    for q in queries:
        if "results_seen" not in q:
            warns.append(f"query '{str(q.get('q'))[:40]}' missing results_seen")
        elif q.get("results_seen", 99) <= 1:
            token = str(q.get("q", ""))[:25].lower()
            if token and token not in audit_text:
                warns.append(f"low-coverage query ({q.get('results_seen')} results) not "
                             f"discussed in coverage-audit: '{str(q.get('q'))[:50]}'")

    for c in companies:
        n = norm(c.get("name", ""))
        if n in seen_companies:
            continue
        pc = plan_companies.get(n, {})
        statuses = {s.get("status") for s in pc.get("sources", [])}
        if statuses & {"blocked", "empty", "failed"} and pc.get("fallbacks_used"):
            continue
        fails.append(f"universe company '{c.get('name')}' never attempted: no log entries "
                     "and no blocked/empty source with fallbacks_used in source-plan")

    print(f"artifacts: 5/5 | universe: {len(companies)} companies | "
          f"log: {len(cands)} candidates, {len(queries)} queries")
    for w in warns:
        print(f"  WARN: {w}")
    if fails:
        print(f"FAIL: {len(fails)} issue(s)")
        for f in fails:
            print(f"  - {f}")
        return 1
    print("PASS: research artifacts mechanically valid" + (f" ({len(warns)} warnings)" if warns else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
