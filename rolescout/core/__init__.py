"""rolescout.core — import-path shims over the prototype's scripts/ (no logic moves).

The deterministic scripts in <repo>/scripts/ ARE the product core (product-spec §4).
This module loads them by file path and registers them under their bare names so
their internal imports (`import store_io`, `from schema_defs import ...`) keep
working exactly as they do when run from the repo. Semantics are never modified
here — dev-plan §0 forbids it.

Two access styles:
  - library:   core.store_io, core.schema_defs, core.normalize_job_url ... (lazy attrs)
  - CLI-style: core.run_script("validate_job_rows", rows_json, "--existing", csv)
    (subprocess with the same interpreter — preserves exit codes and stdout contracts)
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

from ..paths import repo_root

SCRIPT_NAMES = [
    "schema_defs", "store_io", "normalize_job_url", "job_url_policy",
    "location_normalize", "jd_text_cleaner", "validate_job_rows",
    "validate_tracker_rows", "validate_resume_bullets", "score_jobs",
    "validate_resume_tailoring", "grade_run", "qa_static", "upsert_rows",
    "init_db", "new_project", "ensure_recruiting_sheet",
    "capture_linkedin_profile", "resolve_company_sources",
    "build_location_search_urls",
    "merge_research_parts", "persist_job_rows", "probe_linkedin_jobs",
    "deterministic_search", "reconcile_job_lifecycle", "finalize_score",
    "location_eligibility", "validate_linkedin_review",
    "validate_interview_prep", "render_docx_gate", "build_resume_docx",
    "generate_coverage_audit", "analyze_search_plan", "analyze_search_coverage",
    "validate_application_packets",
]

_loaded: dict[str, ModuleType] = {}


def scripts_dir() -> Path:
    return repo_root() / "scripts"


def load(name: str) -> ModuleType:
    """Load scripts/<name>.py once, registered so intra-script imports resolve."""
    if name in _loaded:
        return _loaded[name]
    if name not in SCRIPT_NAMES:
        raise AttributeError(f"unknown core script: {name}")
    sdir = scripts_dir()
    if str(sdir) not in sys.path:
        sys.path.insert(0, str(sdir))
    if name in sys.modules:  # already imported via sys.path by a sibling script
        _loaded[name] = sys.modules[name]
        return _loaded[name]
    spec = importlib.util.spec_from_file_location(name, sdir / f"{name}.py")
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load scripts/{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    _loaded[name] = mod
    return mod


def __getattr__(name: str) -> ModuleType:
    if name in SCRIPT_NAMES:
        return load(name)
    raise AttributeError(name)


def run_script(name: str, *args: str, env: dict | None = None,
               capture: bool = True, cwd: Path | None = None) -> subprocess.CompletedProcess:
    """Run scripts/<name>.py as the prototype intends (own process, own exit code)."""
    cmd = [sys.executable, str(scripts_dir() / f"{name}.py"), *[str(a) for a in args]]
    run_env = {**os.environ, **(env or {})}
    run_env.setdefault("PYTHONIOENCODING", "utf-8")
    return subprocess.run(cmd, capture_output=capture, text=True,
                          encoding="utf-8", errors="replace", env=run_env,
                          cwd=str(cwd or repo_root()))
