"""`rolenavi run <workflow>` — headless workflow invocations.

search | score | prep | prep-* | apply map onto the repo's skills. Each run
resolves the target project, gets an envelope from the provider, executes events
through the validated local pipeline, and records the run in the local telemetry
store. Public RoleNavi does not execute external actions.

Mock runs NEVER touch a real project: they execute against a disposable copy of
the bundled mock fixture project under ROLENAVI_HOME so
canned rows can't pollute user data. Live runs use the active project (or
--project) — exactly what the user asked for.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from .. import (
    application_audit,
    core,
    decision_policy,
    llm,
    profile_meta,
    project_meta,
    score_staging,
)
from .. import universe as universe_state
from ..paths import RoleNaviError, active_project_dir, home_dir, repo_root
from ..privacy import source_extract
from ..telemetry import store as tstore

jd_text_cleaner = core.load("jd_text_cleaner")

WORKFLOW_SKILLS = {
    "profile-intake": ["candidate-profile-builder"],
    "opportunity-plan": [],
    "search": ["job-opening-research", "target-job-group-strategy"],
    "prep": [
        "prep-strategy",
        "prep-resume",
        "prep-linkedin",
        "prep-interview",
    ],
    "score": ["target-job-group-strategy"],
    "prep-strategy": ["prep-strategy"],
    "prep-resume": ["prep-resume"],
    "prep-linkedin": ["prep-linkedin"],
    "prep-interview": ["prep-interview"],
    "story-bank": ["prep-interview"],
    "apply": ["application-strategy", "application-tracker"],
}
WORKFLOW_PHASE_SKILLS = {
    ("search", "plan_repair"): ["job-opening-research"],
    ("search", "capture_shard"): ["job-opening-research"],
    ("search", "capture_repair"): ["job-opening-research"],
}

# workflows whose scope is the user's focused positions (data/focused-jobs.json)
FOCUS_SCOPED = {"prep", "prep-strategy", "prep-resume", "prep-linkedin",
                "prep-interview"}
PROFILE_WORKFLOW = "profile-intake"
DEFAULT_SEARCH_RETRY_COMPANY_LIMIT = 8
SCORE_CONTRACT_VERSION = "rolenavi-score-contract-v3"
SCORE_BATCH_MAX_JOBS = 4
SCORE_BATCH_MAX_REQUIREMENTS = 16
SCORE_BATCH_RETRY_JOBS = 1
SCORE_BATCH_MAX_CHARS = 28000
SCORE_BATCH_WORKERS = 3
DERIVED_SCORE_CRITERIA = frozenset({"minimum_requirement", "essential_qualification"})
SCORE_FIELD_LIMITS = {
    "must_have_requirements": 700,
    "nice_to_have_requirements": 350,
    "jd_summary": 700,
    "notes": 350,
    "essential_qualifications": 700,
}
SCORE_PROFILE_LIMIT = 8000
SCORE_EVIDENCE_LIMIT = 8000
RESUME_ARTIFACT_SCHEMA = "rolenavi-resume-group-artifacts-v1"
RESUME_TARGET_BRIEF_SCHEMA = "rolenavi-resume-target-brief-v1"
RESUME_REASON_VALUES = (
    "req_match", "impact", "scope", "domain", "differentiator", "narrative",
)
RESUME_REWRITE_TYPES = (
    "new", "substantial_rewrite", "compressed", "reframed", "selected",
)
STAGED_PUBLISH_WORKFLOWS = {
    "prep-strategy", "prep-resume", "prep-linkedin", "story-bank", "prep-interview", "apply",
}


def _legacy_llm_search_enabled() -> bool:
    return os.environ.get("ROLENAVI_LEGACY_LLM_SEARCH", "").strip().lower() in {
        "1", "true", "yes", "on"
    }


def _focused_job_ids(project: Path) -> list[str]:
    fj = project / "data" / "focused-jobs.json"
    try:
        ids = json.loads(fj.read_text(encoding="utf-8")).get("job_ids", [])
    except (OSError, json.JSONDecodeError):
        ids = []
    out: list[str] = []
    seen: set[str] = set()
    for job_id in ids if isinstance(ids, list) else []:
        key = str(job_id or "").strip()
        if key and key not in seen:
            seen.add(key)
            out.append(key)
    return out


def _focused_job_rows(project: Path) -> list[dict]:
    """Focused positions joined with full job_list rows, preserving focus order."""
    from ..repositories import job_rows
    ids = _focused_job_ids(project)
    if not ids:
        return []
    rows: dict[str, dict] = {}
    for r in job_rows(project, job_ids=ids):
        job_id = str(r.get("job_id", "") or "").strip()
        row = dict(r)
        row.setdefault("url", row.get("job_page_url") or row.get("source_url", ""))
        rows[job_id] = row
    return [rows[job_id] for job_id in ids if job_id in rows]


def focused_jobs(project: Path) -> list[dict]:
    """Focused positions joined with job_list rows (empty list when none)."""
    return _focused_job_rows(project)


def _current_scored_job_ids(project: Path) -> set[str]:
    """Return dependency-current scored IDs; stale/unfinished rows are excluded."""
    freshness_path = project / "strategy" / "score-freshness.json"
    freshness = _read_json(freshness_path, None)
    if isinstance(freshness, dict) and isinstance(freshness.get("current_job_ids"), list):
        return {
            str(job_id).strip() for job_id in freshness["current_job_ids"]
            if str(job_id).strip()
        }
    # Legacy compatibility: before score-freshness existed, a canonical rating
    # plus a persisted fit score is the strongest available currentness signal.
    rating_ids = {
        str(item.get("job_id", "")).strip() for item in _load_score_ratings(project)
        if str(item.get("job_id", "")).strip()
    }
    return {
        str(row.get("job_id", "")).strip() for row in _focused_job_rows(project)
        if str(row.get("job_id", "")).strip() in rating_ids
        and str(row.get("fit_score", "")).strip()
    }


def _strategy_focused_job_rows(project: Path) -> list[dict]:
    scored = _current_scored_job_ids(project)
    return [
        row for row in _focused_job_rows(project)
        if str(row.get("job_id", "")).strip() in scored
    ]


def _focused_group_slugs(project: Path) -> list[str]:
    groups: list[str] = []
    seen: set[str] = set()
    for row in focused_jobs(project):
        slug = _slug(row.get("job_group", ""), "")
        if slug and slug not in seen:
            seen.add(slug)
            groups.append(slug)
    return groups

ARTIFACT_VALIDATORS = {"resume_bullets": "validate_resume_bullets"}


class RunCancelled(RoleNaviError):
    """User-requested stop: the run halts at the next safe checkpoint."""


class RunContext:
    def __init__(self, workflow: str, project: Path, mode: str):
        self.workflow = workflow
        self.project = project
        self.mode = mode
        self.validator_results: list[dict] = []
        self.summary = ""
        self.failure_class = ""
        self.pending_reasons: dict[str, Path] = {}  # resume path -> reasons file
        self.streamed = False          # provider already surfaced progress live
        self.on_event = None           # optional sink: fn(kind, text, extra=None)
        self.cancel_event = None       # optional threading.Event (web UI stop button)
        self.run_id = ""
        self.partial_reasons: list[dict] = []
        self.blocked_reasons: list[dict] = []
        self.artifacts_written: list[str] = []
        # Local-only publish diagnostics. These are intentionally structured so
        # telemetry can retain a safe code/target while detailed validator text
        # remains in the run staging directory.
        self.publish_errors: dict[str, dict] = {}
        self.publish_results: dict[str, dict] = {}

    def check_cancelled(self) -> None:
        """Cooperative cancellation checkpoint — safe places only (never mid-write)."""
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise RunCancelled("run cancelled by user")

    def emit(self, kind: str, text: str, extra: dict | None = None) -> None:
        """Single output path: stdout (flushed, so the CLI shows life) + sink."""
        prefix = {"stream": "  ⟳ ", "progress": "  · ", "artifact": "  artifact: ",
                  "store": "  ", "validator": "  ", "result": "  ✓ ",
                  "info": "  "}.get(kind, "  ")
        line = f"{prefix}{text}"
        try:
            print(line, flush=True)
        except UnicodeEncodeError:
            encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
            safe = line.encode(encoding, errors="replace").decode(encoding, errors="replace")
            print(safe, flush=True)
        if self.on_event is not None:
            try:
                self.on_event(kind, text, extra)
            except Exception:
                pass  # a broken sink must never kill a run

    def mark_partial(self, scope: str, reason: str) -> None:
        item = {"scope": scope, "reason": reason}
        if item not in self.partial_reasons:
            self.partial_reasons.append(item)
            self.validator_results.append({
                "validator": f"partial[{scope}]",
                "target": scope,
                "returncode": 2,
                "output": reason[:800],
            })

    def mark_blocked(self, scope: str, reason: str) -> None:
        item = {"scope": scope, "reason": reason}
        if item not in self.blocked_reasons:
            self.blocked_reasons.append(item)
            self.validator_results.append({
                "validator": f"blocked[{scope}]",
                "target": scope,
                "returncode": 3,
                "output": reason[:800],
            })

    def run_status(self) -> str:
        if self.failure_class == "cancelled":
            return "cancelled"
        if self.blocked_reasons:
            return "blocked"
        if self.failure_class:
            return "failed"
        if self.partial_reasons:
            return "partial"
        return "ok"


def _update_prep_progress(ctx: RunContext, phase: str, state: str, *,
                          completed: int | None = None, total: int | None = None,
                          detail: str = "") -> None:
    """Persist and emit a compact prep state that the WebUI can poll mid-run."""
    if ctx.workflow != "prep":
        return
    run_id = _slug(getattr(ctx, "run_id", ""), "unrecorded-run")
    root = ctx.project / "runtime" / "runs" / run_id
    root.mkdir(parents=True, exist_ok=True)
    path = root / "prep-progress.json"
    doc = _read_json(path, {})
    if not isinstance(doc, dict):
        doc = {}
    phases = doc.get("phases") if isinstance(doc.get("phases"), dict) else {}
    item = {"state": state, "updated_at": _now()}
    if completed is not None:
        item["completed"] = completed
    if total is not None:
        item["total"] = total
    if detail:
        item["detail"] = detail[:500]
    phases[phase] = item
    doc.update({
        "schema": "rolenavi-prep-progress-v1",
        "run_id": getattr(ctx, "run_id", ""),
        "workflow": "prep",
        "current_phase": phase,
        "state": state,
        "phases": phases,
        "updated_at": _now(),
        "revision": int(doc.get("revision", 0) or 0) + 1,
    })
    temp = path.with_suffix(".json.tmp")
    temp.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temp, path)
    count = (f" {completed}/{total}" if completed is not None and total is not None else "")
    ctx.emit("prep_progress", f"{phase}: {state}{count}", {"prep_progress": doc})

BLOCKED_ERROR_SNIPPETS = (
    "selected model is at capacity",
    "model is at capacity",
    "please try a different model",
    "rate limit",
    "quota exceeded",
    "temporarily unavailable",
    "browser spawn denied",
    "winerror 5",
)


def _looks_blocked_error(reason: str) -> bool:
    text = str(reason or "").lower()
    return any(snippet in text for snippet in BLOCKED_ERROR_SNIPPETS)


def _mark_exception(ctx: RunContext, scope: str, exc: Exception) -> None:
    reason = f"{type(exc).__name__}: {exc}"
    if _looks_blocked_error(reason):
        ctx.mark_blocked(scope, reason)
        ctx.summary = ctx.summary or f"Blocked: {reason[:300]}"
    else:
        ctx.failure_class = ctx.failure_class or _slug(scope, "workflow_exception")
        ctx.summary = ctx.summary or f"Failed: {reason[:300]}"
    ctx.emit("error", reason[:800])


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _safe_print(line: str) -> None:
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
        safe = line.encode(encoding, errors="replace").decode(encoding, errors="replace")
        print(safe, flush=True)


def _env_for(project: Path) -> dict:
    return {**os.environ, "RECRUITING_PROJECT_DIR": str(project)}


def skills_for_workflow_phase(workflow: str, phase: str | None = None) -> list[str]:
    return list(WORKFLOW_PHASE_SKILLS.get((workflow, phase or ""),
                WORKFLOW_SKILLS[workflow]))


def _slug(text: str, fallback: str = "agent") -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(text or "").lower()).strip("-")
    return (slug[:48].strip("-") or fallback)


def _agent_label(command: str, index: int, generated_name: str | None = None) -> str:
    base = f"{_slug(command, 'run')}-{index:02d}"
    suffix = _slug(generated_name or "", "")
    return f"{base}-{suffix}" if suffix else base


def _shard_generated_name(companies: list[dict]) -> str:
    names = [_slug(c.get("name", ""), "") for c in companies[:2] if isinstance(c, dict)]
    return "-".join(n for n in names if n) or "capture"


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return default


def _company_aliases(name: str) -> list[str]:
    """Conservative aliases for matching run-level task text to known companies."""
    raw = str(name or "").strip()
    if not raw:
        return []
    aliases = [raw]
    aliases.extend(part.strip() for part in re.split(r"[;/|]", raw) if part.strip())
    paren = re.findall(r"\(([^)]+)\)", raw)
    for group in paren:
        aliases.extend(part.strip() for part in re.split(r"[/|;]", group) if part.strip())
    cleaned = re.sub(r"\s*\([^)]*\)", "", raw).strip()
    if cleaned and cleaned != raw:
        aliases.append(cleaned)
    out, seen = [], set()
    for alias in aliases:
        alias = re.sub(r"\s+", " ", alias).strip()
        if len(alias) < 3:
            continue
        key = alias.lower()
        if key not in seen:
            seen.add(key)
            out.append(alias)
    return out


def _known_company_names(project: Path) -> list[str]:
    names: list[str] = []
    for rel in ("targets/source-plan.json", "targets/company-universe.json"):
        data = _read_json(project / rel, {})
        if rel.endswith("source-plan.json"):
            for company in data.get("companies", []) if isinstance(data, dict) else []:
                if isinstance(company, dict) and company.get("name"):
                    names.append(str(company["name"]))
        else:
            for bucket in data.get("buckets", []) if isinstance(data, dict) else []:
                for company in bucket.get("companies", []) if isinstance(bucket, dict) else []:
                    if isinstance(company, dict) and company.get("name"):
                        names.append(str(company["name"]))
    meta = _read_json(project / "project-meta.json", {})
    for value in meta.get("target_companies", []) if isinstance(meta, dict) else []:
        for part in re.split(r"[;,]", str(value)):
            part = part.strip()
            if part:
                names.append(part)
    try:
        registry = core.resolve_company_sources.load_registered_registry()
        for entry in registry.values():
            if entry.get("name"):
                names.append(str(entry["name"]))
    except Exception:
        pass
    out, seen = [], set()
    for name in names:
        key = _slug(name, "")
        if key and key not in seen:
            seen.add(key)
            out.append(name)
    return out


def _mentions_company(task: str, alias: str) -> bool:
    pattern = r"(?<![A-Za-z0-9])" + re.escape(alias) + r"(?![A-Za-z0-9])"
    return re.search(pattern, task, flags=re.I) is not None


def _looks_like_company_token(token: str) -> bool:
    token = token.strip(" \t\r\n\"'`()[]{}.,:;")
    if not token:
        return False
    suffixes = {"ai", "co", "corp", "corporation", "inc", "llc", "ltd", "plc"}
    if token.lower().rstrip(".") in suffixes:
        return True
    return bool(re.search(r"[A-Z0-9]", token))


def _extract_inline_company_mentions(task: str) -> list[str]:
    """Fallback for one-off run prompts that name a company not in our registry.

    Known-company matching remains primary. This intentionally only handles
    explicit preposition phrases such as "from CompanyX" or "at Acme AI" so
    ordinary title text is not mistaken for a company.
    """
    task = str(task or "")
    stopwords = {
        "and", "or", "with", "without", "for", "from", "at", "in", "near",
        "role", "roles", "job", "jobs", "position", "positions", "opening",
        "openings", "team", "teams", "title", "titles", "senior", "manager",
        "director", "principal", "strategy", "product",
    }
    out: list[str] = []
    patterns = [
        r"\b(?:from|at|within|inside)\s+([A-Za-z0-9][A-Za-z0-9&.+'\-]*(?:\s+[A-Za-z0-9][A-Za-z0-9&.+'\-]*){0,4})",
        r"\b(?:company|companies)\s+[\"'`]([^\"'`]+)[\"'`]",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, task):
            raw = re.split(r"[\n{}:;,]", match.group(1).strip(), maxsplit=1)[0]
            tokens: list[str] = []
            for token in raw.split():
                clean = token.strip(" \t\r\n\"'`()[]{}.,:;")
                if not clean or clean.lower() in stopwords:
                    break
                if not _looks_like_company_token(clean):
                    break
                tokens.append(clean)
            if tokens:
                out.append(" ".join(tokens))
    deduped, seen = [], set()
    for name in out:
        key = _slug(name, "")
        if key and key not in seen:
            seen.add(key)
            deduped.append(name)
    return deduped


def _extract_requested_companies(project: Path, task: str) -> list[str]:
    task = str(task or "")
    if not task.strip():
        return []
    matches: list[tuple[int, str]] = []
    for name in _known_company_names(project):
        aliases = _company_aliases(name)
        if any(_mentions_company(task, alias) for alias in aliases):
            # Prefer the most specific known company name when aliases overlap.
            matches.append((len(name), name))
    out, seen = [], set()
    for _, name in sorted(matches, key=lambda item: item[0], reverse=True):
        key = _slug(name, "")
        if key not in seen:
            seen.add(key)
            out.append(name)
    for name in _extract_inline_company_mentions(task):
        key = _slug(name, "")
        if key and key not in seen:
            seen.add(key)
            out.append(name)
    return out


def _extract_requested_titles(task: str) -> list[str]:
    titles: list[str] = []
    for group in re.findall(r"\{([^}]+)\}", str(task or ""), flags=re.S):
        for item in re.split(r"[;\n]", group):
            item = re.sub(r"\s+", " ", item).strip(" -")
            if item:
                titles.append(item)
    out, seen = [], set()
    for title in titles:
        key = title.lower()
        if key not in seen:
            seen.add(key)
            out.append(title)
    return out


def _build_run_intent(project: Path, workflow: str, task: str | None) -> dict:
    """Parse a free-text run instruction into lightweight, auditable hints.

    This never edits project targets. It only lets one run bias or narrow work
    while preserving project hard constraints such as location and negatives.
    """
    task_text = str(task or "").strip()
    companies = _extract_requested_companies(project, task_text)
    titles = _extract_requested_titles(task_text)
    targeted_language = bool(re.search(
        r"\b(include|collect|find|refresh|more|only|from|at|for)\b",
        task_text, flags=re.I))
    mode = "targeted_incremental" if (companies or titles or targeted_language) else "default"
    return {
        "schema": "rolenavi-run-intent-v1",
        "workflow": workflow,
        "mode": mode,
        "raw_instruction": task_text,
        "requested_companies": companies,
        "requested_titles": titles,
        "hard_scope_companies": bool(companies),
        "notes": (
            "Run-level instruction biases this run only; project target locations, "
            "negatives, truthfulness, local-only, and approval boundaries still apply."
            if task_text else ""
        ),
    }


_APPLICATION_TITLE_STOPWORDS = frozenset({
    "a", "an", "and", "apac", "at", "for", "in", "lead", "manager", "of", "on",
    "senior", "singapore", "the", "to",
})


def _application_title_tokens(value: str) -> set[str]:
    return {
        token for token in re.findall(r"[a-z0-9]+", str(value or "").lower())
        if token not in _APPLICATION_TITLE_STOPWORDS and (len(token) > 2 or token in {"ai", "vc"})
    }


def _select_application_jobs(project: Path, task: str | None,
                             run_intent: dict | None = None) -> list[dict]:
    """Resolve an apply instruction to focused rows without semantic guessing.

    Exact job IDs win.  Otherwise company names constrain scope and distinctive
    title-token overlap identifies requested roles.  A bare company request
    intentionally selects all focused roles for that company.
    """
    rows = _focused_job_rows(project)
    if not rows:
        return []
    raw = str(task or "").strip()
    lowered = raw.lower()
    exact = [row for row in rows if str(row.get("job_id", "")) in raw]
    if exact:
        return exact
    intent = run_intent or _build_run_intent(project, "apply", raw)
    requested_companies = {
        _slug(name, "") for name in intent.get("requested_companies", []) if _slug(name, "")
    }
    company_rows = [
        row for row in rows
        if not requested_companies or _slug(row.get("company", ""), "") in requested_companies
    ]
    if not raw:
        return company_rows
    matched: list[dict] = []
    for row in company_rows:
        tokens = _application_title_tokens(str(row.get("title", "")))
        overlap = {token for token in tokens if re.search(rf"(?<![a-z0-9]){re.escape(token)}(?![a-z0-9])", lowered)}
        # Two distinctive words are enough.  VC is accepted alone because it is
        # a specific role-family acronym; a generic shared word such as
        # deployment is deliberately insufficient.
        if len(overlap) >= 2 or "vc" in overlap:
            matched.append(row)
    if matched:
        return matched
    if requested_companies:
        return company_rows
    return rows


def _agent_log_dir(ctx: RunContext) -> Path | None:
    if not ctx.run_id:
        return None
    return ctx.project / "runtime" / "runs" / ctx.run_id / "agents"


def _append_agent_log(ctx: RunContext, label: str, text: str) -> None:
    # Raw streams/results can contain resume or profile text. They are disabled
    # by default; the opt-in is intentionally explicit and developer-oriented.
    if os.environ.get("ROLENAVI_RAW_RUN_LOGS") != "1":
        return
    log_dir = _agent_log_dir(ctx)
    if log_dir is None:
        return
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        with (log_dir / f"{_slug(label)}.log").open("a", encoding="utf-8") as f:
            f.write(text.rstrip() + "\n")
    except OSError:
        pass


def _append_agent_result_log(ctx: RunContext, label: str, envelope: dict) -> None:
    text = _result_text(envelope)
    if not text:
        return
    _append_agent_log(ctx, label, "\n--- FULL_RESULT ---\n" + text)


def _write_agent_manifest(ctx: RunContext, records: list[dict]) -> None:
    log_dir = _agent_log_dir(ctx)
    if log_dir is None:
        return
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        path = log_dir / "manifest.json"
        existing = _read_json(path, {})
        prior = existing.get("agents", []) if isinstance(existing, dict) else []
        merged: dict[str, dict] = {}
        for item in [*prior, *records]:
            if isinstance(item, dict) and item.get("label"):
                merged[str(item["label"])] = item
        path.write_text(
            json.dumps({"schema": "rolenavi-agent-log-manifest-v1",
                        "run_id": ctx.run_id, "agents": list(merged.values())},
                       indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8")
    except OSError:
        pass


def _labelled_stream(ctx: RunContext, label: str, on_stream):
    def _sink(text: str) -> None:
        line = str(text)
        _append_agent_log(ctx, label, line)
        on_stream(f"[{label}] {line}")
    return _sink


def _provider_run(provider, workflow: str, context: dict, on_progress,
                  model_workflow: str | None = None) -> dict:
    try:
        return provider.run(workflow, context, on_progress=on_progress,
                            model_workflow=model_workflow)
    except TypeError as e:
        if "model_workflow" not in str(e):
            raise
        return provider.run(workflow, context, on_progress=on_progress)


def _safe_artifact_rel(rel: str) -> tuple[Path, str]:
    """Normalize provider artifact paths at the runner boundary.

    Artifacts are project-relative logical paths. Providers may emit Windows or
    POSIX separators; internally we store the display form as POSIX and only
    convert to an OS path at the filesystem write boundary.
    """
    rel_norm = str(rel or "").replace("\\", "/")
    pure = PurePosixPath(rel_norm)
    if pure.is_absolute() or re.match(r"^[A-Za-z]:/", rel_norm) or not pure.parts:
        raise RoleNaviError("artifact path escapes the project")
    if any(part in ("", ".", "..") for part in pure.parts):
        raise RoleNaviError("artifact path escapes the project")
    return Path(*pure.parts), pure.as_posix()


_LINKEDIN_SCORE_SECTIONS = {
    "headline",
    "about",
    "experienceentries",
    "skills",
    "education",
}

_INTERVIEW_HEADING_ALIASES = {
    "self-introduction": "Self Introduction",
    "self introduction": "Self Introduction",
    "intro": "Self Introduction",
    "jd requirements": "Job Requirements",
    "requirements": "Job Requirements",
    "role requirements": "Job Requirements",
    "job requirements": "Job Requirements",
    "red flags": "Adversarial Questions",
    "adversarial questions": "Adversarial Questions",
    "adversarial/red-flag questions": "Adversarial Questions",
    "the whys": "The Whys",
    "whys": "The Whys",
    "why answers": "The Whys",
    "behavioral": "Behavioral Questions",
    "behavioral questions": "Behavioral Questions",
    "glossary": "Glossary",
    "industry glossary": "Glossary",
    "company glossary": "Glossary",
    "news": "News",
    "recent news": "News",
    "questions": "Questions to Ask",
    "questions to ask": "Questions to Ask",
    "sources": "Sources",
    "citations": "Sources",
}


def _linkedin_section_key(value: str) -> str:
    return "".join(ch for ch in str(value or "").lower() if ch.isalnum())


def _normalize_linkedin_review_text(text: str) -> str:
    """Repair narrow markdown shape issues before validator/UI parsing.

    The evaluator owns content. The runner owns mechanical parser contracts:
    score table cells must be `N/5`, and the Experience section label is
    `Experience entries`.
    """
    lines = str(text or "").splitlines()
    out: list[str] = []
    scores: dict[str, float] = {}
    for raw in lines:
        line = raw
        stripped = line.strip()
        lower = stripped.lower()
        if lower == "verify in 2 minutes":
            if out:
                previous = out[-1].strip()
                if previous and not previous.startswith("```") and len(previous) <= 80:
                    out.pop()
            continue
        if re.match(r"^###\s+Experience\s*$", stripped, flags=re.I):
            indent = line[:len(line) - len(line.lstrip())]
            out.append(f"{indent}### Experience entries")
            continue
        if stripped.startswith("|") and "---" not in stripped:
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            section_key = _linkedin_section_key(cells[0]) if cells else ""
            if len(cells) >= 2 and section_key in _LINKEDIN_SCORE_SECTIONS:
                score = cells[1]
                match = re.fullmatch(r"(\d+(?:\.\d+)?)(?:\s*/\s*5)?", score)
                if match:
                    value = min(5.0, max(1.0, float(match.group(1))))
                    shown = str(int(value)) if value.is_integer() else f"{value:g}"
                    cells[1] = f"{shown}/5"
                    scores[section_key] = value
                cells = [_strip_linkedin_name_mismatch_phrases(cell) for cell in cells]
                line = "| " + " | ".join(cells) + " |"
        elif _linkedin_name_mismatch_line(stripped):
            continue
        out.append(_strip_linkedin_name_mismatch_phrases(line))
    if _LINKEDIN_SCORE_SECTIONS.issubset(scores):
        weighted = (
            scores["headline"] + scores["about"]
            + scores["experienceentries"] * 3
            + scores["skills"] + scores["education"]
        ) / 7
        canonical_overall = (
            f"Overall score: {weighted:.1f}/5 (weighted: Experience x3)."
        )
        replaced = False
        for index, line in enumerate(out):
            if re.match(r"^\s*Overall score\s*:", line, re.I):
                if not replaced:
                    out[index] = canonical_overall
                    replaced = True
                else:
                    out[index] = ""
        if not replaced:
            table_end = next((
                index for index, line in enumerate(out)
                if index > 0 and not line.strip() and any(
                    candidate.strip().startswith("|") for candidate in out[max(0, index - 8):index]
                )
            ), len(out))
            out.insert(table_end, canonical_overall)
    normalized = "\n".join(out)
    if text.endswith("\n"):
        normalized += "\n"
    return normalized


def _linkedin_name_mismatch_line(line: str) -> bool:
    lower = line.lower()
    patterns = (
        "identity mismatch",
        "name mismatch",
        "profile mismatch",
        "resume/profile identify",
        "resume and profile materials identify",
        "captured linkedin identifies",
        "captured linkedin profile is for",
        "linkedin profile is for",
    )
    return any(pattern in lower for pattern in patterns)


def _strip_linkedin_name_mismatch_phrases(line: str) -> str:
    replacements = [
        (r"\s*,?\s*and the name does not match [^.|]+", ""),
        (r"\s*It names [^,|]+,\s*not [^,|]+,\s*and\s*", "It "),
        (r"\s*It names [^,|]+,\s*not [^,|]+,?\s*", " "),
        (r"\s*No [^|.;]*;\s*identity mismatch (?:exists|remains)\.?", ""),
        (r"\s*;\s*identity mismatch (?:exists|remains)", ""),
        (r"\s*and [A-Z][A-Za-z .'-]+ identity", ""),
        (r"\s*Resolve the LinkedIn identity mismatch\.?", ""),
        (r"\s*Resolve the [^.]*profile mismatch[^.]*\.?", ""),
        (r"\s*Confirm whether the captured [^.]*profile is the correct target profile\.?", ""),
    ]
    out = line
    for pattern, repl in replacements:
        out = re.sub(pattern, repl, out, flags=re.I)
    out = re.sub(r"\s{2,}", " ", out)
    out = out.replace("| . |", "| |")
    return out


def _normalize_interview_prep_text(text: str) -> str:
    out: list[str] = []
    for raw in str(text or "").splitlines():
        match = re.match(r"^(\s*)##\s+(.+?)\s*$", raw)
        if match:
            key = re.sub(r"\s+", " ", match.group(2).strip()).lower()
            key = key.replace("&", "and")
            canonical = _INTERVIEW_HEADING_ALIASES.get(key)
            if canonical:
                out.append(f"{match.group(1)}## {canonical}")
                continue
        out.append(raw)
    normalized = "\n".join(out)
    if text.endswith("\n"):
        normalized += "\n"
    return normalized


def make_mock_project(run_id: str) -> Path:
    """Disposable copy of the fixture project for canned runs."""
    template = Path(__file__).resolve().parents[1] / "fixtures" / "mock-project"
    if not template.is_dir():
        raise RoleNaviError(f"fixture project template missing: {template}")
    dest = home_dir() / "mock-runs" / run_id / "project"
    shutil.copytree(template, dest)
    r = core.run_script("init_db", env=_env_for(dest))
    if r.returncode != 0:
        raise RoleNaviError(f"fixture project store init failed:\n{r.stdout}{r.stderr}")
    return dest


def _write_artifact(ctx: RunContext, ev: dict, provenance: dict | None = None) -> None:
    import tempfile

    from ..repositories import artifacts as artifact_repository
    rel_path, rel = _safe_artifact_rel(ev["path"])
    path = (ctx.project / rel_path).resolve()
    try:
        path.relative_to(ctx.project.resolve())
    except ValueError as e:
        raise RoleNaviError("artifact path escapes the project") from e
    path.parent.mkdir(parents=True, exist_ok=True)
    if "json" in ev:
        content = json.dumps(ev["json"], indent=2, ensure_ascii=False) + "\n"
    else:
        text = str(ev.get("text", ""))
        if rel.startswith("linkedin/") and rel.endswith("/linkedin-review.md"):
            text = _normalize_linkedin_review_text(text)
        elif rel.startswith("interviews/") and rel.endswith(".md"):
            text = _normalize_interview_prep_text(text)
        content = text
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    provenance = provenance or {}
    artifact_repository.record(
        ctx.project, rel, path, getattr(ctx, "workflow", "unknown"),
        input_fingerprint=str(provenance.get("prompt_fingerprint", "")),
        model=str(provenance.get("model", "")),
    )
    ctx.emit("artifact", rel, {"artifact_path": rel, "published": True})
    ctx.artifacts_written.append(rel)
    if rel.endswith("reasons.json"):
        ctx.pending_reasons[str(Path(rel).parent)] = path
    validate = ev.get("validate")
    if validate in ARTIFACT_VALIDATORS:
        args = [str(path)]
        reasons = ctx.pending_reasons.get(str(Path(rel).parent))
        if validate == "resume_bullets" and reasons:
            args += ["--reasons", str(reasons)]
        r = core.run_script(ARTIFACT_VALIDATORS[validate], *args, env=_env_for(ctx.project))
        ctx.validator_results.append({
            "validator": ARTIFACT_VALIDATORS[validate], "target": rel,
            "returncode": r.returncode, "output": r.stdout.strip()[:500]})
        ctx.emit("validator", f"validator {ARTIFACT_VALIDATORS[validate]}: "
                 f"{'PASS' if r.returncode == 0 else 'FAIL'}")
        if r.returncode != 0:
            ctx.failure_class = ctx.failure_class or "validator_failure"


def _store_write(ctx: RunContext, ev: dict) -> bool:
    store = ev["store"]
    rows_path = ctx.project / "data" / f"_incoming_{store}.json"
    rows_path.parent.mkdir(parents=True, exist_ok=True)
    rows_path.write_text(json.dumps(ev["rows"], indent=2), encoding="utf-8")
    r = core.run_script("upsert_rows", store, str(rows_path), env=_env_for(ctx.project))
    ctx.validator_results.append({
        "validator": f"upsert_rows[{store}]", "target": f"{len(ev['rows'])} row(s)",
        "returncode": r.returncode, "output": (r.stdout + r.stderr).strip()[:500]})
    ctx.emit("store", f"store_write {store}: {'OK' if r.returncode == 0 else 'REFUSED'} "
             f"({len(ev['rows'])} row(s))")
    if r.returncode != 0:
        ctx.failure_class = ctx.failure_class or "store_write_refused"
        ctx.emit("store", (r.stdout + r.stderr).strip()[:500])
    try:
        rows_path.unlink()
    except OSError:
        pass
    return r.returncode == 0


def execute_events(ctx: RunContext, events: list[dict],
                   allowed_artifacts: set[str] | None = None,
                   *, allow_provider_mutations: bool = False) -> None:
    for ev in events:
        ctx.check_cancelled()  # between events: never mid-write
        t = ev.get("type")
        if t == "progress":
            if not ctx.streamed:  # streamed providers already showed these live
                ctx.emit("progress", ev.get("text", ""))
        elif t == "artifact":
            if not allow_provider_mutations:
                ctx.mark_partial("provider_direct_write_rejected",
                                 "provider artifact events are not accepted; use typed output")
                continue
            if allowed_artifacts is not None:
                try:
                    _, rel = _safe_artifact_rel(ev.get("path", ""))
                except RoleNaviError as e:
                    ctx.mark_partial("runner_artifact_path_invalid", str(e))
                    continue
                if rel not in allowed_artifacts:
                    ctx.validator_results.append({
                        "validator": "runner_artifact_path_scope",
                        "target": rel,
                        "returncode": 2,
                        "output": "discarded artifact outside allowed path(s): "
                                  + ", ".join(sorted(allowed_artifacts)),
                    })
                    ctx.emit("validator", f"discarded out-of-scope artifact: {rel}")
                    continue
            _write_artifact(ctx, ev)
        elif t == "store_write":
            if not allow_provider_mutations:
                ctx.mark_partial("provider_direct_write_rejected",
                                 "provider store events are not accepted; use typed output")
                continue
            _store_write(ctx, ev)
        elif t == "external_action":
            raise RoleNaviError(
                "external actions are not supported in the public runtime; "
                "write local instructions or tracker notes instead")
        elif t == "result":
            ctx.summary = ev.get("summary", "")
            ctx.emit("result", ctx.summary)
        else:
            ctx.emit("info", f"(ignored unknown event type: {t})")


def _search_gate_text(report: dict) -> str:
    lines = [
        f"{str(report.get('status', 'unknown')).upper()}: "
        f"companies={report.get('companies', 0)} "
        f"candidates={report.get('candidates_logged', 0)} "
        f"kept={report.get('kept', 0)} "
        f"failed_capture={report.get('failed_capture', 0)} "
        f"linkedin_queries={report.get('linkedin_queries', 0)}"
    ]
    for item in report.get("blocking", []):
        lines.append(f"  BLOCKED: {item}")
    for item in report.get("issues", []):
        lines.append(f"  PARTIAL: {item}")
    retry = report.get("retry_companies", [])
    if retry:
        lines.append("  RETRY: " + ", ".join(str(x) for x in retry[:12]))
    return "\n".join(lines)


def _run_search_coverage_gate(ctx: RunContext, label: str,
                              mark: bool = True) -> tuple[int, dict | None]:
    """Run deterministic coverage analysis and reflect partial/blocked status."""
    result = core.run_script("analyze_search_coverage", str(ctx.project),
                             "--json", env=_env_for(ctx.project))
    raw = (result.stdout + result.stderr).strip()
    report = None
    out = raw[:800]
    try:
        report = json.loads(result.stdout)
        out = _search_gate_text(report)
    except (json.JSONDecodeError, TypeError):
        pass
    ctx.validator_results.append({
        "validator": f"analyze_search_coverage[{label}]",
        "target": ctx.project.name,
        "returncode": result.returncode,
        "output": out[:800],
    })
    status_label = (
        "PASS" if result.returncode == 0 else
        "PARTIAL" if result.returncode == 2 else
        "BLOCKED" if result.returncode == 3 else
        "FAIL"
    )
    ctx.emit("validator", f"analyze_search_coverage[{label}]: {status_label}")
    if mark and result.returncode == 2:
        ctx.mark_partial("search_coverage", out[:800])
        ctx.emit("validator", out[:800])
    elif mark and result.returncode == 3:
        ctx.mark_blocked("search_coverage", out[:800])
        ctx.emit("validator", out[:800])
    elif result.returncode not in (0, 2, 3):
        ctx.failure_class = ctx.failure_class or "search_coverage_analysis_failure"
        ctx.emit("validator", out[:800])
    return result.returncode, report


def _plan_gate_text(report: dict) -> str:
    lines = [
        f"{str(report.get('status', 'unknown')).upper()}: "
        f"companies={report.get('companies', 0)} "
        f"source_plan_companies={report.get('source_plan_companies', 0)} "
        f"declared_seeds={len(report.get('declared_seeds', []))}"
    ]
    for item in report.get("issues", []):
        lines.append(f"  PARTIAL: {item}")
    return "\n".join(lines)


def _run_search_plan_gate(ctx: RunContext, label: str,
                          mark: bool = True) -> tuple[int, dict | None]:
    result = core.run_script("analyze_search_plan", str(ctx.project), "--json",
                             env=_env_for(ctx.project))
    raw = (result.stdout + result.stderr).strip()
    report = None
    out = raw[:800]
    try:
        report = json.loads(result.stdout)
        out = _plan_gate_text(report)
    except (json.JSONDecodeError, TypeError):
        pass
    ctx.validator_results.append({
        "validator": f"analyze_search_plan[{label}]",
        "target": ctx.project.name,
        "returncode": result.returncode,
        "output": out[:800],
    })
    status_label = (
        "PASS" if result.returncode == 0 else
        "PARTIAL" if result.returncode == 2 else
        "FAIL"
    )
    ctx.emit("validator", f"analyze_search_plan[{label}]: {status_label}")
    if mark and result.returncode == 2:
        ctx.mark_partial("search_plan", out[:800])
        ctx.emit("validator", out[:800])
    elif result.returncode not in (0, 2):
        ctx.failure_class = ctx.failure_class or "search_plan_analysis_failure"
        ctx.emit("validator", out[:800])
    return result.returncode, report


def _note_merge_status(ctx: RunContext) -> None:
    try:
        merged_log = json.loads((ctx.project / "targets" / "research-log.json")
                                .read_text(encoding="utf-8-sig"))
        if merged_log.get("merge_status") == "partial":
            failed = merged_log.get("failed_parts", [])
            ctx.mark_partial("merge_research_parts",
                             f"merged valid shard parts but skipped {len(failed)} invalid part(s)")
    except (OSError, json.JSONDecodeError):
        pass


def _run_search_merge(ctx: RunContext, label: str) -> bool:
    merge = core.run_script("merge_research_parts", str(ctx.project),
                            env=_env_for(ctx.project))
    merge_out = (merge.stdout + merge.stderr).strip()
    ctx.validator_results.append({
        "validator": f"merge_research_parts{label}",
        "target": ctx.project.name,
        "returncode": merge.returncode,
        "output": merge_out[:800],
    })
    ctx.emit("validator", f"merge_research_parts{label}: "
             f"{'PASS' if merge.returncode == 0 else 'FAIL'}")
    if merge.returncode != 0:
        ctx.emit("validator", merge_out[:800])
        return False
    _note_merge_status(ctx)
    return True


def _run_search_coverage_scaffold(ctx: RunContext, label: str) -> bool:
    coverage = core.run_script("generate_coverage_audit", str(ctx.project),
                               env=_env_for(ctx.project))
    coverage_out = (coverage.stdout + coverage.stderr).strip()
    ctx.validator_results.append({
        "validator": f"generate_coverage_audit{label}",
        "target": ctx.project.name,
        "returncode": coverage.returncode,
        "output": coverage_out[:800],
    })
    ctx.emit("validator", f"generate_coverage_audit{label}: "
             f"{'PASS' if coverage.returncode == 0 else 'FAIL'}")
    if coverage.returncode != 0:
        ctx.emit("validator", coverage_out[:800])
        return False
    return True


def _research_log_kept_count(log_path: Path) -> int:
    try:
        data = json.loads(log_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return 0
    if not isinstance(data, dict):
        return 0
    return sum(
        1 for candidate in data.get("candidates", [])
        if isinstance(candidate, dict) and candidate.get("decision") == "kept"
    )


def _salvage_search_results(ctx: RunContext, reason: str) -> bool:
    """Merge/persist valid captured search rows without asking an LLM to finalize.

    Used when a search is interrupted or a retry lane fails after earlier capture
    produced valid part files. Persistence still goes through persist_job_rows.py,
    which validates and upserts by job_id instead of replacing tables.
    """
    ctx.mark_partial("search_salvage", reason)
    parts_dir = ctx.project / "targets" / "research-log.parts"
    log_path = ctx.project / "targets" / "research-log.json"
    if parts_dir.is_dir() and any(parts_dir.glob("*.json")):
        if not _run_search_merge(ctx, "[salvage]") and not log_path.exists():
            ctx.failure_class = ctx.failure_class or "search_salvage_merge_failure"
            return False
    elif not log_path.exists():
        ctx.failure_class = ctx.failure_class or "search_salvage_no_research_log"
        ctx.emit("validator", "search salvage: no research-log parts or merged log to persist")
        return False

    _run_search_coverage_scaffold(ctx, "[salvage]")
    kept_count = _research_log_kept_count(log_path)
    if kept_count == 0:
        ctx.mark_partial("search_salvage_empty",
                         "No kept job candidates were available to persist.")
        return False

    persist = core.run_script("persist_job_rows", str(log_path),
                              "--project", str(ctx.project),
                              env=_env_for(ctx.project))
    persist_out = (persist.stdout + persist.stderr).strip()
    ctx.validator_results.append({
        "validator": "persist_job_rows[salvage]",
        "target": str(log_path),
        "returncode": persist.returncode,
        "output": persist_out[:800],
    })
    ctx.emit("store", "persist_job_rows[salvage]: "
             f"{'OK' if persist.returncode == 0 else 'REFUSED'}")
    if persist.returncode != 0:
        ctx.mark_partial("search_salvage_persist", persist_out[:800])
        ctx.emit("store", persist_out[:800])
        return False
    ctx.summary = ctx.summary or (
        f"Search interrupted or retry-limited; saved {kept_count} captured job row(s) "
        "with validated upsert."
    )
    return True


def _post_run_resume_checks(ctx: RunContext) -> None:
    resume_artifacts = [
        rel for rel in ctx.artifacts_written
        if rel.startswith("resumes/") and (
            rel.endswith("/resume-draft.md") or rel.endswith("/resume-not-generated.md")
        )
    ]
    draft_rels = [rel for rel in resume_artifacts if rel.endswith("/resume-draft.md")]
    expected_groups = _focused_group_slugs(ctx.project)
    covered_groups = {
        Path(rel).parts[1] for rel in resume_artifacts
        if len(Path(rel).parts) >= 3
    }
    missing_groups = [group for group in expected_groups if group not in covered_groups]
    if missing_groups:
        ctx.validator_results.append({
            "validator": "prep-resume_artifact_generation",
            "target": ctx.project.name,
            "returncode": 1,
            "output": "missing current-run resume artifact(s) for group(s): "
                      + ", ".join(missing_groups),
        })
        ctx.emit("validator", "post-run prep-resume group coverage: FAIL")
        if ctx.workflow == "prep-resume":
            ctx.failure_class = ctx.failure_class or "prep_resume_artifact_missing"
        else:
            ctx.mark_partial("prep-resume_artifact_missing", ", ".join(missing_groups))
    if ctx.workflow == "prep-resume" and not resume_artifacts:
        ctx.failure_class = ctx.failure_class or "prep_resume_artifact_missing"
        ctx.validator_results.append({
            "validator": "prep-resume_artifact_generation",
            "target": ctx.project.name,
            "returncode": 1,
            "output": "no resumes/*/resume-draft.md or resume-not-generated.md artifact was generated in this run",
        })
        ctx.emit("validator", "post-run prep-resume artifact generation: FAIL")
        return
    baseline = ctx.project / "resumes" / "baseline-extracted.md"
    project_doc = _read_json(ctx.project / "project.json", {})
    person_slug = str(project_doc.get("person", "candidate") or "candidate")
    try:
        from . import preflight as _prep_preflight
        profile_dir = _prep_preflight.profile_dir(ctx.project)
    except Exception:
        profile_dir = None
    display_name = str(profile_meta.load(profile_dir).get("name", "") if profile_dir else "")
    user_name = "".join(re.findall(r"[A-Za-z0-9]+", display_name or person_slug)) or "Candidate"
    for rel in draft_rels:
        draft = ctx.project / Path(rel)
        group_dir = draft.parent
        reasons = group_dir / "reasons.json"
        target_brief = group_dir / "target-brief.json"
        bullet_args = [str(draft)]
        if reasons.exists():
            bullet_args += ["--reasons", str(reasons)]
        result = core.run_script("validate_resume_bullets", *bullet_args,
                                 env=_env_for(ctx.project))
        bullet_ok = result.returncode == 0
        out = (result.stdout + result.stderr).strip()
        ctx.validator_results.append({
            "validator": "validate_resume_bullets[prep-resume]",
            "target": rel,
            "returncode": result.returncode,
            "output": out[:800],
        })
        ctx.emit("validator", "post-run validate_resume_bullets: "
                 f"{'PASS' if result.returncode == 0 else 'FAIL'}")
        if result.returncode != 0:
            if ctx.workflow == "prep":
                ctx.mark_partial("prep-resume_validation", out[:800])
            else:
                ctx.failure_class = ctx.failure_class or "prep_resume_validation_failure"
            ctx.emit("validator", out[:800])
        missing = [
            name for name, path in (
                ("baseline-extracted.md", baseline),
                ("reasons.json", reasons),
                ("target-brief.json", target_brief),
            ) if not path.exists()
        ]
        if missing:
            ctx.validator_results.append({
                "validator": "validate_resume_tailoring[prep-resume]",
                "target": rel,
                "returncode": 2,
                "output": "skipped; missing " + ", ".join(missing),
            })
            ctx.mark_partial(
                "prep-resume_tailoring_validation",
                f"{rel}: skipped tailoring validation; missing {', '.join(missing)}",
            )
            continue
        result = core.run_script(
            "validate_resume_tailoring",
            str(draft),
            "--baseline", str(baseline),
            "--target-brief", str(target_brief),
            "--reasons", str(reasons),
            env=_env_for(ctx.project),
        )
        tailoring_ok = result.returncode == 0
        out = (result.stdout + result.stderr).strip()
        ctx.validator_results.append({
            "validator": "validate_resume_tailoring[prep-resume]",
            "target": rel,
            "returncode": result.returncode,
            "output": out[:800],
        })
        ctx.emit("validator", "post-run validate_resume_tailoring: "
                 f"{'PASS' if result.returncode == 0 else 'FAIL'}")
        if result.returncode != 0:
            if ctx.workflow == "prep":
                ctx.mark_partial("prep-resume_tailoring_validation", out[:800])
            else:
                ctx.failure_class = ctx.failure_class or "prep_resume_tailoring_failure"
            ctx.emit("validator", out[:800])
        if not (bullet_ok and tailoring_ok):
            continue

        group = group_dir.name
        docx = group_dir / f"resume_{user_name}_{group}.docx"
        built = core.run_script("build_resume_docx", str(draft), str(docx),
                                env=_env_for(ctx.project))
        built_out = (built.stdout + built.stderr).strip()
        ctx.validator_results.append({
            "validator": "build_resume_docx[prep-resume]", "target": rel,
            "returncode": built.returncode, "output": built_out[:800],
        })
        ctx.emit("validator", "build_resume_docx: " +
                 ("PASS" if built.returncode == 0 else "FAIL"))
        if built.returncode != 0 or not docx.exists():
            if ctx.workflow == "prep":
                ctx.mark_partial("prep-resume_docx", built_out[:800])
            else:
                ctx.failure_class = ctx.failure_class or "prep_resume_docx_failure"
            continue
        from ..repositories import artifacts as artifact_repository
        docx_rel = docx.relative_to(ctx.project).as_posix()
        artifact_repository.record(ctx.project, docx_rel, docx, ctx.workflow)
        ctx.artifacts_written.append(docx_rel)
        ctx.emit("artifact", docx_rel)

        gate = core.run_script("render_docx_gate", str(docx), env=_env_for(ctx.project))
        gate_out = (gate.stdout + gate.stderr).strip()
        ctx.validator_results.append({
            "validator": "render_docx_gate[prep-resume]", "target": docx_rel,
            "returncode": gate.returncode, "output": gate_out[:800],
        })
        validation_path = group_dir / "resume-validation.md"
        with validation_path.open("a", encoding="utf-8") as handle:
            handle.write("\n\n## DOCX QA\n\n" + gate_out + "\n")
        if gate.returncode != 0:
            ctx.mark_partial("prep-resume_render_qa", f"{docx_rel}: {gate_out[:600]}")
            ctx.emit("validator", "render_docx_gate: BLOCKED")
        else:
            ctx.emit("validator", "render_docx_gate: PASS")


def _post_run_strategy_checks(ctx: RunContext) -> None:
    strategy_artifacts = set(ctx.artifacts_written)
    required = {
        "strategy/prep-strategy.md",
        "strategy/target-priorities.md",
        "strategy/group-assignments.json",
    }
    missing = sorted(path for path in required if path not in strategy_artifacts)
    group_artifacts = [
        rel for rel in strategy_artifacts
        if rel.startswith("targets/job-groups/") and rel.endswith(".md")
    ]
    if not group_artifacts:
        missing.append("targets/job-groups/*.md")
    if not missing:
        ctx.validator_results.append({
            "validator": "prep-strategy_artifact_generation",
            "target": ctx.project.name,
            "returncode": 0,
            "output": f"PASS: {len(group_artifacts)} group artifact(s)",
        })
        ctx.emit("validator", "post-run prep-strategy artifact generation: PASS")
        return
    ctx.validator_results.append({
        "validator": "prep-strategy_artifact_generation",
        "target": ctx.project.name,
        "returncode": 1,
        "output": "missing current-run artifact(s): " + ", ".join(missing),
    })
    ctx.emit("validator", "post-run prep-strategy artifact generation: FAIL")
    if ctx.workflow == "prep-strategy":
        ctx.failure_class = ctx.failure_class or "prep_strategy_artifact_missing"
    else:
        ctx.mark_partial("prep-strategy_artifact_missing", ", ".join(missing))


def _post_run_checks(ctx: RunContext) -> None:
    """Workflow-level validation after the agent has written artifacts directly."""
    if ctx.workflow == PROFILE_WORKFLOW and ctx.mode == "live":
        from . import preflight as _pf
        pdir = ctx.project
        stale = _pf._stale_profile(pdir)
        has_profile = (pdir / "candidate-profile.md").exists()
        has_evidence = (pdir / "evidence-map.md").exists()
        ok = not stale and has_profile and has_evidence
        ctx.validator_results.append({
            "validator": "profile_freshness", "target": str(pdir),
            "returncode": 0 if ok else 1,
            "output": (f"STALE: '{stale}' is still newer than candidate-profile.md — "
                       "the run did not rebuild the profile/evidence map"
                       if stale else
                       "candidate-profile.md and evidence-map.md are fresh"
                       if ok else
                       "candidate-profile.md and evidence-map.md must both exist")})
        ctx.emit("validator",
                 f"post-run profile freshness: {'PASS' if ok else 'FAIL'}")
        if not ok:
            ctx.failure_class = ctx.failure_class or "profile_intake_incomplete"
    if ctx.workflow == "prep" and ctx.failure_class:
        # An upstream orchestration gate already recorded the actionable failure.
        # Do not misreport downstream phases that intentionally never started as
        # missing-artifact failures.
        return
    if ctx.workflow in {"prep-strategy", "prep"}:
        _post_run_strategy_checks(ctx)
    prep_validators = []
    if ctx.workflow in {"prep-linkedin", "prep"}:
        linked_artifacts = [
            rel for rel in ctx.artifacts_written
            if rel.startswith("linkedin/") and rel.endswith("/linkedin-review.md")
        ]
        if ctx.workflow == "prep-linkedin" and not linked_artifacts:
            ctx.failure_class = ctx.failure_class or "prep_linkedin_artifact_missing"
            ctx.validator_results.append({
                "validator": "prep-linkedin_artifact_generation",
                "target": ctx.project.name,
                "returncode": 1,
                "output": "no linkedin/*/linkedin-review.md artifact was generated in this run",
            })
            ctx.emit("validator", "post-run prep-linkedin artifact generation: FAIL")
        elif linked_artifacts:
            prep_validators.append(("validate_linkedin_review", "prep-linkedin"))
    if ctx.workflow in {"prep-interview", "prep"}:
        interview_artifacts = [
            rel for rel in ctx.artifacts_written
            if rel.startswith("interviews/") and rel.endswith("/prep-notes.md")
        ]
        if ctx.workflow == "prep-interview" and not interview_artifacts:
            ctx.failure_class = ctx.failure_class or "prep_interview_artifact_missing"
            ctx.validator_results.append({
                "validator": "prep-interview_artifact_generation",
                "target": ctx.project.name,
                "returncode": 1,
                "output": "no interviews/*/prep-notes.md artifact was generated in this run",
            })
            ctx.emit("validator", "post-run prep-interview artifact generation: FAIL")
        elif interview_artifacts:
            prep_validators.append(("validate_interview_prep", "prep-interview"))
    for script_name, label in prep_validators:
        result = core.run_script(script_name, str(ctx.project), env=_env_for(ctx.project))
        out = (result.stdout + result.stderr).strip()
        ctx.validator_results.append({
            "validator": f"{script_name}[{label}]",
            "target": ctx.project.name,
            "returncode": result.returncode,
            "output": out[:800],
        })
        status_label = ("PASS" if result.returncode == 0 else
                        "QUALITY" if script_name == "validate_interview_prep"
                        and result.returncode == 2 else "FAIL")
        ctx.emit("validator", f"post-run {script_name}: {status_label}")
        if result.returncode != 0:
            if ctx.workflow == "prep" or (
                script_name == "validate_interview_prep" and result.returncode == 2
            ):
                ctx.mark_partial(f"{label}_validation", out[:800])
            else:
                ctx.failure_class = ctx.failure_class or f"{label}_validation_failure"
            ctx.emit("validator", out[:800])
    if ctx.workflow in {"prep-resume", "prep"}:
        _post_run_resume_checks(ctx)
    if ctx.workflow == "apply":
        result = core.run_script("validate_application_packets", str(ctx.project),
                                 env=_env_for(ctx.project))
        out = (result.stdout + result.stderr).strip()
        ctx.validator_results.append({
            "validator": "validate_application_packets[apply]",
            "target": ctx.project.name,
            "returncode": result.returncode,
            "output": out[:800],
        })
        ctx.emit("validator", "post-run validate_application_packets: "
                 f"{'PASS' if result.returncode == 0 else 'FAIL'}")
        if result.returncode != 0:
            ctx.failure_class = ctx.failure_class or "application_packet_validation_failure"
            ctx.emit("validator", out[:800])
    if ctx.workflow != "search":
        return
    coverage = core.run_script("generate_coverage_audit", str(ctx.project),
                               env=_env_for(ctx.project))
    cov_out = (coverage.stdout + coverage.stderr).strip()
    ctx.validator_results.append({
        "validator": "generate_coverage_audit[search]",
        "target": ctx.project.name,
        "returncode": coverage.returncode,
        "output": cov_out[:800],
    })
    ctx.emit("validator", "post-run generate_coverage_audit: "
             f"{'PASS' if coverage.returncode == 0 else 'FAIL'}")
    if coverage.returncode != 0:
        ctx.failure_class = ctx.failure_class or "coverage_audit_generation_failure"
        ctx.emit("validator", cov_out[:800])
        return
    gate_rc, _ = _run_search_coverage_gate(ctx, "post-run")
    if gate_rc not in (0, 2, 3):
        return
    artifacts = core.run_script("validate_research_artifacts", str(ctx.project),
                                env=_env_for(ctx.project))
    art_out = (artifacts.stdout + artifacts.stderr).strip()
    ctx.validator_results.append({
        "validator": "validate_research_artifacts[search]",
        "target": ctx.project.name,
        "returncode": artifacts.returncode,
        "output": art_out[:800],
    })
    ctx.emit("validator", "post-run validate_research_artifacts: "
             f"{'PASS' if artifacts.returncode == 0 else 'FAIL'}")
    if artifacts.returncode != 0:
        if ctx.partial_reasons and (ctx.project / "targets" / "research-log.json").exists():
            ctx.mark_partial("post_run_artifact_validation", art_out[:800])
        else:
            ctx.failure_class = ctx.failure_class or "post_run_artifact_validation_failure"
        ctx.emit("validator", art_out[:800])
    r = core.run_script("grade_run", str(ctx.project), env=_env_for(ctx.project))
    out = (r.stdout + r.stderr).strip()
    ctx.validator_results.append({
        "validator": "grade_run[search]", "target": ctx.project.name,
        "returncode": r.returncode, "output": out[:800]})
    ctx.emit("validator", f"post-run grade_run: {'PASS' if r.returncode == 0 else 'FAIL'}")
    if r.returncode != 0:
        if ctx.partial_reasons:
            ctx.mark_partial("post_run_grade", out[:800])
        else:
            ctx.failure_class = ctx.failure_class or "post_run_grade_failure"
        ctx.emit("validator", out[:800])


def _stream_script(ctx: RunContext, name: str, *args: str) -> int:
    cmd = [sys.executable, str(core.scripts_dir() / f"{name}.py"),
           *[str(a) for a in args]]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, text=True,
                            encoding="utf-8", errors="replace",
                            env=_env_for(ctx.project), cwd=str(repo_root()))
    assert proc.stdout is not None
    for line in proc.stdout:
        if ctx.cancel_event is not None and ctx.cancel_event.is_set():
            proc.kill()
            raise RunCancelled("run cancelled by user")
        text = line.rstrip()
        if text:
            ctx.emit("progress", text)
    return proc.wait()


def _extract_json_object(text: str) -> dict:
    raw = str(text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.I).strip()
        raw = re.sub(r"\s*```$", "", raw).strip()
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, re.S)
        if not match:
            raise
        value = json.loads(match.group(0))
    if not isinstance(value, dict):
        raise ValueError("expected JSON object")
    return value


def _build_search_view_filter_plan(ctx: RunContext) -> None:
    meta = project_meta.load(ctx.project)
    if str(meta.get("search_view_filter_mode", "llm")).strip().lower() != "llm":
        return
    if llm.provider_choice(force_mock=False) == "mock":
        ctx.emit("info", "search view filter: no live lightweight LLM provider; using deterministic default")
        return
    core.run_script("build_search_view", str(ctx.project), "--json",
                    env=_env_for(ctx.project))
    default_plan = {}
    plan_path = ctx.project / "targets" / "search-view-filter-plan.json"
    try:
        default_plan = json.loads(plan_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pass
    prompt = (
        "Return ONLY JSON for RoleNavi search-view filtering. "
        "Do not score role fit and do not inspect individual job rows. "
        "Given target locations and target level, produce a conservative "
        "rolenavi-search-view-filter-plan-v1 object. Location filter is positive: "
        "fill target_cities and target_countries; country match includes all cities; "
        "remote postings are included; missing-location postings are excluded. "
        "Level filter is negative: include only clearly out-of-band title terms. "
        "Protect phrases where a low-looking word can be senior, e.g. associate partner, "
        "assistant VP, assistant vice president. Do not use `executive` as a negative "
        "term because titles like Executive Operations can be in-scope. Keep schema keys compatible with this "
        "default plan and change only location_filter/level_filter values when needed.\n\n"
        "Project targets:\n"
        f"{json.dumps({k: meta.get(k) for k in ('target_locations', 'target_level')}, ensure_ascii=False)}\n\n"
        "Default deterministic plan:\n"
        f"{json.dumps(default_plan, indent=2, ensure_ascii=False)}\n"
    )
    try:
        provider = llm.get_provider(False)
        raw = provider.complete(prompt)
        plan = _extract_json_object(raw)
        plan["schema"] = "rolenavi-search-view-filter-plan-v1"
        plan["source"] = "llm_lightweight"
        plan["generated_at"] = _now()
        current_meta = project_meta.load(ctx.project)
        plan["preference_revision"] = int(
            current_meta.get("preference_revision", 0) or 0)
        plan["preference_fingerprint"] = project_meta.preference_fingerprint(current_meta)
        plan_path.parent.mkdir(parents=True, exist_ok=True)
        plan_path.write_text(json.dumps(plan, indent=2, ensure_ascii=False) + "\n",
                             encoding="utf-8")
        ctx.emit("info", "search view filter: lightweight LLM plan written")
    except Exception as exc:
        ctx.emit("info", f"search view filter: LLM plan skipped ({type(exc).__name__}: {str(exc)[:220]})")
    finally:
        rebuild = core.run_script("build_search_view", str(ctx.project), env=_env_for(ctx.project))
        output = (rebuild.stdout + rebuild.stderr).strip()
        ctx.validator_results.append({
            "validator": "build_search_view",
            "target": "public-opportunities.db:job_visibility",
            "returncode": rebuild.returncode,
            "output": output[:800],
        })
        ctx.emit("validator", "build_search_view: " + ("PASS" if rebuild.returncode == 0 else "FAIL"))


def _run_deterministic_search(ctx: RunContext) -> dict:
    ctx.emit("info", "deterministic search: provider-first discovery and JD extraction")
    rc = _stream_script(ctx, "deterministic_search", str(ctx.project))
    summary_path = ctx.project / "targets" / "deterministic-search" / "summary.json"
    summary = {}
    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pass
    out = json.dumps(summary, ensure_ascii=False)[:800] if summary else ""
    ctx.validator_results.append({
        "validator": "deterministic_search",
        "target": ctx.project.name,
        "returncode": rc,
        "output": out,
    })
    if rc == 0:
        ctx.emit("validator", "deterministic_search: PASS")
    elif rc == 2:
        ctx.emit("validator", "deterministic_search: PARTIAL")
        ctx.mark_partial(
            "deterministic_search",
            "Deterministic search completed but captured no persisted rows or left "
            "source gaps. See targets/deterministic-search/summary.json.",
        )
    else:
        ctx.emit("validator", "deterministic_search: FAIL")
        ctx.failure_class = ctx.failure_class or "deterministic_search_failure"

    if summary:
        kept = summary.get("kept_rows", 0)
        seen = summary.get("candidates_seen", 0)
        tasks = summary.get("source_tasks", 0)
        ctx.summary = (
            f"Deterministic search finished: {kept} kept row(s), "
            f"{seen} candidate(s) seen across {tasks} source task(s). "
            "Run `rolenavi run score` for post-capture fit scoring."
        )
    _build_search_view_filter_plan(ctx)
    envelope = {
        "events": [],
        "usage": {"tokens_in": 0, "tokens_out": 0, "cost_usd": 0},
        "model_config": {
            "provider": "deterministic",
            "search_engine": "provider-first-v1",
            "llm_capture": False,
        },
    }
    if not ctx.failure_class:
        if not _run_search_coverage_scaffold(ctx, "[deterministic]"):
            ctx.failure_class = ctx.failure_class or "coverage_audit_generation_failure"
        else:
            gate_rc, _ = _run_search_coverage_gate(ctx, "deterministic")
            if gate_rc not in (0, 2, 3):
                ctx.failure_class = ctx.failure_class or "search_coverage_analysis_failure"
    return envelope


def _run_linkedin_capture_helper(ctx: RunContext, linkedin_url: str,
                                 source_path: Path, required: bool = True) -> int:
    ctx.emit("progress", "running scripts/capture_linkedin_profile.py "
             "(Chrome DevTools Protocol first, fallback to Playwright)")
    rc = _stream_script(ctx, "capture_linkedin_profile",
                        "--url", linkedin_url, "--out", str(source_path))
    ctx.validator_results.append({
        "validator": "capture_linkedin_profile",
        "target": str(source_path),
        "returncode": rc,
        "output": "captured" if rc == 0 else "capture helper did not produce a source file",
    })
    if rc != 0 and required:
        ctx.mark_blocked("capture_linkedin_profile",
                         "LinkedIn capture helper did not produce a source file")
        ctx.failure_class = ctx.failure_class or "linkedin_capture_failed"
        ctx.summary = "LinkedIn capture did not complete; follow the helper guidance and rerun."
    elif rc != 0:
        ctx.mark_partial("capture_linkedin_profile",
                         "LinkedIn capture helper did not produce a source file")
        ctx.emit("info", "LinkedIn capture did not complete; continuing non-LinkedIn prep "
                 "artifacts and leaving LinkedIn review gated on a fresh capture")
    return rc


def _linkedin_capture_fingerprint(path: Path | None) -> str:
    """Hash stable profile facts while ignoring capture-time and sidebar churn."""
    if path is None:
        return ""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    marker = "## Visible LinkedIn Profile Text"
    visible = text.split(marker, 1)[1] if marker in text else text
    stable_sections: list[str] = []
    for label in ("Experience", "Skills", "Education"):
        match = re.search(
            rf"^###\s+{label}\s+surface\s*$\n(.*?)(?=^###\s+\w+\s+surface\s*$|\Z)",
            visible,
            re.I | re.M | re.S,
        )
        if match:
            stable_sections.append(match.group(1).strip())
    stable = "\n\n".join(stable_sections) if stable_sections else visible.strip()
    return hashlib.sha256(stable.encode("utf-8")).hexdigest()


def _job_list_has_rows(project: Path) -> bool:
    from ..repositories import job_rows
    return bool(job_rows(project))


def _ratings_need_profile_refresh(project: Path) -> bool:
    path = project / "strategy" / "job-ratings.json"
    if not path.exists():
        return _job_list_has_rows(project)
    try:
        entries = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _job_list_has_rows(project)
    stale_tokens = ("candidate evidence map missing", "provisional", "title/jd only")
    for entry in entries if isinstance(entries, list) else []:
        rationale = entry.get("rationale", {}) if isinstance(entry, dict) else {}
        text = " ".join(str(v).lower() for v in rationale.values())
        if any(t in text for t in stale_tokens):
            return True
    return False


def _score_rows(project: Path) -> list[dict[str, Any]]:
    from ..repositories import job_rows
    rows = job_rows(project, visible=True)
    for row in rows:
        job_id = str(row.get("job_id", "")).strip()
        snapshot_path = project / "targets" / "jobs" / f"{job_id}.json"
        snapshot = _read_json(snapshot_path, {})
        sections = snapshot.get("structured_sections", {}) if isinstance(snapshot, dict) else {}
        raw_jd = str(snapshot.get("jd_text") or snapshot.get("raw_text") or "")
        requirement_contract: dict[str, Any] = {}
        if isinstance(snapshot, dict) and raw_jd:
            source_fingerprint = hashlib.sha256(raw_jd.encode("utf-8")).hexdigest()
            requirement_path = project / "targets" / "requirements" / f"{job_id}.json"
            cached = _read_json(requirement_path, {})
            if (
                cached.get("source_fingerprint") == source_fingerprint
                and cached.get("schema") == jd_text_cleaner.REQUIREMENT_ATOMS_SCHEMA
            ):
                requirement_contract = cached
            else:
                requirement_contract = jd_text_cleaner.requirement_atoms(raw_jd)
                requirement_contract.update({
                    "job_id": job_id,
                    "source_fingerprint": source_fingerprint,
                    "normalization_status": "deterministic_candidates_ready",
                    "coverage_issues": jd_text_cleaner.requirement_coverage_issues(
                        raw_jd, requirement_contract
                    ),
                })
                requirement_path.parent.mkdir(parents=True, exist_ok=True)
                tmp = requirement_path.with_name(requirement_path.name + ".tmp")
                tmp.write_text(
                    json.dumps(requirement_contract, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                )
                os.replace(tmp, requirement_path)
            atoms = requirement_contract.get("atoms", [])
            minimum = [
                item for item in atoms if isinstance(item, dict)
                and item.get("obligation") in {"minimum_required", "required"}
            ]
            preferred = [
                item for item in atoms if isinstance(item, dict)
                and item.get("obligation") in {"preferred", "nice_to_have"}
            ]
            sections = {
                "requirements": "; ".join(
                    str(item.get("source_quote", "")) for item in minimum
                ),
                "preferred": "; ".join(
                    str(item.get("source_quote", "")) for item in preferred
                ),
                "essential_qualifications": [
                    str(item.get("source_quote", "")) for item in minimum
                    if item.get("category") in {"degree", "license", "security"}
                ],
            }
            snapshot["structured_sections"] = sections
            snapshot["requirements_schema"] = jd_text_cleaner.REQUIREMENT_ATOMS_SCHEMA
            tmp = snapshot_path.with_name(snapshot_path.name + ".tmp")
            tmp.write_text(
                json.dumps(snapshot, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
            )
            os.replace(tmp, snapshot_path)
        if isinstance(sections, dict):
            row["must_have_requirements"] = str(sections.get("requirements", ""))
            row["nice_to_have_requirements"] = str(sections.get("preferred", ""))
        essential = sections.get("essential_qualifications", []) if isinstance(sections, dict) else []
        if isinstance(essential, list):
            row["essential_qualifications"] = "; ".join(str(item) for item in essential)
        else:
            row["essential_qualifications"] = str(essential or "")
        row["requirement_contract"] = requirement_contract.get(
            "scoring_requirements", []
        )
        row["preferred_requirement_contract"] = requirement_contract.get(
            "preferred_requirements", []
        )
        row["requirement_source_fingerprint"] = requirement_contract.get(
            "source_fingerprint", ""
        )
        row["requirement_coverage_issues"] = requirement_contract.get(
            "coverage_issues", []
        )
    return rows


def _truncate_field(value: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 20)].rstrip() + " ...[truncated]"


def _score_compact_job(row: dict[str, Any]) -> dict[str, Any]:
    out = {
        "job_id": row.get("job_id", ""),
        "company": row.get("company", ""),
        "title": row.get("title", ""),
        "location": row.get("location", ""),
        "remote_policy": row.get("remote_policy", ""),
        "posting_status": row.get("posting_status", ""),
        "seniority": row.get("seniority", ""),
        "source_url": row.get("job_page_url") or row.get("source_url", ""),
    }
    for key, limit in SCORE_FIELD_LIMITS.items():
        out[key] = _truncate_field(row.get(key, ""), limit)
    out["requirements"] = row.get("requirement_contract", [])
    out["preferred_requirements"] = row.get("preferred_requirement_contract", [])
    out["requirement_source_fingerprint"] = row.get(
        "requirement_source_fingerprint", ""
    )
    out["requirement_coverage_issues"] = row.get("requirement_coverage_issues", [])
    return out


def _score_batch_size(jobs: list[dict[str, Any]]) -> int:
    return len(json.dumps(jobs, ensure_ascii=False, separators=(",", ":")))


def _make_score_batches(
    jobs: list[dict[str, Any]],
    *,
    max_jobs: int = SCORE_BATCH_MAX_JOBS,
    max_requirements: int = SCORE_BATCH_MAX_REQUIREMENTS,
    max_chars: int = SCORE_BATCH_MAX_CHARS,
) -> list[list[dict[str, Any]]]:
    batches: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for job in jobs:
        candidate = [*current, job]
        requirement_count = sum(
            len(item.get("requirements", [])) for item in candidate
        )
        if current and (
            len(candidate) > max_jobs
            or requirement_count > max_requirements
            or _score_batch_size(candidate) > max_chars
        ):
            batches.append(current)
            current = [job]
        else:
            current = candidate
    if current:
        batches.append(current)
    return batches


def _score_criteria(project: Path) -> list[dict]:
    path = project / "strategy" / "scoring-config.json"
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    criteria = cfg.get("criteria", []) if isinstance(cfg, dict) else []
    if isinstance(cfg, dict) and isinstance(criteria, list):
        existing = {
            str(item.get("name", "")) for item in criteria if isinstance(item, dict)
        }
        additions = [
            {
                "name": "career_trajectory",
                "weight": 0,
                "description": "Career progression under the canonical decision policy; 1 violates it.",
            },
            {
                "name": "essential_qualification",
                "weight": 0,
                "description": "Explicit must-have degree/license/certification coverage; 1 is unmet.",
            },
            {
                "name": "minimum_requirement",
                "weight": 0,
                "description": "Central minimum or eligibility requirement coverage; 1 is unmet.",
            },
        ]
        changed = False
        for item in additions:
            if item["name"] not in existing:
                criteria.append(item)
                changed = True
        dealbreakers = cfg.setdefault("dealbreaker_criteria", [])
        if isinstance(dealbreakers, list):
            for name in ("career_trajectory", "essential_qualification", "minimum_requirement"):
                if name not in dealbreakers:
                    dealbreakers.append(name)
                    changed = True
        if changed:
            path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return [item for item in criteria if isinstance(item, dict) and item.get("name")]


def _score_text_file(path: Path | None, limit: int) -> str:
    if path is None:
        return ""
    try:
        return _truncate_field(path.read_text(encoding="utf-8", errors="replace"), limit)
    except OSError:
        return ""


def _capability_source_fingerprint(profile_text: str, evidence_text: str) -> str:
    return hashlib.sha256(
        (profile_text + "\0" + evidence_text).encode("utf-8")
    ).hexdigest()


def _validate_capability_ledger(
    payload: Any,
    expected_fingerprint: str,
    errors: list[str] | None = None,
) -> dict[str, Any] | None:
    def fail(message: str) -> None:
        if errors is not None:
            errors.append(message)

    if not isinstance(payload, dict):
        fail("payload must be an object")
        return None
    if payload.get("schema") != "rolenavi-capability-ledger-v1":
        fail("schema must be rolenavi-capability-ledger-v1")
        return None
    if payload.get("source_fingerprint") != expected_fingerprint:
        fail("source_fingerprint must exactly match the supplied value")
        return None
    entries = payload.get("entries", [])
    if not isinstance(entries, list):
        fail("entries must be a list")
        return None
    clean: list[dict[str, Any]] = []
    for index, item in enumerate(entries):
        if not isinstance(item, dict):
            fail(f"entries[{index}] must be an object")
            return None
        experience_id = str(item.get("experience_id", "")).strip()
        function = str(item.get("function", "")).strip()
        coverage_type = str(item.get("coverage_type", "")).strip()
        evidence_ids = item.get("evidence_ids", [])
        if (
            not experience_id or not function
            or coverage_type not in {"direct", "adjacent", "exposure"}
            or not isinstance(evidence_ids, list)
        ):
            fail(
                f"entries[{index}] needs experience_id, function, valid coverage_type, "
                "and evidence_ids list"
            )
            return None
        clean.append({
            "experience_id": experience_id[:80],
            "function": function[:160],
            "coverage_type": coverage_type,
            "start": str(item.get("start", ""))[:20],
            "end": str(item.get("end", ""))[:20],
            "evidence_ids": [str(value)[:40] for value in evidence_ids[:20]],
            "scope": str(item.get("scope", ""))[:500],
        })
    return {
        "schema": "rolenavi-capability-ledger-v1",
        "source_fingerprint": expected_fingerprint,
        "generated_at": _now(),
        "entries": clean,
    }


def _ensure_capability_ledger(
    ctx: RunContext,
    provider,
    base_context: dict,
    on_stream,
) -> dict[str, Any]:
    """Build only the cached ledger when canonical profile evidence changed."""
    pdir_raw = str(base_context.get("profile_dir") or "").strip()
    if not pdir_raw:
        return {}
    pdir = Path(pdir_raw)
    profile_text = _score_text_file(pdir / "candidate-profile.md", SCORE_PROFILE_LIMIT)
    evidence_text = _score_text_file(pdir / "evidence-map.md", SCORE_EVIDENCE_LIMIT)
    if not profile_text or not evidence_text:
        return {}
    fingerprint = _capability_source_fingerprint(profile_text, evidence_text)
    path = pdir / "capability-ledger.json"
    cached = _validate_capability_ledger(_read_json(path, {}), fingerprint)
    if cached is not None:
        return cached
    ctx.emit("info", "capability ledger: building from current profile evidence")
    context = {
        "capability_ledger_packet": {
            "candidate_profile_md": profile_text,
            "evidence_map_md": evidence_text,
            "source_fingerprint": fingerprint,
            "expected_artifact": "capability-ledger.json",
        }
    }
    ledger: dict[str, Any] | None = None
    validation_errors: list[str] = []
    for attempt in range(2):
        if attempt:
            context["capability_ledger_packet"]["repair_errors"] = validation_errors
            ctx.emit("info", "capability ledger: repairing invalid typed output once")
        envelope = _provider_run(
            provider,
            "capability-ledger",
            context,
            _labelled_stream(ctx, f"capability-ledger-{attempt + 1}", on_stream),
            model_workflow="capability-ledger",
        )
        output = _extract_runner_artifact_payload(_result_text(envelope))
        artifacts = output.get("artifacts", []) if isinstance(output, dict) else []
        ledger_value: Any = None
        for artifact in artifacts if isinstance(artifacts, list) else []:
            if not isinstance(artifact, dict) or artifact.get("path") != "capability-ledger.json":
                continue
            ledger_value = artifact.get("json")
            if ledger_value is None and isinstance(artifact.get("text"), str):
                try:
                    ledger_value = json.loads(artifact["text"])
                except json.JSONDecodeError:
                    ledger_value = None
        validation_errors = []
        ledger = _validate_capability_ledger(
            ledger_value, fingerprint, validation_errors
        )
        if ledger is not None:
            break
    if ledger is None:
        raise RoleNaviError(
            "capability-ledger build returned invalid typed output after repair: "
            + "; ".join(validation_errors[:5])
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(ledger, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    ctx.emit("artifact", f"capability ledger ready: {len(ledger['entries'])} experience entries")
    return ledger


def _score_candidate_context(base_context: dict, project: Path | None = None) -> dict[str, Any]:
    pdir_raw = str(base_context.get("profile_dir") or "").strip()
    pdir = Path(pdir_raw) if pdir_raw else None
    return {
        "candidate_profile_md": _score_text_file(
            pdir / "candidate-profile.md" if pdir else None,
            SCORE_PROFILE_LIMIT,
        ),
        "evidence_map_md": _score_text_file(
            pdir / "evidence-map.md" if pdir else None,
            SCORE_EVIDENCE_LIMIT,
        ),
        "decision_policy": decision_policy.load(pdir),
        "project_preferences": project_meta.load(project) if project else {},
        "capability_ledger": _read_json(
            pdir / "capability-ledger.json", {}
        ) if pdir else {},
    }


def _static_scoring_policy() -> dict:
    return _read_json(
        Path(__file__).resolve().parents[2] / "references" / "scoring-policy.json", {}
    )


def _score_dependency_fingerprint(
    job: dict[str, Any],
    candidate_context: dict[str, Any],
    criteria: list[dict],
    scoring_policy: dict[str, Any],
) -> str:
    payload = {
        "contract": SCORE_CONTRACT_VERSION,
        "job_id": job.get("job_id", ""),
        "requirement_source_fingerprint": job.get("requirement_source_fingerprint", ""),
        "requirements": job.get("requirements", []),
        "preferred_requirements": job.get("preferred_requirements", []),
        "candidate": candidate_context,
        "criteria": criteria,
        "scoring_policy": scoring_policy,
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _rating_is_current(
    rating: dict[str, Any],
    dependency_fingerprint: str,
    criteria_names: set[str],
    expected_requirements: list[dict],
) -> bool:
    meta = rating.get("score_meta", {})
    if not isinstance(meta, dict) or meta.get("dependency_fingerprint") != dependency_fingerprint:
        return False
    values = rating.get("ratings", {})
    if (
        not isinstance(values, dict) or set(values) != criteria_names
        or any(not isinstance(value, int) or not 1 <= value <= 5 for value in values.values())
    ):
        return False
    expected_ids = {
        str(item.get("requirement_id", "")) for item in expected_requirements
        if isinstance(item, dict) and item.get("requirement_id")
    }
    evaluations = rating.get("requirement_evaluations", [])
    if not isinstance(evaluations, list):
        return False
    actual_ids = {
        str(item.get("requirement_id", "")) for item in evaluations
        if isinstance(item, dict) and item.get("requirement_id")
    }
    return expected_ids == actual_ids


def _text_limited(path: Path | None, limit: int = 12000) -> str:
    if path is None:
        return ""
    try:
        return _truncate_field(path.read_text(encoding="utf-8", errors="replace"), limit)
    except OSError:
        return ""


def _glob_texts(base: Path, pattern: str, *, limit_each: int = 5000,
                max_files: int = 20) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    try:
        paths = sorted(base.glob(pattern))
    except OSError:
        return out
    for path in paths[:max_files]:
        if not path.is_file():
            continue
        try:
            rel = path.relative_to(base).as_posix()
        except ValueError:
            rel = path.name
        out.append({"path": rel, "text": _text_limited(path, limit_each)})
    return out


def _groups_for_rows(rows: list[dict]) -> list[dict]:
    groups: list[dict] = []
    by_slug: dict[str, dict] = {}
    for row in rows:
        slug = _slug(row.get("job_group", ""), "")
        if not slug:
            slug = "ungrouped"
        group = by_slug.get(slug)
        if group is None:
            group = {"slug": slug, "jobs": []}
            by_slug[slug] = group
            groups.append(group)
        group["jobs"].append(row)
    return groups


def _focused_groups(project: Path) -> list[dict]:
    return _groups_for_rows(_focused_job_rows(project))


PREP_DISPOSITIONS = {"pursue", "conditional", "parked"}


def _strategy_dispositions(project: Path) -> dict[str, dict[str, str]]:
    """Return the validated strategy scope, with legacy files defaulting to pursue."""
    data = _read_json(project / "strategy" / "group-assignments.json", {})
    rows = data.get("assignments", []) if isinstance(data, dict) else []
    out: dict[str, dict[str, str]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        job_id = str(row.get("job_id", "")).strip()
        if not job_id:
            continue
        disposition = str(row.get("disposition", "pursue")).strip().lower()
        if disposition not in PREP_DISPOSITIONS:
            disposition = "pursue"
        out[job_id] = {
            "disposition": disposition,
            "reason": str(row.get("disposition_reason", "")).strip(),
        }
    return out


def _groups_for_prep(project: Path, allowed: set[str]) -> tuple[list[dict], list[dict]]:
    dispositions = _strategy_dispositions(project)
    eligible: list[dict] = []
    skipped: list[dict] = []
    for group in _focused_groups(project):
        jobs = list(group.get("jobs", []))
        selected = [row for row in jobs if dispositions.get(
            str(row.get("job_id", "")), {"disposition": "pursue"}
        )["disposition"] in allowed]
        target = eligible if selected else skipped
        target.append({**group, "jobs": selected or jobs})
    return eligible, skipped


def _processed_jd_rel(job_id: str) -> str:
    raw = str(job_id or "job")
    digest = hashlib.sha1(raw.encode("utf-8", errors="replace")).hexdigest()[:10]
    return f"targets/jobs-processed/{_slug(raw, 'job')[:48].strip('-')}-{digest}.json"


def _processed_jd_brief(project: Path, row: dict) -> dict:
    job_id = str(row.get("job_id", "") or "").strip()
    snapshot = _read_json(project / "targets" / "jobs" / f"{job_id}.json", {}) if job_id else {}
    return jd_text_cleaner.jd_interview_brief(row, snapshot if isinstance(snapshot, dict) else {})


def _prepare_processed_jds(ctx: RunContext,
                           rows: list[dict] | None = None) -> dict[str, dict]:
    briefs: dict[str, dict] = {}
    rows = _focused_job_rows(ctx.project) if rows is None else rows
    if not rows:
        return briefs
    for row in rows:
        job_id = str(row.get("job_id", "") or "").strip()
        if not job_id:
            continue
        brief = _processed_jd_brief(ctx.project, row)
        payload = {
            "schema": "rolenavi-processed-jd-v1",
            "generated_at": _now(),
            "job_id": job_id,
            "brief": brief,
        }
        rel = _processed_jd_rel(job_id)
        _write_artifact(ctx, {"type": "artifact", "path": rel, "json": payload})
        briefs[job_id] = brief
    ctx.emit("validator", f"processed JD cache: PASS ({len(briefs)} focused role(s))")
    return briefs


def _load_processed_jds(project: Path, rows: list[dict]) -> list[dict]:
    out: list[dict] = []
    for row in rows:
        job_id = str(row.get("job_id", "") or "").strip()
        if not job_id:
            continue
        data = _read_json(project / _processed_jd_rel(job_id), {})
        brief = data.get("brief") if isinstance(data, dict) else None
        if not isinstance(brief, dict):
            brief = _processed_jd_brief(project, row)
        out.append(brief)
    return out


def _group_file_packet(project: Path, group_slug: str, *, limit: int = 5000) -> str:
    if not group_slug or group_slug == "ungrouped":
        return ""
    return _text_limited(project / "targets" / "job-groups" / f"{group_slug}.md", limit)


def _strategy_support_packet(project: Path) -> dict[str, str]:
    return {
        "prep_strategy_md": _text_limited(project / "strategy" / "prep-strategy.md", 4000),
        "target_priorities_md": _text_limited(project / "strategy" / "target-priorities.md", 2500),
    }


def _packet_job_row(row: dict) -> dict[str, str]:
    keys = (
        "job_id", "company", "title", "job_group", "location", "remote_policy",
        "source_url", "job_page_url", "posting_status", "seniority",
        "fit_score", "priority",
    )
    return {key: str(row.get(key, "") or "")[:500] for key in keys}


def _packet_job_rows(rows: list[dict]) -> list[dict[str, str]]:
    return [_packet_job_row(row) for row in rows]


def _baseline_resume_context(project: Path) -> dict[str, str]:
    return {
        "baseline_extracted_md": _text_limited(
            project / "resumes" / "baseline-extracted.md", 9000
        )
    }


def _latest_resume_source(profile_dir: Path | None) -> Path | None:
    """Choose the latest user-provided resume, preferring resume-like documents."""
    if profile_dir is None:
        return None
    candidates = []
    for item in profile_meta.material_files(profile_dir):
        path = profile_dir / str(item.get("name", ""))
        if not path.is_file():
            continue
        name = path.name.lower()
        suffix_rank = 2 if path.suffix.lower() in {".pdf", ".docx"} else 1
        name_rank = 2 if re.search(r"(?:resume|curriculum|\bcv\b)", name) else 0
        candidates.append((name_rank, suffix_rank, int(item.get("mtime", 0)), path))
    if not candidates:
        return None
    # A clearly named resume beats a newer auxiliary note; within that class use
    # the latest source. PDF/DOCX wins over plain text on equal timestamps.
    return max(candidates, key=lambda value: value[:3])[3]


def _ensure_baseline_extracted(ctx: RunContext, profile_dir: Path | None) -> bool:
    source = _latest_resume_source(profile_dir)
    if source is None:
        ctx.failure_class = ctx.failure_class or "baseline_resume_missing"
        ctx.emit("validator", "baseline resume extraction: FAIL (no user-provided resume)")
        return False
    try:
        text = source_extract.extract_text(source).strip()
    except (OSError, ValueError) as exc:
        text = ""
        detail = str(exc)
    else:
        detail = ""
    if not text:
        ctx.failure_class = ctx.failure_class or "baseline_resume_extraction_failure"
        ctx.emit("validator", "baseline resume extraction: FAIL " +
                 (f"({detail})" if detail else "(no extractable text)"))
        return False
    body = (
        "# Baseline Resume Extraction\n\n"
        f"> Source: {source.name}\n"
        f"> Source mtime: {int(source.stat().st_mtime)}\n"
        "> Runner-owned; group agents must not overwrite this file.\n\n"
        + text + "\n"
    )
    _write_artifact(ctx, {
        "type": "artifact",
        "path": "resumes/baseline-extracted.md",
        "text": body,
    })
    ctx.emit("validator", f"baseline resume extraction: PASS ({source.name})")
    return True


def _resume_group_allowed_artifacts(group_slug: str) -> set[str]:
    group = _slug(group_slug, "ungrouped")
    return {
        f"resumes/{group}/target-brief.json",
        f"resumes/{group}/resume-score.md",
        f"resumes/{group}/resume-draft.md",
        f"resumes/{group}/reasons.json",
        f"resumes/{group}/resume-validation.md",
        f"resumes/{group}/resume-not-generated.md",
    }


def _resume_artifact_contract(group_slug: str) -> dict:
    """Single machine-readable contract shared by prompt, runner, and gate."""
    group = _slug(group_slug, "ungrouped")
    base = f"resumes/{group}"
    return {
        "schema": RESUME_ARTIFACT_SCHEMA,
        "group_slug": group,
        "active_group": {
            "required_artifacts": [
                f"{base}/target-brief.json", f"{base}/resume-score.md",
                f"{base}/resume-draft.md", f"{base}/reasons.json",
                f"{base}/resume-validation.md",
            ],
            "json_artifacts_use_json_field": True,
        },
        "parked_group": {
            "required_artifacts": [f"{base}/resume-not-generated.md"],
            "mutually_exclusive_with_active_group": True,
        },
        "target_brief": {
            "schema": RESUME_TARGET_BRIEF_SCHEMA,
            "required": [
                "schema", "group", "source_job_ids", "positioning_angle",
                "requirements", "gaps",
            ],
            "requirement_required": [
                "id", "priority", "text", "keywords", "source_job_ids",
            ],
            "priority_enum": ["must", "preferred"],
            "gap_required": ["requirement_id", "gap"],
        },
        "reasons": {
            "type": "array",
            "item_required": [
                "bullet_prefix", "reason", "evidence", "requirement_ids",
                "source_job_ids", "rewrite_type", "baseline_source_bullet_id",
            ],
            "reason_enum": list(RESUME_REASON_VALUES),
            "rewrite_type_enum": list(RESUME_REWRITE_TYPES),
        },
        "lifecycle": ["generated", "validated", "published"],
    }


def _linkedin_group_artifact(group_slug: str) -> str:
    return f"linkedin/{_slug(group_slug, 'group')}/linkedin-review.md"


INTERVIEW_STAGE_SECTIONS = {
    "company-research": ("Glossary", "News", "Sources"),
    "whys": ("The Whys",),
    "qa": (
        "Self Introduction",
        "Job Requirements",
        "Adversarial Questions",
        "Behavioral Questions",
        "Questions to Ask",
    ),
}


def _runner_interview_role_packet(project: Path, pdir: Path | None, role: dict,
                                  expected_artifact: str,
                                  quality_retry: str | None = None,
                                  stage: str | None = None) -> dict:
    group = _slug(role.get("job_group", ""), "")
    job_id = str(role.get("job_id", "")).strip()
    resume_dir = project / "resumes" / group if group else project / "resumes"
    snapshot = _read_json(project / "targets" / "jobs" / f"{job_id}.json", {}) if job_id else {}
    jd_brief = jd_text_cleaner.jd_interview_brief(role, snapshot if isinstance(snapshot, dict) else {})
    packet = {
        "workflow": "prep-interview",
        "scope": "single-focused-role",
        "stage": stage or "full",
        "hard_rule": (
            "Generate exactly one artifact at expected_artifact. Do not generate "
            "or reference any other focused role as the target position."
        ),
        "expected_artifact": expected_artifact,
        "expected_sections": list(INTERVIEW_STAGE_SECTIONS.get(stage or "", ())),
        "role": role,
        "candidate_profile_md": _text_limited(
            pdir / "candidate-profile.md" if pdir else None, 5000
        ),
        "decision_policy": decision_policy.load(pdir),
        "evidence_map_md": _text_limited(
            pdir / "evidence-map.md" if pdir else None, 5000
        ),
        "jd_interview_brief": jd_brief,
        "target_group_file": _text_limited(
            project / "targets" / "job-groups" / f"{group}.md", 4000
        ) if group else "",
    }
    if stage in {"whys", "qa"}:
        packet["story_bank_json"] = _text_limited(
            project / "interviews" / "story-bank.json", 12000
        )
    if stage == "qa":
        packet["resume_group_files"] = _glob_texts(
            resume_dir, "*.md", limit_each=5000, max_files=8
        )
    if stage == "company-research":
        packet.pop("candidate_profile_md", None)
        packet.pop("evidence_map_md", None)
    if quality_retry:
        packet["quality_retry_validator_output"] = quality_retry[:4000]
    return packet


def _runner_resume_group_packet(project: Path, pdir: Path | None,
                                group: dict) -> dict:
    slug = str(group.get("slug") or "ungrouped")
    jobs = [job for job in group.get("jobs", []) if isinstance(job, dict)]
    resume_dir = project / "resumes" / slug
    packet = {
        "workflow": "prep-resume",
        "scope": "single-focused-group",
        "group_slug": slug,
        "expected_artifacts": sorted(_resume_group_allowed_artifacts(slug)),
        "artifact_contract": _resume_artifact_contract(slug),
        "focused_jobs": _packet_job_rows(jobs),
        "processed_jd_briefs": _load_processed_jds(project, jobs),
        "candidate_profile_md": _text_limited(
            pdir / "candidate-profile.md" if pdir else None, 6000
        ),
        "evidence_map_md": _text_limited(
            pdir / "evidence-map.md" if pdir else None, 6000
        ),
        "decision_policy": decision_policy.load(pdir),
        "baseline_resume": _baseline_resume_context(project),
        "target_group_file": _group_file_packet(project, slug, limit=5000),
        "strategy_context": _strategy_support_packet(project),
        "existing_group_resume_files": _glob_texts(
            resume_dir, "*.md", limit_each=3000, max_files=5
        ),
    }
    return packet


def _runner_linkedin_group_packet(project: Path, pdir: Path | None,
                                  group: dict) -> dict:
    slug = str(group.get("slug") or "ungrouped")
    jobs = [job for job in group.get("jobs", []) if isinstance(job, dict)]
    return {
        "workflow": "prep-linkedin",
        "scope": "single-focused-group",
        "group_slug": slug,
        "expected_artifact": _linkedin_group_artifact(slug),
        "focused_jobs": _packet_job_rows(jobs),
        "processed_jd_briefs": _load_processed_jds(project, jobs),
        "linkedin_current_md": _text_limited(
            pdir / "linkedin-current.md" if pdir else None, 50000
        ),
        "candidate_profile_md": _text_limited(
            pdir / "candidate-profile.md" if pdir else None, 6000
        ),
        "evidence_map_md": _text_limited(
            pdir / "evidence-map.md" if pdir else None, 6000
        ),
        "decision_policy": decision_policy.load(pdir),
        "target_group_file": _group_file_packet(project, slug, limit=5000),
        "strategy_context": _strategy_support_packet(project),
        "resume_group_files": _glob_texts(
            project / "resumes" / slug, "*.md", limit_each=3000, max_files=5
        ),
    }


def _recommended_application_resume(project: Path, row: dict) -> str:
    group = _slug(row.get("job_group", ""), "")
    directory = project / "resumes" / group if group else None
    if directory and directory.is_dir():
        docx = sorted(directory.glob("*.docx"), key=lambda p: p.stat().st_mtime, reverse=True)
        if docx:
            return docx[0].relative_to(project).as_posix()
        draft = directory / "resume-draft.md"
        if draft.exists():
            return draft.relative_to(project).as_posix()
    baseline = project / "resumes" / "baseline-extracted.md"
    return baseline.relative_to(project).as_posix() if baseline.exists() else ""


def _recommended_application_linkedin(project: Path, row: dict) -> str:
    group = _slug(row.get("job_group", ""), "")
    review = project / "linkedin" / group / "linkedin-review.md"
    return review.relative_to(project).as_posix() if group and review.exists() else ""


def _application_artifact_path(row: dict) -> str:
    raw = str(row.get("job_id", "") or "job")
    digest = hashlib.sha1(raw.encode("utf-8", errors="replace")).hexdigest()[:8]
    stem = _slug(raw, "job")[:48].strip("-")
    return f"applications/{stem}-{digest}/application-instructions.md"


def _runner_application_role_packet(project: Path, pdir: Path | None, row: dict,
                                    route_audit: dict, selected: list[dict]) -> dict:
    expected = _application_artifact_path(row)
    same_company = [
        _packet_job_row(other) for other in selected
        if other.get("job_id") != row.get("job_id")
        and _slug(other.get("company", ""), "") == _slug(row.get("company", ""), "")
    ]
    prior = project / PurePosixPath(expected)
    return {
        "workflow": "apply",
        "scope": "single-focused-position-read-only-route-audit",
        "expected_artifact": expected,
        "role": _packet_job_row(row),
        "processed_jd_brief": _processed_jd_brief(project, row),
        "application_route_audit": route_audit,
        "recommended_resume_path": _recommended_application_resume(project, row),
        "recommended_linkedin_path": _recommended_application_linkedin(project, row),
        "candidate_profile_md": _text_limited(
            pdir / "candidate-profile.md" if pdir else None, 9000
        ),
        "evidence_map_md": _text_limited(
            pdir / "evidence-map.md" if pdir else None, 9000
        ),
        "baseline_resume": _baseline_resume_context(project),
        "decision_policy": decision_policy.load(pdir),
        "target_group_file": _group_file_packet(
            project, _slug(row.get("job_group", ""), ""), limit=5000
        ),
        "same_company_selected_roles": same_company,
        "existing_packet": _text_limited(prior, 6000),
        "artifact_contract": {
            "exact_path": expected,
            "exact_count": 1,
            "store_writes": "Return an empty list; the runner creates/preserves the tracker row atomically.",
            "required_headings": [
                "Position summary", "Current posting state", "Application route",
                "Required materials", "Field-by-field guidance", "Sensitive fields",
                "Step-by-step user instructions", "What to save after submission",
                "Tracker update recommendation",
            ],
        },
        "safety_boundary": (
            "Local instructions only. Never authenticate, enter/transmit candidate data, upload a "
            "file, click Next/Submit/Apply, accept terms, create an account, or claim submission."
        ),
    }


def _runner_context_packet(project: Path, pdir: Path | None,
                           workflow: str) -> dict:
    focused = _focused_job_rows(project)
    groups = _focused_groups(project)
    base_profile = {
        "candidate_profile_md": _text_limited(
            pdir / "candidate-profile.md" if pdir else None, 9000
        ),
        "evidence_map_md": _text_limited(
            pdir / "evidence-map.md" if pdir else None, 9000
        ),
        "decision_policy": decision_policy.load(pdir),
    }
    if workflow == "prep-strategy":
        all_focused = focused
        focused = _strategy_focused_job_rows(project)
        groups = _groups_for_rows(focused)
        scoped_ids = {str(row.get("job_id", "")) for row in focused}
        excluded_unscored = [
            str(row.get("job_id", "")) for row in all_focused
            if str(row.get("job_id", "")) not in scoped_ids
        ]
        current_group_files = [
            {"group_slug": group["slug"], "text": _group_file_packet(project, group["slug"], limit=4500)}
            for group in groups if group.get("slug") and group.get("slug") != "ungrouped"
        ]
        return {
            "workflow": workflow,
            **base_profile,
            "scope": "focused-and-current-scored-only",
            "excluded_unscored_job_ids": excluded_unscored,
            "focused_jobs": _packet_job_rows(focused),
            "focused_groups": [{"slug": g["slug"], "job_ids": [j.get("job_id", "") for j in g["jobs"]]} for g in groups],
            "processed_jd_briefs": _load_processed_jds(project, focused),
            "current_group_files": current_group_files,
            "scoring_context": {
                "job_scores_json": _text_limited(project / "strategy" / "job-scores.json", 4000),
                "job_ratings_json": _text_limited(project / "strategy" / "job-ratings.json", 4000),
            },
            "prioritization_model_md": _text_limited(
                Path(__file__).resolve().parents[2] / "references" / "prioritization-model.md",
                5000,
            ),
        }
    if workflow == "story-bank":
        return {
            "workflow": workflow,
            **base_profile,
            "baseline_resume": _baseline_resume_context(project),
            "existing_story_bank_json": _text_limited(
                project / "interviews" / "story-bank.json", 12000
            ),
        }
    packet = {
        "workflow": workflow,
        "focused_jobs": _packet_job_rows(focused),
        **base_profile,
        "target_group_files": [
            {"group_slug": group["slug"], "text": _group_file_packet(project, group["slug"], limit=5000)}
            for group in groups if group.get("slug") and group.get("slug") != "ungrouped"
        ],
        "strategy_context": _strategy_support_packet(project),
    }
    if workflow == "prep-linkedin":
        packet["linkedin_current_md"] = _text_limited(
            pdir / "linkedin-current.md" if pdir else None, 50000
        )
        packet["resume_files"] = _glob_texts(
            project / "resumes", "**/*.md", limit_each=6000, max_files=30
        )
    elif workflow == "prep-resume":
        packet["focused_groups"] = [{"slug": g["slug"], "job_ids": [j.get("job_id", "") for j in g["jobs"]]} for g in groups]
        packet["baseline_resume"] = _baseline_resume_context(project)
    elif workflow == "prep-interview":
        packet["interview_context_json"] = _text_limited(
            project / "interviews" / "interview-context.json", 20000
        )
        packet["story_bank_json"] = _text_limited(
            project / "interviews" / "story-bank.json", 16000
        )
        packet["resume_files"] = _glob_texts(
            project / "resumes", "**/*.md", limit_each=6000, max_files=30
        )
    elif workflow == "apply":
        packet["application_packets"] = _glob_texts(
            project / "applications", "**/*.md", limit_each=6000, max_files=30
        )
    return packet


def _load_score_ratings(project: Path) -> list[dict]:
    path = project / "strategy" / "job-ratings.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return [item for item in data if isinstance(item, dict)]


def _merge_score_ratings(project: Path, incoming: list[dict]) -> int:
    if not incoming:
        return 0
    strategy_dir = project / "strategy"
    strategy_dir.mkdir(parents=True, exist_ok=True)
    # A completed score pass is a snapshot of the current visible job set.
    # Retaining prior partial/removed rows lets stale malformed ratings poison
    # deterministic finalization even after the current pass is complete.
    by_id: dict[str, dict] = {}
    for item in incoming:
        job_id = str(item.get("job_id", "")).strip()
        ratings = item.get("ratings", {})
        if not job_id or not isinstance(ratings, dict):
            continue
        by_id[job_id] = item
    changed = len(by_id)
    if changed:
        out = [by_id[key] for key in sorted(by_id)]
        path = strategy_dir / "job-ratings.json"
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(
            json.dumps(out, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp, path)
    return changed


def _write_score_freshness(
    project: Path,
    *,
    current_ids: set[str],
    unresolved_ids: set[str],
    dependency_fingerprints: dict[str, str],
) -> None:
    payload = {
        "schema": "rolenavi-score-freshness-v1",
        "contract_version": SCORE_CONTRACT_VERSION,
        "generated_at": _now(),
        "current_job_ids": sorted(current_ids),
        "unresolved_job_ids": sorted(unresolved_ids),
        "dependency_fingerprints": {
            key: dependency_fingerprints[key] for key in sorted(current_ids)
        },
    }
    path = project / "strategy" / "score-freshness.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _score_batch_result_text(envelope: dict) -> str:
    for ev in reversed(envelope.get("events", [])):
        if isinstance(ev, dict) and ev.get("type") == "result":
            return str(ev.get("content") or ev.get("summary") or "")
    return ""


def _extract_score_batch_ratings(text: str) -> list[dict]:
    raw = str(text or "").strip()
    if not raw:
        return []
    for marker in ("SCORE_BATCH_OUTPUT_JSON:", "SCORE_OUTPUT_JSON:"):
        if marker in raw:
            raw = raw.split(marker, 1)[1].strip()
            break
    try:
        payload = _extract_json_object(raw)
    except (ValueError, json.JSONDecodeError):
        return []
    if payload.get("schema") == "rolenavi-score-batch-output-v1":
        ratings = payload.get("job_ratings", [])
    elif payload.get("schema") == "rolenavi-score-output-v1":
        ratings = payload.get("job_ratings", [])
    else:
        return []
    return [item for item in ratings if isinstance(item, dict)]


def _validate_score_batch_ratings(
    ratings: list[dict],
    expected_ids: set[str],
    criteria_names: set[str],
    policy_specs: dict[str, dict] | None = None,
    requirements_by_job: dict[str, list[dict]] | None = None,
    diagnostics: dict[str, list[str]] | None = None,
) -> tuple[dict[str, dict], set[str], list[str]]:
    accepted: dict[str, dict] = {}
    issues: list[str] = []
    unknown = duplicate = incomplete = 0
    for item in ratings:
        job_id = str(item.get("job_id", "")).strip()
        if job_id not in expected_ids:
            unknown += 1
            continue
        if job_id in accepted:
            duplicate += 1
            continue
        rating_values = item.get("ratings", {})
        if not isinstance(rating_values, dict):
            incomplete += 1
            if diagnostics is not None:
                diagnostics[job_id] = ["ratings must be an object"]
            continue
        normalized_values: dict[str, int] = {}
        nested_rationale: dict[str, str] = {}
        for key, value in rating_values.items():
            name = str(key)
            if isinstance(value, dict):
                score = value.get("score")
                rationale = str(value.get("rationale", "")).strip()
                if rationale:
                    nested_rationale[name] = rationale
                value = score
            if isinstance(value, int) and 1 <= value <= 5:
                normalized_values[name] = value
        rating_keys = {str(k) for k in rating_values}
        missing_criteria = criteria_names - rating_keys
        extra_criteria = rating_keys - criteria_names
        bad_values = [
            key for key, value in rating_values.items()
            if key in criteria_names and key not in normalized_values
        ]
        expected_policy_ids = set(policy_specs or {})
        evaluations = item.get("policy_evaluations", [])
        if not isinstance(evaluations, list):
            evaluations = []
        normalized_evaluations: list[dict[str, str]] = []
        seen_policy_ids: set[str] = set()
        bad_policy_evaluation = False
        for evaluation in evaluations:
            if not isinstance(evaluation, dict):
                bad_policy_evaluation = True
                continue
            policy_id = str(evaluation.get("policy_id", "")).strip()
            outcome = str(evaluation.get("outcome", "")).strip().lower()
            confidence = str(evaluation.get("confidence", "")).strip().lower()
            evidence = str(evaluation.get("evidence", "")).strip()
            if (
                policy_id not in expected_policy_ids
                or policy_id in seen_policy_ids
                or outcome not in {"satisfied", "violated", "uncertain"}
                or confidence not in {"high", "medium", "low"}
                or not evidence
            ):
                bad_policy_evaluation = True
                continue
            seen_policy_ids.add(policy_id)
            normalized_evaluations.append({
                "policy_id": policy_id,
                "outcome": outcome,
                "confidence": confidence,
                "evidence": evidence[:160],
            })
        if seen_policy_ids != expected_policy_ids:
            bad_policy_evaluation = True
        expected_requirements = {
            str(req.get("requirement_id", "")): req
            for req in (requirements_by_job or {}).get(job_id, [])
            if isinstance(req, dict) and str(req.get("requirement_id", ""))
        }
        evaluations = item.get("requirement_evaluations", [])
        if not isinstance(evaluations, list):
            evaluations = []
        normalized_requirement_evaluations: list[dict[str, Any]] = []
        seen_requirements: set[str] = set()
        bad_requirement_evaluation = False
        for evaluation in evaluations:
            if not isinstance(evaluation, dict):
                bad_requirement_evaluation = True
                continue
            requirement_id = str(evaluation.get("requirement_id", "")).strip()
            coverage = str(evaluation.get("coverage", "")).strip().lower()
            confidence = str(evaluation.get("confidence", "")).strip().lower()
            reason = str(evaluation.get("reason", "")).strip()
            evidence_ids = evaluation.get("evidence_ids", [])
            direct_months = evaluation.get("direct_months", 0)
            adjacent_months = evaluation.get("adjacent_months", 0)
            if (
                requirement_id not in expected_requirements
                or requirement_id in seen_requirements
                or coverage not in {"met", "partial", "unmet", "unknown"}
                or confidence not in {"high", "medium", "low"}
                or not isinstance(evidence_ids, list)
                or not isinstance(direct_months, int) or direct_months < 0
                or not isinstance(adjacent_months, int) or adjacent_months < 0
                or not reason
            ):
                bad_requirement_evaluation = True
                continue
            seen_requirements.add(requirement_id)
            normalized_requirement_evaluations.append({
                "requirement_id": requirement_id,
                "coverage": coverage,
                "confidence": confidence,
                "direct_months": direct_months,
                "adjacent_months": adjacent_months,
                "evidence_ids": [str(value)[:40] for value in evidence_ids[:12]],
                "reason": reason[:160],
            })
        if seen_requirements != set(expected_requirements):
            bad_requirement_evaluation = True
        if (missing_criteria or extra_criteria or bad_values
                or bad_policy_evaluation or bad_requirement_evaluation):
            incomplete += 1
            if diagnostics is not None:
                details: list[str] = []
                if missing_criteria:
                    details.append("missing criteria: " + ", ".join(sorted(missing_criteria)))
                if extra_criteria:
                    details.append("unexpected criteria: " + ", ".join(sorted(extra_criteria)))
                if bad_values:
                    details.append("non-integer criteria: " + ", ".join(sorted(bad_values)))
                if bad_policy_evaluation:
                    details.append(
                        "policy IDs must exactly equal: " + ", ".join(sorted(expected_policy_ids))
                    )
                if bad_requirement_evaluation:
                    details.append(
                        "requirement IDs must exactly equal: "
                        + ", ".join(sorted(expected_requirements))
                    )
                diagnostics[job_id] = details
            continue
        normalized = dict(item)
        normalized["ratings"] = {
            name: normalized_values[name] for name in sorted(criteria_names)
        }
        rationale = item.get("rationale", {})
        if not isinstance(rationale, dict):
            rationale = {}
        normalized["rationale"] = {
            name: str(rationale.get(name) or nested_rationale.get(name) or "").strip()
            for name in sorted(criteria_names)
        }
        normalized["policy_evaluations"] = normalized_evaluations
        normalized["requirement_evaluations"] = normalized_requirement_evaluations
        accepted[job_id] = normalized
    missing = expected_ids - set(accepted)
    if unknown:
        issues.append(f"unknown_job_id={unknown}")
    if duplicate:
        issues.append(f"duplicate_job_id={duplicate}")
    if incomplete:
        issues.append(f"incomplete_rating={incomplete}")
    if missing:
        issues.append(f"missing={len(missing)}")
    return accepted, missing, issues


def _active_score_policy_specs(candidate_context: dict) -> dict[str, dict]:
    policy = candidate_context.get("decision_policy", {})
    policies = policy.get("policies", []) if isinstance(policy, dict) else []
    return {
        str(item.get("id", "")).strip(): item
        for item in policies
        if isinstance(item, dict) and str(item.get("id", "")).strip()
    }


def _apply_policy_evaluation_enforcement(
    accepted: dict[str, dict],
    candidate_context: dict,
) -> None:
    """Translate model policy outcomes into configured scoring criteria.

    This layer never interprets titles or policy prose. The model owns semantic
    judgment; deterministic code only applies the declared criterion mapping.
    """
    specs = _active_score_policy_specs(candidate_context)
    outcome_rating = {"violated": 1, "uncertain": 3, "satisfied": 5}
    for rating in accepted.values():
        values = rating.get("ratings", {})
        rationale = rating.get("rationale", {})
        evaluations = rating.get("policy_evaluations", [])
        if not isinstance(values, dict) or not isinstance(evaluations, list):
            continue
        for evaluation in evaluations:
            if not isinstance(evaluation, dict):
                continue
            spec = specs.get(str(evaluation.get("policy_id", "")))
            if not isinstance(spec, dict):
                continue
            criterion = str(spec.get("criterion", "")).strip()
            outcome = str(evaluation.get("outcome", "")).strip().lower()
            if criterion not in values or outcome not in outcome_rating:
                continue
            values[criterion] = outcome_rating[outcome]
            if isinstance(rationale, dict):
                rationale[criterion] = str(evaluation.get("evidence", ""))[:160]


def _seed_derived_score_criteria(
    accepted: dict[str, dict],
    final_criteria_names: set[str],
) -> None:
    for rating in accepted.values():
        values = rating.setdefault("ratings", {})
        rationale = rating.setdefault("rationale", {})
        for name in DERIVED_SCORE_CRITERIA & final_criteria_names:
            values[name] = 5
            rationale[name] = "Runner-derived from normalized requirement evidence."


def _apply_requirement_evaluation_enforcement(
    accepted: dict[str, dict],
    jobs_by_id: dict[str, dict],
) -> None:
    """Enforce model requirement coverage without reinterpreting JD semantics."""
    for job_id, rating in accepted.items():
        values = rating.get("ratings", {})
        rationale = rating.get("rationale", {})
        evaluations = rating.get("requirement_evaluations", [])
        specs = {
            str(req.get("requirement_id", "")): req
            for req in jobs_by_id.get(job_id, {}).get("requirements", [])
            if isinstance(req, dict)
        }
        if not isinstance(values, dict) or not isinstance(evaluations, list):
            continue
        gate = 5
        gate_reason = "All evaluated central minimum requirements are met."
        for evaluation in evaluations:
            if not isinstance(evaluation, dict):
                continue
            spec = specs.get(str(evaluation.get("requirement_id", "")), {})
            obligation = str(spec.get("obligation", ""))
            importance = str(spec.get("importance", ""))
            coverage = str(evaluation.get("coverage", ""))
            central_minimum = (
                obligation in {"minimum_required", "required"}
                and importance == "central"
            )
            eligibility = importance == "eligibility"
            if coverage == "unmet" and (central_minimum or eligibility):
                gate = 1
                gate_reason = str(evaluation.get("reason", ""))[:160]
                if "role_fit" in values:
                    values["role_fit"] = min(int(values["role_fit"]), 2)
                if "likelihood" in values:
                    values["likelihood"] = min(int(values["likelihood"]), 2)
            elif coverage == "unknown" and (central_minimum or eligibility) and gate > 1:
                gate = min(gate, 3)
                gate_reason = str(evaluation.get("reason", ""))[:160]
                if "role_fit" in values:
                    values["role_fit"] = min(int(values["role_fit"]), 3)
                if "likelihood" in values:
                    values["likelihood"] = min(int(values["likelihood"]), 3)
        if "minimum_requirement" in values:
            values["minimum_requirement"] = gate
            if isinstance(rationale, dict):
                rationale["minimum_requirement"] = gate_reason


def _apply_deterministic_qualification_gates(
    accepted: dict[str, dict],
    jobs_by_id: dict[str, dict],
    candidate_context: dict,
) -> None:
    """Force only high-confidence hard-credential mismatches.

    Requirement extraction already excludes preferred/nice-to-have sections.
    Broad experience/field matching remains semantic; deterministic overrides
    are limited to credentials whose absence is unambiguous in profile text.
    """
    candidate = " ".join([
        str(candidate_context.get("candidate_profile_md", "")),
        str(candidate_context.get("evidence_map_md", "")),
    ]).lower()
    checks = (
        (re.compile(r"\b(?:ph\.?d\.?|doctorate|doctoral degree)\b", re.I),
         re.compile(r"\b(?:ph\.?d\.?|doctorate|doctoral degree)\b", re.I), "PhD/doctorate"),
        (re.compile(r"\b(?:cpa|certified public accountant)\b", re.I),
         re.compile(r"\b(?:cpa|certified public accountant)\b", re.I), "CPA"),
        (re.compile(r"\b(?:bar admission|admitted to (?:the )?bar)\b", re.I),
         re.compile(r"\b(?:bar admission|admitted to (?:the )?bar)\b", re.I), "bar admission"),
        (re.compile(r"\bsecurity clearance\b", re.I),
         re.compile(r"\bsecurity clearance\b", re.I), "security clearance"),
    )
    for job_id, rating in accepted.items():
        job = jobs_by_id.get(job_id, {})
        essential = str(job.get("essential_qualifications", ""))
        values = rating.get("ratings", {})
        rationale = rating.get("rationale", {})
        if not isinstance(values, dict):
            continue
        if "essential_qualification" not in values:
            continue
        if not essential.strip():
            values["essential_qualification"] = 5
            if isinstance(rationale, dict):
                rationale["essential_qualification"] = "No explicit must-have credential extracted."
            continue
        missing = [label for req, evidence, label in checks
                   if req.search(essential) and not evidence.search(candidate)]
        if missing:
            values["essential_qualification"] = 1
            if isinstance(rationale, dict):
                rationale["essential_qualification"] = (
                    "Missing explicit must-have: " + ", ".join(missing)
                )[:80]


def _write_basic_score_group_artifacts(project: Path, ratings: list[dict]) -> None:
    if not ratings:
        return
    groups: dict[str, list[dict]] = {}
    for item in ratings:
        group = _slug(str(item.get("job_group") or item.get("group") or "parked"), "parked")
        groups.setdefault(group, []).append(item)
    groups_dir = project / "targets" / "job-groups"
    groups_dir.mkdir(parents=True, exist_ok=True)
    for slug, entries in sorted(groups.items()):
        lines = [
            f"# Target Group: {slug}",
            "",
            "## Roles in group",
        ]
        for entry in entries[:250]:
            job_id = str(entry.get("job_id", "")).strip()
            ratings_map = entry.get("ratings", {}) if isinstance(entry.get("ratings"), dict) else {}
            fit = ratings_map.get("role_fit", "")
            reason = str(entry.get("reason") or "").strip()
            lines.append(f"- {job_id} | fit={fit} | {reason[:180]}")
        lines += [
            "",
            "## Why this group",
            "Generated from score batch evaluator output.",
            "",
            "## Ideal role shape",
            "See per-row rationale in strategy/job-ratings.json.",
            "",
            "## Fit strengths",
            "See per-row rationale in strategy/job-ratings.json.",
            "",
            "## Gaps & concerns",
            "See per-row rationale in strategy/job-ratings.json.",
            "",
            "## Positioning angle",
            "Triage grouping for the current visible job list.",
            "",
            "## Next action",
            "Review high-priority rows first.",
            "",
            "## Confidence",
            "Provisional until the user selects focused roles.",
        ]
        (groups_dir / f"{slug}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    priority_lines = [
        "# Target Priorities",
        "",
        "Generated by the RoleNavi score batch workflow.",
        "",
    ]
    for slug, entries in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        priority_lines.append(f"- `{slug}`: {len(entries)} visible row(s)")
    (project / "strategy" / "target-priorities.md").write_text(
        "\n".join(priority_lines) + "\n",
        encoding="utf-8",
    )


def _normalization_errors_by_job(rows: list[dict[str, Any]]) -> dict[str, list[str]]:
    return {
        str(row.get("job_id", "")): [
            f"requirement normalization: {str(issue)}"
            for issue in row.get("requirement_coverage_issues", [])
        ]
        for row in rows
        if row.get("job_id") and row.get("requirement_coverage_issues")
    }


def _run_score_batches(ctx: RunContext, provider, base_context: dict, on_stream) -> dict:
    _ensure_capability_ledger(ctx, provider, base_context, on_stream)
    rows = _score_rows(ctx.project)
    normalization_errors = _normalization_errors_by_job(rows)
    all_jobs = [_score_compact_job(row) for row in rows if row.get("job_id")]
    jobs = [
        job for job in all_jobs
        if str(job.get("job_id", "")) not in normalization_errors
    ]
    criteria = _score_criteria(ctx.project)
    if not all_jobs or not criteria:
        return {"events": [], "usage": {}, "ratings": 0}

    candidate_context = _score_candidate_context(base_context, ctx.project)
    policy_specs = _active_score_policy_specs(candidate_context)
    criteria_names = {
        str(item.get("name", "")).strip() for item in criteria if item.get("name")
    }
    model_criteria = [
        item for item in criteria
        if str(item.get("name", "")).strip() not in DERIVED_SCORE_CRITERIA
    ]
    model_criteria_names = {
        str(item.get("name", "")).strip() for item in model_criteria if item.get("name")
    }
    scoring_policy = _static_scoring_policy()
    jobs_by_id = {str(job.get("job_id", "")).strip(): job for job in all_jobs}
    scoreable_ids = {str(job.get("job_id", "")).strip() for job in jobs}
    requirements_by_job = {
        job_id: job.get("requirements", [])
        for job_id, job in jobs_by_id.items()
    }
    dependency_fingerprints = {
        job_id: _score_dependency_fingerprint(
            job, candidate_context, criteria, scoring_policy
        )
        for job_id, job in jobs_by_id.items()
    }
    snapshot_raw = json.dumps(
        {
            "contract": SCORE_CONTRACT_VERSION,
            "dependencies": dependency_fingerprints,
        },
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    snapshot_fingerprint = hashlib.sha256(snapshot_raw.encode("utf-8")).hexdigest()
    checkpoint_key = f"score-{snapshot_fingerprint[:24]}"
    score_staging.begin(
        ctx.project,
        checkpoint_key=checkpoint_key,
        run_id=ctx.run_id or checkpoint_key,
        contract_version=SCORE_CONTRACT_VERSION,
        snapshot_fingerprint=snapshot_fingerprint,
        total_jobs=len(jobs_by_id),
    )
    # Keep the exact score snapshot on the run context so cancellation can
    # promote only dependency-current checkpoint rows without recomputing or
    # trusting stale staging data.
    ctx.score_checkpoint_key = checkpoint_key
    ctx.score_dependency_fingerprints = dependency_fingerprints
    ctx.score_jobs_by_id = jobs_by_id
    ctx.score_requirements_by_job = requirements_by_job
    ctx.score_criteria_names = criteria_names
    if normalization_errors:
        score_staging.checkpoint_batch(
            ctx.project,
            checkpoint_key=checkpoint_key,
            batch_id="normalization-gate",
            dependency_fingerprints=dependency_fingerprints,
            validated={},
            invalid=normalization_errors,
        )
        sample = ", ".join(sorted(normalization_errors)[:5])
        ctx.mark_partial(
            "score_requirement_normalization",
            f"{len(normalization_errors)} row(s) quarantined before scoring; "
            f"validated rows will continue and quarantined rows keep prior DB scores: {sample}",
        )
        ctx.emit(
            "validator",
            f"requirement normalization gate: QUARANTINED "
            f"{len(normalization_errors)}/{len(all_jobs)} row(s)",
        )
    cached_by_id: dict[str, dict] = {}
    for rating in _load_score_ratings(ctx.project):
        job_id = str(rating.get("job_id", "")).strip()
        if job_id in scoreable_ids and _rating_is_current(
            rating,
            dependency_fingerprints[job_id],
            criteria_names,
            requirements_by_job[job_id],
        ):
            cached_by_id[job_id] = rating
    resumed_by_id: dict[str, dict] = {}
    for job_id, rating in score_staging.load_validated(
        ctx.project,
        checkpoint_key=checkpoint_key,
        dependency_fingerprints=dependency_fingerprints,
    ).items():
        if job_id not in cached_by_id and _rating_is_current(
            rating,
            dependency_fingerprints[job_id],
            criteria_names,
            requirements_by_job[job_id],
        ):
            resumed_by_id[job_id] = rating
    available_ids = set(cached_by_id) | set(resumed_by_id)
    pending_jobs = [
        job for job in jobs if str(job.get("job_id", "")) not in available_ids
    ]
    batches = _make_score_batches(pending_jobs)
    ctx.emit(
        "info",
        f"score batch evaluation: visible_rows={len(all_jobs)} scoreable={len(jobs)} "
        f"cached={len(cached_by_id)} "
        f"resumed={len(resumed_by_id)} checkpoint={checkpoint_key} "
        f"stale={len(pending_jobs)} batches={len(batches)} max_jobs={SCORE_BATCH_MAX_JOBS} "
        f"max_requirements={SCORE_BATCH_MAX_REQUIREMENTS} "
        f"workers={SCORE_BATCH_WORKERS} normalization_quarantined={len(normalization_errors)}",
    )

    def _one(
        pass_name: str,
        index: int,
        total: int,
        batch: list[dict[str, Any]],
        repair: dict[str, Any] | None = None,
    ) -> tuple[str, int, list[dict[str, Any]], list[dict], dict]:
        batch_context = dict(base_context)
        batch_context.update({
            "score_batch": {
                "index": index,
                "total": total,
                "pass": pass_name,
                "jobs": batch,
                "criteria": model_criteria,
                "derived_criteria": sorted(DERIVED_SCORE_CRITERIA & criteria_names),
                "candidate": candidate_context,
                "scoring_policy": scoring_policy,
                "repair": repair or {},
            }
        })
        label = f"score-{pass_name}-{index:03d}"
        envelope = _provider_run(
            provider,
            "score",
            batch_context,
            _labelled_stream(ctx, label, on_stream),
            model_workflow="score",
        )
        ratings = _extract_score_batch_ratings(_score_batch_result_text(envelope))
        return pass_name, index, batch, ratings, envelope

    accepted_by_id: dict[str, dict] = {}
    missing_after_first: set[str] = set()
    validation_errors_by_id: dict[str, list[str]] = {}
    issue_count = 0
    envelope: dict = {"events": [], "usage": {}}
    with ThreadPoolExecutor(max_workers=SCORE_BATCH_WORKERS) as pool:
        futures = {
            pool.submit(_one, "batch", i, len(batches), batch): i
            for i, batch in enumerate(batches, start=1)
        }
        for fut in as_completed(futures):
            if ctx.cancel_event is not None and ctx.cancel_event.is_set():
                # Executor.__exit__ waits for queued futures unless they are
                # cancelled explicitly. Without this, Stop appears frozen while
                # hundreds of already-submitted score batches keep running.
                for pending in futures:
                    pending.cancel()
            ctx.check_cancelled()
            index = futures[fut]
            try:
                _, _, batch, ratings, child = fut.result()
            except Exception as exc:
                ctx.mark_partial(f"score_batch_{index}", str(exc)[:500])
                ctx.emit("info", f"score batch {index}/{len(batches)} failed: {str(exc)[:240]}")
                for job in batches[index - 1]:
                    job_id = str(job.get("job_id", "")).strip()
                    if job_id:
                        missing_after_first.add(job_id)
                score_staging.checkpoint_batch(
                    ctx.project,
                    checkpoint_key=checkpoint_key,
                    batch_id=f"batch-{index:03d}",
                    dependency_fingerprints=dependency_fingerprints,
                    validated={},
                    invalid={
                        str(job.get("job_id", "")): [f"provider failure: {str(exc)[:300]}"]
                        for job in batches[index - 1] if job.get("job_id")
                    },
                )
                continue
            expected_ids = {str(job.get("job_id", "")).strip() for job in batch if job.get("job_id")}
            accepted, missing, issues = _validate_score_batch_ratings(
                ratings, expected_ids, model_criteria_names, policy_specs,
                requirements_by_job, validation_errors_by_id
            )
            _seed_derived_score_criteria(accepted, criteria_names)
            _apply_policy_evaluation_enforcement(accepted, candidate_context)
            _apply_requirement_evaluation_enforcement(accepted, jobs_by_id)
            _apply_deterministic_qualification_gates(
                accepted, jobs_by_id, candidate_context
            )
            for job_id, rating in accepted.items():
                rating["score_meta"] = {
                    "contract_version": SCORE_CONTRACT_VERSION,
                    "dependency_fingerprint": dependency_fingerprints[job_id],
                    "scored_at": _now(),
                }
            accepted_by_id.update(accepted)
            missing_after_first.update(missing)
            score_staging.checkpoint_batch(
                ctx.project,
                checkpoint_key=checkpoint_key,
                batch_id=f"batch-{index:03d}",
                dependency_fingerprints=dependency_fingerprints,
                validated=accepted,
                invalid={
                    job_id: validation_errors_by_id.get(
                        job_id, ["job missing from evaluator output"]
                    )
                    for job_id in missing
                },
            )
            if issues:
                issue_count += 1
                ctx.emit("info", f"score batch {index}/{len(batches)} validation: {', '.join(issues)}")
            _merge_usage(envelope, child)
            ctx.emit(
                "progress",
                f"score batch {index}/{len(batches)} accepted "
                f"{len(accepted)}/{len(expected_ids)} rating(s); checkpointed",
            )

    retry_ids = sorted(job_id for job_id in missing_after_first if job_id not in accepted_by_id)
    retry_batches = [
        [jobs_by_id[job_id] for job_id in retry_ids[i:i + SCORE_BATCH_RETRY_JOBS] if job_id in jobs_by_id]
        for i in range(0, len(retry_ids), SCORE_BATCH_RETRY_JOBS)
    ]
    retry_batches = [batch for batch in retry_batches if batch]
    ctx.check_cancelled()
    if retry_batches:
        ctx.emit(
            "info",
            f"score retry: missing_rows={len(retry_ids)} "
            f"retry_batches={len(retry_batches)} retry_size={SCORE_BATCH_RETRY_JOBS}",
        )
        with ThreadPoolExecutor(max_workers=SCORE_BATCH_WORKERS) as pool:
            futures = {}
            for i, batch in enumerate(retry_batches, start=1):
                job = batch[0]
                job_id = str(job.get("job_id", ""))
                repair = {
                    "validation_errors": [
                        *validation_errors_by_id.get(job_id, []),
                    ] or ["previous output omitted this job entirely"],
                    "job_id": job_id,
                    "expected_rating_criteria": sorted(model_criteria_names),
                    "expected_policy_ids": sorted(policy_specs),
                    "expected_requirement_ids": [
                        str(item.get("requirement_id", ""))
                        for item in requirements_by_job.get(job_id, [])
                    ],
                }
                futures[pool.submit(
                    _one, "repair", i, len(retry_batches), batch, repair
                )] = i
            for fut in as_completed(futures):
                if ctx.cancel_event is not None and ctx.cancel_event.is_set():
                    for pending in futures:
                        pending.cancel()
                ctx.check_cancelled()
                index = futures[fut]
                try:
                    _, _, batch, ratings, child = fut.result()
                except Exception as exc:
                    ctx.mark_partial(f"score_retry_{index}", str(exc)[:500])
                    ctx.emit("info", f"score retry {index}/{len(retry_batches)} failed: {str(exc)[:240]}")
                    batch = retry_batches[index - 1]
                    score_staging.checkpoint_batch(
                        ctx.project,
                        checkpoint_key=checkpoint_key,
                        batch_id=f"repair-{index:03d}",
                        dependency_fingerprints=dependency_fingerprints,
                        validated={},
                        invalid={
                            str(job.get("job_id", "")): [
                                f"repair provider failure: {str(exc)[:300]}"
                            ]
                            for job in batch if job.get("job_id")
                        },
                    )
                    continue
                expected_ids = {str(job.get("job_id", "")).strip() for job in batch if job.get("job_id")}
                repair_diagnostics: dict[str, list[str]] = {}
                accepted, missing, issues = _validate_score_batch_ratings(
                    ratings, expected_ids, model_criteria_names, policy_specs,
                    requirements_by_job, repair_diagnostics
                )
                _seed_derived_score_criteria(accepted, criteria_names)
                _apply_policy_evaluation_enforcement(accepted, candidate_context)
                _apply_requirement_evaluation_enforcement(accepted, jobs_by_id)
                _apply_deterministic_qualification_gates(
                    accepted, jobs_by_id, candidate_context
                )
                for job_id, rating in accepted.items():
                    rating["score_meta"] = {
                        "contract_version": SCORE_CONTRACT_VERSION,
                        "dependency_fingerprint": dependency_fingerprints[job_id],
                        "scored_at": _now(),
                    }
                accepted_by_id.update(accepted)
                score_staging.checkpoint_batch(
                    ctx.project,
                    checkpoint_key=checkpoint_key,
                    batch_id=f"repair-{index:03d}",
                    dependency_fingerprints=dependency_fingerprints,
                    validated=accepted,
                    invalid={
                        job_id: repair_diagnostics.get(
                            job_id, ["job missing from repair output"]
                        )
                        for job_id in missing
                    },
                )
                if issues:
                    issue_count += 1
                    ctx.emit(
                        "info",
                        f"score retry {index}/{len(retry_batches)} validation: {', '.join(issues)}",
                    )
                if missing:
                    ctx.mark_partial(
                        f"score_retry_{index}",
                        f"{len(missing)} row(s) still missing after retry",
                    )
                _merge_usage(envelope, child)
                ctx.emit(
                    "progress",
                    f"score retry {index}/{len(retry_batches)} accepted "
                    f"{len(accepted)}/{len(expected_ids)} rating(s); checkpointed",
                )

    ctx.check_cancelled()
    current_by_id = {**cached_by_id, **resumed_by_id, **accepted_by_id}
    unresolved_ids = set(jobs_by_id) - set(current_by_id)
    all_ratings = [current_by_id[key] for key in sorted(current_by_id)]
    changed = _merge_score_ratings(ctx.project, all_ratings)
    _write_score_freshness(
        ctx.project,
        current_ids=set(current_by_id),
        unresolved_ids=unresolved_ids,
        dependency_fingerprints=dependency_fingerprints,
    )
    _write_basic_score_group_artifacts(ctx.project, all_ratings)
    unresolved = len(unresolved_ids)
    score_staging.finish(
        ctx.project, checkpoint_key=checkpoint_key, unresolved=unresolved
    )
    ctx.validator_results.append({
        "validator": "score_batch_evaluation",
        "target": ctx.project.name,
        "returncode": 0 if changed else 2,
        "output": (
            f"{len(accepted_by_id)} refreshed, {len(cached_by_id)} cached, "
            f"{len(resumed_by_id)} resumed, "
            f"{changed} current rating(s) committed from {len(batches)} batch(es); "
            f"retry_batches={len(retry_batches)} unresolved={unresolved} "
            f"validation_issue_batches={issue_count} "
            f"normalization_quarantined={len(normalization_errors)}"
        ),
    })
    if unresolved:
        ctx.mark_partial(
            "score_batch_coverage",
            f"{unresolved} row(s) unresolved; validated current rows were committed "
            "atomically and prior DB scores remain unchanged for unresolved rows",
        )
    ctx.emit(
        "artifact",
        f"score ratings current={changed}; refreshed={len(accepted_by_id)}; "
        f"cached={len(cached_by_id)}; resumed={len(resumed_by_id)}; "
        f"unresolved={unresolved}",
    )
    envelope["ratings"] = changed
    envelope["refreshed_ratings"] = len(accepted_by_id)
    envelope["resumed_ratings"] = len(resumed_by_id)
    envelope["unresolved_ratings"] = unresolved
    envelope["checkpoint_key"] = checkpoint_key
    return envelope


def _salvage_score_checkpoint(ctx: RunContext) -> int:
    """Publish dependency-current validated rows after an interrupted score run."""
    latest = score_staging.latest_summary(ctx.project)
    checkpoint_key = str(
        getattr(ctx, "score_checkpoint_key", "")
        or latest.get("checkpoint_key", "")
    )
    if not checkpoint_key:
        return 0

    jobs_by_id = getattr(ctx, "score_jobs_by_id", None)
    dependency_fingerprints = getattr(ctx, "score_dependency_fingerprints", None)
    requirements_by_job = getattr(ctx, "score_requirements_by_job", None)
    criteria_names = getattr(ctx, "score_criteria_names", None)
    if not all(isinstance(value, dict) for value in (
        jobs_by_id, dependency_fingerprints, requirements_by_job
    )) or not isinstance(criteria_names, set):
        rows = _score_rows(ctx.project)
        all_jobs = [_score_compact_job(row) for row in rows if row.get("job_id")]
        jobs_by_id = {
            str(job.get("job_id", "")).strip(): job for job in all_jobs
            if str(job.get("job_id", "")).strip()
        }
        criteria = _score_criteria(ctx.project)
        criteria_names = {
            str(item.get("name", "")).strip() for item in criteria if item.get("name")
        }
        from . import preflight as _pf
        pdir = _pf.profile_dir(ctx.project)
        candidate_context = _score_candidate_context(
            {"profile_dir": str(pdir) if pdir else ""}, ctx.project
        )
        scoring_policy = _static_scoring_policy()
        dependency_fingerprints = {
            job_id: _score_dependency_fingerprint(
                job, candidate_context, criteria, scoring_policy
            )
            for job_id, job in jobs_by_id.items()
        }
        requirements_by_job = {
            job_id: job.get("requirements", []) for job_id, job in jobs_by_id.items()
        }

    staged = score_staging.load_validated(
        ctx.project,
        checkpoint_key=checkpoint_key,
        dependency_fingerprints=dependency_fingerprints,
    )
    if not staged:
        return 0
    current: dict[str, dict] = {}
    for rating in _load_score_ratings(ctx.project):
        job_id = str(rating.get("job_id", "")).strip()
        if job_id in jobs_by_id and _rating_is_current(
            rating,
            dependency_fingerprints[job_id],
            criteria_names,
            requirements_by_job[job_id],
        ):
            current[job_id] = rating
    current.update(staged)
    unresolved_ids = set(jobs_by_id) - set(current)
    ratings = [current[key] for key in sorted(current)]
    _merge_score_ratings(ctx.project, ratings)
    _write_score_freshness(
        ctx.project,
        current_ids=set(current),
        unresolved_ids=unresolved_ids,
        dependency_fingerprints=dependency_fingerprints,
    )
    _write_basic_score_group_artifacts(ctx.project, ratings)
    score_staging.finish(
        ctx.project, checkpoint_key=checkpoint_key, unresolved=len(unresolved_ids)
    )
    ctx.emit(
        "artifact",
        f"cancel salvage: promoted {len(staged)} checkpointed rating(s); "
        f"current={len(current)} unresolved={len(unresolved_ids)}",
    )
    _finalize_score(ctx, complete_on_cancel=True)
    return len(staged)


def _finalize_score(ctx: RunContext, *, complete_on_cancel: bool = False) -> None:
    """Run deterministic score math + store update outside the agent sandbox."""
    cancel_was_set = bool(
        complete_on_cancel and ctx.cancel_event is not None
        and ctx.cancel_event.is_set()
    )
    if cancel_was_set:
        # The user explicitly wants already-checkpointed rows committed. Model
        # calls are stopped first; only this short local deterministic publish
        # is allowed to complete before the run becomes cancelled.
        ctx.cancel_event.clear()
    try:
        rc = _stream_script(ctx, "finalize_score", str(ctx.project))
    finally:
        if cancel_was_set:
            ctx.cancel_event.set()
    summary_path = ctx.project / "strategy" / "score-finalize-summary.json"
    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        summary = {}
    out = json.dumps(summary, ensure_ascii=False)[:1000] if summary else ""
    ctx.validator_results.append({
        "validator": "finalize_score",
        "target": ctx.project.name,
        "returncode": rc,
        "output": out,
    })
    status_label = "PASS" if rc == 0 else ("PARTIAL" if rc == 2 else "FAIL")
    ctx.emit("validator", f"finalize_score: {status_label}")
    if summary:
        rated = summary.get("rated_visible_rows", 0)
        visible = summary.get("visible_rows", 0)
        updated = summary.get("updated_rows", 0)
        ctx.summary = (
            f"Score finalized for {rated}/{visible} visible row(s); "
            f"{updated} job_list row(s) updated."
        )
    if rc == 2:
        reason = (summary.get("reason") if isinstance(summary, dict) else "") or (
            "score finalization completed partially"
        )
        missing = summary.get("missing_visible_rows", 0) if isinstance(summary, dict) else 0
        ctx.mark_partial("score_finalization", f"{reason}; missing_visible_rows={missing}")
    elif rc != 0:
        ctx.failure_class = ctx.failure_class or "score_finalization_failure"
        if out:
            ctx.emit("validator", out[:800])


def _score_result_text(envelope: dict) -> str:
    for ev in reversed(envelope.get("events", [])):
        if isinstance(ev, dict) and ev.get("type") == "result":
            return str(ev.get("content") or ev.get("summary") or "")
    return ""


def _result_text(envelope: dict) -> str:
    for ev in reversed(envelope.get("events", [])):
        if isinstance(ev, dict) and ev.get("type") == "result":
            return str(ev.get("content") or ev.get("summary") or "")
    return ""


def _extract_universe_proposal(text: str) -> dict | None:
    raw = str(text or "").strip()
    marker = "UNIVERSE_PROPOSAL_JSON:"
    if marker not in raw:
        return None
    try:
        payload = _extract_json_object(raw.split(marker, 1)[1].strip())
    except (ValueError, json.JSONDecodeError):
        return None
    if payload.get("schema") != "rolenavi-universe-proposal-v1":
        return None
    return payload


def _run_progressive_universe(
    ctx: RunContext,
    provider,
    base_context: dict,
    on_stream,
) -> dict:
    """Run proposal-only expansion workers and commit through one coordinator."""
    meta = project_meta.load(ctx.project)
    revision = int(meta.get("preference_revision", 0) or 0)
    inputs = [str(value).strip() for value in meta.get("target_companies", []) if str(value).strip()]
    tasks = [{
        "input": value,
        "kind": (
            "descriptor_expansion" if universe_state.is_descriptor(value)
            else "seed_peer_expansion"
        ),
    } for value in inputs]
    universe_state.materialize_seed_universe(ctx.project)
    if not tasks:
        merged = universe_state.merge_proposals(
            ctx.project, [], expected_revision=revision
        )
        ctx.summary = "Employer universe ready with no declared company inputs."
        return {"events": [], "usage": {}, "universe": merged}

    aggregate: dict = {"events": [], "usage": {}}
    proposals: list[dict] = []
    max_workers = min(4, max(1, len(tasks)))
    ctx.emit(
        "info",
        f"background universe expansion: inputs={len(tasks)} workers={max_workers} revision={revision}",
    )

    def one(index: int, task_spec: dict) -> tuple[dict | None, dict]:
        context = dict(base_context)
        context["universe_task"] = {
            **task_spec,
            "preference_revision": revision,
            "target_locations": meta.get("target_locations", []),
            "focus_role": meta.get("focus_role", ""),
            "target_level": meta.get("target_level", ""),
            "negatives": meta.get("negatives", []),
            "relationship_types": [
                "direct_competitor", "same_talent_pool", "adjacent_product",
                "ecosystem_partner", "funded_entrant", "location_peer",
            ],
        }
        envelope = _provider_run(
            provider,
            "universe-expand",
            context,
            _labelled_stream(ctx, f"universe-{index:02d}", on_stream),
            model_workflow="opportunity-plan",
        )
        return _extract_universe_proposal(_result_text(envelope)), envelope

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(one, index, task_spec): task_spec
            for index, task_spec in enumerate(tasks, start=1)
        }
        for future in as_completed(futures):
            task_spec = futures[future]
            try:
                proposal, envelope = future.result()
            except Exception as exc:
                ctx.mark_partial(
                    f"universe:{task_spec['input']}", str(exc)[:300]
                )
                continue
            _merge_usage(aggregate, envelope)
            if not proposal:
                ctx.mark_partial(
                    f"universe:{task_spec['input']}", "worker returned no valid typed proposal"
                )
                continue
            if (
                str(proposal.get("input", "")).strip() != task_spec["input"]
                or str(proposal.get("kind", "")) != task_spec["kind"]
                or int(proposal.get("preference_revision", -1)) != revision
            ):
                ctx.mark_partial(
                    f"universe:{task_spec['input']}", "proposal input/kind/revision mismatch"
                )
                continue
            proposals.append(proposal)
    try:
        merged = universe_state.merge_proposals(
            ctx.project, proposals, expected_revision=revision
        )
    except ValueError as exc:
        ctx.mark_partial("universe_coordinator", str(exc))
        return aggregate
    rel = "targets/company-universe.json"
    if rel not in ctx.artifacts_written:
        ctx.artifacts_written.append(rel)
    companies = sum(
        len(bucket.get("companies", []))
        for bucket in merged.get("buckets", []) if isinstance(bucket, dict)
    )
    ctx.validator_results.append({
        "validator": "progressive_universe_coordinator",
        "target": ctx.project.name,
        "returncode": 0 if merged.get("state") == "ready" else 2,
        "output": f"state={merged.get('state')} proposals={len(proposals)}/{len(tasks)} companies={companies}",
    })
    ctx.summary = (
        f"Employer universe {merged.get('state')}: {companies} companies; "
        f"{len(proposals)}/{len(tasks)} expansion proposals accepted."
    )
    aggregate["universe"] = merged
    return aggregate


def _validate_opportunity_universe(project: Path, payload: dict) -> list[str]:
    resolver = core.load("resolve_company_sources")
    meta = project_meta.load(project)
    names: set[str] = set()
    errors: list[str] = []
    for bucket in payload.get("buckets", []) if isinstance(payload, dict) else []:
        if not isinstance(bucket, dict):
            continue
        for company in bucket.get("companies", []):
            if not isinstance(company, dict):
                continue
            name = str(company.get("name", "")).strip()
            if not name:
                errors.append("company universe contains an unnamed employer")
                continue
            if resolver.is_category_seed(name):
                errors.append(f"category descriptor was emitted as an employer: {name}")
            if not str(company.get("rationale", "")).strip():
                errors.append(f"company universe employer lacks rationale: {name}")
            names.add("".join(ch for ch in name.lower() if ch.isalnum()))
    descriptor_inputs = {
        str(item.get("input", "")).strip().lower()
        for item in payload.get("expanded_descriptors", [])
        if isinstance(item, dict)
    }
    for target in meta.get("target_companies", []):
        target = str(target).strip()
        if not target:
            continue
        if resolver.is_category_seed(target):
            if target.lower() not in descriptor_inputs:
                errors.append(f"category descriptor was not expanded: {target}")
            continue
        key = "".join(ch for ch in target.lower() if ch.isalnum())
        if key not in names:
            errors.append(f"declared employer seed is missing: {target}")
    if not names:
        errors.append("company universe contains no named employers")
    return errors


def _extract_runner_artifact_payload(text: str) -> dict | None:
    raw = str(text or "").strip()
    if not raw:
        return None
    marker = "ROLENAVI_ARTIFACT_OUTPUT_JSON:"
    if marker in raw:
        raw = raw.split(marker, 1)[1].strip()
    else:
        return None
    try:
        payload = _extract_json_object(raw)
    except (ValueError, json.JSONDecodeError):
        return None
    if payload.get("schema") != "rolenavi-artifact-output-v1":
        return None
    if set(payload) - {"schema", "artifacts", "store_writes", "notes"}:
        return None
    return payload


def _merge_repair_artifact_envelope(previous: dict, repair: dict) -> dict:
    """Apply a model repair payload as a path-keyed patch over its prior set."""
    before = _extract_runner_artifact_payload(_result_text(previous)) or {}
    patch = _extract_runner_artifact_payload(_result_text(repair)) or {}
    before_items = before.get("artifacts", [])
    patch_items = patch.get("artifacts", [])
    if not isinstance(before_items, list) or not isinstance(patch_items, list) or not patch_items:
        return repair
    by_path: dict[str, dict] = {}
    order: list[str] = []
    for item in [*before_items, *patch_items]:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path", "")).replace("\\", "/").strip()
        if not path:
            continue
        if path not in by_path:
            order.append(path)
        by_path[path] = item
    merged_payload = {
        "schema": "rolenavi-artifact-output-v1",
        "artifacts": [by_path[path] for path in order],
        "store_writes": patch.get("store_writes", before.get("store_writes", [])),
        "notes": patch.get("notes", []),
    }
    merged = dict(repair)
    events = [dict(event) if isinstance(event, dict) else event
              for event in repair.get("events", [])]
    for event in reversed(events):
        if isinstance(event, dict) and event.get("type") == "result":
            event["content"] = (
                "ROLENAVI_ARTIFACT_OUTPUT_JSON:" +
                json.dumps(merged_payload, ensure_ascii=False, separators=(",", ":"))
            )
            break
    merged["events"] = events
    return merged


def _artifact_payload_value(item: dict) -> Any:
    if "json" in item:
        return item.get("json")
    text = str(item.get("text", ""))
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def _canonical_story_bank(value: Any) -> tuple[dict | None, list[str]]:
    data = {"meta": "legacy list normalized by runner", "entries": value} if isinstance(value, list) else value
    if not isinstance(data, dict):
        return None, ["story-bank.json must be an object with an entries array"]
    entries = data.get("entries")
    if not isinstance(entries, list) or not entries:
        return None, ["story-bank.json entries must be a non-empty list"]
    required = ("title", "source", "situation", "task", "action", "result", "best_for", "ev_refs")
    errors: list[str] = []
    normalized: list[dict] = []
    used: set[str] = set()
    next_id = 1
    for index, raw in enumerate(entries):
        if not isinstance(raw, dict):
            errors.append(f"entry {index} must be an object")
            continue
        entry = dict(raw)
        missing = [key for key in required if key not in entry or entry.get(key) in (None, "", [])]
        if missing:
            errors.append(f"entry {index} missing/empty {', '.join(missing)}")
            continue
        original_id = str(entry.get("id", "")).strip().upper()
        if re.fullmatch(r"ST-\d{2,}", original_id) and original_id not in used:
            story_id = original_id
        else:
            while f"ST-{next_id:02d}" in used:
                next_id += 1
            story_id = f"ST-{next_id:02d}"
            next_id += 1
        used.add(story_id)
        entry["id"] = story_id
        if isinstance(entry.get("ev_refs"), str):
            entry["ev_refs"] = [
                part.strip() for part in re.split(r"[,;]", entry["ev_refs"]) if part.strip()
            ]
        normalized.append(entry)
    if errors:
        return None, errors
    return {
        "schema": "rolenavi-story-bank-v1",
        "meta": data.get("meta", ""),
        "entries": normalized,
    }, []


def _story_bank_markdown(data: dict) -> str:
    def cell(value: Any) -> str:
        if isinstance(value, list):
            value = ", ".join(str(item) for item in value)
        return str(value or "").replace("|", "\\|").replace("\n", " ").strip()

    lines = [
        "# Story Bank", "",
        "| ID | Title | Source | S | T | A | R | Best for | EV refs |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for entry in data.get("entries", []):
        lines.append("| " + " | ".join(cell(entry.get(key)) for key in (
            "id", "title", "source", "situation", "task", "action", "result",
            "best_for", "ev_refs")) + " |")
    return "\n".join(lines) + "\n"


def _prevalidate_strategy_payload(ctx: RunContext, items: list[dict]) -> bool:
    by_path = {str(item.get("path", "")).replace("\\", "/"): item for item in items}
    required = {"strategy/prep-strategy.md", "strategy/target-priorities.md",
                "strategy/group-assignments.json"}
    missing = sorted(required - set(by_path))
    group_paths = sorted(path for path in by_path
                         if path.startswith("targets/job-groups/") and path.endswith(".md"))
    if not group_paths:
        missing.append("targets/job-groups/*.md")
    assignments = _artifact_payload_value(by_path.get("strategy/group-assignments.json", {}))
    expected_ids = {
        str(row.get("job_id", "")).strip()
        for row in _strategy_focused_job_rows(ctx.project)
        if str(row.get("job_id", "")).strip()
    }
    seen_ids: set[str] = set()
    assigned_groups: set[str] = set()
    errors = list(missing)
    if not isinstance(assignments, dict) or assignments.get("schema") != "rolenavi-focused-group-assignments-v1":
        errors.append("group-assignments.json has the wrong schema")
    else:
        rows = assignments.get("assignments", [])
        if not isinstance(rows, list):
            errors.append("group-assignments.json assignments must be a list")
        else:
            for row in rows:
                if not isinstance(row, dict):
                    errors.append("group assignment must be an object")
                    continue
                job_id = str(row.get("job_id", "")).strip()
                group = _slug(row.get("job_group", ""), "")
                disposition = str(row.get("disposition", "")).strip().lower()
                disposition_reason = str(row.get("disposition_reason", "")).strip()
                if not job_id or not group or job_id in seen_ids:
                    errors.append(f"invalid/duplicate group assignment for {job_id or '<missing>'}")
                    continue
                if disposition not in PREP_DISPOSITIONS:
                    errors.append(
                        f"group assignment {job_id} needs disposition pursue, conditional, or parked"
                    )
                if not disposition_reason:
                    errors.append(f"group assignment {job_id} needs disposition_reason")
                seen_ids.add(job_id)
                assigned_groups.add(group)
    if seen_ids != expected_ids:
        errors.append("group assignments must cover every focused job exactly once")
    file_groups = {_slug(Path(path).stem, "") for path in group_paths}
    if assigned_groups and assigned_groups != file_groups:
        errors.append("active group files must exactly match assigned group slugs")
    if len(expected_ids) >= 6:
        max_groups = _max_strategy_groups(len(expected_ids))
        if len(assigned_groups) > max_groups:
            errors.append(
                f"pathologically fragmented grouping: {len(assigned_groups)} groups for "
                f"{len(expected_ids)} focused jobs (maximum {max_groups}); consolidate roles "
                "that can share one positioning and resume"
            )
    strategy_text = str(by_path.get("strategy/prep-strategy.md", {}).get("text", ""))
    for heading in ("Executive summary", "Strengths / weaknesses", "Application priority",
                    "Portfolio strategy", "Resume emphasis", "LinkedIn direction",
                    "Same-company multi-position"):
        if heading.lower() not in strategy_text.lower():
            errors.append(f"strategy missing substantive section: {heading}")
    companies = [str(row.get("company", "")).strip().lower()
                 for row in _strategy_focused_job_rows(ctx.project)]
    if len(companies) != len(set(companies)) and not re.search(r"https?://", strategy_text):
        errors.append("same-company strategy requires cited public web research")
    for path in group_paths:
        group_text = str(by_path[path].get("text", ""))
        required_terms = ("why", "fit", "gap", "position", "next", "confidence")
        if len(group_text.strip()) < 300 or any(
                term not in group_text.lower() for term in required_terms):
            errors.append(f"group file lacks substantive decision scaffolding: {path}")
    if errors:
        ctx.mark_partial("prep-strategy_publish_gate", "; ".join(errors[:12]))
        ctx.emit("validator", "prep-strategy publish gate: FAIL")
        return False
    ctx.emit("validator", "prep-strategy publish gate: PASS")
    return True


def _prevalidate_linkedin_payload(ctx: RunContext, items: list[dict],
                                  label: str = "prep-linkedin") -> bool:
    import tempfile

    reviews = [item for item in items if str(item.get("path", "")).endswith("linkedin-review.md")]
    if len(reviews) != 1:
        _record_publish_error(
            ctx, label, "linkedin_artifact_set",
            "expected exactly one linkedin-review.md",
        )
        ctx.emit("validator", "prep-linkedin publish gate: FAIL")
        return False
    reviews[0]["text"] = _normalize_linkedin_review_text(
        str(reviews[0].get("text", ""))
    )
    with tempfile.TemporaryDirectory(prefix="rolenavi-linkedin-") as tmp:
        path = Path(tmp) / "linkedin-review.md"
        path.write_text(str(reviews[0].get("text", "")), encoding="utf-8")
        result = core.run_script("validate_linkedin_review", str(path), env=_env_for(ctx.project))
    out = (result.stdout + result.stderr).strip()
    ctx.validator_results.append({
        "validator": "validate_linkedin_review[publish-gate]",
        "target": str(reviews[0].get("path", "")),
        "returncode": result.returncode,
        "output": out[:800],
    })
    ctx.emit("validator", "prep-linkedin publish gate: " +
             ("PASS" if result.returncode == 0 else "FAIL"))
    if result.returncode != 0:
        _record_publish_error(
            ctx, label, "linkedin_review_validation", out[:4000]
        )
        return False
    return True


def _record_publish_error(ctx: RunContext, label: str, code: str, detail: str) -> None:
    safe_label = label or "artifact"
    safe_code = _slug(code, "publish-gate")[:80]
    if not hasattr(ctx, "publish_errors"):
        ctx.publish_errors = {}
    ctx.publish_errors[safe_label] = {
        "code": safe_code,
        "detail": str(detail or "publish gate rejected the generated output")[:4000],
    }
    # Global telemetry strips output, but the validator name and target retain a
    # useful, non-sensitive failure code and group label.
    ctx.validator_results.append({
        "validator": f"publish_gate[{safe_code}]",
        "target": safe_label,
        "returncode": 2,
        "output": str(detail or "")[:800],
    })


def _coerce_typed_json_artifact(item: dict, expected_type: type) -> Any:
    value = _artifact_payload_value(item)
    if not isinstance(value, expected_type):
        return None
    item.pop("text", None)
    item["json"] = value
    return value


def _validate_resume_target_brief(brief: Any) -> list[str]:
    errors: list[str] = []
    contract = _resume_artifact_contract("group")["target_brief"]
    if not isinstance(brief, dict):
        return ["target-brief.json must be a JSON object"]
    if brief.get("schema") != RESUME_TARGET_BRIEF_SCHEMA:
        errors.append(f"target brief schema must be {RESUME_TARGET_BRIEF_SCHEMA}")
    for key in contract["required"]:
        if key not in brief:
            errors.append(f"target brief missing {key}")
    if not isinstance(brief.get("source_job_ids"), list) or not brief.get("source_job_ids"):
        errors.append("target brief source_job_ids must be a non-empty list")
    requirements = brief.get("requirements")
    if not isinstance(requirements, list) or not requirements:
        errors.append("target brief requirements must be a non-empty list")
    else:
        for index, req in enumerate(requirements, start=1):
            if not isinstance(req, dict):
                errors.append(f"requirement {index} must be an object")
                continue
            for key in contract["requirement_required"]:
                if key not in req:
                    errors.append(f"requirement {index} missing {key}")
            if str(req.get("priority", "")).lower() not in {"must", "preferred"}:
                errors.append(f"requirement {index} priority must be must or preferred")
            if not isinstance(req.get("keywords"), list):
                errors.append(f"requirement {index} keywords must be a list")
            if not isinstance(req.get("source_job_ids"), list):
                errors.append(f"requirement {index} source_job_ids must be a list")
    gaps = brief.get("gaps")
    if not isinstance(gaps, list):
        errors.append("target brief gaps must be a list")
    else:
        for index, gap in enumerate(gaps, start=1):
            if not isinstance(gap, dict) or any(
                    not str(gap.get(key, "")).strip()
                    for key in contract["gap_required"]):
                errors.append(f"gap {index} requires requirement_id and gap")
    return errors


def _validate_resume_reasons_shape(reasons: Any) -> list[str]:
    if not isinstance(reasons, list):
        return ["reasons.json must be a JSON array"]
    errors: list[str] = []
    required = set(_resume_artifact_contract("group")["reasons"]["item_required"])
    for index, reason in enumerate(reasons, start=1):
        if not isinstance(reason, dict):
            errors.append(f"reason {index} must be an object")
            continue
        missing = sorted(required - set(reason))
        if missing:
            errors.append(f"reason {index} missing {', '.join(missing)}")
        if str(reason.get("reason", "")) not in RESUME_REASON_VALUES:
            errors.append(f"reason {index} has invalid reason")
        if str(reason.get("rewrite_type", "")) not in RESUME_REWRITE_TYPES:
            errors.append(f"reason {index} has invalid rewrite_type")
        for key in ("requirement_ids", "source_job_ids"):
            if not isinstance(reason.get(key), list):
                errors.append(f"reason {index} {key} must be a list")
    return errors


def _resume_reason_mapping_diagnostics(draft_text: str, reasons: Any) -> dict:
    """Return exact, deterministic bullet↔reason mismatches for repair prompts."""
    if not isinstance(reasons, list):
        return {"bullet_count": 0, "reason_count": 0, "unmatched_bullets": [],
                "unmatched_reason_prefixes": []}
    validator = core.load("validate_resume_tailoring")
    bullets = validator.extract_bullets(draft_text)
    matched_reason_ids: set[int] = set()
    unmatched_bullets: list[dict] = []
    for index, bullet in enumerate(bullets):
        reason = validator.find_reason(bullet, reasons)
        if reason is None:
            unmatched_bullets.append({"index": index, "text": bullet[:250]})
        else:
            matched_reason_ids.add(id(reason))
    unmatched_prefixes = [
        str(reason.get("bullet_prefix", ""))[:160]
        for reason in reasons if isinstance(reason, dict) and id(reason) not in matched_reason_ids
    ]
    return {
        "bullet_count": len(bullets),
        "reason_count": len(reasons),
        "unmatched_bullets": unmatched_bullets,
        "unmatched_reason_prefixes": unmatched_prefixes,
    }


def _validator_failure_first(output: str, limit: int = 3500) -> str:
    """Keep blocking diagnostics ahead of noisy warnings in repair/UI packets."""
    lines = [line.rstrip() for line in str(output or "").splitlines() if line.strip()]
    blocking: list[str] = []
    warnings: list[str] = []
    in_failure = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("FAIL:"):
            in_failure = True
            blocking.append(stripped)
        elif stripped.startswith("WARN") or stripped.startswith("WARNINGS:"):
            warnings.append(stripped)
        elif in_failure and stripped.startswith("-"):
            blocking.append(stripped)
        elif not blocking and not stripped.startswith("PASS:"):
            blocking.append(stripped)
    parts = blocking or ["validator failed without a blocking diagnostic"]
    if warnings:
        parts.append(f"Non-blocking warnings ({len(warnings)}): " + " | ".join(warnings[:4]))
    return "\n".join(parts)[:limit]


def _prevalidate_resume_payload(ctx: RunContext, items: list[dict],
                                label: str = "prep-resume") -> bool:
    import tempfile

    by_path = {str(item.get("path", "")).replace("\\", "/"): item for item in items}
    drafts = [path for path in by_path if path.endswith("/resume-draft.md")]
    parked = [path for path in by_path if path.endswith("/resume-not-generated.md")]
    if bool(drafts) == bool(parked) or len(drafts) > 1:
        _record_publish_error(ctx, label, "resume_artifact_set",
                              "return exactly one draft or one resume-not-generated artifact")
        return False
    if parked:
        ctx.emit("validator", "prep-resume publish gate: PASS (parked group)")
        return True
    draft_rel = drafts[0]
    group_dir = str(PurePosixPath(draft_rel).parent)
    expected = {
        f"{group_dir}/target-brief.json", f"{group_dir}/resume-score.md",
        draft_rel, f"{group_dir}/reasons.json", f"{group_dir}/resume-validation.md",
    }
    missing = sorted(expected - set(by_path))
    draft_text = str(by_path[draft_rel].get("text", ""))
    forbidden = []
    if re.search(r"\bEV-\d+\b", draft_text, re.I):
        forbidden.append("EV IDs")
    if re.search(r"^#{1,3}\s+(?:Evidence Gaps?|Validation|Target)\b", draft_text, re.I | re.M):
        forbidden.append("audit-only section")
    reasons_item = by_path.get(f"{group_dir}/reasons.json", {})
    brief_item = by_path.get(f"{group_dir}/target-brief.json", {})
    reasons = _coerce_typed_json_artifact(reasons_item, list)
    brief = _coerce_typed_json_artifact(brief_item, dict)
    missing.extend(_validate_resume_reasons_shape(reasons))
    missing.extend(_validate_resume_target_brief(brief))
    if missing or forbidden:
        detail = "; ".join(missing + forbidden)
        _record_publish_error(ctx, label, "resume_schema", detail)
        ctx.emit("validator", "prep-resume publish gate: FAIL")
        return False
    baseline = ctx.project / "resumes" / "baseline-extracted.md"
    with tempfile.TemporaryDirectory(prefix="rolenavi-resume-") as tmp:
        root = Path(tmp)
        staged: dict[str, Path] = {}
        for rel, item in by_path.items():
            target = root / PurePosixPath(rel)
            target.parent.mkdir(parents=True, exist_ok=True)
            if "json" in item:
                target.write_text(json.dumps(item["json"], indent=2, ensure_ascii=False) + "\n",
                                  encoding="utf-8")
            else:
                target.write_text(str(item.get("text", "")), encoding="utf-8")
            staged[rel] = target
        bullets = core.run_script(
            "validate_resume_bullets", str(staged[draft_rel]), "--reasons",
            str(staged[f"{group_dir}/reasons.json"]), env=_env_for(ctx.project))
        tailoring = core.run_script(
            "validate_resume_tailoring", str(staged[draft_rel]), "--baseline", str(baseline),
            "--target-brief", str(staged[f"{group_dir}/target-brief.json"]), "--reasons",
            str(staged[f"{group_dir}/reasons.json"]), env=_env_for(ctx.project))
    failures = []
    for name, result in (("validate_resume_bullets", bullets),
                         ("validate_resume_tailoring", tailoring)):
        out = (result.stdout + result.stderr).strip()
        ctx.validator_results.append({
            "validator": f"{name}[publish-gate]", "target": draft_rel,
            "returncode": result.returncode, "output": out[:800],
        })
        if result.returncode != 0:
            failures.append(f"{name}: {_validator_failure_first(out)}")
    mapping = _resume_reason_mapping_diagnostics(draft_text, reasons)
    if mapping["unmatched_bullets"] or mapping["unmatched_reason_prefixes"]:
        failures.append(
            "reason mapping: " + json.dumps(mapping, ensure_ascii=False)[:2000]
        )
    if failures:
        _record_publish_error(ctx, label, "resume_content_validation", "; ".join(failures))
        ctx.emit("validator", "prep-resume publish gate: FAIL")
        return False
    ctx.emit("validator", "prep-resume publish gate: PASS")
    return True


def _prevalidate_artifact_payload(ctx: RunContext, stage_workflow: str,
                                  artifacts: list[dict], label: str = "") -> bool:
    if stage_workflow == "prep-strategy":
        return _prevalidate_strategy_payload(ctx, artifacts)
    if stage_workflow == "prep-resume":
        return _prevalidate_resume_payload(ctx, artifacts, label)
    if stage_workflow == "prep-linkedin":
        return _prevalidate_linkedin_payload(ctx, artifacts, label)
    if stage_workflow == "apply":
        markdown = [item for item in artifacts if isinstance(item, dict)
                    and str(item.get("path", "")).replace("\\", "/")
                    .endswith("/application-instructions.md")]
        if len(markdown) != 1 or len(artifacts) != 1:
            _record_publish_error(
                ctx, label, "application_artifact_set",
                "expected exactly one applications/<job>/application-instructions.md artifact",
            )
            return False
        text = str(markdown[0].get("text", ""))
        required = (
            "Position summary", "Current posting state", "Application route",
            "Required materials", "Field-by-field guidance", "Sensitive fields",
            "Step-by-step user instructions", "What to save after submission",
            "Tracker update recommendation",
        )
        missing = [heading for heading in required if not re.search(
            rf"^#{{1,3}}\s+{re.escape(heading)}\s*$", text, re.I | re.M
        )]
        unsafe = []
        if not re.search(r"\b(?:do not|never|not)\b.{0,80}\bsubmit", text, re.I | re.S):
            unsafe.append("explicit no-submit boundary")
        if not re.search(r"capture (?:completeness|boundary)|required upload", text, re.I):
            unsafe.append("route-capture completeness/boundary")
        if re.search(r"https?://[^\s]*\[(?:phone|contact) redacted\]", text, re.I):
            unsafe.append("unbroken verified posting/application URLs")
        if missing or unsafe:
            _record_publish_error(
                ctx, label, "application_packet_schema",
                "; ".join([*(f"missing {item}" for item in missing),
                           *(f"missing {item}" for item in unsafe)]),
            )
            ctx.emit("validator", "apply publish gate: FAIL")
            return False
        ctx.emit("validator", "apply publish gate: PASS")
    if stage_workflow == "story-bank":
        story_items = [item for item in artifacts
                       if str(item.get("path", "")).replace("\\", "/") == "interviews/story-bank.json"]
        if len(story_items) != 1:
            ctx.mark_partial("story-bank_publish_gate", "expected exactly one story-bank.json")
            return False
        canonical, errors = _canonical_story_bank(_artifact_payload_value(story_items[0]))
        if errors or canonical is None:
            ctx.mark_partial("story-bank_publish_gate", "; ".join(errors[:10]))
            ctx.emit("validator", "story-bank publish gate: FAIL")
            return False
        story_items[0].pop("text", None)
        story_items[0]["json"] = canonical
        mirror = [item for item in artifacts
                  if str(item.get("path", "")).replace("\\", "/") == "interviews/story-bank.md"]
        if len(mirror) != 1:
            ctx.mark_partial("story-bank_publish_gate", "expected exactly one story-bank.md")
            return False
        mirror[0].pop("json", None)
        mirror[0]["text"] = _story_bank_markdown(canonical)
        ctx.emit("validator", "story-bank publish gate: PASS")
    return True


def _max_strategy_groups(job_count: int) -> int:
    return job_count if job_count < 6 else max(3, (job_count * 3 + 4) // 5)


def _hydrate_strategy_group_assignments(project: Path,
                                        assignments: list[dict]) -> list[dict]:
    from ..repositories import job_rows

    group_by_id = {
        str(row.get("job_id", "")): _slug(row.get("job_group", ""), "")
        for row in assignments if isinstance(row, dict)
    }
    # validate_job_rows intentionally accepts only complete, persistable rows.
    # Hydrate the semantic group patch from SQLite before passing it through the
    # normal all-or-nothing upsert pipeline; partial rows would fail required-field
    # validation even though store_io.upsert itself supports patch semantics.
    current = {
        str(row.get("job_id", "")): dict(row)
        for row in job_rows(project, job_ids=list(group_by_id))
    }
    hydrated: list[dict] = []
    for job_id, group in group_by_id.items():
        row = current.get(job_id)
        if row is None or not group:
            return []
        row["job_group"] = group
        hydrated.append(row)
    return hydrated if len(hydrated) == len(group_by_id) else []


def _apply_strategy_group_assignments(ctx: RunContext) -> bool:
    path = ctx.project / "strategy" / "group-assignments.json"
    data = _read_json(path, {})
    assignments = data.get("assignments", []) if isinstance(data, dict) else []
    hydrated = _hydrate_strategy_group_assignments(ctx.project, assignments)
    if not hydrated:
        ctx.emit("validator", "strategy assignment hydration failed")
        return False
    return _store_write(
        ctx, {"type": "store_write", "store": "job_list", "rows": hydrated})


def _artifact_snapshot(project: Path, artifacts: list[dict]) -> dict[Path, bytes | None]:
    snapshot: dict[Path, bytes | None] = {}
    for item in artifacts:
        try:
            rel_path, _ = _safe_artifact_rel(str(item.get("path", "")))
        except RoleNaviError:
            continue
        path = project / rel_path
        snapshot[path] = path.read_bytes() if path.is_file() else None
    manifest = project / "artifacts" / "manifest.json"
    snapshot[manifest] = manifest.read_bytes() if manifest.is_file() else None
    return snapshot


def _restore_artifact_snapshot(snapshot: dict[Path, bytes | None]) -> None:
    for path, content in snapshot.items():
        if content is None:
            path.unlink(missing_ok=True)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(path.name + ".rollback")
        temp.write_bytes(content)
        os.replace(temp, path)


def _run_staging_dir(ctx: RunContext, label: str) -> Path:
    run_id = _slug(getattr(ctx, "run_id", ""), "unrecorded-run")
    # status.json retains the complete logical label. Do not duplicate that
    # label in the physical path: the run root plus nested logical artifact path
    # otherwise exceeds the Windows legacy MAX_PATH budget for long role names.
    # A fixed-size content-derived key remains stable and collision-safe while
    # giving every workflow the same predictable staging path budget.
    logical_label = str(label or "artifact")
    safe_label = "s-" + hashlib.sha256(logical_label.encode("utf-8")).hexdigest()[:16]
    return (ctx.project / "runtime" / "runs" / run_id / "staging" /
            safe_label)


def _write_staging_status(ctx: RunContext, label: str, *, workflow: str,
                          state: str, artifacts: list[str] | None = None,
                          error: dict | None = None) -> None:
    root = _run_staging_dir(ctx, label)
    root.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": "rolenavi-generated-artifact-staging-v1",
        "run_id": getattr(ctx, "run_id", ""),
        "label": label,
        "workflow": workflow,
        "state": state,
        "artifacts": list(artifacts or []),
        "updated_at": _now(),
    }
    if error:
        payload["error_code"] = str(error.get("code", "publish-gate"))[:80]
        payload["error_detail"] = str(error.get("detail", ""))[:4000]
    path = root / "status.json"
    temp = path.with_suffix(".json.tmp")
    temp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8")
    os.replace(temp, path)


def _mark_staging_repairing(ctx: RunContext, label: str, workflow: str) -> None:
    status_path = _run_staging_dir(ctx, label) / "status.json"
    current = _read_json(status_path, {})
    artifacts = current.get("artifacts", []) if isinstance(current, dict) else []
    _write_staging_status(
        ctx, label, workflow=workflow, state="repairing",
        artifacts=[str(item) for item in artifacts if str(item)],
    )


def _stage_generated_artifacts(ctx: RunContext, label: str, workflow: str,
                               artifacts: list[dict]) -> list[str]:
    """Preserve generated output before validation without touching canonical files."""
    stage_dir = _run_staging_dir(ctx, label)
    root = stage_dir / "artifacts"
    staged: list[str] = []
    for item in artifacts:
        if not isinstance(item, dict):
            continue
        try:
            rel_path, rel = _safe_artifact_rel(str(item.get("path", "")))
        except RoleNaviError:
            continue
        target = root / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        value = _artifact_payload_value(item)
        if isinstance(value, (dict, list)):
            content = json.dumps(value, indent=2, ensure_ascii=False) + "\n"
        else:
            content = str(value or "")
        target.write_text(content, encoding="utf-8")
        staged.append(f"runtime/runs/{_slug(getattr(ctx, 'run_id', ''), 'unrecorded-run')}/staging/"
                      f"{stage_dir.name}/artifacts/{rel}")
    _write_staging_status(ctx, label, workflow=workflow, state="generated",
                          artifacts=staged)
    return staged


def _materialize_runner_artifact_output(ctx: RunContext, envelope: dict,
                                        allowed_paths: set[str] | None = None,
                                        stage_workflow: str | None = None,
                                        label: str = "") -> bool:
    payload = _extract_runner_artifact_payload(_result_text(envelope))
    if not payload:
        return False
    artifacts = payload.get("artifacts", [])
    store_writes = payload.get("store_writes", [])
    artifact_count = 0
    store_count = 0
    if not isinstance(artifacts, list):
        artifacts = []
    if not isinstance(store_writes, list):
        store_writes = []
    if len(artifacts) > 50 or len(store_writes) > 5:
        ctx.mark_partial("runner_artifact_output_oversized",
                         "typed output exceeds artifact/store-write count limits")
        return False
    effective_workflow = stage_workflow or ctx.workflow
    stage_label = label or effective_workflow
    stage_enabled = effective_workflow in STAGED_PUBLISH_WORKFLOWS
    staged_paths = (_stage_generated_artifacts(
        ctx, stage_label, effective_workflow,
        [item for item in artifacts if isinstance(item, dict)],
    ) if stage_enabled else [])
    if not _prevalidate_artifact_payload(ctx, effective_workflow, artifacts, stage_label):
        error = getattr(ctx, "publish_errors", {}).get(stage_label, {
            "code": "publish-gate", "detail": "artifact publish gate rejected output",
        })
        if stage_enabled:
            _write_staging_status(ctx, stage_label, workflow=effective_workflow,
                                  state="validation_failed", artifacts=staged_paths,
                                  error=error)
        return False
    if stage_enabled:
        _write_staging_status(ctx, stage_label, workflow=effective_workflow,
                              state="validated", artifacts=staged_paths)
    publish_snapshot = (_artifact_snapshot(ctx.project, artifacts)
                        if effective_workflow in {"prep-strategy", "prep-resume",
                                                  "prep-linkedin", "story-bank", "apply"}
                        else {})
    written_before = len(ctx.artifacts_written)
    usage = envelope.get("usage", {}) if isinstance(envelope, dict) else {}
    model_config = envelope.get("model_config", {}) if isinstance(envelope, dict) else {}
    provenance = {
        "prompt_fingerprint": usage.get("prompt_fingerprint", ""),
        "model": model_config.get("model", ""),
    }
    seen_paths: set[str] = set()
    for item in artifacts:
        if not isinstance(item, dict):
            continue
        if set(item) - {"path", "text", "json"}:
            ctx.mark_partial("runner_artifact_unknown_fields",
                             "artifact contains unknown fields")
            continue
        path = str(item.get("path", "")).strip()
        if not path:
            continue
        try:
            _, rel = _safe_artifact_rel(path)
        except RoleNaviError as e:
            ctx.mark_partial("runner_artifact_path_invalid", str(e))
            continue
        if allowed_paths is not None and rel not in allowed_paths:
            ctx.validator_results.append({
                "validator": "runner_artifact_path_scope",
                "target": rel,
                "returncode": 2,
                "output": "discarded artifact outside allowed path(s): "
                          + ", ".join(sorted(allowed_paths)),
            })
            ctx.emit("validator", f"discarded out-of-scope artifact: {rel}")
            continue
        if rel in seen_paths:
            ctx.mark_partial("runner_artifact_duplicate", f"duplicate artifact path: {rel}")
            continue
        seen_paths.add(rel)
        if "text" in item and len(str(item.get("text", "")).encode("utf-8")) > 1_000_000:
            ctx.mark_partial("runner_artifact_oversized", f"artifact too large: {rel}")
            continue
        ev = {"type": "artifact", "path": path}
        opportunity_universe = (
            ctx.workflow == "opportunity-plan"
            and rel == "targets/company-universe.json"
        )
        artifact_json = item.get("json")
        if opportunity_universe and artifact_json is None and "text" in item:
            try:
                artifact_json = json.loads(str(item.get("text", "")))
            except json.JSONDecodeError:
                ctx.mark_partial(
                    "opportunity_plan_validation",
                    "company universe artifact must contain a JSON object",
                )
                continue
        if artifact_json is not None:
            if opportunity_universe and isinstance(artifact_json, dict):
                universe_errors = _validate_opportunity_universe(ctx.project, artifact_json)
                if universe_errors:
                    ctx.mark_partial("opportunity_plan_validation", "; ".join(universe_errors[:8]))
                    ctx.emit("validator", "company universe rejected: " + "; ".join(universe_errors[:4]))
                    continue
                current_meta = project_meta.load(ctx.project)
                artifact_json = dict(artifact_json)
                artifact_json["preference_revision"] = int(
                    current_meta.get("preference_revision", 0) or 0)
                artifact_json["preference_fingerprint"] = (
                    project_meta.preference_fingerprint(current_meta))
            elif opportunity_universe:
                ctx.mark_partial(
                    "opportunity_plan_validation",
                    "company universe artifact must be a JSON object",
                )
                continue
            ev["json"] = artifact_json
        else:
            ev["text"] = str(item.get("text", ""))
        _write_artifact(ctx, ev, provenance=provenance)
        artifact_count += 1
    for item in store_writes:
        if not isinstance(item, dict):
            continue
        store = str(item.get("store", "")).strip()
        rows = item.get("rows", [])
        # Apply tracker rows are runner-owned so a model cannot invent or regress
        # private application state.  The dedicated apply workflow writes them
        # only after the corresponding packet has passed its publish gate.
        allowed_stores = {"job_list"} if effective_workflow == "search" else set()
        if store not in allowed_stores or not isinstance(rows, list) or len(rows) > 500:
            if store:
                ctx.mark_partial("runner_store_scope",
                                 f"store write not allowed for {ctx.workflow}: {store}")
            continue
        _store_write(ctx, {"type": "store_write", "store": store, "rows": rows})
        store_count += 1
    ctx.validator_results.append({
        "validator": "runner_artifact_output_materialized",
        "target": ctx.project.name,
        "returncode": 0,
        "output": f"{artifact_count} artifact(s), {store_count} store write(s)",
    })
    ctx.emit(
        "artifact",
        f"runner artifact output materialized: {artifact_count} artifact(s), "
        f"{store_count} store write(s)",
    )
    if effective_workflow == "prep-strategy" and not _apply_strategy_group_assignments(ctx):
        _restore_artifact_snapshot(publish_snapshot)
        del ctx.artifacts_written[written_before:]
        ctx.mark_partial("prep-strategy_store_gate",
                         "focused job-group assignments were not persisted; artifact publish rolled back")
        ctx.emit("validator", "prep-strategy atomic publish: ROLLED BACK")
        if stage_enabled:
            _write_staging_status(
                ctx, stage_label, workflow=effective_workflow, state="publish_failed",
                artifacts=staged_paths,
                error={"code": "strategy-store-gate",
                       "detail": "focused job-group assignments were not persisted"},
            )
        return False
    if stage_enabled:
        if not hasattr(ctx, "publish_results"):
            ctx.publish_results = {}
        ctx.publish_results[stage_label] = {
            "state": "published", "workflow": effective_workflow,
            "artifacts": staged_paths,
        }
        _write_staging_status(ctx, stage_label, workflow=effective_workflow,
                              state="published", artifacts=staged_paths)
    return True


def _extract_score_output_payload(text: str) -> dict | None:
    raw = str(text or "").strip()
    if not raw:
        return None
    marker = "SCORE_OUTPUT_JSON:"
    if marker in raw:
        raw = raw.split(marker, 1)[1].strip()
    try:
        payload = _extract_json_object(raw)
    except (ValueError, json.JSONDecodeError):
        return None
    if payload.get("schema") != "rolenavi-score-output-v1":
        return None
    return payload


def _materialize_score_output(ctx: RunContext, envelope: dict) -> bool:
    """Persist score artifacts from final JSON when the agent sandbox is read-only."""
    payload = _extract_score_output_payload(_score_result_text(envelope))
    if not payload:
        return False
    ratings = payload.get("job_ratings")
    if not isinstance(ratings, list):
        ctx.mark_partial("score_output", "rolenavi-score-output-v1 missing job_ratings list")
        return False
    strategy_dir = ctx.project / "strategy"
    groups_dir = ctx.project / "targets" / "job-groups"
    strategy_dir.mkdir(parents=True, exist_ok=True)
    groups_dir.mkdir(parents=True, exist_ok=True)
    (strategy_dir / "job-ratings.json").write_text(
        json.dumps(ratings, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    priorities = str(payload.get("target_priorities_md", "")).strip()
    if priorities:
        (strategy_dir / "target-priorities.md").write_text(priorities + "\n", encoding="utf-8")
    group_count = 0
    for item in payload.get("job_groups", []):
        if not isinstance(item, dict):
            continue
        slug = _slug(item.get("slug", ""), "")
        markdown = str(item.get("markdown", "")).strip()
        if not slug or not markdown:
            continue
        (groups_dir / f"{slug}.md").write_text(markdown + "\n", encoding="utf-8")
        group_count += 1
    ctx.emit(
        "artifact",
        f"score output materialized: {len(ratings)} rating(s), {group_count} group file(s)",
    )
    ctx.validator_results.append({
        "validator": "score_output_materialized",
        "target": ctx.project.name,
        "returncode": 0,
        "output": f"{len(ratings)} rating(s), {group_count} group file(s)",
    })
    return True


def _profile_ready(pdir: Path | None) -> bool:
    return bool(pdir and (pdir / "candidate-profile.md").exists()
                and (pdir / "evidence-map.md").exists())


def _merge_usage(parent: dict, child: dict) -> None:
    parent.setdefault("events", []).extend(child.get("events", []))
    usage = parent.setdefault("usage", {})
    for key, value in child.get("usage", {}).items():
        if isinstance(value, (int, float)):
            usage[key] = usage.get(key, 0) + value
    if "model_config" not in parent and child.get("model_config"):
        parent["model_config"] = child["model_config"]


def _execute_subagent(ctx: RunContext, provider, workflow: str, context: dict,
                      on_stream, *, label: str | None = None,
                      model_workflow: str | None = None,
                      allowed_artifacts: set[str] | None = None) -> dict:
    label = label or workflow
    ctx.emit("info", f"subagent start: {label}")
    envelope = _provider_run(provider, workflow, context,
                             _labelled_stream(ctx, label, on_stream),
                             model_workflow=model_workflow)
    ctx.streamed = bool(envelope.get("streamed"))
    events = envelope.get("events", [])
    if workflow in STAGED_PUBLISH_WORKFLOWS:
        # Canonical prep files are written only after the typed final payload
        # passes its gate. Streaming artifact events must not bypass atomicity.
        events = [event for event in events if not isinstance(event, dict)
                  or event.get("type") not in {"artifact", "store_write"}]
    execute_events(ctx, events, allowed_artifacts=allowed_artifacts)
    _append_agent_result_log(ctx, label, envelope)
    materialized = _materialize_runner_artifact_output(
        ctx, envelope, allowed_paths=allowed_artifacts, stage_workflow=workflow,
        label=label)
    if workflow in {"prep-strategy", "prep-resume", "prep-linkedin", "story-bank"} and not materialized:
        ctx.failure_class = ctx.failure_class or f"{workflow.replace('-', '_')}_publish_gate_failure"
    for ev in envelope.get("events", []):
        if isinstance(ev, dict) and ev.get("type") == "result" and workflow != "score":
            ev.pop("content", None)
    ctx.emit("info", f"subagent done: {label}")
    return envelope


def _run_score_workflow(ctx: RunContext, provider, context: dict, on_stream,
                        *, label: str = "score") -> dict:
    del label
    envelope = _run_score_batches(ctx, provider, context, on_stream)
    if not envelope.get("ratings"):
        ctx.mark_partial(
            "score_batch",
            "no current validated ratings are available; finalizer was skipped and job rows "
            "remain unchanged",
        )
        return envelope
    if not ctx.failure_class and not ctx.blocked_reasons:
        _materialize_score_output(ctx, envelope)
        for ev in envelope.get("events", []):
            if isinstance(ev, dict) and ev.get("type") == "result":
                ev.pop("content", None)
        _finalize_score(ctx)
    return envelope


def _application_tracker_row(project: Path, row: dict, artifact_path: str,
                             route_audit: dict) -> dict:
    from ..repositories import tracker_rows

    application_id = f"app--{row.get('job_id', '')}"
    existing = next((dict(item) for item in tracker_rows(project)
                     if item.get("application_id") == application_id), {})
    status = str(existing.get("status") or "to_apply")
    today = date.today()
    due_days = 2 if str(row.get("priority", "")) == "high" else 5
    packet_note = (
        f"Local application packet: {artifact_path}. Route capture: "
        f"{route_audit.get('capture_completeness', 'unknown')}."
    )
    prior_notes = str(existing.get("notes", "")).strip()
    prior_notes = re.sub(
        r"(?:^|\s)Local application packet:\s*applications/\S+/application-instructions\.md\.\s*"
        r"Route capture:\s*[^.]+\.",
        " ", prior_notes, flags=re.I,
    ).strip()
    notes = f"{prior_notes} {packet_note}".strip()
    next_action = str(existing.get("next_action", "")).strip()
    if status == "to_apply" or not next_action:
        next_action = f"Review {artifact_path}, confirm manual-only fields, then submit personally."
    return {
        "application_id": application_id,
        "job_id": str(row.get("job_id", "")),
        "company": str(row.get("company", "")),
        "title": str(row.get("title", "")),
        "job_group": str(row.get("job_group", "")),
        "status": status,
        "applied_at": str(existing.get("applied_at", "")),
        "resume_version": str(existing.get("resume_version") or
                              _recommended_application_resume(project, row)),
        "linkedin_version": str(existing.get("linkedin_version") or
                                _recommended_application_linkedin(project, row)),
        "contact": str(existing.get("contact", "")),
        "next_action": next_action,
        "next_action_due": str(existing.get("next_action_due") or
                               (today + timedelta(days=due_days)).isoformat()),
        "last_updated": today.isoformat(),
        "outcome": str(existing.get("outcome", "")),
        "notes": notes,
    }


def _run_apply_workflow(ctx: RunContext, provider, context_for, stale_material: str,
                        on_stream, pdir: Path | None, task: str | None,
                        run_intent: dict) -> dict:
    selected = _select_application_jobs(ctx.project, task, run_intent)
    if not selected:
        ctx.mark_blocked(
            "apply_selection",
            "No focused role matched this apply instruction. Focus a role first or name an exact focused job.",
        )
        ctx.emit("attention", "Apply blocked: focus a role first or name an exact focused job.")
        return {"events": [], "usage": {}}
    ctx.emit(
        "info",
        "apply scope: " + "; ".join(
            f"{row.get('company')} — {row.get('title')}" for row in selected
        ),
    )

    jobs: list[dict] = []
    by_label: dict[str, tuple[dict, dict, str]] = {}
    snapshots: dict[str, dict] = {}
    for index, row in enumerate(selected, start=1):
        ctx.check_cancelled()
        posting_url = str(row.get("job_page_url") or row.get("source_url") or "").strip()
        label = _agent_label("apply", index, str(row.get("company", "")))
        expected = _application_artifact_path(row)
        ctx.emit("progress", f"{label}: auditing public application route (read-only)")
        audit = application_audit.audit_application_route(posting_url, allow_browser=True)
        completeness = str(audit.get("capture_completeness", "failed"))
        ctx.validator_results.append({
            "validator": "application_route_audit",
            "target": str(row.get("job_id", "")),
            "returncode": 1 if completeness == "failed" else 0,
            "output": (
                f"vendor={audit.get('vendor', 'unknown')} capture={completeness}; "
                f"{audit.get('capture_boundary', '')}"
            )[:800],
        })
        if completeness == "failed":
            detail = "; ".join(str(item) for item in audit.get("errors", [])) or \
                "No verified application route/form could be captured."
            ctx.mark_partial(label, detail)
            ctx.emit("attention", f"{label}: application route audit failed: {detail[:400]}")
            continue
        packet = _runner_application_role_packet(ctx.project, pdir, row, audit, selected)
        extra = {"application_role_packet": packet}
        job_context = context_for("apply", stale_material, extra)
        jobs.append({
            "label": label,
            "workflow": "apply",
            "context": job_context,
            "allowed_artifacts": {expected},
            "repair_on_publish_fail": True,
            "publish_repair_attempts": 1,
        })
        by_label[label] = (row, audit, expected)
        snapshots[label] = _artifact_snapshot(ctx.project, [{"path": expected}])

    if not jobs:
        ctx.failure_class = ctx.failure_class or "application_route_audit_failed"
        return {"events": [], "usage": {}}
    merged = _run_parallel_named_subagents(ctx, provider, jobs, on_stream)
    for label in merged.get("published_labels", []):
        row, audit, expected = by_label[label]
        tracker = _application_tracker_row(ctx.project, row, expected, audit)
        if not _store_write(ctx, {"type": "store_write", "store": "tracker", "rows": [tracker]}):
            _restore_artifact_snapshot(snapshots[label])
            ctx.artifacts_written[:] = [path for path in ctx.artifacts_written if path != expected]
            ctx.failure_class = ctx.failure_class or "application_tracker_atomic_publish_failed"
            ctx.emit("validator", f"{label}: packet publish rolled back because tracker write failed")
        else:
            ctx.emit("result", f"Application instructions ready: {expected}")
    if not merged.get("published_labels") and not ctx.failure_class:
        ctx.failure_class = "application_packet_publish_failed"
    return merged


def _run_parallel_subagents(ctx: RunContext, provider, workflows_to_run: list[str],
                            context_for, on_stream) -> dict:
    if not workflows_to_run:
        return {"events": [], "usage": {}, "failed_labels": []}
    if len(workflows_to_run) == 1:
        wf = workflows_to_run[0]
        label = _agent_label(wf, 1)
        try:
            return _execute_subagent(ctx, provider, wf, context_for(wf), on_stream,
                                     label=label)
        except Exception as e:
            ctx.mark_partial(label, str(e))
            ctx.emit("info", f"subagent failed: {label}: {str(e)[:300]}")
            if _looks_blocked_error(str(e)):
                ctx.mark_blocked(label, str(e))
            else:
                ctx.failure_class = ctx.failure_class or "workflow_subagent_failed"
            return {"events": [], "usage": {}, "failed_labels": [label]}

    ctx.emit("info", "subagents start: " + ", ".join(workflows_to_run))
    results: dict[str, dict] = {}
    failures: dict[str, str] = {}
    labels = {wf: _agent_label(wf, i) for i, wf in enumerate(workflows_to_run, start=1)}
    with ThreadPoolExecutor(max_workers=len(workflows_to_run)) as pool:
        futures = {
            pool.submit(_provider_run, provider, wf, context_for(wf),
                        _labelled_stream(ctx, labels[wf], on_stream)): wf
            for wf in workflows_to_run
        }
        for fut in as_completed(futures):
            wf = futures[fut]
            try:
                results[wf] = fut.result()
            except Exception as e:
                failures[labels[wf]] = str(e)
                ctx.mark_partial(labels[wf], str(e))
                ctx.emit("info", f"subagent failed: {labels[wf]}: {str(e)[:300]}")
    merged: dict = {"events": [], "usage": {}, "failed_labels": sorted(failures)}
    if failures and not results:
        reason = "; ".join(f"{label}: {msg}" for label, msg in failures.items())
        if all(_looks_blocked_error(msg) for msg in failures.values()):
            ctx.mark_blocked("all_parallel_subagents", reason)
        else:
            ctx.failure_class = ctx.failure_class or "all_parallel_subagents_failed"
    for wf in workflows_to_run:
        if wf not in results:
            continue
        envelope = results[wf]
        ctx.streamed = bool(envelope.get("streamed"))
        execute_events(ctx, envelope.get("events", []))
        _materialize_runner_artifact_output(ctx, envelope)
        for ev in envelope.get("events", []):
            if isinstance(ev, dict) and ev.get("type") == "result" and wf != "score":
                ev.pop("content", None)
        _merge_usage(merged, envelope)
        ctx.emit("info", f"subagent done: {labels[wf]}")
    return merged


def _is_linkedin_source(source: dict) -> bool:
    text = " ".join(str(source.get(k, "")) for k in ("type", "url", "query", "scope"))
    return "linkedin" in text.lower()


def _search_capture_shards(project: Path, max_shards: int = 6,
                           run_intent: dict | None = None) -> list[dict]:
    """Build deterministic non-LinkedIn capture shards from source-plan.json."""
    source_plan = project / "targets" / "source-plan.json"
    try:
        data = json.loads(source_plan.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    companies = data.get("companies", []) if isinstance(data, dict) else []
    filtered: list[dict] = []
    for company in companies:
        if not isinstance(company, dict):
            continue
        sources = [
            src for src in company.get("sources", [])
            if isinstance(src, dict) and not _is_linkedin_source(src)
        ]
        item = dict(company)
        item["sources"] = sources
        if item.get("name") and sources:
            filtered.append(item)
    requested = {
        _slug(name, "") for name in (run_intent or {}).get("requested_companies", [])
        if _slug(name, "")
    }
    if requested:
        filtered = [
            company for company in filtered
            if _slug(company.get("name", ""), "") in requested
        ]
    if not filtered:
        return []

    # Isolate companies so one source blocker (for example a DNS-blocked Google
    # careers API) cannot cause the agent to abandon other companies in the same
    # shard. _run_parallel_named_subagents caps concurrent workers separately.
    shards = [{"companies": [company]} for company in filtered]
    for idx, shard in enumerate(shards, start=1):
        label = _agent_label("search", idx, _shard_generated_name(shard["companies"]))
        shard["id"] = label
        shard["part_path"] = f"targets/research-log.parts/{label}.json"
    return [shard for shard in shards if shard["companies"]]


def _search_repair_shards(project: Path, retry_companies: list[str]) -> list[dict]:
    source_plan = project / "targets" / "source-plan.json"
    try:
        data = json.loads(source_plan.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    companies = data.get("companies", []) if isinstance(data, dict) else []
    by_norm = {
        _slug(company.get("name", ""), ""): company
        for company in companies if isinstance(company, dict) and company.get("name")
    }
    shards: list[dict] = []
    seen: set[str] = set()
    for name in retry_companies:
        key = _slug(name, "")
        if not key or key in seen or key not in by_norm:
            continue
        seen.add(key)
        company = dict(by_norm[key])
        company["sources"] = [
            src for src in company.get("sources", [])
            if isinstance(src, dict) and not _is_linkedin_source(src)
        ]
        if not company["sources"]:
            continue
        idx = len(shards) + 1
        label = _agent_label("search-retry", idx, _shard_generated_name([company]))
        shards.append({
            "id": label,
            "companies": [company],
            "part_path": f"targets/research-log.parts/{label}.json",
        })
    return shards


def _bounded_retry_companies(retry_companies: list[str]) -> list[str]:
    try:
        limit = int(os.environ.get("ROLENAVI_SEARCH_MAX_RETRY_COMPANIES",
                                   str(DEFAULT_SEARCH_RETRY_COMPANY_LIMIT)) or
                    DEFAULT_SEARCH_RETRY_COMPANY_LIMIT)
    except ValueError:
        limit = DEFAULT_SEARCH_RETRY_COMPANY_LIMIT
    if limit <= 0:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for name in retry_companies:
        clean = str(name or "").strip()
        key = _slug(clean, "")
        if not clean or not key or key in seen:
            continue
        seen.add(key)
        out.append(clean)
        if len(out) >= limit:
            break
    return out


def _run_parallel_named_subagents(ctx: RunContext, provider, jobs: list[dict],
                                  on_stream, *,
                                  hard_fail_when_all_fail: bool = True) -> dict:
    if not jobs:
        return {"events": [], "usage": {}}

    ctx.emit("info", "subagents start: " + ", ".join(job["label"] for job in jobs))
    results: dict[str, dict] = {}
    failures: dict[str, str] = {}
    records: list[dict] = []
    try:
        max_workers = int(os.environ.get("ROLENAVI_SEARCH_MAX_PARALLEL", "6") or "6")
    except ValueError:
        max_workers = 6
    workers = max(1, min(len(jobs), max_workers))
    prep_phase = str(jobs[0].get("workflow", "")).removeprefix("prep-")
    prep_completed = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(
                _provider_run,
                provider,
                job["workflow"],
                job["context"],
                _labelled_stream(ctx, job["label"], on_stream),
                job.get("model_workflow"),
            ): job
            for job in jobs
        }
        for fut in as_completed(futures):
            job = futures[fut]
            label = job["label"]
            try:
                envelope = fut.result()
                results[label] = envelope
                # Publish a passing independent group as soon as its provider call
                # completes. Slow siblings and their repairs must not hide ready work.
                ctx.streamed = bool(envelope.get("streamed"))
                allowed = job.get("allowed_artifacts")
                event_batch = envelope.get("events", [])
                if job["workflow"] in STAGED_PUBLISH_WORKFLOWS:
                    event_batch = [
                        event for event in event_batch if not isinstance(event, dict)
                        or event.get("type") not in {"artifact", "store_write"}
                    ]
                execute_events(ctx, event_batch, allowed_artifacts=allowed)
                _append_agent_result_log(ctx, label, envelope)
                job["_initial_materialized"] = _materialize_runner_artifact_output(
                    ctx, envelope, allowed_paths=allowed,
                    stage_workflow=job["workflow"], label=label,
                )
                if (not job["_initial_materialized"]
                        and job.get("repair_on_publish_fail")):
                    _mark_staging_repairing(ctx, label, job["workflow"])
                job["_initial_processed"] = True
                records.append({"label": label, "workflow": job["workflow"],
                                "model_workflow": job.get("model_workflow", ""),
                                "status": "ok"})
            except RunCancelled:
                raise
            except Exception as e:
                failures[label] = str(e)
                records.append({"label": label, "workflow": job["workflow"],
                                "model_workflow": job.get("model_workflow", ""),
                                "status": "failed", "error": str(e)[:1000]})
                _append_agent_log(ctx, label, f"ERROR: {e}")
                ctx.mark_partial(label, str(e))
                ctx.emit("info", f"subagent failed: {label}: {str(e)[:300]}")
            prep_completed += 1
            _update_prep_progress(
                ctx, prep_phase, "running", completed=prep_completed, total=len(jobs),
                detail=("published" if job.get("_initial_materialized")
                        else "generated; validation/repair pending"),
            )
    _write_agent_manifest(ctx, records)
    merged: dict = {"events": [], "usage": {}, "failed_labels": sorted(failures),
                    "published_labels": [], "validation_failed_labels": []}
    if hard_fail_when_all_fail and failures and not results:
        reason = "; ".join(f"{label}: {msg}" for label, msg in failures.items())
        if all(_looks_blocked_error(msg) for msg in failures.values()):
            ctx.mark_blocked("all_named_subagents", reason)
        else:
            ctx.failure_class = ctx.failure_class or "all_named_subagents_failed"
    for job in jobs:
        label = job["label"]
        if label not in results:
            continue
        envelope = results[label]
        ctx.streamed = bool(envelope.get("streamed"))
        allowed = job.get("allowed_artifacts")
        if job.get("_initial_processed"):
            materialized = bool(job.get("_initial_materialized"))
        else:
            event_batch = envelope.get("events", [])
            if job["workflow"] in STAGED_PUBLISH_WORKFLOWS:
                event_batch = [event for event in event_batch if not isinstance(event, dict)
                               or event.get("type") not in {"artifact", "store_write"}]
            execute_events(ctx, event_batch, allowed_artifacts=allowed)
            _append_agent_result_log(ctx, label, envelope)
            materialized = _materialize_runner_artifact_output(
                ctx, envelope, allowed_paths=allowed, stage_workflow=job["workflow"],
                label=label)
        attempt_label = label
        attempt_envelope = envelope
        try:
            max_repairs = int(job.get("publish_repair_attempts", 1) or 1)
        except (TypeError, ValueError):
            max_repairs = 1
        repair_feedback_history: list[str] = []
        for repair_number in range(1, max(0, max_repairs) + 1):
            if materialized or not job.get("repair_on_publish_fail"):
                break
            error = ctx.publish_errors.get(attempt_label, {
                "code": "publish-gate", "detail": "generated output failed validation",
            })
            feedback = str(error.get("detail", "")).strip()
            if feedback and feedback not in repair_feedback_history:
                repair_feedback_history.append(feedback)
            repair_label = f"{label}-repair-{repair_number}"
            ctx.emit("validator", f"{label}: bounded publish-gate repair {repair_number}/{max_repairs}")
            _mark_staging_repairing(ctx, attempt_label, job["workflow"])
            repair_context = json.loads(json.dumps(job["context"]))
            packet = repair_context.get("runner_context_packet")
            if isinstance(packet, dict):
                previous = _extract_runner_artifact_payload(_result_text(attempt_envelope)) or {}
                previous_artifacts = previous.get("artifacts", [])
                if job["workflow"] == "prep-linkedin":
                    repair_instruction = (
                        "Revise the supplied previous_generated_artifacts; do not restart from "
                        "the baseline. Return exactly one complete linkedin-review.md for this "
                        "group. Resolve every validator item. The score table must contain only "
                        "Headline, About, Experience entries, Skills, and Education; include an "
                        "Overall score line that explicitly says Experience x3; then include one "
                        "`### <Section>` proposal for each scored section with mandatory fenced "
                        "`Current` and fenced `Proposed` copyable blocks. `Proposed` must contain "
                        "only final LinkedIn content; keep advisory prose in separate Add, Change, "
                        "or Guidance blocks."
                    )
                elif job["workflow"] == "apply":
                    repair_instruction = (
                        "Revise the supplied previous_generated_artifacts and return exactly one "
                        "complete application-instructions.md at artifact_contract.exact_path. "
                        "Resolve every validator item; include every required heading, an explicit "
                        "no-submit boundary, route capture completeness/boundary, every captured "
                        "field with evidence-backed draft guidance, and an empty store_writes list."
                    )
                elif job["workflow"] == "prep-resume":
                    repair_instruction = (
                        "Revise the supplied previous_generated_artifacts; do not restart from "
                        "the baseline. Return the complete group set. Resolve every validator "
                        "item from every attempt simultaneously. Keep at most 16 experience "
                        "bullets and 360 experience-bullet words, selected at no more than 25%, "
                        "and requirement coverage at or above 80%. If coverage is missing while "
                        "already at the bullet budget, replace or retarget a lower-priority "
                        "bullet; never add a seventeenth. Keep one reasons entry for every "
                        "experience bullet and none for Education/Skills. Follow "
                        "artifact_contract exactly."
                    )
                else:
                    repair_instruction = (
                        "Revise the supplied previous_generated_artifacts; do not restart from "
                        "the baseline. Return the complete artifact set and resolve every "
                        "validator item while preserving earlier passing constraints."
                    )
                cumulative_feedback = "\n\n".join(
                    f"Attempt {index}:\n{item}"
                    for index, item in enumerate(repair_feedback_history, start=1)
                )
                packet["repair"] = {
                    "attempt": repair_number,
                    "error_code": error.get("code", "publish-gate"),
                    "validator_feedback": cumulative_feedback[-12000:],
                    "previous_generated_artifacts": (
                        previous_artifacts if isinstance(previous_artifacts, list) else []
                    ),
                    "instruction": repair_instruction,
                }
            try:
                repaired = _provider_run(
                    provider, job["workflow"], repair_context,
                    _labelled_stream(ctx, repair_label, on_stream),
                    job.get("repair_model_workflow", job.get("model_workflow")),
                )
                records.append({
                    "label": repair_label, "workflow": job["workflow"],
                    "model_workflow": job.get(
                        "repair_model_workflow", job.get("model_workflow", "")),
                    "status": "ok",
                })
                repair_events = repaired.get("events", [])
                if job["workflow"] in STAGED_PUBLISH_WORKFLOWS:
                    repair_events = [event for event in repair_events
                                     if not isinstance(event, dict)
                                     or event.get("type") not in {"artifact", "store_write"}]
                execute_events(ctx, repair_events, allowed_artifacts=allowed)
                _append_agent_result_log(ctx, repair_label, repaired)
                repaired = _merge_repair_artifact_envelope(attempt_envelope, repaired)
                materialized = _materialize_runner_artifact_output(
                    ctx, repaired, allowed_paths=allowed,
                    stage_workflow=job["workflow"], label=repair_label)
                _merge_usage(merged, repaired)
                attempt_label = repair_label
                attempt_envelope = repaired
            except RunCancelled:
                raise
            except Exception as exc:
                records.append({
                    "label": repair_label, "workflow": job["workflow"],
                    "model_workflow": job.get("model_workflow", ""),
                    "status": "failed", "error": str(exc)[:1000],
                })
                ctx.mark_partial(repair_label, str(exc))
                _append_agent_log(ctx, repair_label, f"ERROR: {exc}")
                ctx.emit("info", f"subagent failed: {repair_label}: {str(exc)[:300]}")
                status_path = _run_staging_dir(ctx, attempt_label) / "status.json"
                current_status = _read_json(status_path, {})
                _write_staging_status(
                    ctx, attempt_label, workflow=job["workflow"],
                    state="validation_failed",
                    artifacts=(current_status.get("artifacts", [])
                               if isinstance(current_status, dict) else []),
                    error={"code": "repair-provider-failed", "detail": str(exc)},
                )
                break
        if materialized:
            merged["published_labels"].append(label)
        elif job["workflow"] in {"prep-resume", "prep-linkedin"}:
            merged["validation_failed_labels"].append(label)
            final_error = (ctx.publish_errors.get(attempt_label) or
                           ctx.publish_errors.get(label) or {})
            code = str(final_error.get("code", "publish-gate"))
            ctx.mark_partial(label, f"{code}: generated output retained in run staging")
            ctx.emit("validator", f"{label}: NOT PUBLISHED [{code}]")
        for ev in envelope.get("events", []):
            if isinstance(ev, dict) and ev.get("type") == "result" and job["workflow"] != "score":
                ev.pop("content", None)
        _merge_usage(merged, envelope)
        ctx.emit("info", f"subagent done: {label}")
    _write_agent_manifest(ctx, records)
    return merged


def _run_search_orchestration(ctx: RunContext, provider, context_for,
                              stale_material: str, on_stream,
                              run_intent: dict | None = None) -> dict:
    """Runner-owned search lane: plan, shard capture, merge, probe, finalize."""
    envelope: dict = {"events": [], "usage": {}}
    lead = _execute_subagent(
        ctx, provider, "search",
        context_for("search", stale_material, {"search_phase": "plan"}),
        on_stream, label=_agent_label("search", 1, "plan"),
        model_workflow="search-plan")
    _merge_usage(envelope, lead)
    if ctx.failure_class or ctx.blocked_reasons:
        return envelope

    targeted = bool((run_intent or {}).get("requested_companies"))
    if targeted:
        ctx.emit("info", "targeted incremental search: limiting capture to "
                 + ", ".join((run_intent or {}).get("requested_companies", [])))
        plan_rc, plan_report = 0, None
    else:
        plan_rc, plan_report = _run_search_plan_gate(ctx, "post-plan", mark=False)
    if plan_rc == 2:
        repair = _execute_subagent(
            ctx, provider, "search",
            context_for("search", stale_material, {
                "search_phase": "plan_repair",
                "search_plan_gate": _plan_gate_text(plan_report or {}),
            }),
            on_stream, label=_agent_label("search", 1, "plan-repair"),
            model_workflow="search-plan")
        _merge_usage(envelope, repair)
        if ctx.failure_class or ctx.blocked_reasons:
            return envelope
        plan_rc, _ = _run_search_plan_gate(ctx, "post-plan-repair", mark=True)
    elif plan_rc != 0:
        return envelope
    if plan_rc not in (0, 2):
        return envelope

    shards = _search_capture_shards(ctx.project, run_intent=run_intent)
    if not shards:
        ctx.emit("info", "search source-plan missing or empty after lead phase; "
                 "falling back to one legacy search subagent")
        legacy = _execute_subagent(
            ctx, provider, "search",
            context_for("search", stale_material, {"search_phase": "legacy"}),
            on_stream)
        _merge_usage(envelope, legacy)
        return envelope

    parts_dir = ctx.project / "targets" / "research-log.parts"
    parts_dir.mkdir(parents=True, exist_ok=True)
    for stale_part in parts_dir.glob("runner-shard-*.json"):
        try:
            stale_part.unlink()
        except OSError:
            pass
    for stale_part in parts_dir.glob("search-*.json"):
        try:
            stale_part.unlink()
        except OSError:
            pass

    jobs = []
    for shard in shards:
        jobs.append({
            "label": shard["id"],
            "workflow": "search",
            "model_workflow": "search-capture-shard",
            "context": context_for("search", stale_material, {
                "search_phase": "capture_shard",
                "search_shard": shard,
                "search_part_path": shard["part_path"],
            }),
        })
    captured = _run_parallel_named_subagents(ctx, provider, jobs, on_stream)
    _merge_usage(envelope, captured)
    if ctx.failure_class or ctx.blocked_reasons:
        _salvage_search_results(
            ctx,
            "Search capture ended before normal finalize; saving valid captured parts.",
        )
        return envelope

    if not _run_search_merge(ctx, ""):
        ctx.failure_class = ctx.failure_class or "search_merge_failure"
        return envelope

    probe = core.run_script("probe_linkedin_jobs", str(ctx.project),
                            env=_env_for(ctx.project))
    probe_out = (probe.stdout + probe.stderr).strip()
    ctx.validator_results.append({
        "validator": "probe_linkedin_jobs",
        "target": ctx.project.name,
        "returncode": probe.returncode,
        "output": probe_out[:800],
    })
    ctx.emit("validator", "LinkedIn Jobs probe: "
             f"{'OK' if probe.returncode == 0 else 'RECORDED'}")
    if probe.returncode not in (0, 2):
        ctx.mark_partial("probe_linkedin_jobs", probe_out[:800] or
                         f"probe returned {probe.returncode}")
        ctx.emit("validator", probe_out[:800])

    if not _run_search_coverage_scaffold(ctx, ""):
        ctx.failure_class = ctx.failure_class or "coverage_audit_generation_failure"
        return envelope
    gate_rc, gate_report = _run_search_coverage_gate(ctx, "pre-finalize", mark=False)
    if gate_rc == 2:
        raw_retry_companies = [
            str(name) for name in (gate_report or {}).get("retry_companies", [])
            if str(name).strip()
        ]
        retry_companies = _bounded_retry_companies(raw_retry_companies)
        if len(retry_companies) < len(raw_retry_companies):
            ctx.mark_partial(
                "search_retry_capped",
                "Automatic retry limited to "
                f"{len(retry_companies)} of {len(raw_retry_companies)} companies. "
                "Remaining gaps are reported for an explicit follow-up run.",
            )
        repair_shards = _search_repair_shards(ctx.project, retry_companies)
        if repair_shards:
            for stale_part in parts_dir.glob("search-retry-*.json"):
                try:
                    stale_part.unlink()
                except OSError:
                    pass
            ctx.emit("info", "search coverage partial; retrying unresolved companies: "
                     + ", ".join(retry_companies[:8]))
            repair_jobs = []
            for shard in repair_shards:
                repair_jobs.append({
                    "label": shard["id"],
                    "workflow": "search",
                    "model_workflow": "search-capture-shard",
                    "context": context_for("search", stale_material, {
                        "search_phase": "capture_repair",
                        "search_shard": shard,
                        "search_part_path": shard["part_path"],
                        "search_coverage_gate": _search_gate_text(gate_report or {}),
                    }),
                })
            repaired = _run_parallel_named_subagents(
                ctx, provider, repair_jobs, on_stream,
                hard_fail_when_all_fail=False)
            _merge_usage(envelope, repaired)
            if repaired.get("failed_labels"):
                ctx.mark_partial(
                    "search_retry_failed",
                    "Retry subagent(s) failed: "
                    + ", ".join(str(label) for label in repaired["failed_labels"][:8])
                    + ". Saving valid rows captured before/around retry.",
                )
            if ctx.failure_class or ctx.blocked_reasons:
                _salvage_search_results(
                    ctx,
                    "Search retry ended before normal finalize; saving valid captured parts.",
                )
                return envelope
            if not _run_search_merge(ctx, "[repair]"):
                ctx.failure_class = ctx.failure_class or "search_merge_failure"
                return envelope
            if not _run_search_coverage_scaffold(ctx, "[repair]"):
                ctx.failure_class = ctx.failure_class or "coverage_audit_generation_failure"
                return envelope
            gate_rc, gate_report = _run_search_coverage_gate(ctx, "post-repair", mark=True)
        else:
            gate_rc, gate_report = _run_search_coverage_gate(ctx, "pre-finalize", mark=True)
    if gate_rc == 3:
        _salvage_search_results(
            ctx,
            "Search coverage remained blocked; saving valid captured rows before exit.",
        )
        return envelope
    if gate_rc not in (0, 2):
        _salvage_search_results(
            ctx,
            "Search coverage analysis did not complete; saving valid captured rows before exit.",
        )
        return envelope

    final = _execute_subagent(
        ctx, provider, "search",
        context_for("search", stale_material, {
            "search_phase": "finalize",
            "search_failed_shards": captured.get("failed_labels", []),
            "search_partial_reasons": ctx.partial_reasons,
        }),
        on_stream, label=_agent_label("search", 1, "finalize"),
        model_workflow="search-finalize")
    _merge_usage(envelope, final)
    if not ctx.failure_class and not ctx.blocked_reasons:
        coverage = core.run_script("generate_coverage_audit", str(ctx.project),
                                   env=_env_for(ctx.project))
        coverage_out = (coverage.stdout + coverage.stderr).strip()
        ctx.validator_results.append({
            "validator": "generate_coverage_audit[post-finalize]",
            "target": ctx.project.name,
            "returncode": coverage.returncode,
            "output": coverage_out[:800],
        })
        ctx.emit("validator", "generate_coverage_audit[post-finalize]: "
                 f"{'PASS' if coverage.returncode == 0 else 'FAIL'}")
        if coverage.returncode != 0:
            ctx.failure_class = ctx.failure_class or "coverage_audit_generation_failure"
            ctx.emit("validator", coverage_out[:800])
        else:
            _run_search_coverage_gate(ctx, "post-finalize")
    return envelope


def _record_run(ctx: RunContext, run_id: str, started: str, t0: float,
                envelope: dict | None = None) -> dict:
    envelope = envelope or {}
    latency = round(time.monotonic() - t0, 2)
    status = ctx.run_status()
    usage = envelope.get("usage", {})
    summary = ctx.summary
    if status == "partial" and ctx.partial_reasons:
        scopes = ", ".join(item["scope"] for item in ctx.partial_reasons[:5])
        if summary:
            summary = f"{summary}\n\nPartial completion: {scopes}"
        else:
            summary = f"Partial completion: {scopes}"
    rec = {
        "run_id": run_id, "started_at": started, "finished_at": _now(),
        "workflow": ctx.workflow, "mode": ctx.mode, "project": ctx.project.name,
        "model_config": envelope.get("model_config", {}),
        "cost_usd": usage.get("cost_usd", 0), "tokens_in": usage.get("tokens_in", 0),
        "tokens_out": usage.get("tokens_out", 0), "latency_s": latency,
        "input_bytes": usage.get("input_bytes", 0),
        "prompt_fingerprint": usage.get("prompt_fingerprint", ""),
        "data_classes": usage.get("data_classes", []),
        "validator_results": ctx.validator_results,
        "failure_class": ctx.failure_class, "status": status, "summary": summary,
        "events": envelope.get("events", []),
    }
    return rec


def _exit_code_for_status(status: str) -> int:
    return 0 if status in {"ok", "partial"} else 1


def _build_interview_context(ctx: RunContext,
                             scoped_job_ids: set[str] | None = None) -> None:
    result = core.run_script("build_interview_context", str(ctx.project),
                             env=_env_for(ctx.project))
    out = (result.stdout + result.stderr).strip()
    ctx.validator_results.append({
        "validator": "build_interview_context[prep-interview]",
        "target": ctx.project.name,
        "returncode": result.returncode,
        "output": out[:800],
    })
    ctx.emit("validator", "build_interview_context: "
             f"{'PASS' if result.returncode == 0 else 'FAIL'}")
    if result.returncode != 0:
        ctx.mark_partial("build_interview_context", out[:800])
        ctx.emit("validator", out[:800])
        return
    if scoped_job_ids is not None:
        path = ctx.project / "interviews" / "interview-context.json"
        data = _read_json(path, {})
        if isinstance(data, dict):
            data["prep_scope_job_ids"] = sorted(scoped_job_ids)
            _write_artifact(ctx, {
                "type": "artifact", "path": "interviews/interview-context.json",
                "json": data,
            })


def _load_interview_context_roles(project: Path) -> list[dict]:
    data = _read_json(project / "interviews" / "interview-context.json", {})
    roles = data.get("roles", []) if isinstance(data, dict) else []
    return [role for role in roles if isinstance(role, dict)]


def _interview_role_slug(role: dict) -> str:
    # Company + title is not a position identity: an employer can publish two
    # distinct requisitions with the same visible title. Keep the readable
    # prefix, but reserve the final 8 characters for the stable job identity so
    # per-position packs never overwrite one another.
    from ..interview_paths import role_slug
    return role_slug(role)


def _interview_expected_artifact(role: dict) -> str:
    return f"interviews/{_interview_role_slug(role)}/prep-notes.md"


def _interview_stage_artifact(role: dict, stage: str) -> str:
    return f"interviews/{_interview_role_slug(role)}/_stages/{stage}.md"


def _interview_validator_paths(output: str) -> set[str]:
    paths: set[str] = set()
    for match in re.finditer(r"interviews[\\/](.*?)[\\/]prep-notes\.md", str(output or "")):
        rel = PurePosixPath("interviews") / match.group(1).replace("\\", "/") / "prep-notes.md"
        paths.add(rel.as_posix())
    return paths


def _reuse_current_interview_packs(ctx: RunContext, roles: list[dict]) -> bool:
    """Reuse a complete same-day pack set when no grounding input is newer.

    Prep-interview performs expensive public research per role. A same-day retry
    after a UI disconnect, validator repair, or server restart should validate
    and reuse already-published packs instead of paying for all research again.
    """
    role_ids = [str(role.get("job_id", "")).strip() for role in roles]
    if not role_ids:
        return False
    outputs = [ctx.project / _interview_expected_artifact(role) for role in roles]
    if not all(path.is_file() for path in outputs):
        return False
    input_paths = [
        ctx.project / "interviews" / "interview-context.json",
        ctx.project / "interviews" / "story-bank.json",
        ctx.project / "data" / "focused-jobs.json",
    ]
    from . import preflight as _pf
    pdir = _pf.profile_dir(ctx.project)
    if pdir:
        input_paths.extend(
            pdir / name for name in (
                "candidate-profile.md", "evidence-map.md", "decision-policy.json",
                "standing-instructions.md",
            )
        )
    existing_inputs = [path for path in input_paths if path.is_file()]
    newest_input = max((path.stat().st_mtime for path in existing_inputs), default=0)
    oldest_output = min(path.stat().st_mtime for path in outputs)
    local_today = datetime.now().astimezone().date()
    if oldest_output < newest_input or datetime.fromtimestamp(oldest_output).date() != local_today:
        return False
    results = [
        core.run_script("validate_interview_prep", str(path), env=_env_for(ctx.project))
        for path in outputs
    ]
    out = "\n".join((result.stdout + result.stderr).strip() for result in results)
    returncode = max((result.returncode for result in results), default=1)
    ctx.validator_results.append({
        "validator": "validate_interview_prep[same-day-cache]",
        "target": ctx.project.name,
        "returncode": returncode,
        "output": out[:800],
    })
    ctx.emit("validator", "same-day interview pack validation: "
             f"{'PASS' if returncode == 0 else 'MISS'}")
    if returncode != 0:
        return False
    ctx.artifacts_written.extend(
        path for path in (_interview_expected_artifact(role) for role in roles)
        if path not in ctx.artifacts_written
    )
    ctx.summary = f"Reused {len(outputs)} current same-day interview pack(s)."
    ctx.emit("result", ctx.summary)
    return True


def _extract_h2_section(markdown: str, section: str) -> str:
    pattern = re.compile(rf"^##\s+{re.escape(section)}\s*$", re.I | re.M)
    match = pattern.search(markdown or "")
    if not match:
        return ""
    next_match = re.search(r"^##\s+.+?\s*$", markdown[match.end():], re.M)
    end = match.end() + next_match.start() if next_match else len(markdown)
    return markdown[match.start():end].strip()


def _placeholder_section(section: str, reason: str) -> str:
    return (
        f"## {section}\n\n"
        "| Item | Status |\n"
        "|---|---|\n"
        f"| Pending | {reason} |\n"
    )


def _assemble_interview_prep(ctx: RunContext, role: dict) -> bool:
    slug = _interview_role_slug(role)
    stage_dir = ctx.project / "interviews" / slug / "_stages"
    stage_text: dict[str, str] = {}
    for stage in INTERVIEW_STAGE_SECTIONS:
        path = stage_dir / f"{stage}.md"
        try:
            stage_text[stage] = path.read_text(encoding="utf-8")
        except OSError:
            stage_text[stage] = ""
    ordered = [
        ("qa", "Self Introduction"),
        ("qa", "Job Requirements"),
        ("qa", "Adversarial Questions"),
        ("whys", "The Whys"),
        ("qa", "Behavioral Questions"),
        ("company-research", "Glossary"),
        ("company-research", "News"),
        ("qa", "Questions to Ask"),
        ("company-research", "Sources"),
    ]
    sections: list[str] = []
    missing_sections: list[str] = []
    for stage, section in ordered:
        body = _extract_h2_section(stage_text.get(stage, ""), section)
        if not body:
            missing_sections.append(f"{section} ({stage})")
            continue
        sections.append(body)
    if missing_sections:
        detail = "missing interview stage section(s): " + ", ".join(missing_sections)
        ctx.mark_partial("prep-interview_publish_gate", detail)
        ctx.emit("validator", "prep-interview publish gate: FAIL")
        if ctx.workflow == "prep-interview":
            ctx.failure_class = ctx.failure_class or "prep_interview_stage_incomplete"
        return False
    header = (
        f"> Position: {role.get('company', '')} - {role.get('title', '')}\n"
        f"> Job ID: {role.get('job_id', '')}\n"
        f"> Source: {role.get('source_url', '')}\n\n"
    )
    _write_artifact(ctx, {
        "type": "artifact",
        "path": _interview_expected_artifact(role),
        "text": header + "\n\n".join(sections).strip() + "\n",
    })
    return True


def _validate_story_bank(ctx: RunContext) -> bool:
    path = ctx.project / "interviews" / "story-bank.json"
    data = _read_json(path, {})
    entries = data.get("entries") if isinstance(data, dict) else None
    required = {"id", "title", "source", "situation", "task", "action", "result",
                "best_for", "ev_refs"}
    errors: list[str] = []
    if not isinstance(entries, list) or not entries:
        errors.append("story-bank.json entries must be a non-empty list")
    else:
        seen_ids: set[str] = set()
        for i, entry in enumerate(entries):
            if not isinstance(entry, dict):
                errors.append(f"entry {i} must be an object")
                continue
            missing = sorted(required - set(entry))
            if missing:
                errors.append(f"entry {i} missing {', '.join(missing)}")
                continue
            story_id = str(entry.get("id", ""))
            if not re.fullmatch(r"ST-\d{2,}", story_id):
                errors.append(f"entry {i} has invalid story ID {story_id!r}")
            elif story_id in seen_ids:
                errors.append(f"entry {i} duplicates story ID {story_id}")
            seen_ids.add(story_id)
            for key in required - {"ev_refs"}:
                if not str(entry.get(key, "")).strip():
                    errors.append(f"entry {i} has empty {key}")
            if not entry.get("ev_refs"):
                errors.append(f"entry {i} has empty ev_refs")
    ctx.validator_results.append({
        "validator": "validate_story_bank",
        "target": str(path),
        "returncode": 0 if not errors else 1,
        "output": "; ".join(errors)[:800] if errors else f"PASS: {len(entries or [])} story entries",
    })
    ctx.emit("validator", "validate_story_bank: " + ("PASS" if not errors else "FAIL"))
    if errors:
        detail = "; ".join(errors)[:800]
        ctx.emit("validator", detail)
        ctx.emit(
            "attention",
            "Interview prep needs a valid story bank. Run `rolenavi run story-bank` "
            "first, wait for it to complete, then rerun prep-interview.",
        )
        ctx.emit("error", f"prep-interview blocked by invalid story bank: {detail}")
        ctx.summary = (
            "Interview prep could not start because the story bank is invalid. "
            "Run story-bank first, then retry prep-interview."
        )
        ctx.failure_class = ctx.failure_class or "story_bank_validation_failure"
        return False
    return True


def _story_bank_needs_refresh(project: Path, pdir: Path | None) -> bool:
    story = project / "interviews" / "story-bank.json"
    if not story.exists():
        return True
    if pdir is None:
        return False
    try:
        story_mtime = story.stat().st_mtime
    except OSError:
        return True
    for pattern in ("*.pdf", "*.docx", "*.doc", "*.md", "*.txt"):
        for path in pdir.glob(pattern):
            if path.name in {"candidate-profile.md", "evidence-map.md", "linkedin-current.md",
                             "linkedin-analysis.md"}:
                continue
            try:
                if path.stat().st_mtime > story_mtime:
                    return True
            except OSError:
                continue
    return False


def _run_story_bank_workflow(ctx: RunContext, provider, context: dict, on_stream) -> dict:
    envelope = _execute_subagent(
        ctx, provider, "story-bank", context, on_stream, model_workflow="story-bank")
    if not ctx.failure_class and not ctx.blocked_reasons:
        _validate_story_bank(ctx)
    return envelope


def _run_strategy_workflow(ctx: RunContext, provider, context: dict, on_stream) -> dict:
    strategy_rows = _strategy_focused_job_rows(ctx.project)
    _prepare_processed_jds(ctx, strategy_rows)
    total_focused = len(_focused_job_rows(ctx.project))
    ctx.emit(
        "info",
        f"prep-strategy scope: {len(strategy_rows)}/{total_focused} focused role(s) "
        "have current scores; unscored/stale roles are excluded",
    )
    envelope = _execute_subagent(
        ctx, provider, "prep-strategy", context, on_stream,
        model_workflow="prep-strategy")
    if (ctx.failure_class == "prep_strategy_publish_gate_failure"
            and not ctx.blocked_reasons):
        feedback = next((item.get("reason", "") for item in reversed(ctx.partial_reasons)
                         if item.get("scope") == "prep-strategy_publish_gate"), "")
        if feedback:
            ctx.emit("validator", "prep-strategy publish gate: bounded repair retry")
            ctx.failure_class = ""
            ctx.partial_reasons = [
                item for item in ctx.partial_reasons
                if item.get("scope") != "prep-strategy_publish_gate"
            ]
            repair_context = json.loads(json.dumps(context))
            packet = repair_context.get("runner_context_packet")
            if isinstance(packet, dict):
                packet["strategy_repair"] = {
                    "validator_feedback": feedback[:2000],
                    "instruction": (
                        "Regenerate the complete artifact set. Consolidate compatible roles "
                        "into a small number of positioning/resume groups; do not drop jobs."
                    ),
                }
            retry = _execute_subagent(
                ctx, provider, "prep-strategy", repair_context, on_stream,
                label="prep-strategy-repair", model_workflow="prep-strategy")
            _merge_usage(envelope, retry)
    return envelope


def _run_resume_workflow(ctx: RunContext, provider, context_for,
                         stale_material: str, on_stream,
                         pdir: Path | None = None, *, aggregate: bool = False) -> dict:
    if not _ensure_baseline_extracted(ctx, pdir):
        return {"events": [], "usage": {}}
    _prepare_processed_jds(ctx)
    if aggregate:
        groups, parked_groups = _groups_for_prep(ctx.project, {"pursue", "conditional"})
    else:
        groups, parked_groups = _focused_groups(ctx.project), []
    if not groups and not parked_groups:
        ctx.failure_class = ctx.failure_class or "prep_resume_no_groups"
        ctx.emit("validator", "prep-resume group scope: FAIL (no focused groups)")
        return {"events": [], "usage": {}}
    jobs: list[dict] = []
    for index, group in enumerate(groups, start=1):
        slug = str(group.get("slug") or "ungrouped")
        packet_stub = {"group_slug": slug, "jobs": group.get("jobs", [])}
        extra = {"resume_group_packet": packet_stub}
        jobs.append({
            "workflow": "prep-resume",
            "model_workflow": "prep-resume",
            "label": f"prep-resume-{index:03d}-{slug}",
            "context": context_for("prep-resume", stale_material, extra),
            "allowed_artifacts": _resume_group_allowed_artifacts(slug),
            "repair_on_publish_fail": True,
            "publish_repair_attempts": 2,
            "repair_model_workflow": "prep-resume-repair",
        })
    for group in parked_groups:
        slug = str(group.get("slug") or "ungrouped")
        reasons = _strategy_dispositions(ctx.project)
        detail = next((reasons.get(str(row.get("job_id", "")), {}).get("reason", "")
                       for row in group.get("jobs", []) if reasons.get(
                           str(row.get("job_id", "")), {}).get("reason")),
                      "Strategy marked every role in this group as parked.")
        _write_artifact(ctx, {
            "type": "artifact",
            "path": f"resumes/{slug}/resume-not-generated.md",
            "text": (f"# Resume not generated — {slug}\n\n{detail}\n\n"
                     "Run `prep-resume` explicitly after changing focus or recording an override.\n"),
        })
        ctx.emit("artifact", f"parked group skipped without an LLM call: resumes/{slug}/resume-not-generated.md")
    return _run_parallel_named_subagents(
        ctx, provider, jobs, on_stream, hard_fail_when_all_fail=False)


def _run_linkedin_workflow(ctx: RunContext, provider, context_for,
                           stale_material: str, on_stream, *, aggregate: bool = False) -> dict:
    _prepare_processed_jds(ctx)
    groups = (_groups_for_prep(ctx.project, {"pursue", "conditional"})[0]
              if aggregate else _focused_groups(ctx.project))
    if not groups and aggregate:
        ctx.emit("info", "prep-linkedin skipped: strategy has no pursue/conditional groups")
        return {"events": [], "usage": {}, "published_labels": [],
                "validation_failed_labels": []}
    if not groups:
        ctx.failure_class = ctx.failure_class or "prep_linkedin_no_groups"
        ctx.emit("validator", "prep-linkedin group scope: FAIL (no focused groups)")
        return {"events": [], "usage": {}}
    jobs: list[dict] = []
    for index, group in enumerate(groups, start=1):
        slug = str(group.get("slug") or "ungrouped")
        extra = {"linkedin_group_packet": {"group_slug": slug, "jobs": group.get("jobs", [])}}
        expected = _linkedin_group_artifact(slug)
        jobs.append({
            "workflow": "prep-linkedin",
            "model_workflow": "prep-linkedin",
            "label": f"prep-linkedin-{index:03d}-{slug}",
            "context": context_for("prep-linkedin", stale_material, extra),
            "allowed_artifacts": {expected},
            "repair_on_publish_fail": True,
            "publish_repair_attempts": 2,
            "repair_model_workflow": "prep-linkedin-repair",
        })
    return _run_parallel_named_subagents(
        ctx, provider, jobs, on_stream, hard_fail_when_all_fail=False)


def _run_interview_workflow(ctx: RunContext, provider, context_for,
                            stale_material: str, on_stream, *, aggregate: bool = False) -> dict:
    envelope: dict = {"events": [], "usage": {}}
    story_bank = ctx.project / "interviews" / "story-bank.json"
    if not story_bank.exists():
        ctx.failure_class = ctx.failure_class or "story_bank_missing"
        ctx.emit(
            "attention",
            "Interview prep needs a story bank. Run `rolenavi run story-bank` first, "
            "wait for it to complete, then rerun prep-interview.",
        )
        ctx.emit("error", "prep-interview blocked: interviews/story-bank.json is missing")
        ctx.summary = (
            "Interview prep could not start because the story bank is missing. "
            "Run story-bank first, then retry prep-interview."
        )
        return envelope
    if not _validate_story_bank(ctx):
        return envelope
    roles = _load_interview_context_roles(ctx.project)
    if aggregate:
        dispositions = _strategy_dispositions(ctx.project)
        roles = [role for role in roles if dispositions.get(
            str(role.get("job_id", "")), {"disposition": "pursue"}
        )["disposition"] == "pursue"]
    if roles and _reuse_current_interview_packs(ctx, roles):
        return envelope
    _prepare_processed_jds(ctx)
    scoped_job_ids = None
    if aggregate:
        dispositions = _strategy_dispositions(ctx.project)
        scoped_job_ids = {
            job_id for job_id, item in dispositions.items()
            if item.get("disposition") == "pursue"
        }
    _build_interview_context(ctx, scoped_job_ids=scoped_job_ids)
    if ctx.failure_class or ctx.blocked_reasons:
        return envelope
    roles = _load_interview_context_roles(ctx.project)
    if aggregate:
        dispositions = _strategy_dispositions(ctx.project)
        roles = [role for role in roles if dispositions.get(
            str(role.get("job_id", "")), {"disposition": "pursue"}
        )["disposition"] == "pursue"]
    if not roles:
        if aggregate:
            ctx.emit("info", "prep-interview skipped: strategy has no pursue roles")
            return envelope
        ctx.failure_class = ctx.failure_class or "prep_interview_no_roles"
        ctx.emit("validator", "prep-interview role context: FAIL (no focused roles)")
        return envelope

    def _run_role_packets(quality_retry: str | None = None,
                          only_expected: set[str] | None = None,
                          retry_all_stages: bool = False) -> None:
        for index, role in enumerate(roles, start=1):
            expected = _interview_expected_artifact(role)
            if only_expected is not None and expected not in only_expected:
                continue
            before = set(ctx.artifacts_written)
            stages = (["company-research", "whys", "qa"]
                      if retry_all_stages or not quality_retry else ["whys"])
            for stage in stages:
                stage_expected = _interview_stage_artifact(role, stage)
                label_prefix = "prep-interview-retry" if quality_retry else "prep-interview"
                label = f"{label_prefix}-{index:03d}-{_interview_role_slug(role)}-{stage}"
                extra = {
                    "interview_stage": stage,
                    "interview_role_packet": {
                        "index": index,
                        "total": len(roles),
                        "role": role,
                        "expected_artifact": stage_expected,
                        "final_artifact": expected,
                    }
                }
                if quality_retry:
                    extra["prep_interview_quality_retry"] = quality_retry[:4000]
                role_context = context_for("prep-interview", stale_material, extra)
                stage_before = set(ctx.artifacts_written)
                child = _execute_subagent(
                    ctx, provider, "prep-interview", role_context, on_stream,
                    label=label, model_workflow="prep-interview",
                    allowed_artifacts={stage_expected})
                _merge_usage(envelope, child)
                if stage_expected not in set(ctx.artifacts_written) - stage_before and stage_expected not in ctx.artifacts_written:
                    retry_extra = dict(extra)
                    retry_extra["prep_interview_artifact_retry"] = (
                        f"The previous output did not produce the required artifact path "
                        f"{stage_expected}. Return exactly one artifact at that path and no other path."
                    )
                    retry_context = context_for("prep-interview", stale_material, retry_extra)
                    retry_child = _execute_subagent(
                        ctx, provider, "prep-interview", retry_context, on_stream,
                        label=f"{label}-path-retry", model_workflow="prep-interview",
                        allowed_artifacts={stage_expected})
                    _merge_usage(envelope, retry_child)
            if not _assemble_interview_prep(ctx, role):
                continue
            if expected not in set(ctx.artifacts_written) - before and expected not in ctx.artifacts_written:
                ctx.mark_partial(
                    "prep-interview_artifact_missing",
                    f"{expected} was not generated for "
                    f"{role.get('company')} - {role.get('title')}",
                )
                if ctx.workflow == "prep-interview":
                    ctx.failure_class = ctx.failure_class or "prep_interview_artifact_missing"

    _run_role_packets()
    if not ctx.failure_class and not ctx.blocked_reasons:
        result = core.run_script("validate_interview_prep", str(ctx.project),
                                 env=_env_for(ctx.project))
        out = (result.stdout + result.stderr).strip()
        ctx.validator_results.append({
            "validator": "validate_interview_prep[prep-interview]",
            "target": ctx.project.name,
            "returncode": result.returncode,
            "output": out[:800],
        })
        ctx.emit("validator", "post-run validate_interview_prep: "
                 f"{'PASS' if result.returncode == 0 else 'QUALITY' if result.returncode == 2 else 'FAIL'}")
        if result.returncode != 0:
            retry_kind = "quality" if result.returncode == 2 else "validation"
            ctx.emit("validator", f"prep-interview {retry_kind} retry: "
                     f"{'QUALITY' if result.returncode == 2 else 'FAIL'}")
            retry_paths = _interview_validator_paths(out)
            _run_role_packets(
                out,
                only_expected=retry_paths or None,
                retry_all_stages=result.returncode != 2,
            )
            retry = core.run_script("validate_interview_prep", str(ctx.project),
                                    env=_env_for(ctx.project))
            retry_out = (retry.stdout + retry.stderr).strip()
            ctx.validator_results.append({
                "validator": "validate_interview_prep[prep-interview-quality-retry]",
                "target": ctx.project.name,
                "returncode": retry.returncode,
                "output": retry_out[:800],
            })
            ctx.emit("validator", "post-run validate_interview_prep retry: "
                     f"{'PASS' if retry.returncode == 0 else 'QUALITY' if retry.returncode == 2 else 'FAIL'}")
            if retry.returncode == 2:
                ctx.mark_partial("prep-interview_quality", retry_out[:800])
                ctx.emit("validator", retry_out[:800])
            elif retry.returncode != 0:
                ctx.failure_class = ctx.failure_class or "prep-interview_validation_failure"
                ctx.emit("validator", retry_out[:800])
    return envelope


def _run_prep_orchestration(ctx: RunContext, provider, context_for, stale_material: str,
                            on_stream, pdir: Path | None,
                            linkedin_source: Path | None) -> dict:
    """Full prep is an orchestrator, not a monolithic profile-building agent.

    Strategy is the only global dependency. Downstream phases publish
    independently: a failed resume group does not suppress LinkedIn, the last
    valid story bank can support interview, and the overall run becomes partial
    when only a subset publishes.
    """
    envelope: dict = {"events": [], "usage": {}}

    _update_prep_progress(ctx, "strategy", "running")
    strategy = _run_strategy_workflow(
        ctx, provider, context_for("prep-strategy", stale_material), on_stream)
    _merge_usage(envelope, strategy)
    if ctx.failure_class or ctx.blocked_reasons:
        _update_prep_progress(ctx, "strategy", "failed",
                              detail=ctx.failure_class or "blocked")
        return envelope
    _update_prep_progress(ctx, "strategy", "published")

    _update_prep_progress(ctx, "resume", "running")
    resume = _run_resume_workflow(
        ctx, provider, context_for, stale_material, on_stream, pdir, aggregate=True)
    _merge_usage(envelope, resume)
    if ctx.blocked_reasons:
        _update_prep_progress(ctx, "resume", "blocked")
        return envelope
    if ctx.failure_class:
        reason = ctx.failure_class
        ctx.failure_class = ""
        ctx.mark_partial("prep-resume", reason)
        ctx.emit("info", f"prep-resume incomplete ({reason}); continuing independent phases")
    resume_failed = len(resume.get("validation_failed_labels", []))
    _update_prep_progress(
        ctx, "resume", "partial" if resume_failed else "published",
        completed=len(resume.get("published_labels", [])),
        total=(len(resume.get("published_labels", [])) + resume_failed),
        detail=(f"{resume_failed} group(s) retained in staging" if resume_failed else ""),
    )

    from .. import profile_meta
    linkedin_url = profile_meta.linkedin_url(pdir)
    if (linkedin_url and linkedin_source and linkedin_source.exists()
            and linkedin_source.stat().st_size > 0):
        _update_prep_progress(ctx, "linkedin", "running")
        linkedin = _run_linkedin_workflow(
            ctx, provider, context_for, stale_material, on_stream, aggregate=True)
        _merge_usage(envelope, linkedin)
        linkedin_failed = len(linkedin.get("validation_failed_labels", []))
        _update_prep_progress(
            ctx, "linkedin", "partial" if linkedin_failed else "published",
            completed=len(linkedin.get("published_labels", [])),
            total=(len(linkedin.get("published_labels", [])) + linkedin_failed),
            detail=(f"{linkedin_failed} group(s) retained in staging" if linkedin_failed else ""),
        )
    elif linkedin_url:
        ctx.emit("attention", "prep-linkedin skipped: the saved LinkedIn URL has no "
                 "captured profile yet. Open Profile and choose Resync LinkedIn "
                 "profile, then rerun prep-linkedin.")
        _update_prep_progress(ctx, "linkedin", "skipped", detail="profile capture unavailable")
    else:
        ctx.emit("info", "warning: no LinkedIn URL/current profile; skipping "
                 "prep-linkedin. Add the URL in the profile form and complete "
                 "LinkedIn import/capture before LinkedIn review.")
        _update_prep_progress(ctx, "linkedin", "skipped", detail="no LinkedIn profile")

    if ctx.blocked_reasons:
        return envelope
    if ctx.failure_class:
        reason = ctx.failure_class
        ctx.failure_class = ""
        ctx.mark_partial("prep-linkedin", reason)
        ctx.emit("info", f"prep-linkedin incomplete ({reason}); continuing independent phases")

    if _story_bank_needs_refresh(ctx.project, pdir):
        _update_prep_progress(ctx, "story-bank", "running")
        story = _run_story_bank_workflow(
            ctx, provider, context_for("story-bank", stale_material), on_stream)
        _merge_usage(envelope, story)
        if ctx.blocked_reasons:
            return envelope
        if ctx.failure_class:
            reason = ctx.failure_class
            ctx.failure_class = ""
            ctx.mark_partial("story-bank", reason)
            ctx.emit("info", f"story-bank refresh incomplete ({reason}); trying last valid bank")
            _update_prep_progress(ctx, "story-bank", "partial", detail=reason)
        else:
            _update_prep_progress(ctx, "story-bank", "published")

    if not (ctx.project / "interviews" / "story-bank.json").exists():
        ctx.mark_partial("prep-interview", "story bank unavailable; interview phase skipped")
        ctx.emit("info", "prep-interview skipped: no valid story bank is available")
        _update_prep_progress(ctx, "interview", "skipped", detail="story bank unavailable")
        _update_prep_progress(ctx, "complete", "partial")
        return envelope
    _update_prep_progress(ctx, "interview", "running")
    interview = _run_interview_workflow(
        ctx, provider, context_for, stale_material, on_stream, aggregate=True)
    _merge_usage(envelope, interview)
    if ctx.failure_class and (ctx.publish_results or ctx.artifacts_written):
        reason = ctx.failure_class
        ctx.failure_class = ""
        ctx.mark_partial("prep-interview", reason)
        ctx.emit("info", f"prep-interview incomplete ({reason}); earlier published artifacts retained")
        _update_prep_progress(ctx, "interview", "partial", detail=reason)
    else:
        _update_prep_progress(ctx, "interview", "published")
    _update_prep_progress(
        ctx, "complete", "partial" if ctx.partial_reasons else "published"
    )
    return envelope


def run_profile_intake(person: str, project: Path | None = None, task: str | None = None,
                       force_mock: bool = False, max_turns: int = 40,
                       telemetry_path: Path | None = None, on_event=None,
                       cancel_event=None, capture_linkedin: bool = True,
                       skip_if_unchanged: bool = False) -> dict:
    """Person-scoped profile/evidence intake.

    This lane is intentionally independent of project creation, job search, and
    focused jobs. It may be triggered by profile save, resume upload, or an
    explicit CLI run.
    """
    from .. import profile_meta
    from ..privacy.disclosure import disclosure_lines
    from ..privacy.source_extract import profile_source_packet
    from . import preflight as _pf

    person = str(person or "").strip()
    if not person:
        raise RoleNaviError("profile-intake requires a person code")
    pdir = repo_root() / "profiles" / person
    if not pdir.is_dir():
        raise RoleNaviError(f"person '{person}' has no profile folder")

    mode = llm.mode(force_mock)
    ctx = RunContext(PROFILE_WORKFLOW, pdir, mode)
    ctx.on_event = on_event
    ctx.cancel_event = cancel_event
    run_id = tstore.new_run_id()
    ctx.run_id = run_id
    started = _now()
    t0 = time.monotonic()
    if mode == "live":
        blocking, warnings = _pf.check(PROFILE_WORKFLOW, pdir)
        for w in warnings:
            ctx.emit("info", f"preflight WARN: {w}")
        if blocking:
            for item in blocking:
                ctx.mark_blocked("preflight", item)
            ctx.summary = "Blocked by preflight: " + "; ".join(blocking)
            ctx.emit("error", "not ready for a live run:\n  - " +
                     "\n  - ".join(blocking))
            rec = _record_run(ctx, run_id, started, t0, {"events": []})
            tstore.record_run(rec, path=telemetry_path)
            ctx.emit("info", f"telemetry: run {rec['run_id']} recorded "
                     f"({len(ctx.validator_results)} validator result(s), "
                     f"status={rec['status']})")
            return rec

    provider = llm.get_provider(force_mock)
    ctx.emit("info", f"run {run_id}: workflow={PROFILE_WORKFLOW} "
             f"mode={mode} person={person} backend={provider.name}")
    if mode == "live":
        for line in disclosure_lines(PROFILE_WORKFLOW, provider.name):
            ctx.emit("info", line)
    linkedin_source = pdir / "linkedin-current.md"

    def _on_stream(text: str) -> None:
        ctx.check_cancelled()
        ctx.emit("stream", text)

    def _profile_context(active_workflow: str, stale_material: str) -> dict:
        return {
            "project": str(pdir),
            "task": task,
            "profile_stale": stale_material if active_workflow == PROFILE_WORKFLOW else "",
            "profile_ready": _profile_ready(pdir),
            "profile_source_packet": profile_source_packet(pdir),
        }

    envelope: dict = {"events": [], "usage": {}}
    ran_model = False
    try:
        linkedin_url = profile_meta.linkedin_url(pdir)
        if mode == "live" and linkedin_url and capture_linkedin:
            pending_linkedin = pdir / ".linkedin-current.pending.md"
            pending_linkedin.unlink(missing_ok=True)
            rc = _run_linkedin_capture_helper(
                ctx, linkedin_url, pending_linkedin, required=True
            )
            if rc == 0 and pending_linkedin.exists() and pending_linkedin.stat().st_size > 0:
                os.replace(pending_linkedin, linkedin_source)
                ctx.emit("artifact", "linkedin-current.md updated from explicit capture")
            else:
                pending_linkedin.unlink(missing_ok=True)
        if not ctx.failure_class and not ctx.blocked_reasons:
            if skip_if_unchanged and profile_meta.profile_is_current(pdir):
                ctx.summary = "LinkedIn resynced; captured profile content is unchanged, so no model call was needed."
                ctx.emit("result", ctx.summary)
            else:
                stale_material = _pf._stale_profile(pdir)
                envelope = _execute_subagent(
                    ctx, provider, PROFILE_WORKFLOW,
                    _profile_context(PROFILE_WORKFLOW, stale_material), _on_stream,
                    allowed_artifacts={
                        "candidate-profile.md", "evidence-map.md", "capability-ledger.json"
                    })
                ran_model = True
        if (project is not None and not ctx.failure_class
                and _profile_ready(pdir) and _story_bank_needs_refresh(project, pdir)):
            ctx.emit("info", "candidate profile/evidence map ready; refreshing story bank")
            story_ctx = RunContext("story-bank", project, mode)
            story_ctx.on_event = on_event
            story_ctx.cancel_event = cancel_event
            story_ctx.run_id = run_id

            def _story_context() -> dict:
                from .. import project_meta
                return {
                    "project": str(project),
                    "task": task,
                    "skills": WORKFLOW_SKILLS["story-bank"],
                    "max_turns": max_turns,
                    "focused_jobs": None,
                    "profile_stale": "",
                    "profile_ready": _profile_ready(pdir),
                    "linkedin_url": profile_meta.linkedin_url(pdir),
                    "profile_dir": str(pdir),
                    "linkedin_source_path": str(linkedin_source),
                    "targets": project_meta.targets_text(project),
                    "instructions": profile_meta.instructions(pdir),
                    "runner_context_packet": _runner_context_packet(project, pdir, "story-bank"),
                }

            story_envelope = _run_story_bank_workflow(
                story_ctx, provider, _story_context(), _on_stream)
            ctx.validator_results.extend(story_ctx.validator_results)
            ctx.artifacts_written.extend(story_ctx.artifacts_written)
            if story_ctx.failure_class:
                ctx.mark_partial("story-bank", story_ctx.failure_class)
            if story_ctx.summary and not ctx.summary:
                ctx.summary = story_ctx.summary
            _merge_usage(envelope, story_envelope)
        if (project is not None and not ctx.failure_class
                and _profile_ready(pdir) and _ratings_need_profile_refresh(project)):
            ctx.emit("info", "candidate profile/evidence map ready; rerunning score "
                     "to replace provisional title/JD-only fit rationales")
            score_ctx = RunContext("score", project, mode)
            score_ctx.on_event = on_event
            score_ctx.cancel_event = cancel_event

            def _score_context(active_workflow: str, stale: str) -> dict:
                from .. import project_meta
                return {
                    "project": str(project), "task": task,
                    "skills": WORKFLOW_SKILLS[active_workflow], "max_turns": max_turns,
                    "focused_jobs": None,
                    "profile_stale": stale,
                    "profile_ready": _profile_ready(pdir),
                    "linkedin_url": profile_meta.linkedin_url(pdir),
                    "profile_dir": str(pdir),
                    "linkedin_source_path": str(linkedin_source),
                    "targets": project_meta.targets_text(project),
                    "instructions": profile_meta.instructions(pdir),
                }

            score_envelope = _run_score_workflow(
                score_ctx, provider, _score_context("score", ""), _on_stream)
            ctx.validator_results.extend(score_ctx.validator_results)
            if score_ctx.failure_class:
                ctx.failure_class = ctx.failure_class or score_ctx.failure_class
            if score_ctx.summary:
                ctx.summary = score_ctx.summary
            _merge_usage(envelope, score_envelope)
        if ran_model and not ctx.failure_class and not ctx.blocked_reasons and _profile_ready(pdir):
            fingerprint = profile_meta.mark_profile_built(pdir)
            ctx.emit("artifact", f"profile source fingerprint committed: {fingerprint[:12]}")
        _post_run_checks(ctx)
    except RunCancelled:
        ctx.failure_class = "cancelled"
        if not ctx.summary:
            ctx.summary = "Run cancelled by user."
        ctx.emit("info", "run cancelled — the agent was stopped")
    except Exception as e:
        _mark_exception(ctx, PROFILE_WORKFLOW, e)
    rec = _record_run(ctx, run_id, started, t0, envelope)
    tstore.record_run(rec, path=telemetry_path)
    ctx.emit("info", f"telemetry: run {rec['run_id']} recorded "
             f"({len(ctx.validator_results)} validator result(s), status={rec['status']})")
    return rec


def run_workflow(workflow: str, project: Path | None = None, task: str | None = None,
                 force_mock: bool = False, max_turns: int = 40,
                 telemetry_path: Path | None = None, on_event=None,
                 cancel_event=None) -> dict:
    """Programmatic entry (CLI and web). Returns the telemetry record.

    on_event(kind, text, extra)   — mirrors everything printed (web UI event feed).
    cancel_event (threading.Event)— cooperative stop: the run halts at the next
                                    safe checkpoint and is recorded as 'cancelled'."""
    if workflow == PROFILE_WORKFLOW:
        person = task or ""
        if not person and project is not None:
            from . import preflight as _pf
            pdir = _pf.profile_dir(project)
            person = pdir.name if pdir is not None else ""
        return run_profile_intake(person, project=project, task=task,
                                  force_mock=force_mock,
                                  max_turns=max_turns,
                                  telemetry_path=telemetry_path,
                                  on_event=on_event,
                                  cancel_event=cancel_event)

    deterministic_search = (
        workflow == "search"
        and not force_mock
        and os.environ.get("LLM_MOCK") != "1"
        and not _legacy_llm_search_enabled()
    )
    mode = "live" if deterministic_search else llm.mode(force_mock)
    run_id = tstore.new_run_id()
    if mode == "mock" and project is None:
        project = make_mock_project(run_id)
        print(f"mock mode: disposable fixture project at {project}")
    elif project is None:
        project = active_project_dir()
        if project is None:
            raise RoleNaviError("no active project — run `rolenavi init` first")

    ctx = RunContext(workflow, project, mode)
    ctx.on_event = on_event
    ctx.cancel_event = cancel_event
    started = _now()
    t0 = time.monotonic()
    ctx.run_id = run_id
    if mode == "live":
        from . import preflight
        blocking, warnings = preflight.check(workflow, project)
        repair = preflight.profile_repair_candidate(workflow, project)
        if repair:
            ctx.emit(
                "attention",
                f"{repair['reason']}; running profile-intake once before retrying {workflow}.",
            )
            repair_rec = run_profile_intake(
                repair["person"],
                task=f"Auto-repair profile prerequisites before {workflow}.",
                force_mock=force_mock,
                max_turns=max_turns,
                telemetry_path=telemetry_path,
                on_event=on_event,
                cancel_event=cancel_event,
            )
            repair_ok = repair_rec.get("status") == "ok"
            ctx.validator_results.append({
                "validator": "profile_intake_auto_repair",
                "target": repair["person"],
                "returncode": 0 if repair_ok else 1,
                "output": str(repair_rec.get("summary", ""))[:800],
            })
            if repair_ok:
                ctx.emit("result", "Profile intake completed; retrying prep preflight now.")
            else:
                ctx.emit(
                    "error",
                    "Automatic profile intake did not complete successfully; "
                    "prep will remain blocked rather than using ungrounded evidence.",
                )
            blocking, warnings = preflight.check(workflow, project)
        for w in warnings:
            ctx.emit("info", f"preflight WARN: {w}")
        if blocking:
            if (workflow in FOCUS_SCOPED
                    and preflight.focused_job_count(project) == 0):
                ctx.emit(
                    "attention",
                    "Select focused roles first — open Jobs and star at least one role "
                    "before running prep.",
                )
            if (workflow == "prep-interview"
                    and any("story bank" in item.lower() for item in blocking)):
                ctx.emit(
                    "attention",
                    "Build the story bank first — run story-bank, wait for it to "
                    "complete, then run prep-interview again.",
                )
            for item in blocking:
                ctx.mark_blocked("preflight", item)
            ctx.summary = "Blocked by preflight: " + "; ".join(blocking)
            ctx.emit("error", "not ready for a live run:\n  - " +
                     "\n  - ".join(blocking))
            rec = _record_run(ctx, run_id, started, t0, {"events": []})
            tstore.record_run(rec, path=telemetry_path)
            ctx.emit("info", f"telemetry: run {run_id} recorded "
                     f"({len(ctx.validator_results)} validator result(s), "
                     f"status={rec['status']})")
            return rec

    provider = None if deterministic_search else llm.get_provider(force_mock)
    backend_name = "deterministic" if provider is None else provider.name
    ctx.emit("info", f"run {run_id}: workflow={workflow} mode={mode} "
             f"project={project.name} backend={backend_name}")
    from .. import profile_meta, project_meta
    from ..privacy.disclosure import disclosure_lines
    from . import preflight as _pf
    pdir = _pf.profile_dir(project)
    linkedin_source = pdir / "linkedin-current.md" if pdir else None
    run_intent = _build_run_intent(project, workflow, task)
    if mode == "live" and provider is not None:
        for line in disclosure_lines(workflow, provider.name):
            ctx.emit("info", line)

    def _on_stream(text: str) -> None:
        ctx.check_cancelled()  # providers stream through here — kills the agent call
        ctx.emit("stream", text)

    def _provider_context(active_workflow: str, stale_material: str,
                          extra: dict | None = None) -> dict:
        role_packet = (
            extra.get("interview_role_packet")
            if active_workflow == "prep-interview" and isinstance(extra, dict)
            else None
        )
        resume_group_packet = (
            extra.get("resume_group_packet")
            if active_workflow == "prep-resume" and isinstance(extra, dict)
            else None
        )
        linkedin_group_packet = (
            extra.get("linkedin_group_packet")
            if active_workflow == "prep-linkedin" and isinstance(extra, dict)
            else None
        )
        application_role_packet = (
            extra.get("application_role_packet")
            if active_workflow == "apply" and isinstance(extra, dict)
            else None
        )
        scoped_role = role_packet.get("role") if isinstance(role_packet, dict) else None
        scoped_group_slug = ""
        if isinstance(resume_group_packet, dict):
            scoped_group_slug = str(resume_group_packet.get("group_slug", "") or "")
        elif isinstance(linkedin_group_packet, dict):
            scoped_group_slug = str(linkedin_group_packet.get("group_slug", "") or "")
        scoped_groups = [g for g in _focused_groups(project) if g.get("slug") == scoped_group_slug]
        scoped_group = scoped_groups[0] if scoped_groups else None
        payload = {
            "project": str(project), "task": task,
            "focused_jobs": (
                [scoped_role] if isinstance(scoped_role, dict)
                else list(scoped_group.get("jobs", [])) if isinstance(scoped_group, dict)
                else _strategy_focused_job_rows(project)
                if active_workflow == "prep-strategy"
                else focused_jobs(project) if active_workflow in FOCUS_SCOPED
                else None
            ),
            "profile_stale": stale_material if active_workflow in FOCUS_SCOPED else "",
            "profile_ready": _profile_ready(pdir),
            "targets": project_meta.targets_text(project),
            "run_intent": run_intent,
            "profile_dir": str(pdir) if pdir else "",
        }
        if active_workflow == "opportunity-plan":
            resolver = core.load("resolve_company_sources")
            registry = resolver.load_registered_registry()
            unique_entries: dict[str, dict] = {}
            for entry in registry.values():
                if not isinstance(entry, dict) or not entry.get("name"):
                    continue
                unique_entries.setdefault(str(entry["name"]), {
                    "name": str(entry["name"]),
                    "source_kind": str(entry.get("source_kind", "")),
                })
            payload["universe_seed_catalog"] = list(unique_entries.values())[:100]
        if extra:
            payload.update(extra)
        if active_workflow in {"prep-strategy", "prep-resume", "prep-linkedin",
                               "prep-interview", "story-bank", "apply"}:
            if active_workflow == "prep-interview" and isinstance(role_packet, dict):
                expected = str(role_packet.get("expected_artifact", "")).strip()
                payload["runner_context_packet"] = _runner_interview_role_packet(
                    project, pdir, scoped_role if isinstance(scoped_role, dict) else {},
                    expected,
                    quality_retry=str(extra.get("prep_interview_quality_retry", "") if extra else ""),
                    stage=str(extra.get("interview_stage", "") if extra else ""),
                )
            elif active_workflow == "prep-resume" and isinstance(scoped_group, dict):
                payload["runner_context_packet"] = _runner_resume_group_packet(
                    project, pdir, scoped_group
                )
            elif active_workflow == "prep-linkedin" and isinstance(scoped_group, dict):
                payload["runner_context_packet"] = _runner_linkedin_group_packet(
                    project, pdir, scoped_group
                )
            elif active_workflow == "apply" and isinstance(application_role_packet, dict):
                payload["runner_context_packet"] = application_role_packet
            else:
                payload["runner_context_packet"] = _runner_context_packet(
                    project, pdir, active_workflow
                )
        return payload

    envelope: dict = {"events": []}
    try:
        if mode == "live" and workflow == "prep-linkedin":
            linkedin_url = profile_meta.linkedin_url(pdir)
            if not linkedin_url:
                ctx.mark_blocked(
                    "linkedin_url_missing",
                    "No LinkedIn URL is saved. Add it in Profile and save before running "
                    "prep-linkedin.",
                )
            elif not linkedin_source or not linkedin_source.exists() or linkedin_source.stat().st_size == 0:
                ctx.mark_blocked(
                    "linkedin_source_missing",
                    "The LinkedIn URL is saved, but no captured profile is available. Open "
                    "Profile and choose Resync LinkedIn profile before running prep-linkedin.",
                )
            if ctx.blocked_reasons:
                ctx.failure_class = ctx.failure_class or "linkedin_profile_not_synced"
                ctx.summary = ctx.blocked_reasons[-1]["detail"]
                ctx.emit("attention", ctx.summary)
                rec = _record_run(ctx, run_id, started, t0, {"events": []})
                tstore.record_run(rec, path=telemetry_path)
                ctx.emit("info", f"telemetry: run {run_id} recorded "
                         f"({len(ctx.validator_results)} validator result(s), "
                         f"status={rec['status']})")
                return rec

        ctx.check_cancelled()
        stale_material = _pf._stale_profile(pdir) if pdir else ""
        if mode == "mock":
            mock_allowed = ({"targets/company-universe.json"}
                            if workflow == "opportunity-plan" else None)
            envelope = _execute_subagent(
                ctx, provider, workflow,
                _provider_context(workflow, stale_material), _on_stream,
                allowed_artifacts=mock_allowed)
        elif workflow == "prep":
            envelope = _run_prep_orchestration(
                ctx, provider, _provider_context, stale_material,
                _on_stream, pdir, linkedin_source)
        elif workflow == "search":
            if deterministic_search:
                envelope = _run_deterministic_search(ctx)
            else:
                envelope = _run_search_orchestration(
                    ctx, provider, _provider_context, stale_material,
                    _on_stream, run_intent=run_intent)
        elif workflow == "opportunity-plan":
            envelope = _run_progressive_universe(
                ctx, provider, _provider_context(workflow, stale_material), _on_stream
            )
        elif workflow == "score":
            envelope = _run_score_workflow(
                ctx, provider, _provider_context("score", stale_material), _on_stream)
        elif workflow == "prep-strategy":
            envelope = _run_strategy_workflow(
                ctx, provider, _provider_context("prep-strategy", stale_material), _on_stream)
        elif workflow == "prep-resume":
            envelope = _run_resume_workflow(
                ctx, provider, _provider_context, stale_material, _on_stream, pdir)
        elif workflow == "prep-linkedin":
            envelope = _run_linkedin_workflow(
                ctx, provider, _provider_context, stale_material, _on_stream)
        elif workflow == "story-bank":
            if _ensure_baseline_extracted(ctx, pdir):
                envelope = _run_story_bank_workflow(
                    ctx, provider, _provider_context("story-bank", stale_material), _on_stream)
            else:
                envelope = {"events": [], "usage": {}}
        elif workflow == "prep-interview":
            envelope = _run_interview_workflow(
                ctx, provider, _provider_context, stale_material, _on_stream)
        elif workflow == "apply":
            envelope = _run_apply_workflow(
                ctx, provider, _provider_context, stale_material, _on_stream,
                pdir, task, run_intent,
            )
        else:
            envelope = _execute_subagent(
                ctx, provider, workflow,
                _provider_context(workflow, stale_material), _on_stream)
        if mode != "mock" and not ctx.blocked_reasons:
            _post_run_checks(ctx)
    except RunCancelled:
        saved = False
        saved_scores = 0
        if workflow == "search":
            saved = _salvage_search_results(
                ctx,
                "Run cancelled by user; saving job rows captured before the stop.",
            )
        elif workflow == "score":
            try:
                saved_scores = _salvage_score_checkpoint(ctx)
            except Exception as salvage_error:
                ctx.emit("error", f"checkpointed score publish failed: {salvage_error}")
                saved_scores = 0
            saved = saved_scores > 0
        ctx.failure_class = "cancelled"
        if saved_scores:
            ctx.summary = (
                f"Run cancelled by user; published {saved_scores} validated "
                "checkpointed score rating(s)."
            )
            ctx.emit("result", ctx.summary)
        elif saved:
            ctx.summary = "Run cancelled by user; saved captured job rows collected so far."
        elif not ctx.summary:
            ctx.summary = "Run cancelled by user."
        ctx.emit("info", "run cancelled — the agent was stopped")
    except Exception as e:
        _mark_exception(ctx, workflow, e)
    if workflow == "prep":
        final_state = {
            "ok": "published", "partial": "partial", "blocked": "blocked",
            "failed": "failed", "cancelled": "cancelled",
        }.get(ctx.run_status(), ctx.run_status())
        _update_prep_progress(ctx, "complete", final_state)
    rec = _record_run(ctx, run_id, started, t0, envelope)
    tstore.record_run(rec, path=telemetry_path)
    ctx.emit("info", f"telemetry: run {run_id} recorded "
             f"({len(ctx.validator_results)} validator result(s), status={rec['status']})")
    return rec


def main(args) -> int:
    if args.workflow == PROFILE_WORKFLOW:
        from . import preflight as _pf
        person = getattr(args, "person", None)
        project = None
        if getattr(args, "project", None):
            p = repo_root() / "projects" / args.project
            if not (p / "project.json").exists():
                raise RoleNaviError(f"project not found: {args.project}")
            project = p
        if not person and project is not None:
            pdir = _pf.profile_dir(project)
            person = pdir.name if pdir is not None else ""
        if not person:
            active = active_project_dir()
            if active is not None:
                pdir = _pf.profile_dir(active)
                person = pdir.name if pdir is not None else ""
                project = active if project is None else project
        if not person:
            raise RoleNaviError("profile-intake requires --person or an active project")
        rec = run_profile_intake(person, project=project, task=args.task,
                                 force_mock=args.mock, max_turns=args.max_turns)
        return _exit_code_for_status(rec["status"])

    project = None
    if getattr(args, "project", None):
        if llm.mode(args.mock) == "mock":
            # canned rows must never land in a real project store
            print("mock mode: --project ignored — mock runs use a disposable fixture "
                  "copy (for live runs against real projects: sign in with "
                  "`codex login`, or use --provider cli with an authenticated local CLI)")
        else:
            p = repo_root() / "projects" / args.project
            if not (p / "project.json").exists():
                raise RoleNaviError(f"project not found: {args.project}")
            project = p
    task = args.task
    if not task and llm.mode(args.mock) == "live" and sys.stdin.isatty():
        task = input(f"task focus for '{args.workflow}' "
                     "(optional — Enter uses the skill's default behavior): ").strip() or None
    rec = run_workflow(args.workflow, project=project, task=task,
                       force_mock=args.mock, max_turns=args.max_turns)
    return _exit_code_for_status(rec["status"])
