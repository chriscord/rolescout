"""`rolescout web` — local web UI server (projects, profile, runs).

LOCAL only (127.0.0.1). The UI is a window onto the repo's real directory state:
persons = profiles/<person>/, projects = projects/<code>/ (with their stores,
strategy files, prep artifacts). Every button maps to the same skill+prompt
combination the CLI runs; writes go through the validated pipeline. One
workflow run at a time (a project ≈ one codex chat session).
"""

from __future__ import annotations

import csv
import json
import os
import re
import threading
import time
from datetime import date, datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from urllib.parse import parse_qs, urlparse

from .. import __version__, core, llm, profile_meta, project_meta
from ..paths import RoleScoutError, repo_root
from ..runner import preflight, workflows

UI_PATH = Path(__file__).parent / "ui.html"
_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]*$")
PREVIEW_DIRS = ("strategy", "resumes", "linkedin", "interviews", "applications",
                "targets", "profile", "data")
PREVIEW_SUFFIXES = {".md", ".txt", ".json", ".csv"}
CHAT_MAX_MESSAGES = 500
CHAT_TEXT_LIMIT = 8000
TRACKER_STATUS_ALIASES = {"dropped": "withdrawn", "drop": "withdrawn",
                          "removed": "withdrawn"}
TRACKER_STATUS_OUTCOME = {"accepted": "offer_accepted", "rejected": "rejected",
                          "withdrawn": "withdrawn"}


def _project_or_raise(code: str) -> Path:
    proj = repo_root() / "projects" / code
    if not (proj / "project.json").exists():
        raise RoleScoutError(f"project not found: {code}")
    return proj


def _chat_path(proj: Path) -> Path:
    return proj / "data" / "chat-session.json"


def _chat_stamp() -> tuple[str, str]:
    now = datetime.now().astimezone()
    return now.isoformat(timespec="seconds"), now.strftime("%H:%M:%S")


def _decode_best(raw: bytes) -> str:
    """Decode bytes as UTF-8, falling back to common OS codepages (Korean/JP/CN/
    Western Windows) so a legacy non-UTF-8 artifact renders instead of crashing or
    turning into mojibake. UTF-8 is always tried first."""
    for enc in ("utf-8", "cp949", "cp932", "cp936", "cp950", "cp1252"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def _read_chat_doc(proj: Path) -> dict:
    fp = _chat_path(proj)
    if not fp.exists():
        return {"version": 1, "messages": []}
    try:
        raw = fp.read_bytes()
    except OSError:
        return {"version": 1, "messages": []}
    # Prefer UTF-8, but recover chat files a prior version wrote in the OS default
    # codepage (e.g. cp949 on Korean Windows) so a legacy file doesn't crash reads —
    # the next append_chat_message() rewrites it as UTF-8 (self-migrating).
    text = _decode_best(raw)
    try:
        doc = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return {"version": 1, "messages": []}
    if not isinstance(doc.get("messages"), list):
        doc["messages"] = []
    doc["version"] = 1
    return doc


def append_chat_message(code: str, kind: str, text: str,
                        workflow: str = "", rid: str = "") -> dict:
    """Persist one project chat message, trimming from the oldest entries."""
    proj = _project_or_raise(code)
    at, ts = _chat_stamp()
    clean = str(text or "")
    if len(clean) > CHAT_TEXT_LIMIT:
        clean = clean[:CHAT_TEXT_LIMIT] + "\n[trimmed]"
    msg = {"id": f"{int(time.time() * 1000)}-{kind}",
           "at": at, "ts": ts, "kind": str(kind or "info"), "text": clean}
    if workflow:
        msg["workflow"] = workflow
    if rid:
        msg["rid"] = rid
    doc = _read_chat_doc(proj)
    doc["messages"].append(msg)
    doc["messages"] = doc["messages"][-CHAT_MAX_MESSAGES:]
    doc["updated_at"] = at
    fp = _chat_path(proj)
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return msg


def chat_history(code: str) -> dict:
    proj = _project_or_raise(code)
    doc = _read_chat_doc(proj)
    return {"messages": doc["messages"][-CHAT_MAX_MESSAGES:],
            "max_messages": CHAT_MAX_MESSAGES,
            "updated_at": doc.get("updated_at", "")}


def _run_user_text(workflow: str, task: str | None) -> str:
    task = (task or "").strip()
    return task if task else f"Run {workflow}"


# ---------------------------------------------------------------- run manager

class RunManager:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.runs: dict[str, dict] = {}
        self.order: list[str] = []

    def active_run(self, project: str | None = None) -> str | None:
        with self.lock:
            for rid in reversed(self.order):
                e = self.runs[rid]
                if e["status"] == "running" and (project is None or e["project"] == project):
                    return rid
        return None

    def start(self, workflow: str, task: str | None, force_mock: bool,
              project_code: str | None) -> str:
        if workflow not in workflows.WORKFLOW_SKILLS:
            raise RoleScoutError(f"unknown workflow {workflow!r}")
        if self.active_run():
            raise RoleScoutError("a run is already in progress — one at a time")
        project_path = None
        mode = llm.mode(force_mock)
        if project_code:
            project_dir = _project_or_raise(project_code)
            if mode == "live":
                project_path = project_dir
        rid = f"web-{datetime.now(timezone.utc).strftime('%H%M%S')}-{workflow}"
        entry = {"rid": rid, "workflow": workflow, "task": task or "",
                 "project": project_code or "", "mode": mode,
                 "status": "running", "started": time.time(), "events": [],
                 "_cancel": threading.Event(), "summary": ""}
        with self.lock:
            self.runs[rid] = entry
            self.order.append(rid)
        try:
            if project_code:
                # Chat-log persistence is best-effort: a failure here (e.g. a legacy
                # non-UTF-8 chat file, or a disk error) must NEVER block the workflow
                # or leave the run stuck registered as 'running'.
                try:
                    append_chat_message(project_code, "user",
                                        _run_user_text(workflow, task),
                                        workflow=workflow, rid=rid)
                except Exception:
                    pass
            if project_code and mode == "mock":
                self._append(entry, "info",
                             "mock mode: running on the disposable fixture project — "
                             "canned rows never touch your real project")
            threading.Thread(target=self._execute, args=(entry, force_mock, project_path),
                             daemon=True).start()
        except Exception as exc:
            # Anything that stops the worker from starting must mark the entry failed,
            # never leave it 'running' — otherwise the one-at-a-time gate blocks the UI
            # forever (active_run() only clears when nothing is 'running').
            with self.lock:
                entry["status"] = "failed"
                entry["summary"] = f"failed to start run: {exc}"
            raise RoleScoutError(f"failed to start run: {exc}") from exc
        return rid

    def _append(self, entry: dict, kind: str, text: str) -> None:
        with self.lock:
            entry["events"].append({"seq": len(entry["events"]), "kind": kind,
                                    "text": text, "ts": time.strftime("%H:%M:%S")})
        if entry.get("project"):
            try:
                append_chat_message(entry["project"], kind, text,
                                    workflow=entry.get("workflow", ""),
                                    rid=entry.get("rid", ""))
            except Exception:
                pass  # chat-log persistence is best-effort; never crash the run over it

    def _execute(self, entry: dict, force_mock: bool, project_path: Path | None) -> None:
        def on_event(kind, text, extra=None):
            self._append(entry, kind, text)

        try:
            rec = workflows.run_workflow(
                entry["workflow"], project=project_path, task=entry["task"] or None,
                force_mock=force_mock, on_event=on_event,
                cancel_event=entry["_cancel"])
            entry["summary"] = rec.get("summary", "")
            status = rec.get("status")
            entry["status"] = (status if status in ("failed", "blocked",
                                                    "partial", "cancelled")
                               else "done")
        except RoleScoutError as e:
            self._append(entry, "error", str(e))
            entry["status"] = "failed"
        except Exception as e:
            self._append(entry, "error", f"{type(e).__name__}: {e}")
            entry["status"] = "failed"
        self._append(entry, "done",
                     {"done": "Run completed.",
                      "partial": "Run completed with partial results.",
                      "blocked": "Run blocked before execution.",
                      "cancelled": "Run cancelled."}.get(entry["status"], "Run failed."))

    def decide(self, rid: str, approve: bool) -> bool:
        return False

    def cancel(self, rid: str) -> bool:
        """User stop button: cooperative cancel — the run halts at the next safe
        checkpoint (between events / on the next stream tick, never mid-write)."""
        with self.lock:
            entry = self.runs.get(rid)
            if entry is None or entry["status"] != "running":
                return False
            entry["_cancel"].set()
        self._append(entry, "info", "stop requested — cancelling at the next safe checkpoint")
        return True

    def run_view(self, rid: str, since: int = 0) -> dict | None:
        with self.lock:
            e = self.runs.get(rid)
            if e is None:
                return None
            return {"rid": rid, "workflow": e["workflow"], "status": e["status"],
                    "task": e["task"], "project": e["project"], "mode": e["mode"],
                    "pending": None, "summary": e["summary"],
                    "events": e["events"][since:], "n_events": len(e["events"])}

    def summaries(self, project: str | None = None) -> list[dict]:
        with self.lock:
            out = []
            for rid in reversed(self.order):
                e = self.runs[rid]
                if project is not None and e["project"] != project:
                    continue
                out.append({"rid": rid, "workflow": e["workflow"], "status": e["status"],
                            "project": e["project"], "summary": e["summary"][:160]})
            return out[:20]


MANAGER = RunManager()


class ProfileRunManager:
    """Background person-scoped intake runs.

    This is deliberately separate from MANAGER: profile intake must not trip the
    project-level "one run at a time" gate because search/score may start while
    profile building is still running.
    """

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.runs: dict[str, dict] = {}

    def active(self, person: str) -> bool:
        with self.lock:
            e = self.runs.get(person)
            return bool(e and e["status"] == "running")

    def start(self, person: str) -> bool:
        if not _SLUG.match(person):
            return False
        with self.lock:
            e = self.runs.get(person)
            if e and e["status"] == "running":
                return False
            self.runs[person] = {"person": person, "status": "running",
                                 "started": time.time(), "summary": "",
                                 "events": []}

        def worker() -> None:
            entry = self.runs[person]

            def on_event(kind, text, extra=None):
                with self.lock:
                    entry["events"].append({"kind": kind, "text": text,
                                            "ts": time.strftime("%H:%M:%S")})

            try:
                rec = workflows.run_profile_intake(person, on_event=on_event)
                with self.lock:
                    entry["summary"] = rec.get("summary", "")
                    status = rec.get("status")
                    entry["status"] = (status if status in ("failed", "blocked",
                                                            "partial", "cancelled")
                                       else "done")
            except Exception as e:
                with self.lock:
                    entry["summary"] = str(e)
                    entry["status"] = "failed"

        threading.Thread(target=worker, daemon=True).start()
        return True

    def summaries(self) -> dict:
        with self.lock:
            return {k: {"status": v["status"], "summary": v["summary"],
                        "started": v["started"]}
                    for k, v in self.runs.items()}


PROFILE_MANAGER = ProfileRunManager()


def _maybe_start_profile_intake(person: str) -> bool:
    pdir = repo_root() / "profiles" / person
    if not pdir.is_dir():
        return False
    has_material = bool(profile_meta.material_files(pdir))
    has_linkedin_pointer = bool(profile_meta.linkedin_url(pdir))
    if not (has_material or has_linkedin_pointer):
        return False
    return PROFILE_MANAGER.start(person)


def start_profile_intake(payload: dict) -> dict:
    """Explicit profile rebuild action for Profile/Fix-profile UI controls."""
    person = str(payload.get("person", "")).strip()
    if not _SLUG.match(person):
        raise RoleScoutError("person code must be a lowercase slug (a-z, 0-9, hyphen)")
    pdir = repo_root() / "profiles" / person
    if not pdir.is_dir():
        raise RoleScoutError(f"person '{person}' has no profile folder yet")
    has_material = bool(profile_meta.material_files(pdir))
    has_linkedin_pointer = bool(profile_meta.linkedin_url(pdir))
    if not (has_material or has_linkedin_pointer):
        raise RoleScoutError("add resume/materials or LinkedIn URL before rebuilding profile")
    started = PROFILE_MANAGER.start(person)
    status = "running" if PROFILE_MANAGER.active(person) else (
        PROFILE_MANAGER.summaries().get(person, {}).get("status", "idle"))
    return {"ok": True, "person": person, "profile_intake_started": started,
            "status": status}


# ---------------------------------------------------------------- collectors

def _csv_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _job_view_path(proj: Path) -> Path:
    visible = proj / "data" / "job_list.visible.csv"
    return visible if visible.exists() else proj / "data" / "job_list.csv"


def list_projects() -> list[dict]:
    root = repo_root()
    out = []
    for pj in sorted((root / "projects").glob("*/project.json")):
        proj = pj.parent
        try:
            doc = json.loads(pj.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        meta = project_meta.load(proj)
        jobs = _csv_rows(_job_view_path(proj))
        tracker = _csv_rows(proj / "data" / "tracker.csv")
        out.append({"code": proj.name, "person": doc.get("person", ""),
                    "focus": doc.get("focus", ""), "created_at": doc.get("created_at", ""),
                    "archived": bool(meta.get("archived")),
                    "targets_summary": project_meta.summary(proj),
                    "meta": {k: meta[k] for k in project_meta.DEFAULTS},
                    "n_jobs": len(jobs), "n_tracked": len(tracker),
                    "updated_at": meta.get("updated_at", doc.get("created_at", ""))})
    return out


def _inbox(proj: Path, code: str) -> list[dict]:
    """Change-feed: needs-action tracker rows + recent runs (archived-product style)."""
    items = []
    today = date.today().isoformat()
    for r in _csv_rows(proj / "data" / "tracker.csv"):
        if r.get("status") in ("accepted", "rejected", "withdrawn"):
            continue
        due = r.get("next_action_due", "")
        if due and due < today:
            items.append({"kind": "overdue", "title": f"{r.get('company')} — {r.get('title')}",
                          "text": f"next action overdue ({due}): {r.get('next_action')}"})
        elif not r.get("next_action", "").strip():
            items.append({"kind": "gap", "title": f"{r.get('company')} — {r.get('title')}",
                          "text": "active application without a next action"})
        elif due == today:
            items.append({"kind": "today", "title": f"{r.get('company')} — {r.get('title')}",
                          "text": f"due today: {r.get('next_action')}"})
    for s in MANAGER.summaries(project=code)[:5]:
        items.append({"kind": f"run-{s['status']}",
                      "title": f"{s['workflow']} run ({s['status']})",
                      "text": s["summary"]})
    return items


def _pretty_slug(text: str) -> str:
    return " ".join(p.capitalize() for p in re.split(r"[-_]+", text) if p)


def _rel_posix(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _safe_project_rel(rel: str) -> tuple[Path, str]:
    rel_norm = str(rel or "").replace("\\", "/")
    if re.match(r"^[A-Za-z]:", rel_norm):
        raise RoleScoutError("path escapes the project")
    pure = PurePosixPath(rel_norm)
    if pure.is_absolute() or not pure.parts:
        raise RoleScoutError("path escapes the project")
    if any(part in ("", ".", "..") for part in pure.parts):
        raise RoleScoutError("path escapes the project")
    return Path(*pure.parts), pure.as_posix()


def _prep_artifacts(proj: Path) -> list[dict]:
    # NOTE: prep-resume writes resumes/<group>/resume-draft.md (the validated
    # draft the DOCX is generated from). The legacy resume.md pattern is kept
    # for old projects; per (kind, target) the first match wins so the ready
    # badge / variant tabs update as soon as a draft exists.
    groups, seen = [], set()
    specs = (("Resume variants", "resume", "resumes/*/resume-draft.md"),
             ("Resume variants", "resume", "resumes/*/resume.md"),
             ("LinkedIn reviews", "linkedin", "linkedin/*/linkedin-review.md"),
             ("LinkedIn packets", "linkedin", "linkedin/*/update-packet.md"),
             ("Interview packets", "interview", "interviews/*/prep-notes.md"),
             ("Application instructions", "application",
              "applications/*/application-instructions.md"),
             ("Application instructions", "application",
              "applications/*/application-strategy.md"))
    for group, kind, pattern in specs:
        for f in sorted(proj.glob(pattern)):
            target = f.parent.name
            if (group, kind, target) in seen:
                continue
            seen.add((group, kind, target))
            groups.append({"group": group, "kind": kind,
                           "target": target, "label": _pretty_slug(target),
                           "path": _rel_posix(f, proj),
                           "mtime": datetime.fromtimestamp(f.stat().st_mtime)
                           .strftime("%Y-%m-%d %H:%M")})
    return groups


def _strategy_files(proj: Path) -> list[dict]:
    out = []
    for pattern in ("strategy/*.md", "strategy/*.json", "targets/job-groups/*.md"):
        for f in sorted(proj.glob(pattern)):
            out.append({"path": _rel_posix(f, proj)})
    return out


def _load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


# ---- story bank (independent, canonical per project; edited inline in the UI) ----
# Derived from the person's resume/evidence-map by prep-interview, but stored and
# managed as its own artifact under interviews/ so the UI can render it once at the
# bottom of the interview tab and let the user edit S/T/A/R/Best-for in place.
STORY_FIELDS = ("id", "title", "source", "situation", "task", "action",
                "result", "best_for", "ev_refs")
STORY_EDITABLE = ("situation", "task", "action", "result", "best_for")


def _story_bank_paths(proj: Path) -> tuple[Path, Path]:
    d = proj / "interviews"
    return d / "story-bank.json", d / "story-bank.md"


def _load_story_bank(proj: Path) -> list[dict]:
    jp, _ = _story_bank_paths(proj)
    data = _load_json(jp, None)
    entries = data.get("entries", []) if isinstance(data, dict) else (data or [])
    out = []
    for e in entries:
        if isinstance(e, dict) and e.get("id"):
            out.append({k: str(e.get(k, "")) for k in STORY_FIELDS})
    return out


def _render_story_bank_md(entries: list[dict], meta: str = "") -> str:
    head = "# Story Bank (canonical)\n"
    if meta:
        head += f"\n> {meta}\n"
    cols = ["ID", "Title", "Source", "S", "T", "A", "R", "Best for", "EV refs"]
    keys = ["id", "title", "source", "situation", "task", "action", "result",
            "best_for", "ev_refs"]
    lines = [head, "", "| " + " | ".join(cols) + " |",
             "|" + "|".join(["---"] * len(cols)) + "|"]
    for e in entries:
        cells = [str(e.get(k, "")).replace("\n", " ").replace("|", "\\|") for k in keys]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def story_bank(code: str) -> dict:
    proj = repo_root() / "projects" / code
    if not (proj / "project.json").exists():
        raise RoleScoutError(f"project not found: {code}")
    return {"code": code, "entries": _load_story_bank(proj)}


def save_story_bank_entry(code: str, payload: dict) -> dict:
    proj = (repo_root() / "projects" / code).resolve()
    if not (proj / "project.json").exists():
        raise RoleScoutError(f"project not found: {code}")
    sid = str(payload.get("id", "")).strip()
    if not sid:
        raise RoleScoutError("story id required")
    entries = _load_story_bank(proj)
    idx = next((i for i, e in enumerate(entries) if e["id"] == sid), -1)
    if idx < 0:
        raise RoleScoutError(f"story id not found: {sid}")
    for f in STORY_EDITABLE:
        if f in payload:
            entries[idx][f] = str(payload[f]).strip()
    jp, mp = _story_bank_paths(proj)
    jp.parent.mkdir(parents=True, exist_ok=True)
    existing = _load_json(jp, {}) if jp.exists() else {}
    meta = existing.get("meta", "") if isinstance(existing, dict) else ""
    jp.write_text(json.dumps({"meta": meta, "entries": entries}, indent=2,
                             ensure_ascii=False), encoding="utf-8")
    mp.write_text(_render_story_bank_md(entries, meta), encoding="utf-8")
    return {"ok": True, "id": sid, "entries": entries}


def _md_section(md: str, head_re: re.Pattern) -> str:
    """Body of the first markdown section whose heading matches head_re
    (up to the next heading of the same or higher level)."""
    lines = md.splitlines()
    start, level = -1, 0
    for i, line in enumerate(lines):
        m = re.match(r"^(#{1,4})\s+(.*)$", line)
        if m and head_re.search(m.group(2)):
            start, level = i + 1, len(m.group(1))
            break
    if start < 0:
        return ""
    out = []
    for line in lines[start:]:
        m = re.match(r"^(#{1,4})\s", line)
        if m and len(m.group(1)) <= level:
            break
        out.append(line)
    return "\n".join(out).strip()


def _first_sentence(text: str, cap: int = 160) -> str:
    text = " ".join(str(text).split())
    for stop in (". ", "; "):
        if stop in text:
            text = text.split(stop)[0] + "."
            break
    return text[:cap]


def _focused_ids(proj: Path) -> set:
    try:
        return set(json.loads((proj / "data" / "focused-jobs.json").read_text(
            encoding="utf-8")).get("job_ids", []))
    except (OSError, json.JSONDecodeError):
        return set()


def toggle_job_focus(code: str, payload: dict) -> dict:
    """Register/unregister a position as focused (prep-* skills operate on this set)."""
    proj = repo_root() / "projects" / code
    if not (proj / "project.json").exists():
        raise RoleScoutError(f"project not found: {code}")
    jid = str(payload.get("job_id", "")).strip()
    if not jid:
        raise RoleScoutError("job_id required")
    ids = _focused_ids(proj)
    focused = bool(payload.get("focused", jid not in ids))
    (ids.add(jid) if focused else ids.discard(jid))
    fp = proj / "data" / "focused-jobs.json"
    fp.parent.mkdir(parents=True, exist_ok=True)
    from datetime import date
    fp.write_text(json.dumps({"job_ids": sorted(ids),
                              "updated_at": date.today().isoformat()}, indent=1), encoding="utf-8")
    return {"job_id": jid, "focused": focused, "n_focused": len(ids)}


def add_manual_job(code: str, payload: dict) -> dict:
    """Add a manually-entered opening through the same validated job_list pipeline."""
    proj = repo_root() / "projects" / code
    if not (proj / "project.json").exists():
        raise RoleScoutError(f"project not found: {code}")
    required = ("company", "title", "location", "source_url")
    missing = [f for f in required if not str(payload.get(f, "")).strip()]
    if missing:
        raise RoleScoutError("manual job missing required field(s): " + ", ".join(missing))

    today = date.today().isoformat()
    row = {
        "captured_at": today,
        "company": str(payload.get("company", "")).strip(),
        "title": str(payload.get("title", "")).strip(),
        "job_group": str(payload.get("job_group", "")).strip(),
        "location": str(payload.get("location", "")).strip(),
        "remote_policy": str(payload.get("remote_policy", "unknown") or "unknown").strip(),
        "source_url": str(payload.get("source_url", "")).strip(),
        "job_page_url": str(payload.get("job_page_url", "")).strip(),
        "posting_status": str(payload.get("posting_status", "open") or "open").strip(),
        "seniority": str(payload.get("seniority", "")).strip(),
        "must_have_requirements": str(payload.get("must_have_requirements", "")).strip(),
        "nice_to_have_requirements": str(payload.get("nice_to_have_requirements", "")).strip(),
        "jd_summary": str(payload.get("jd_summary", "")).strip(),
        "fit_score": str(payload.get("fit_score", "")).strip(),
        "priority": str(payload.get("priority", "")).strip(),
        "notes": str(payload.get("notes", "Manual opening added from web UI.") or
                     "Manual opening added from web UI.").strip(),
        "last_seen_at": today,
    }

    env = {**os.environ, "RECRUITING_PROJECT_DIR": str(proj)}
    init = core.run_script("init_db", env=env)
    if init.returncode != 0:
        raise RoleScoutError("store init failed: " + (init.stdout + init.stderr).strip()[:400])

    data_dir = proj / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    raw_path = data_dir / "_manual_job_raw.json"
    norm_path = data_dir / "_manual_job_normalized.json"
    try:
        raw_path.write_text(json.dumps([row], indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        norm = core.run_script("normalize_job_url", "--json", str(raw_path), env=env)
        if norm.returncode != 0:
            raise RoleScoutError("URL normalization failed: " +
                                 (norm.stdout + norm.stderr).strip()[:400])
        try:
            rows = json.loads(norm.stdout)
        except json.JSONDecodeError as e:
            raise RoleScoutError(f"URL normalization returned invalid JSON: {e}") from e
        if not rows or not rows[0].get("job_id"):
            raise RoleScoutError("URL normalization did not produce a job_id")
        norm_path.write_text(json.dumps(rows, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        up = core.run_script("upsert_rows", "job_list", str(norm_path), env=env)
        if up.returncode != 0:
            raise RoleScoutError("manual job save refused: " +
                                 (up.stdout + up.stderr).strip()[:800])
        core.run_script("build_search_view", str(proj), env=env)
    finally:
        for p in (raw_path, norm_path):
            try:
                p.unlink()
            except OSError:
                pass

    saved = rows[0]
    snap_dir = proj / "targets" / "jobs"
    snap_dir.mkdir(parents=True, exist_ok=True)
    snap = {
        "source": "manual_add",
        "job_id": saved.get("job_id", ""),
        "snapshot_date": today,
        "url": saved.get("source_url", ""),
        "company": saved.get("company", ""),
        "title": saved.get("title", ""),
        "location": saved.get("location", ""),
        "jd_text": str(payload.get("jd_text", "")).strip() or "\n".join(
            v for v in (saved.get("jd_summary", ""),
                        saved.get("must_have_requirements", ""),
                        saved.get("nice_to_have_requirements", "")) if v),
        "row": saved,
    }
    (snap_dir / f"{saved['job_id']}.json").write_text(
        json.dumps(snap, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return {"ok": True, "job": saved}


def update_tracker_status(code: str, payload: dict) -> dict:
    """Update one application status through the validated tracker upsert path."""
    proj = repo_root() / "projects" / code
    if not (proj / "project.json").exists():
        raise RoleScoutError(f"project not found: {code}")
    app_id = str(payload.get("application_id", "")).strip()
    job_id = str(payload.get("job_id", "")).strip()
    raw_status = str(payload.get("status", "")).strip()
    status = TRACKER_STATUS_ALIASES.get(raw_status, raw_status)
    if status not in core.schema_defs.STATUSES:
        raise RoleScoutError(f"unknown tracker status: {raw_status}")

    tracker_path = proj / "data" / "tracker.csv"
    rows = _csv_rows(tracker_path)
    row = next((r for r in rows if app_id and r.get("application_id") == app_id), None)
    if row is None:
        row = next((r for r in rows if job_id and r.get("job_id") == job_id), None)
    if row is None:
        raise RoleScoutError("tracker row not found")

    updated = {c: row.get(c, "") for c in core.schema_defs.TRACKER_COLUMNS}
    today = date.today().isoformat()
    updated["status"] = status
    updated["last_updated"] = today
    if status == "applied" and not updated.get("applied_at"):
        updated["applied_at"] = today
    if status in TRACKER_STATUS_OUTCOME:
        updated["outcome"] = TRACKER_STATUS_OUTCOME[status]
    elif status not in ("accepted", "rejected", "withdrawn"):
        updated["outcome"] = ""

    env = {**os.environ, "RECRUITING_PROJECT_DIR": str(proj)}
    tmp_path = proj / "data" / "_tracker_status_update.json"
    try:
        tmp_path.write_text(json.dumps([updated], indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        up = core.run_script("upsert_rows", "tracker", str(tmp_path), env=env)
        if up.returncode != 0:
            raise RoleScoutError("tracker status update refused: " +
                                 (up.stdout + up.stderr).strip()[:800])
    finally:
        try:
            tmp_path.unlink()
        except OSError:
            pass

    fresh = _csv_rows(tracker_path)
    final = next((r for r in fresh if r.get("application_id") == updated["application_id"]),
                 updated)
    return {"ok": True, "row": final, "project": project_detail(code)}


def jobs_with_scores(proj: Path) -> list[dict]:
    """job_list rows + the 0–100 weighted score (strategy/job-scores.json) and a
    one-sentence rationale note (strategy/job-ratings.json). fit_score (1–5,
    coarse manual fit) is a DIFFERENT measure and stays as-is.

    app_status: once a tracker row moves past to_apply (applied, interviews,
    offer, …), the job row mirrors that application status — view-level join,
    the tracker stays the single source of truth."""
    scores = {s["job_id"]: s for s in _load_json(proj / "strategy" / "job-scores.json", [])}
    ratings = {r.get("job_id"): r for r in _load_json(proj / "strategy" / "job-ratings.json", [])}
    applied = {r["job_id"]: r.get("status", "")
               for r in _csv_rows(proj / "data" / "tracker.csv")
               if r.get("job_id") and r.get("status", "") not in ("", "to_apply")}
    rows = _csv_rows(_job_view_path(proj))
    focused = _focused_ids(proj)
    for row in rows:
        jid = row.get("job_id", "")
        row["focused"] = jid in focused
        row["app_status"] = applied.get(jid, "")
        s = scores.get(jid)
        row["score"] = s["score"] if s else ""
        row["suggested_priority"] = s["suggested_priority"] if s else ""
        rationale = (ratings.get(jid) or {}).get("rationale") or {}
        note = ""
        for crit in ("role_fit", "company_quality", "growth_path", "comp_potential"):
            if str(rationale.get(crit, "")).strip():
                note = _first_sentence(rationale[crit])
                break
        if not note and rationale:
            note = _first_sentence(next(iter(rationale.values())))
        row["score_note"] = note or row.get("notes", "")
    rows.sort(key=lambda r: (r["score"] == "", -(r["score"] or 0)
                             if isinstance(r["score"], (int, float)) else 0))
    return rows


def strategy_report(code: str) -> dict:
    """Organized strategy article: exec summary, priorities+why, per-group and
    per-company approach — composed from the run's real artifacts (no LLM at
    render time; the agent wrote these, the view organizes them)."""
    root = repo_root()
    proj = root / "projects" / code
    if not (proj / "project.json").exists():
        raise RoleScoutError(f"project not found: {code}")

    prio_md = ""
    prio_p = proj / "strategy" / "target-priorities.md"
    if prio_p.exists():
        prio_md = prio_p.read_text(encoding="utf-8")[:60000]
    # exec summary preference order:
    #   1. the one-paragraph "Executive summary" section of strategy/prep-strategy.md
    #      (prep-strategy writes it — ~3 sentences, the whole play in one look)
    #   2. fallback: content up to the second heading of target-priorities.md
    exec_summary_text = ""
    ps_p = proj / "strategy" / "prep-strategy.md"
    if ps_p.exists():
        exec_summary_text = _md_section(ps_p.read_text(encoding="utf-8")[:60000],
                                        re.compile(r"executive\s+summary", re.I))
    exec_summary, seen_heads = [], 0
    for line in prio_md.splitlines():
        if line.startswith("#"):
            seen_heads += 1
            if seen_heads > 1:
                break
        exec_summary.append(line)
    if exec_summary_text:
        exec_summary = exec_summary_text.splitlines()
    jobs = jobs_with_scores(proj)
    priorities = [{"company": r.get("company"), "title": r.get("title"),
                   "job_group": r.get("job_group"), "score": r.get("score"),
                   "priority": r.get("priority"),
                   "suggested_priority": r.get("suggested_priority"),
                   "note": r.get("score_note"), "url": r.get("job_page_url")
                   or r.get("source_url")}
                  for r in jobs if r.get("score") != ""]
    groups = [{"name": f.stem, "content": f.read_text(encoding="utf-8")[:20000]}
              for f in sorted(proj.glob("targets/job-groups/*.md"))]
    companies = []
    overrides = _load_json(proj / "strategy" / "overrides.json", [])
    return {"code": code,
            "has_content": bool(prio_md or priorities or groups or companies),
            "exec_summary_md": "\n".join(exec_summary).strip(),
            "priorities_md": prio_md,
            "priorities": priorities,
            "overrides": overrides,
            "groups": groups,
            "companies": companies}


def project_detail(code: str) -> dict:
    root = repo_root()
    proj = root / "projects" / code
    if not (proj / "project.json").exists():
        raise RoleScoutError(f"project not found: {code}")
    doc = json.loads((proj / "project.json").read_text(encoding="utf-8"))
    meta = project_meta.load(proj)
    pdir = preflight.profile_dir(proj)
    blocking, warnings = preflight.check("search", proj)
    return {"code": code, "person": doc.get("person", ""), "focus": doc.get("focus", ""),
            "meta": {k: meta[k] for k in project_meta.DEFAULTS} | {
                "updated_at": meta.get("updated_at", "")},
            "jobs": jobs_with_scores(proj),
            "tracker": _csv_rows(proj / "data" / "tracker.csv"),
            "inbox": _inbox(proj, code),
            "strategy_files": _strategy_files(proj),
            "prep": _prep_artifacts(proj),
            "story_bank": _load_story_bank(proj),
            "profile_ready": bool(pdir and (pdir / "candidate-profile.md").exists()),
            "preflight": {"blocking": blocking, "warnings": warnings},
            "runs": MANAGER.summaries(project=code),
            "active_run": MANAGER.active_run(project=code)}


def state() -> dict:
    root = repo_root()
    try:
        backend = llm.provider_choice()
    except RoleScoutError as e:
        backend = f"error: {e}"
    return {"version": __version__, "backend": backend,
            "persons": profile_meta.list_persons(root),
            "projects": list_projects(),
            "active_run": MANAGER.active_run(),
            "profile_runs": PROFILE_MANAGER.summaries(),
            "workflows": sorted(workflows.WORKFLOW_SKILLS)}


# ---------------------------------------------------------------- mutations

def create_person(payload: dict) -> dict:
    person = str(payload.get("person", "")).strip()
    if not _SLUG.match(person):
        raise RoleScoutError("person code must be a lowercase slug (a-z, 0-9, hyphen)")
    name = str(payload.get("name", "")).strip()
    if not name:
        raise RoleScoutError("display name is required")
    fields = {"name": name}
    if payload.get("linkedin_url"):
        fields["linkedin_url"] = profile_meta.normalize_linkedin_url(
            str(payload["linkedin_url"]))
    if payload.get("instructions") is not None:
        fields["instructions"] = str(payload["instructions"]).strip()
    pdir = repo_root() / "profiles" / person
    pdir.mkdir(parents=True, exist_ok=True)
    profile_meta.save(pdir, **fields)
    # allow explicit clearing of instructions
    if payload.get("instructions") == "":
        meta = profile_meta.load(pdir)
        meta["instructions"] = ""
        (pdir / profile_meta.META_NAME).write_text(
            json.dumps(meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    started = _maybe_start_profile_intake(person)
    return {"ok": True, "person": person, "profile_intake_started": started}


def create_project(payload: dict) -> dict:
    person = str(payload.get("person", "")).strip()
    focus = str(payload.get("focus", "")).strip()
    if not (_SLUG.match(person) and _SLUG.match(focus)):
        raise RoleScoutError("person/focus must be lowercase slugs")
    locations = project_meta._parse_list(payload.get("target_locations"))
    if not locations:
        raise RoleScoutError("target locations are required — they ground every search")
    if not (repo_root() / "profiles" / person).is_dir():
        raise RoleScoutError(f"person '{person}' has no profile yet — create it first")
    r = core.run_script("new_project", "--person", person, "--focus", focus)
    if r.returncode != 0:
        raise RoleScoutError(f"project creation failed: {(r.stdout + r.stderr)[:300]}")
    proj = repo_root() / "projects" / f"{person}--{focus}"
    project_meta.update(proj,
                        target_locations=locations,
                        focus_role=payload.get("focus_role"),
                        target_level=payload.get("target_level"),
                        target_companies=payload.get("target_companies"),
                        comp_range=payload.get("comp_range"),
                        negatives=payload.get("negatives"))
    return {"ok": True, "code": proj.name}


def update_project(code: str, payload: dict) -> dict:
    proj = repo_root() / "projects" / code
    if not (proj / "project.json").exists():
        raise RoleScoutError(f"project not found: {code}")
    allowed = {k: payload[k] for k in
              ("target_locations", "focus_role", "target_level", "target_companies",
                "comp_range", "search_runtime_profile", "search_view_filter_mode",
                "negatives", "archived") if k in payload}
    meta = project_meta.update(proj, **allowed)
    return {"ok": True, "meta": {k: meta[k] for k in project_meta.DEFAULTS}}


def archive_projects(payload: dict) -> dict:
    codes = payload.get("codes") or []
    archived = bool(payload.get("archived", True))
    done = []
    for code in codes:
        proj = repo_root() / "projects" / str(code)
        if (proj / "project.json").exists():
            project_meta.update(proj, archived=archived)
            done.append(str(code))
    return {"ok": True, "codes": done, "archived": archived}


def save_upload(person: str, filename: str, data: bytes) -> dict:
    if not _SLUG.match(person):
        raise RoleScoutError("bad person code")
    pdir = repo_root() / "profiles" / person
    if not pdir.is_dir():
        raise RoleScoutError(f"person '{person}' has no profile folder yet")
    name = re.sub(r"[^A-Za-z0-9._\- ]", "_", Path(filename).name) or "upload"
    if Path(name).suffix.lower() not in profile_meta.RESUME_SUFFIXES:
        raise RoleScoutError(f"unsupported file type: {name} "
                             f"(allowed: {sorted(profile_meta.RESUME_SUFFIXES)})")
    if len(data) > 15 * 1024 * 1024:
        raise RoleScoutError("file too large (15MB max)")
    (pdir / name).write_bytes(data)
    started = _maybe_start_profile_intake(person)
    return {"ok": True, "name": name, "size": len(data),
            "profile_intake_started": started}


def delete_material(payload: dict) -> dict:
    """Remove one user-supplied material file from profiles/<person>/ (local
    delete). Only files material_files() lists are removable — generated
    artifacts (candidate-profile.md, evidence-map.md, profile-meta.json) and
    anything outside the profile folder are refused."""
    person = str(payload.get("person", "")).strip()
    name = str(payload.get("name", "")).strip()
    if not _SLUG.match(person):
        raise RoleScoutError("bad person code")
    pdir = repo_root() / "profiles" / person
    removable = {f["name"] for f in profile_meta.material_files(pdir)}
    if name not in removable:
        raise RoleScoutError(f"not a removable material file: {name}")
    try:
        (pdir / name).unlink()
    except OSError as e:
        raise RoleScoutError(f"could not remove {name}: {e}") from e
    return {"ok": True, "removed": name}


def read_project_file(code: str, rel: str) -> dict:
    proj = (repo_root() / "projects" / code).resolve()
    rel_path, rel_display = _safe_project_rel(rel)
    target = (proj / rel_path).resolve()
    try:
        target.relative_to(proj)
    except ValueError:
        raise RoleScoutError("path escapes the project")
    if not any(rel_display.startswith(d + "/") or rel_display == d for d in PREVIEW_DIRS):
        raise RoleScoutError("preview limited to project artifact folders")
    if target.suffix.lower() not in PREVIEW_SUFFIXES:
        raise RoleScoutError("preview limited to text artifacts")
    if not target.is_file():
        raise RoleScoutError("file not found")
    text = _decode_best(target.read_bytes())
    return {"path": rel_display, "content": text[:40000],
            "truncated": len(text) > 40000}


def _parse_multipart(handler: BaseHTTPRequestHandler) -> tuple[dict, dict]:
    """Minimal multipart/form-data parser: returns (fields, files{name:(fn,bytes)})."""
    ctype = handler.headers.get("Content-Type", "")
    m = re.search(r"boundary=([^;]+)", ctype)
    if not m:
        raise RoleScoutError("multipart boundary missing")
    boundary = m.group(1).strip('"').encode()
    length = int(handler.headers.get("Content-Length", "0") or 0)
    body = handler.rfile.read(length)
    fields, files = {}, {}
    for part in body.split(b"--" + boundary):
        part = part.strip(b"\r\n")
        if not part or part == b"--":
            continue
        if b"\r\n\r\n" not in part:
            continue
        head, _, payload = part.partition(b"\r\n\r\n")
        head_text = head.decode(errors="replace")
        name_m = re.search(r'name="([^"]+)"', head_text)
        if not name_m:
            continue
        fn_m = re.search(r'filename="([^"]*)"', head_text)
        if fn_m and fn_m.group(1):
            files[name_m.group(1)] = (fn_m.group(1), payload)
        else:
            fields[name_m.group(1)] = payload.decode(errors="replace")
    return fields, files


# ---------------------------------------------------------------- http

class Handler(BaseHTTPRequestHandler):
    server_version = f"rolescout-web/{__version__}"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _json(self, obj, code: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        # live-polled data: any caching stalls the run feed (the ?since=0 URL repeats)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _guard(self, fn, *a):
        try:
            return self._json(fn(*a))
        except RoleScoutError as e:
            return self._json({"error": str(e)}, 400)
        except ValueError as e:
            return self._json({"error": str(e)}, 400)

    def do_GET(self):  # noqa: N802
        url = urlparse(self.path)
        q = parse_qs(url.query)
        if url.path in ("/", "/index.html"):
            body = UI_PATH.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if url.path == "/api/state":
            return self._json(state())
        if url.path.startswith("/api/project/") and url.path.endswith("/file"):
            code = url.path.split("/")[3]
            return self._guard(read_project_file, code, q.get("path", [""])[0])
        if url.path.startswith("/api/project/") and url.path.endswith("/strategy"):
            return self._guard(strategy_report, url.path.split("/")[3])
        if url.path.startswith("/api/project/") and url.path.endswith("/chat-log"):
            return self._guard(chat_history, url.path.split("/")[3])
        if url.path.startswith("/api/project/") and url.path.endswith("/story-bank"):
            return self._guard(story_bank, url.path.split("/")[3])
        if url.path.startswith("/api/project/"):
            return self._guard(project_detail, url.path.split("/")[3])
        if url.path.startswith("/api/runs/"):
            rid = url.path.split("/")[3]
            view = MANAGER.run_view(rid, int(q.get("since", ["0"])[0]))
            return self._json(view or {"error": "unknown run"}, 200 if view else 404)
        return self._json({"error": "not found"}, 404)

    def do_POST(self):  # noqa: N802
        url = urlparse(self.path)
        if url.path == "/api/profile/resume":
            try:
                fields, files = _parse_multipart(self)
                person = fields.get("person", "")
                if not files:
                    raise RoleScoutError("no file in upload")
                results = [save_upload(person, fn, data)
                           for fn, data in files.values()]
                return self._json({"ok": True, "files": results})
            except RoleScoutError as e:
                return self._json({"error": str(e)}, 400)
        length = int(self.headers.get("Content-Length", "0") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return self._json({"error": "bad json"}, 400)

        if url.path == "/api/profile":
            return self._guard(create_person, payload)
        if url.path == "/api/profile/intake":
            return self._guard(start_profile_intake, payload)
        if url.path == "/api/profile/material/delete":
            return self._guard(delete_material, payload)
        if url.path == "/api/projects":
            return self._guard(create_project, payload)
        if url.path == "/api/projects/archive":
            return self._guard(archive_projects, payload)
        if url.path.startswith("/api/project/") and url.path.endswith("/job-focus"):
            return self._guard(toggle_job_focus, url.path.split("/")[3], payload)
        if url.path.startswith("/api/project/") and url.path.endswith("/jobs/manual"):
            return self._guard(add_manual_job, url.path.split("/")[3], payload)
        if url.path.startswith("/api/project/") and url.path.endswith("/tracker/status"):
            return self._guard(update_tracker_status, url.path.split("/")[3], payload)
        if url.path.startswith("/api/project/") and url.path.endswith("/story-bank"):
            return self._guard(save_story_bank_entry, url.path.split("/")[3], payload)
        if url.path.startswith("/api/project/") and url.path.endswith("/meta"):
            return self._guard(update_project, url.path.split("/")[3], payload)
        if url.path == "/api/run":
            try:
                rid = MANAGER.start(payload.get("workflow", ""), payload.get("task"),
                                    bool(payload.get("mock")),
                                    payload.get("project") or None)
                return self._json({"rid": rid})
            except RoleScoutError as e:
                return self._json({"error": str(e)}, 409)
        if url.path.startswith("/api/runs/") and url.path.endswith("/decision"):
            rid = url.path.split("/")[3]
            ok = MANAGER.decide(rid, bool(payload.get("approve")))
            return self._json({"ok": ok}, 200 if ok else 409)
        if url.path.startswith("/api/runs/") and url.path.endswith("/cancel"):
            rid = url.path.split("/")[3]
            ok = MANAGER.cancel(rid)
            return self._json({"ok": ok}, 200 if ok else 409)
        return self._json({"error": "not found"}, 404)


def serve(port: int = 8787) -> ThreadingHTTPServer:
    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def main(args) -> int:
    httpd = serve(getattr(args, "port", 8787))
    url = f"http://127.0.0.1:{httpd.server_address[1]}"
    print(f"RoleScout web UI: {url}  (local only; Ctrl-C to stop)")
    backend = state()["backend"]
    print(f"backend: {backend}" + ("" if backend != "mock" else
          "  — mock mode (no codex/external CLI); runs use the disposable fixture project"))
    if not getattr(args, "no_open", False):
        import webbrowser
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    return 0
