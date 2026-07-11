"""`rolescout run <workflow>` — headless workflow invocations.

search | score | prep | prep-* | apply map onto the repo's skills. Each run
resolves the target project, gets an envelope from the provider, executes events
through the validated local pipeline, and records the run in the local telemetry
store. Public RoleScout does not execute external actions.

Mock runs NEVER touch a real project: they execute against a disposable copy of
the bundled mock fixture project under ROLESCOUT_HOME so
canned rows can't pollute user data. Live runs use the active project (or
--project) — exactly what the user asked for.
"""

from __future__ import annotations

import json
import hashlib
import os
import re
import shutil
import subprocess
import sys
import time
import csv
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from .. import core, llm, project_meta
from ..paths import RoleScoutError, active_project_dir, home_dir, repo_root
from ..telemetry import store as tstore

jd_text_cleaner = core.load("jd_text_cleaner")

WORKFLOW_SKILLS = {
    "profile-intake": ["candidate-profile-builder"],
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
SCORE_BATCH_MAX_JOBS = 20
SCORE_BATCH_RETRY_JOBS = 5
SCORE_BATCH_MAX_CHARS = 28000
SCORE_BATCH_WORKERS = 3
SCORE_FIELD_LIMITS = {
    "must_have_requirements": 700,
    "nice_to_have_requirements": 350,
    "jd_summary": 700,
    "notes": 350,
}
SCORE_PROFILE_LIMIT = 6000
SCORE_EVIDENCE_LIMIT = 5000


def _legacy_llm_search_enabled() -> bool:
    return os.environ.get("ROLESCOUT_LEGACY_LLM_SEARCH", "").strip().lower() in {
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
    import csv as _csv
    ids = _focused_job_ids(project)
    if not ids:
        return []
    rows: dict[str, dict] = {}
    jl = project / "data" / "job_list.csv"
    if jl.exists():
        with open(jl, newline="", encoding="utf-8") as f:
            for r in _csv.DictReader(f):
                job_id = str(r.get("job_id", "") or "").strip()
                if job_id in ids:
                    row = dict(r)
                    row.setdefault("url", row.get("job_page_url") or row.get("source_url", ""))
                    rows[job_id] = row
    return [rows[job_id] for job_id in ids if job_id in rows]


def focused_jobs(project: Path) -> list[dict]:
    """Focused positions joined with job_list rows (empty list when none)."""
    return _focused_job_rows(project)


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


class RunCancelled(RoleScoutError):
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
        "schema": "rolescout-run-intent-v1",
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


def _agent_log_dir(ctx: RunContext) -> Path | None:
    if not ctx.run_id:
        return None
    return ctx.project / "runs" / ctx.run_id / "agents"


def _append_agent_log(ctx: RunContext, label: str, text: str) -> None:
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
        (log_dir / "manifest.json").write_text(
            json.dumps({"schema": "rolescout-agent-log-manifest-v1",
                        "run_id": ctx.run_id, "agents": records},
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
    if pure.is_absolute() or not pure.parts:
        raise RoleScoutError("artifact path escapes the project")
    if any(part in ("", ".", "..") for part in pure.parts):
        raise RoleScoutError("artifact path escapes the project")
    return Path(*pure.parts), pure.as_posix()


_LINKEDIN_SCORE_SECTIONS = {
    "headline",
    "about",
    "experienceentries",
    "skills",
    "education",
    "activity",
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
            if len(cells) >= 2 and _linkedin_section_key(cells[0]) in _LINKEDIN_SCORE_SECTIONS:
                score = cells[1]
                match = re.fullmatch(r"([1-5](?:\.\d+)?)", score)
                if match:
                    cells[1] = f"{match.group(1)}/5"
                cells = [_strip_linkedin_name_mismatch_phrases(cell) for cell in cells]
                line = "| " + " | ".join(cells) + " |"
        elif _linkedin_name_mismatch_line(stripped):
            continue
        out.append(_strip_linkedin_name_mismatch_phrases(line))
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
        raise RoleScoutError(f"fixture project template missing: {template}")
    dest = home_dir() / "mock-runs" / run_id / "project"
    shutil.copytree(template, dest)
    r = core.run_script("init_db", env=_env_for(dest))
    if r.returncode != 0:
        raise RoleScoutError(f"fixture project store init failed:\n{r.stdout}{r.stderr}")
    return dest


def _write_artifact(ctx: RunContext, ev: dict) -> None:
    rel_path, rel = _safe_artifact_rel(ev["path"])
    path = (ctx.project / rel_path).resolve()
    try:
        path.relative_to(ctx.project.resolve())
    except ValueError as e:
        raise RoleScoutError("artifact path escapes the project") from e
    path.parent.mkdir(parents=True, exist_ok=True)
    if "json" in ev:
        path.write_text(json.dumps(ev["json"], indent=2, ensure_ascii=False) + "\n",
                        encoding="utf-8")
    else:
        text = str(ev.get("text", ""))
        if rel.startswith("linkedin/") and rel.endswith("/linkedin-review.md"):
            text = _normalize_linkedin_review_text(text)
        elif rel.startswith("interviews/") and rel.endswith(".md"):
            text = _normalize_interview_prep_text(text)
        path.write_text(text, encoding="utf-8")
    ctx.emit("artifact", rel)
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


def _store_write(ctx: RunContext, ev: dict) -> None:
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


def execute_events(ctx: RunContext, events: list[dict],
                   allowed_artifacts: set[str] | None = None) -> None:
    for ev in events:
        ctx.check_cancelled()  # between events: never mid-write
        t = ev.get("type")
        if t == "progress":
            if not ctx.streamed:  # streamed providers already showed these live
                ctx.emit("progress", ev.get("text", ""))
        elif t == "artifact":
            if allowed_artifacts is not None:
                try:
                    _, rel = _safe_artifact_rel(ev.get("path", ""))
                except RoleScoutError as e:
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
            _store_write(ctx, ev)
        elif t == "external_action":
            raise RoleScoutError(
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


def _post_run_strategy_checks(ctx: RunContext) -> None:
    strategy_artifacts = set(ctx.artifacts_written)
    required = {
        "strategy/prep-strategy.md",
        "strategy/target-priorities.md",
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
    default_plan_proc = core.run_script("build_search_view", str(ctx.project), "--json",
                                        env=_env_for(ctx.project))
    default_plan = {}
    plan_path = ctx.project / "targets" / "search-view-filter-plan.json"
    try:
        default_plan = json.loads(plan_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pass
    prompt = (
        "Return ONLY JSON for RoleScout search-view filtering. "
        "Do not score role fit and do not inspect individual job rows. "
        "Given target locations and target level, produce a conservative "
        "rolescout-search-view-filter-plan-v1 object. Location filter is positive: "
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
        plan["schema"] = "rolescout-search-view-filter-plan-v1"
        plan["source"] = "llm_lightweight"
        plan["generated_at"] = _now()
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
            "target": str(ctx.project / "data" / "job_list.visible.csv"),
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
            "Run `rolescout run score` for post-capture fit scoring."
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


def _job_list_has_rows(project: Path) -> bool:
    path = project / "data" / "job_list.csv"
    try:
        with path.open(newline="", encoding="utf-8") as f:
            return any(True for _ in csv.DictReader(f))
    except OSError:
        return False


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


def _score_view_path(project: Path) -> Path:
    visible = project / "data" / "job_list.visible.csv"
    return visible if visible.exists() else project / "data" / "job_list.csv"


def _read_csv_dicts(path: Path) -> list[dict[str, str]]:
    try:
        with path.open(newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))
    except OSError:
        return []


def _truncate_field(value: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 20)].rstrip() + " ...[truncated]"


def _score_compact_job(row: dict[str, str]) -> dict[str, str]:
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
    return out


def _score_batch_size(jobs: list[dict[str, str]]) -> int:
    return len(json.dumps(jobs, ensure_ascii=False, separators=(",", ":")))


def _make_score_batches(
    jobs: list[dict[str, str]],
    *,
    max_jobs: int = SCORE_BATCH_MAX_JOBS,
    max_chars: int = SCORE_BATCH_MAX_CHARS,
) -> list[list[dict[str, str]]]:
    batches: list[list[dict[str, str]]] = []
    current: list[dict[str, str]] = []
    for job in jobs:
        candidate = [*current, job]
        if current and (len(candidate) > max_jobs or _score_batch_size(candidate) > max_chars):
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
    return [item for item in criteria if isinstance(item, dict) and item.get("name")]


def _score_text_file(path: Path | None, limit: int) -> str:
    if path is None:
        return ""
    try:
        return _truncate_field(path.read_text(encoding="utf-8", errors="replace"), limit)
    except OSError:
        return ""


def _score_candidate_context(base_context: dict) -> dict[str, str]:
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
    }


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


def _focused_groups(project: Path) -> list[dict]:
    groups: list[dict] = []
    by_slug: dict[str, dict] = {}
    for row in _focused_job_rows(project):
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


def _processed_jd_rel(job_id: str) -> str:
    raw = str(job_id or "job")
    digest = hashlib.sha1(raw.encode("utf-8", errors="replace")).hexdigest()[:10]
    return f"targets/jobs-processed/{_slug(raw, 'job')[:48].strip('-')}-{digest}.json"


def _processed_jd_brief(project: Path, row: dict) -> dict:
    job_id = str(row.get("job_id", "") or "").strip()
    snapshot = _read_json(project / "targets" / "jobs" / f"{job_id}.json", {}) if job_id else {}
    return jd_text_cleaner.jd_interview_brief(row, snapshot if isinstance(snapshot, dict) else {})


def _prepare_processed_jds(ctx: RunContext) -> dict[str, dict]:
    briefs: dict[str, dict] = {}
    rows = _focused_job_rows(ctx.project)
    if not rows:
        return briefs
    for row in rows:
        job_id = str(row.get("job_id", "") or "").strip()
        if not job_id:
            continue
        brief = _processed_jd_brief(ctx.project, row)
        payload = {
            "schema": "rolescout-processed-jd-v1",
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


def _resume_group_allowed_artifacts(group_slug: str) -> set[str]:
    group = _slug(group_slug, "ungrouped")
    return {
        "resumes/baseline-extracted.md",
        f"resumes/{group}/target-brief.json",
        f"resumes/{group}/resume-score.md",
        f"resumes/{group}/resume-draft.md",
        f"resumes/{group}/reasons.json",
        f"resumes/{group}/resume-validation.md",
        f"resumes/{group}/resume-not-generated.md",
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
        "focused_jobs": _packet_job_rows(jobs),
        "processed_jd_briefs": _load_processed_jds(project, jobs),
        "candidate_profile_md": _text_limited(
            pdir / "candidate-profile.md" if pdir else None, 6000
        ),
        "evidence_map_md": _text_limited(
            pdir / "evidence-map.md" if pdir else None, 6000
        ),
        "baseline_resume": _baseline_resume_context(project),
        "target_group_file": _group_file_packet(project, slug, limit=5000),
        "strategy_context": _strategy_support_packet(project),
        "existing_group_resume_files": _glob_texts(
            resume_dir, "*.md", limit_each=3000, max_files=5
        ),
        "linkedin_current_md": _text_limited(
            pdir / "linkedin-current.md" if pdir else None, 5000
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
            pdir / "linkedin-current.md" if pdir else None, 20000
        ),
        "candidate_profile_md": _text_limited(
            pdir / "candidate-profile.md" if pdir else None, 6000
        ),
        "evidence_map_md": _text_limited(
            pdir / "evidence-map.md" if pdir else None, 6000
        ),
        "target_group_file": _group_file_packet(project, slug, limit=5000),
        "strategy_context": _strategy_support_packet(project),
        "resume_group_files": _glob_texts(
            project / "resumes" / slug, "*.md", limit_each=3000, max_files=5
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
    }
    if workflow == "prep-strategy":
        current_group_files = [
            {"group_slug": group["slug"], "text": _group_file_packet(project, group["slug"], limit=4500)}
            for group in groups if group.get("slug") and group.get("slug") != "ungrouped"
        ]
        return {
            "workflow": workflow,
            **base_profile,
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
            pdir / "linkedin-current.md" if pdir else None, 20000
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
    existing = _load_score_ratings(project)
    by_id = {
        str(item.get("job_id", "")).strip(): item
        for item in existing if str(item.get("job_id", "")).strip()
    }
    changed = 0
    for item in incoming:
        job_id = str(item.get("job_id", "")).strip()
        ratings = item.get("ratings", {})
        if not job_id or not isinstance(ratings, dict):
            continue
        by_id[job_id] = item
        changed += 1
    if changed:
        out = [by_id[key] for key in sorted(by_id)]
        (strategy_dir / "job-ratings.json").write_text(
            json.dumps(out, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    return changed


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
    if payload.get("schema") == "rolescout-score-batch-output-v1":
        ratings = payload.get("job_ratings", [])
    elif payload.get("schema") == "rolescout-score-output-v1":
        ratings = payload.get("job_ratings", [])
    else:
        return []
    return [item for item in ratings if isinstance(item, dict)]


def _validate_score_batch_ratings(
    ratings: list[dict],
    expected_ids: set[str],
    criteria_names: set[str],
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
            continue
        missing_criteria = criteria_names - set(str(k) for k in rating_values)
        bad_values = [
            key for key, value in rating_values.items()
            if key in criteria_names and (not isinstance(value, int) or not 1 <= value <= 5)
        ]
        if missing_criteria or bad_values:
            incomplete += 1
        accepted[job_id] = item
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
        "Generated by the RoleScout score batch workflow.",
        "",
    ]
    for slug, entries in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        priority_lines.append(f"- `{slug}`: {len(entries)} visible row(s)")
    (project / "strategy" / "target-priorities.md").write_text(
        "\n".join(priority_lines) + "\n",
        encoding="utf-8",
    )


def _run_score_batches(ctx: RunContext, provider, base_context: dict, on_stream) -> dict:
    rows = _read_csv_dicts(_score_view_path(ctx.project))
    jobs = [_score_compact_job(row) for row in rows if row.get("job_id")]
    criteria = _score_criteria(ctx.project)
    if not jobs or not criteria:
        return {"events": [], "usage": {}, "ratings": 0}

    batches = _make_score_batches(jobs)
    candidate_context = _score_candidate_context(base_context)
    criteria_names = {str(item.get("name", "")).strip() for item in criteria if item.get("name")}
    jobs_by_id = {str(job.get("job_id", "")).strip(): job for job in jobs}
    ctx.emit(
        "info",
        f"score batch evaluation: visible_rows={len(jobs)} "
        f"batches={len(batches)} max_jobs={SCORE_BATCH_MAX_JOBS} "
        f"workers={SCORE_BATCH_WORKERS}",
    )

    def _one(
        pass_name: str,
        index: int,
        total: int,
        batch: list[dict[str, str]],
    ) -> tuple[str, int, list[dict[str, str]], list[dict], dict]:
        batch_context = dict(base_context)
        batch_context.update({
            "score_batch": {
                "index": index,
                "total": total,
                "pass": pass_name,
                "jobs": batch,
                "criteria": criteria,
                "candidate": candidate_context,
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
    issue_count = 0
    envelope: dict = {"events": [], "usage": {}}
    with ThreadPoolExecutor(max_workers=SCORE_BATCH_WORKERS) as pool:
        futures = {
            pool.submit(_one, "batch", i, len(batches), batch): i
            for i, batch in enumerate(batches, start=1)
        }
        for fut in as_completed(futures):
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
                continue
            expected_ids = {str(job.get("job_id", "")).strip() for job in batch if job.get("job_id")}
            accepted, missing, issues = _validate_score_batch_ratings(
                ratings, expected_ids, criteria_names
            )
            accepted_by_id.update(accepted)
            missing_after_first.update(missing)
            if issues:
                issue_count += 1
                ctx.emit("info", f"score batch {index}/{len(batches)} validation: {', '.join(issues)}")
            _merge_usage(envelope, child)
            ctx.emit(
                "progress",
                f"score batch {index}/{len(batches)} accepted "
                f"{len(accepted)}/{len(expected_ids)} rating(s)",
            )

    retry_ids = sorted(job_id for job_id in missing_after_first if job_id not in accepted_by_id)
    retry_batches = [
        [jobs_by_id[job_id] for job_id in retry_ids[i:i + SCORE_BATCH_RETRY_JOBS] if job_id in jobs_by_id]
        for i in range(0, len(retry_ids), SCORE_BATCH_RETRY_JOBS)
    ]
    retry_batches = [batch for batch in retry_batches if batch]
    if retry_batches:
        ctx.emit(
            "info",
            f"score retry: missing_rows={len(retry_ids)} "
            f"retry_batches={len(retry_batches)} retry_size={SCORE_BATCH_RETRY_JOBS}",
        )
        with ThreadPoolExecutor(max_workers=SCORE_BATCH_WORKERS) as pool:
            futures = {
                pool.submit(_one, "retry", i, len(retry_batches), batch): i
                for i, batch in enumerate(retry_batches, start=1)
            }
            for fut in as_completed(futures):
                ctx.check_cancelled()
                index = futures[fut]
                try:
                    _, _, batch, ratings, child = fut.result()
                except Exception as exc:
                    ctx.mark_partial(f"score_retry_{index}", str(exc)[:500])
                    ctx.emit("info", f"score retry {index}/{len(retry_batches)} failed: {str(exc)[:240]}")
                    continue
                expected_ids = {str(job.get("job_id", "")).strip() for job in batch if job.get("job_id")}
                accepted, missing, issues = _validate_score_batch_ratings(
                    ratings, expected_ids, criteria_names
                )
                accepted_by_id.update(accepted)
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
                    f"{len(accepted)}/{len(expected_ids)} rating(s)",
                )

    all_ratings = list(accepted_by_id.values())
    unresolved = len(jobs_by_id) - len(accepted_by_id)
    changed = _merge_score_ratings(ctx.project, all_ratings)
    _write_basic_score_group_artifacts(ctx.project, all_ratings)
    ctx.validator_results.append({
        "validator": "score_batch_evaluation",
        "target": ctx.project.name,
        "returncode": 0 if changed else 2,
        "output": (
            f"{changed} rating(s) merged from {len(batches)} batch(es); "
            f"retry_batches={len(retry_batches)} unresolved={unresolved} "
            f"validation_issue_batches={issue_count}"
        ),
    })
    if unresolved:
        ctx.mark_partial("score_batch_coverage", f"{unresolved} row(s) left for finalizer fallback")
    ctx.emit("artifact", f"score batch ratings merged: {changed}; unresolved={unresolved}")
    envelope["ratings"] = changed
    return envelope


def _finalize_score(ctx: RunContext) -> None:
    """Run deterministic score math + store update outside the agent sandbox."""
    rc = _stream_script(ctx, "finalize_score", str(ctx.project))
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


def _extract_runner_artifact_payload(text: str) -> dict | None:
    raw = str(text or "").strip()
    if not raw:
        return None
    marker = "ROLESCOUT_ARTIFACT_OUTPUT_JSON:"
    if marker in raw:
        raw = raw.split(marker, 1)[1].strip()
    else:
        return None
    try:
        payload = _extract_json_object(raw)
    except (ValueError, json.JSONDecodeError):
        return None
    if payload.get("schema") != "rolescout-artifact-output-v1":
        return None
    return payload


def _materialize_runner_artifact_output(ctx: RunContext, envelope: dict,
                                        allowed_paths: set[str] | None = None) -> bool:
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
    for item in artifacts:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path", "")).strip()
        if not path:
            continue
        try:
            _, rel = _safe_artifact_rel(path)
        except RoleScoutError as e:
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
        ev = {"type": "artifact", "path": path}
        if "json" in item:
            ev["json"] = item["json"]
        else:
            ev["text"] = str(item.get("text", ""))
        _write_artifact(ctx, ev)
        artifact_count += 1
    for item in store_writes:
        if not isinstance(item, dict):
            continue
        store = str(item.get("store", "")).strip()
        rows = item.get("rows", [])
        if store not in {"job_list", "tracker"} or not isinstance(rows, list):
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
    if payload.get("schema") != "rolescout-score-output-v1":
        return None
    return payload


def _materialize_score_output(ctx: RunContext, envelope: dict) -> bool:
    """Persist score artifacts from final JSON when the agent sandbox is read-only."""
    payload = _extract_score_output_payload(_score_result_text(envelope))
    if not payload:
        return False
    ratings = payload.get("job_ratings")
    if not isinstance(ratings, list):
        ctx.mark_partial("score_output", "rolescout-score-output-v1 missing job_ratings list")
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
    execute_events(ctx, envelope.get("events", []), allowed_artifacts=allowed_artifacts)
    _append_agent_result_log(ctx, label, envelope)
    _materialize_runner_artifact_output(ctx, envelope, allowed_paths=allowed_artifacts)
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
        ratings_path = ctx.project / "strategy" / "job-ratings.json"
        if not ratings_path.exists():
            ratings_path.parent.mkdir(parents=True, exist_ok=True)
            ratings_path.write_text("[]\n", encoding="utf-8")
        ctx.mark_partial("score_batch", "no evaluator ratings returned; finalizer will park visible rows")
    if not ctx.failure_class and not ctx.blocked_reasons:
        _materialize_score_output(ctx, envelope)
        for ev in envelope.get("events", []):
            if isinstance(ev, dict) and ev.get("type") == "result":
                ev.pop("content", None)
        _finalize_score(ctx)
    return envelope


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
        limit = int(os.environ.get("ROLESCOUT_SEARCH_MAX_RETRY_COMPANIES",
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
    if len(jobs) == 1:
        job = jobs[0]
        try:
            return _execute_subagent(ctx, provider, job["workflow"], job["context"],
                                     on_stream, label=job["label"],
                                     model_workflow=job.get("model_workflow"),
                                     allowed_artifacts=job.get("allowed_artifacts"))
        except RunCancelled:
            raise
        except Exception as e:
            ctx.mark_partial(job["label"], str(e))
            _append_agent_log(ctx, job["label"], f"ERROR: {e}")
            _write_agent_manifest(ctx, [{
                "label": job["label"], "workflow": job["workflow"],
                "model_workflow": job.get("model_workflow", ""),
                "status": "failed", "error": str(e)[:1000],
            }])
            ctx.emit("info", f"subagent failed: {job['label']}: {str(e)[:300]}")
            if hard_fail_when_all_fail and _looks_blocked_error(str(e)):
                ctx.mark_blocked(job["label"], str(e))
            elif hard_fail_when_all_fail:
                ctx.failure_class = ctx.failure_class or "named_subagent_failed"
            return {"events": [], "usage": {}, "failed_labels": [job["label"]]}

    ctx.emit("info", "subagents start: " + ", ".join(job["label"] for job in jobs))
    results: dict[str, dict] = {}
    failures: dict[str, str] = {}
    records: list[dict] = []
    try:
        max_workers = int(os.environ.get("ROLESCOUT_SEARCH_MAX_PARALLEL", "6") or "6")
    except ValueError:
        max_workers = 6
    workers = max(1, min(len(jobs), max_workers))
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
                results[label] = fut.result()
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
    _write_agent_manifest(ctx, records)
    merged: dict = {"events": [], "usage": {}, "failed_labels": sorted(failures)}
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
        execute_events(ctx, envelope.get("events", []), allowed_artifacts=allowed)
        _append_agent_result_log(ctx, label, envelope)
        _materialize_runner_artifact_output(ctx, envelope, allowed_paths=allowed)
        for ev in envelope.get("events", []):
            if isinstance(ev, dict) and ev.get("type") == "result" and job["workflow"] != "score":
                ev.pop("content", None)
        _merge_usage(merged, envelope)
        ctx.emit("info", f"subagent done: {label}")
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
        "validator_results": ctx.validator_results,
        "failure_class": ctx.failure_class, "status": status, "summary": summary,
        "events": envelope.get("events", []),
    }
    return rec


def _exit_code_for_status(status: str) -> int:
    return 0 if status in {"ok", "partial"} else 1


def _build_interview_context(ctx: RunContext) -> None:
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


def _load_interview_context_roles(project: Path) -> list[dict]:
    data = _read_json(project / "interviews" / "interview-context.json", {})
    roles = data.get("roles", []) if isinstance(data, dict) else []
    return [role for role in roles if isinstance(role, dict)]


def _interview_role_slug(role: dict) -> str:
    return _slug(f"{role.get('company', '')}-{role.get('title', '')}", "role")


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
    for stage, section in ordered:
        body = _extract_h2_section(stage_text.get(stage, ""), section)
        if not body:
            body = _placeholder_section(section, f"Missing {stage} stage output")
        sections.append(body)
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
        for i, entry in enumerate(entries):
            if not isinstance(entry, dict):
                errors.append(f"entry {i} must be an object")
                continue
            missing = sorted(required - set(entry))
            if missing:
                errors.append(f"entry {i} missing {', '.join(missing)}")
    ctx.validator_results.append({
        "validator": "validate_story_bank",
        "target": str(path),
        "returncode": 0 if not errors else 1,
        "output": "; ".join(errors)[:800] if errors else f"PASS: {len(entries or [])} story entries",
    })
    ctx.emit("validator", "validate_story_bank: " + ("PASS" if not errors else "FAIL"))
    if errors:
        ctx.emit("validator", "; ".join(errors)[:800])
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
    _prepare_processed_jds(ctx)
    return _execute_subagent(
        ctx, provider, "prep-strategy", context, on_stream,
        model_workflow="prep-strategy")


def _run_resume_workflow(ctx: RunContext, provider, context_for,
                         stale_material: str, on_stream) -> dict:
    _prepare_processed_jds(ctx)
    groups = _focused_groups(ctx.project)
    if not groups:
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
        })
    return _run_parallel_named_subagents(
        ctx, provider, jobs, on_stream, hard_fail_when_all_fail=True)


def _run_linkedin_workflow(ctx: RunContext, provider, context_for,
                           stale_material: str, on_stream) -> dict:
    _prepare_processed_jds(ctx)
    groups = _focused_groups(ctx.project)
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
        })
    return _run_parallel_named_subagents(
        ctx, provider, jobs, on_stream, hard_fail_when_all_fail=True)


def _run_interview_workflow(ctx: RunContext, provider, context_for,
                            stale_material: str, on_stream) -> dict:
    envelope: dict = {"events": [], "usage": {}}
    _prepare_processed_jds(ctx)
    _build_interview_context(ctx)
    if ctx.failure_class or ctx.blocked_reasons:
        return envelope
    roles = _load_interview_context_roles(ctx.project)
    if not roles:
        ctx.failure_class = ctx.failure_class or "prep_interview_no_roles"
        ctx.emit("validator", "prep-interview role context: FAIL (no focused roles)")
        return envelope
    story_bank = ctx.project / "interviews" / "story-bank.json"
    if not story_bank.exists():
        ctx.failure_class = ctx.failure_class or "story_bank_missing"
        ctx.emit("validator", "story bank missing: run `rolescout run story-bank` first")
        return envelope
    if not _validate_story_bank(ctx):
        return envelope

    def _run_role_packets(quality_retry: str | None = None,
                          only_expected: set[str] | None = None) -> None:
        for index, role in enumerate(roles, start=1):
            expected = _interview_expected_artifact(role)
            if only_expected is not None and expected not in only_expected:
                continue
            before = set(ctx.artifacts_written)
            stages = ["whys"] if quality_retry else ["company-research", "whys", "qa"]
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
            _assemble_interview_prep(ctx, role)
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
        if result.returncode == 2:
            ctx.emit("validator", "prep-interview quality retry: QUALITY")
            retry_paths = _interview_validator_paths(out)
            _run_role_packets(out, only_expected=retry_paths or None)
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
        elif result.returncode != 0:
            ctx.failure_class = ctx.failure_class or "prep-interview_validation_failure"
            ctx.emit("validator", out[:800])
    return envelope


def _run_prep_orchestration(ctx: RunContext, provider, context_for, stale_material: str,
                            on_stream, pdir: Path | None,
                            linkedin_source: Path | None) -> dict:
    """Full prep is an orchestrator, not a monolithic profile-building agent.

    Order: strategy first, resume + LinkedIn in parallel when LinkedIn current
    content exists, then interview after both downstream artifacts have had a
    chance to land.
    """
    envelope: dict = {"events": [], "usage": {}}

    strategy = _run_strategy_workflow(
        ctx, provider, context_for("prep-strategy", stale_material), on_stream)
    _merge_usage(envelope, strategy)
    if ctx.failure_class or ctx.blocked_reasons:
        return envelope

    resume = _run_resume_workflow(
        ctx, provider, context_for, stale_material, on_stream)
    _merge_usage(envelope, resume)
    if ctx.failure_class or ctx.blocked_reasons:
        return envelope

    from .. import profile_meta
    linkedin_url = profile_meta.linkedin_url(pdir)
    if linkedin_url and linkedin_source:
        rc = _run_linkedin_capture_helper(ctx, linkedin_url, linkedin_source,
                                          required=False)
        if rc == 0 and linkedin_source.exists() and linkedin_source.stat().st_size > 0:
            linkedin = _run_linkedin_workflow(
                ctx, provider, context_for, stale_material, on_stream)
            _merge_usage(envelope, linkedin)
        else:
            ctx.emit("info", "warning: LinkedIn current profile is unavailable; "
                     "skipping prep-linkedin. Add a LinkedIn URL and complete "
                     "the supported capture/import step, then rerun prep-linkedin.")
    else:
        ctx.emit("info", "warning: no LinkedIn URL/current profile; skipping "
                 "prep-linkedin. Add the URL in the profile form and complete "
                 "LinkedIn import/capture before LinkedIn review.")

    if ctx.failure_class or ctx.blocked_reasons:
        return envelope

    if _story_bank_needs_refresh(ctx.project, pdir):
        story = _run_story_bank_workflow(
            ctx, provider, context_for("story-bank", stale_material), on_stream)
        _merge_usage(envelope, story)
        if ctx.failure_class or ctx.blocked_reasons:
            return envelope

    interview = _run_interview_workflow(
        ctx, provider, context_for, stale_material, on_stream)
    _merge_usage(envelope, interview)
    return envelope


def run_profile_intake(person: str, project: Path | None = None, task: str | None = None,
                       force_mock: bool = False, max_turns: int = 40,
                       telemetry_path: Path | None = None, on_event=None,
                       cancel_event=None) -> dict:
    """Person-scoped profile/evidence intake.

    This lane is intentionally independent of project creation, job search, and
    focused jobs. It may be triggered by profile save, resume upload, or an
    explicit CLI run.
    """
    from .. import profile_meta
    from . import preflight as _pf

    person = str(person or "").strip()
    if not person:
        raise RoleScoutError("profile-intake requires a person code")
    pdir = repo_root() / "profiles" / person
    if not pdir.is_dir():
        raise RoleScoutError(f"person '{person}' has no profile folder")

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
    linkedin_source = pdir / "linkedin-current.md"

    def _on_stream(text: str) -> None:
        ctx.check_cancelled()
        ctx.emit("stream", text)

    def _profile_context(active_workflow: str, stale_material: str) -> dict:
        return {
            "project": str(pdir),
            "profile_dir": str(pdir),
            "person": person,
            "task": task,
            "skills": WORKFLOW_SKILLS[active_workflow],
            "max_turns": max_turns,
            "focused_jobs": None,
            "profile_stale": stale_material if active_workflow == PROFILE_WORKFLOW else "",
            "profile_ready": _profile_ready(pdir),
            "linkedin_url": profile_meta.linkedin_url(pdir),
            "linkedin_source_path": str(linkedin_source),
            "targets": "",
            "instructions": profile_meta.instructions(pdir),
        }

    envelope: dict = {"events": [], "usage": {}}
    try:
        linkedin_url = profile_meta.linkedin_url(pdir)
        if mode == "live" and linkedin_url:
            _run_linkedin_capture_helper(ctx, linkedin_url, linkedin_source,
                                         required=False)
        stale_material = _pf._stale_profile(pdir)
        envelope = _execute_subagent(
            ctx, provider, PROFILE_WORKFLOW,
            _profile_context(PROFILE_WORKFLOW, stale_material), _on_stream)
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
            raise RoleScoutError("no active project — run `rolescout init` first")

    ctx = RunContext(workflow, project, mode)
    ctx.on_event = on_event
    ctx.cancel_event = cancel_event
    started = _now()
    t0 = time.monotonic()
    ctx.run_id = run_id
    if mode == "live":
        from . import preflight
        blocking, warnings = preflight.check(workflow, project)
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
            ctx.emit("info", f"telemetry: run {run_id} recorded "
                     f"({len(ctx.validator_results)} validator result(s), "
                     f"status={rec['status']})")
            return rec

    provider = None if deterministic_search else llm.get_provider(force_mock)
    backend_name = "deterministic" if provider is None else provider.name
    ctx.emit("info", f"run {run_id}: workflow={workflow} mode={mode} "
             f"project={project.name} backend={backend_name}")
    from .. import profile_meta, project_meta
    from . import preflight as _pf
    pdir = _pf.profile_dir(project)
    linkedin_source = pdir / "linkedin-current.md" if pdir else None
    run_intent = _build_run_intent(project, workflow, task)

    def _on_stream(text: str) -> None:
        ctx.check_cancelled()  # providers stream through here — kills the agent call
        ctx.emit("stream", text)

    def _provider_context(active_workflow: str, stale_material: str,
                          extra: dict | None = None) -> dict:
        phase = extra.get("search_phase") if extra else None
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
            "skills": skills_for_workflow_phase(active_workflow, phase),
            "max_turns": max_turns,
            "focused_jobs": (
                [scoped_role] if isinstance(scoped_role, dict)
                else list(scoped_group.get("jobs", [])) if isinstance(scoped_group, dict)
                else focused_jobs(project) if active_workflow in FOCUS_SCOPED
                else None
            ),
            "profile_stale": stale_material if active_workflow in FOCUS_SCOPED else "",
            "profile_ready": _profile_ready(pdir),
            "linkedin_url": profile_meta.linkedin_url(pdir),
            "profile_dir": str(pdir) if pdir else "",
            "linkedin_source_path": str(linkedin_source) if linkedin_source else "",
            "targets": project_meta.targets_text(project),
            "instructions": profile_meta.instructions(pdir),
            "run_intent": run_intent,
        }
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
            else:
                payload["runner_context_packet"] = _runner_context_packet(
                    project, pdir, active_workflow
                )
        return payload

    envelope: dict = {"events": []}
    try:
        if mode == "live" and workflow == "prep-linkedin":
            linkedin_url = profile_meta.linkedin_url(pdir)
            if linkedin_url and linkedin_source:
                rc = _run_linkedin_capture_helper(ctx, linkedin_url, linkedin_source,
                                                  required=True)
                if rc != 0:
                    rec = _record_run(ctx, run_id, started, t0, {"events": []})
                    tstore.record_run(rec, path=telemetry_path)
                    ctx.emit("info", f"telemetry: run {run_id} recorded "
                             f"({len(ctx.validator_results)} validator result(s), "
                             f"status={rec['status']})")
                    return rec

        ctx.check_cancelled()
        stale_material = _pf._stale_profile(pdir) if pdir else ""
        if mode == "mock":
            envelope = _execute_subagent(
                ctx, provider, workflow,
                _provider_context(workflow, stale_material), _on_stream)
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
        elif workflow == "score":
            envelope = _run_score_workflow(
                ctx, provider, _provider_context("score", stale_material), _on_stream)
        elif workflow == "prep-strategy":
            envelope = _run_strategy_workflow(
                ctx, provider, _provider_context("prep-strategy", stale_material), _on_stream)
        elif workflow == "prep-resume":
            envelope = _run_resume_workflow(
                ctx, provider, _provider_context, stale_material, _on_stream)
        elif workflow == "prep-linkedin":
            envelope = _run_linkedin_workflow(
                ctx, provider, _provider_context, stale_material, _on_stream)
        elif workflow == "story-bank":
            envelope = _run_story_bank_workflow(
                ctx, provider, _provider_context("story-bank", stale_material), _on_stream)
        elif workflow == "prep-interview":
            envelope = _run_interview_workflow(
                ctx, provider, _provider_context, stale_material, _on_stream)
        else:
            envelope = _execute_subagent(
                ctx, provider, workflow,
                _provider_context(workflow, stale_material), _on_stream)
        auto_score = os.environ.get("ROLESCOUT_SEARCH_AUTO_SCORE", "").strip().lower() in {
            "1", "true", "yes", "on"
        }
        if (workflow == "search" and not deterministic_search
                and not ctx.failure_class and not ctx.blocked_reasons):
            if ctx.partial_reasons:
                ctx.emit("info", "search finished with partial coverage; "
                         "running score once for captured rows")
            else:
                ctx.emit("info", "search complete; running score once")
            score_envelope = _run_score_workflow(
                ctx, provider, _provider_context("score", stale_material), _on_stream)
            _merge_usage(envelope, score_envelope)
            if ctx.summary and not ctx.summary.startswith("search + "):
                ctx.summary = f"search + {ctx.summary}"
        elif (workflow == "search" and deterministic_search and auto_score
              and not ctx.failure_class and not ctx.blocked_reasons):
            provider = llm.get_provider(force_mock)
            ctx.emit("info", "deterministic search complete; running optional score once")
            score_envelope = _run_score_workflow(
                ctx, provider, _provider_context("score", stale_material), _on_stream)
            _merge_usage(envelope, score_envelope)
        if mode != "mock" and not ctx.blocked_reasons:
            _post_run_checks(ctx)
    except RunCancelled:
        saved = False
        if workflow == "search":
            saved = _salvage_search_results(
                ctx,
                "Run cancelled by user; saving job rows captured before the stop.",
            )
        ctx.failure_class = "cancelled"
        if saved:
            ctx.summary = "Run cancelled by user; saved captured job rows collected so far."
        elif not ctx.summary:
            ctx.summary = "Run cancelled by user."
        ctx.emit("info", "run cancelled — the agent was stopped")
    except Exception as e:
        _mark_exception(ctx, workflow, e)
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
                raise RoleScoutError(f"project not found: {args.project}")
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
            raise RoleScoutError("profile-intake requires --person or an active project")
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
                raise RoleScoutError(f"project not found: {args.project}")
            project = p
    task = args.task
    if not task and llm.mode(args.mock) == "live" and sys.stdin.isatty():
        task = input(f"task focus for '{args.workflow}' "
                     "(optional — Enter uses the skill's default behavior): ").strip() or None
    rec = run_workflow(args.workflow, project=project, task=task,
                       force_mock=args.mock, max_turns=args.max_turns)
    return _exit_code_for_status(rec["status"])

