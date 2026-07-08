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
from datetime import datetime, timezone
from pathlib import Path

from .. import core, llm
from ..paths import RoleScoutError, active_project_dir, home_dir, repo_root
from ..telemetry import store as tstore
WORKFLOW_SKILLS = {
    "search": ["job-opening-research", "target-job-group-strategy"],
    "prep": [
        "candidate-profile-builder",
        "prep-strategy",
        "prep-resume",
        "prep-linkedin",
        "prep-interview",
    ],
    "score": ["target-job-group-strategy"],
    "prep-strategy": ["prep-strategy"],
    "prep-resume": ["candidate-profile-builder", "prep-resume"],
    "prep-linkedin": ["prep-linkedin"],
    "prep-interview": ["prep-interview"],
    "apply": ["application-strategy", "application-tracker"],
}

# workflows whose scope is the user's focused positions (data/focused-jobs.json)
FOCUS_SCOPED = {"prep", "prep-strategy", "prep-resume", "prep-linkedin",
                "prep-interview"}


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


def _env_for(project: Path) -> dict:
    return {**os.environ, "RECRUITING_PROJECT_DIR": str(project)}


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
    rel = ev["path"]
    path = ctx.project / rel
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
    if ctx.workflow in ("prep", "prep-resume") and ctx.mode == "live":
        # a replaced resume must have forced a profile rebuild — verify, don't trust
        from . import preflight as _pf
        pdir = _pf.profile_dir(ctx.project)
        stale = _pf._stale_profile(pdir)
        ctx.validator_results.append({
            "validator": "profile_freshness", "target": str(pdir or ""),
            "returncode": 1 if stale else 0,
            "output": (f"STALE: '{stale}' is still newer than candidate-profile.md — "
                       "the run did not rebuild the profile/evidence map"
                       if stale else "candidate-profile.md newer than all materials")})
        ctx.emit("validator",
                 f"post-run profile freshness: {'FAIL' if stale else 'PASS'}")
        if stale:
            ctx.failure_class = ctx.failure_class or "stale_profile_not_rebuilt"
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
                                 source_path: Path) -> int:
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
    if rc != 0:
        ctx.failure_class = ctx.failure_class or "linkedin_capture_failed"
        ctx.summary = "LinkedIn capture did not complete; follow the helper guidance and rerun."
    return rc


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


def run_workflow(workflow: str, project: Path | None = None, task: str | None = None,
                 force_mock: bool = False, max_turns: int = 40,
                 telemetry_path: Path | None = None, on_event=None,
                 cancel_event=None) -> dict:
    """Programmatic entry (CLI and web). Returns the telemetry record.

    on_event(kind, text, extra)   — mirrors everything printed (web UI event feed).
    cancel_event (threading.Event)— cooperative stop: the run halts at the next
                                    safe checkpoint and is recorded as 'cancelled'."""
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
            print(f"  preflight WARN: {w}")
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
    linkedin_url = profile_meta.linkedin_url(pdir)
    profile_ready = bool(pdir and (pdir / "candidate-profile.md").exists())

    def _on_stream(text: str) -> None:
        ctx.check_cancelled()  # providers stream through here — kills the agent call
        ctx.emit("stream", text)

    def _provider_context(active_workflow: str, stale_material: str) -> dict:
        return {
            "project": str(project), "task": task,
            "skills": WORKFLOW_SKILLS[active_workflow], "max_turns": max_turns,
            "focused_jobs": focused_jobs(project) if active_workflow in FOCUS_SCOPED else None,
            "profile_stale": stale_material if active_workflow in ("prep", "prep-resume") else "",
            "profile_ready": profile_ready,
            "linkedin_url": linkedin_url,
            "profile_dir": str(pdir) if pdir else "",
            "linkedin_source_path": str(linkedin_source) if linkedin_source else "",
            "targets": project_meta.targets_text(project),
            "instructions": profile_meta.instructions(pdir),
        }

    envelope: dict = {"events": []}
    try:
        if mode == "live" and workflow == "prep-linkedin":
            if linkedin_url and linkedin_source:
                rc = _run_linkedin_capture_helper(ctx, linkedin_url, linkedin_source)
                if rc != 0:
                    rec = _record_run(ctx, run_id, started, t0, {"events": []})
                    tstore.record_run(rec, path=telemetry_path)
                    ctx.emit("info", f"telemetry: run {run_id} recorded "
                             f"({len(ctx.validator_results)} validator result(s), "
                             f"status={rec['status']})")
                    return rec

        ctx.check_cancelled()
        stale_material = _pf._stale_profile(pdir) if pdir else ""
        envelope = provider.run(workflow, _provider_context(workflow, stale_material),
                                on_progress=_on_stream)
        ctx.streamed = bool(envelope.get("streamed"))
        execute_events(ctx, envelope.get("events", []))
        if workflow == "search" and not ctx.failure_class:
            ctx.emit("info", "search complete; running score once")
            score_envelope = provider.run("score", _provider_context("score", stale_material),
                                          on_progress=_on_stream)
            envelope.setdefault("events", []).extend(score_envelope.get("events", []))
            usage = envelope.setdefault("usage", {})
            score_usage = score_envelope.get("usage", {})
            for key, value in score_usage.items():
                if isinstance(value, (int, float)):
                    usage[key] = usage.get(key, 0) + value
            execute_events(ctx, score_envelope.get("events", []))
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
