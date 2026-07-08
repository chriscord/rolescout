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
import os
import shutil
import subprocess
import sys
import time
import csv
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from .. import core, llm
from ..paths import RoleScoutError, active_project_dir, home_dir, repo_root
from ..telemetry import store as tstore
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
    "apply": ["application-strategy", "application-tracker"],
}

# workflows whose scope is the user's focused positions (data/focused-jobs.json)
FOCUS_SCOPED = {"prep", "prep-strategy", "prep-resume", "prep-linkedin",
                "prep-interview"}
PROFILE_WORKFLOW = "profile-intake"


def focused_jobs(project: Path) -> list[dict]:
    """Focused positions joined with job_list rows (empty list when none)."""
    import csv as _csv
    fj = project / "data" / "focused-jobs.json"
    try:
        ids = set(json.loads(fj.read_text(encoding="utf-8")).get("job_ids", []))
    except (OSError, json.JSONDecodeError):
        ids = set()
    if not ids:
        return []
    out = []
    jl = project / "data" / "job_list.csv"
    if jl.exists():
        with open(jl, newline="", encoding="utf-8") as f:
            for r in _csv.DictReader(f):
                if r.get("job_id") in ids:
                    out.append({"job_id": r["job_id"], "company": r["company"],
                                "title": r["title"], "job_group": r.get("job_group", ""),
                                "url": r.get("job_page_url") or r.get("source_url", "")})
    return out

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
        path.write_text(ev.get("text", ""), encoding="utf-8")
    ctx.emit("artifact", rel)
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


def execute_events(ctx: RunContext, events: list[dict]) -> None:
    for ev in events:
        ctx.check_cancelled()  # between events: never mid-write
        t = ev.get("type")
        if t == "progress":
            if not ctx.streamed:  # streamed providers already showed these live
                ctx.emit("progress", ev.get("text", ""))
        elif t == "artifact":
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
    if ctx.workflow != "search":
        return
    r = core.run_script("grade_run", str(ctx.project), env=_env_for(ctx.project))
    out = (r.stdout + r.stderr).strip()
    ctx.validator_results.append({
        "validator": "grade_run[search]", "target": ctx.project.name,
        "returncode": r.returncode, "output": out[:800]})
    ctx.emit("validator", f"post-run grade_run: {'PASS' if r.returncode == 0 else 'FAIL'}")
    if r.returncode != 0:
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
        ctx.failure_class = ctx.failure_class or "linkedin_capture_failed"
        ctx.summary = "LinkedIn capture did not complete; follow the helper guidance and rerun."
    elif rc != 0:
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
                      on_stream) -> dict:
    ctx.emit("info", f"subagent start: {workflow}")
    envelope = provider.run(workflow, context, on_progress=on_stream)
    ctx.streamed = bool(envelope.get("streamed"))
    execute_events(ctx, envelope.get("events", []))
    ctx.emit("info", f"subagent done: {workflow}")
    return envelope


def _run_parallel_subagents(ctx: RunContext, provider, workflows_to_run: list[str],
                            context_for, on_stream) -> dict:
    if not workflows_to_run:
        return {"events": [], "usage": {}}
    if len(workflows_to_run) == 1:
        wf = workflows_to_run[0]
        return _execute_subagent(ctx, provider, wf, context_for(wf), on_stream)

    ctx.emit("info", "subagents start: " + ", ".join(workflows_to_run))
    results: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=len(workflows_to_run)) as pool:
        futures = {
            pool.submit(provider.run, wf, context_for(wf), on_stream): wf
            for wf in workflows_to_run
        }
        for fut in as_completed(futures):
            wf = futures[fut]
            results[wf] = fut.result()
    merged: dict = {"events": [], "usage": {}}
    for wf in workflows_to_run:
        envelope = results[wf]
        ctx.streamed = bool(envelope.get("streamed"))
        execute_events(ctx, envelope.get("events", []))
        _merge_usage(merged, envelope)
        ctx.emit("info", f"subagent done: {wf}")
    return merged


def _record_run(ctx: RunContext, run_id: str, started: str, t0: float,
                envelope: dict | None = None) -> dict:
    envelope = envelope or {}
    latency = round(time.monotonic() - t0, 2)
    status = "ok"
    if ctx.failure_class == "cancelled":
        status = "cancelled"
    elif ctx.failure_class:
        status = "failed"
    usage = envelope.get("usage", {})
    rec = {
        "run_id": run_id, "started_at": started, "finished_at": _now(),
        "workflow": ctx.workflow, "mode": ctx.mode, "project": ctx.project.name,
        "model_config": envelope.get("model_config", {}),
        "cost_usd": usage.get("cost_usd", 0), "tokens_in": usage.get("tokens_in", 0),
        "tokens_out": usage.get("tokens_out", 0), "latency_s": latency,
        "validator_results": ctx.validator_results,
        "failure_class": ctx.failure_class, "status": status, "summary": ctx.summary,
        "events": envelope.get("events", []),
    }
    return rec


def _run_prep_orchestration(ctx: RunContext, provider, context_for, stale_material: str,
                            on_stream, pdir: Path | None,
                            linkedin_source: Path | None) -> dict:
    """Full prep is an orchestrator, not a monolithic profile-building agent.

    Order: strategy first, resume + LinkedIn in parallel when LinkedIn current
    content exists, then interview after both downstream artifacts have had a
    chance to land.
    """
    envelope: dict = {"events": [], "usage": {}}

    strategy = _execute_subagent(
        ctx, provider, "prep-strategy",
        context_for("prep-strategy", stale_material), on_stream)
    _merge_usage(envelope, strategy)
    if ctx.failure_class:
        return envelope

    parallel = ["prep-resume"]
    from .. import profile_meta
    linkedin_url = profile_meta.linkedin_url(pdir)
    if linkedin_url and linkedin_source:
        rc = _run_linkedin_capture_helper(ctx, linkedin_url, linkedin_source,
                                          required=False)
        if rc == 0 and linkedin_source.exists() and linkedin_source.stat().st_size > 0:
            parallel.append("prep-linkedin")
        else:
            ctx.emit("info", "warning: LinkedIn current profile is unavailable; "
                     "skipping prep-linkedin. Add a LinkedIn URL and complete "
                     "the supported capture/import step, then rerun prep-linkedin.")
    else:
        ctx.emit("info", "warning: no LinkedIn URL/current profile; skipping "
                 "prep-linkedin. Add the URL in the profile form and complete "
                 "LinkedIn import/capture before LinkedIn review.")

    resume_linkedin = _run_parallel_subagents(
        ctx, provider, parallel,
        lambda wf: context_for(wf, stale_material),
        on_stream)
    _merge_usage(envelope, resume_linkedin)
    if ctx.failure_class:
        return envelope

    interview = _execute_subagent(
        ctx, provider, "prep-interview",
        context_for("prep-interview", stale_material), on_stream)
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
    if mode == "live":
        blocking, warnings = _pf.check(PROFILE_WORKFLOW, pdir)
        for w in warnings:
            _safe_print(f"  preflight WARN: {w}")
            if on_event is not None:
                try:
                    on_event("info", f"preflight WARN: {w}", None)
                except Exception:
                    pass
        if blocking:
            raise RoleScoutError(
                "not ready for a live run:\n  - " + "\n  - ".join(blocking))

    provider = llm.get_provider(force_mock)
    ctx = RunContext(PROFILE_WORKFLOW, pdir, mode)
    ctx.on_event = on_event
    ctx.cancel_event = cancel_event
    run_id = tstore.new_run_id()
    started = _now()
    t0 = time.monotonic()
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

            score_envelope = _execute_subagent(
                score_ctx, provider, "score", _score_context("score", ""), _on_stream)
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

    mode = llm.mode(force_mock)
    run_id = tstore.new_run_id()
    if mode == "mock" and project is None:
        project = make_mock_project(run_id)
        print(f"mock mode: disposable fixture project at {project}")
    elif project is None:
        project = active_project_dir()
        if project is None:
            raise RoleScoutError("no active project — run `rolescout init` first")

    if mode == "live":
        from . import preflight
        blocking, warnings = preflight.check(workflow, project)
        for w in warnings:
            _safe_print(f"  preflight WARN: {w}")
            if on_event is not None:   # web feed must see warnings too
                try:
                    on_event("info", f"preflight WARN: {w}", None)
                except Exception:
                    pass
        if blocking:
            raise RoleScoutError(
                "not ready for a live run:\n  - " + "\n  - ".join(blocking))

    provider = llm.get_provider(force_mock)
    ctx = RunContext(workflow, project, mode)
    ctx.on_event = on_event
    ctx.cancel_event = cancel_event
    started = _now()
    t0 = time.monotonic()
    ctx.emit("info", f"run {run_id}: workflow={workflow} mode={mode} "
             f"project={project.name} backend={provider.name}")
    from .. import profile_meta, project_meta
    from . import preflight as _pf
    pdir = _pf.profile_dir(project)
    linkedin_source = pdir / "linkedin-current.md" if pdir else None

    def _on_stream(text: str) -> None:
        ctx.check_cancelled()  # providers stream through here — kills the agent call
        ctx.emit("stream", text)

    def _provider_context(active_workflow: str, stale_material: str) -> dict:
        return {
            "project": str(project), "task": task,
            "skills": WORKFLOW_SKILLS[active_workflow], "max_turns": max_turns,
            "focused_jobs": focused_jobs(project) if active_workflow in FOCUS_SCOPED else None,
            "profile_stale": stale_material if active_workflow in FOCUS_SCOPED else "",
            "profile_ready": _profile_ready(pdir),
            "linkedin_url": profile_meta.linkedin_url(pdir),
            "profile_dir": str(pdir) if pdir else "",
            "linkedin_source_path": str(linkedin_source) if linkedin_source else "",
            "targets": project_meta.targets_text(project),
            "instructions": profile_meta.instructions(pdir),
        }

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
        if workflow == "prep":
            envelope = _run_prep_orchestration(
                ctx, provider, _provider_context, stale_material,
                _on_stream, pdir, linkedin_source)
        else:
            envelope = _execute_subagent(
                ctx, provider, workflow,
                _provider_context(workflow, stale_material), _on_stream)
        if workflow == "search" and not ctx.failure_class:
            ctx.emit("info", "search complete; running score once")
            score_envelope = _execute_subagent(
                ctx, provider, "score",
                _provider_context("score", stale_material), _on_stream)
            _merge_usage(envelope, score_envelope)
            if ctx.summary and not ctx.summary.startswith("search + "):
                ctx.summary = f"search + {ctx.summary}"
        _post_run_checks(ctx)
    except RunCancelled:
        ctx.failure_class = "cancelled"
        if not ctx.summary:
            ctx.summary = "Run cancelled by user."
        ctx.emit("info", "run cancelled — the agent was stopped")
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
        return 0 if rec["status"] == "ok" else 1

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
    return 0 if rec["status"] != "failed" else 1
