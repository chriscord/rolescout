#!/usr/bin/env python3
"""Focused self-test for deterministic search.

Uses only stdlib and a temporary project with a local HTTP server. It exercises
the end-to-end path without mutating a real RoleScout project or using the
network.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import build_search_view
from rolescout import core, project_meta
from rolescout.runner.workflows import run_workflow
from rolescout.search.deterministic import _company_set_fingerprint, _infer_location_from_jd


JD = """
Lead Strategy Manager

Location: San Francisco, California

Responsibilities include leading strategic partnerships, business development,
executive planning, market analysis, partner operations, and cross-functional
strategy programs for a high-growth technology platform.

Minimum qualifications require 8 years of experience in strategy, partnerships,
business development, corporate development, investment analysis, or business
operations. Candidates must have proven ability to lead executive-level work,
structure ambiguous problems, communicate with senior stakeholders, and manage
complex partner initiatives from idea through launch.

Preferred qualifications include AI platform experience, enterprise go-to-market
work, investment or M&A exposure, marketplace experience, and comfort working
with product, finance, legal, and operations teams.
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        return

    def do_GET(self) -> None:
        if self.path == "/careers":
            body = (
                '<html><body><header>Cookie Policy Job Alerts</header>'
                '<main><a href="/jobs/123456-lead-strategy-manager">'
                "Lead Strategy Manager</a></main><footer>Privacy Terms</footer></body></html>"
            )
        elif self.path == "/jobs/123456-lead-strategy-manager":
            body = (
                "<html><body><header>Cookie Policy Job Alerts Similar Jobs</header>"
                f"<main><article class='job-description'>{JD}</article></main>"
                "<footer>Privacy Terms Create Account</footer></body></html>"
            )
        else:
            self.send_response(404)
            self.end_headers()
            return
        raw = body.encode("utf-8")
        self.send_response(200)
        self.send_header("content-type", "text/html; charset=utf-8")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def _write(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, str):
        path.write_text(payload, encoding="utf-8")
    else:
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _expect(actual: str, expected: str, label: str) -> bool:
    if actual == expected:
        return True
    print(f"FAIL: {label}: expected {expected!r}, got {actual!r}", file=sys.stderr)
    return False


def main() -> int:
    job_url_policy = core.load("job_url_policy")
    location_normalize = core.load("location_normalize")
    normalize_job_url = core.load("normalize_job_url")
    if not job_url_policy.is_direct_posting_url(
        "https://jobs.lever.co/example/12345678-1234-1234-1234-123456789abc"
    ):
        print("FAIL: Lever detail URL should be accepted as direct", file=sys.stderr)
        return 1
    if job_url_policy.is_direct_posting_url("https://jobs.lever.co/example"):
        print("FAIL: Lever listing URL should not be accepted as direct", file=sys.stderr)
        return 1
    if not job_url_policy.is_direct_posting_url(
        "https://databricks.com/company/careers/open-positions/job?gh_jid=6918763002"
    ):
        print("FAIL: query-id career URL should be accepted as direct", file=sys.stderr)
        return 1
    canonical_url = normalize_job_url.canonicalize(
        "https://boards.greenhouse.io/robinhood/jobs/6669758?t=gh_src%3D&gh_jid=6669758"
    )
    if canonical_url != "https://boards.greenhouse.io/robinhood/jobs/6669758?gh_jid=6669758":
        print(f"FAIL: tracking t=gh_src query should be stripped: {canonical_url}", file=sys.stderr)
        return 1
    risky_tokens = location_normalize.RISKY_LOCATION_ALIAS_TOKENS
    if tuple(sorted(risky_tokens)) != tuple(risky_tokens):
        print(f"FAIL: risky location tokens are not sorted: {risky_tokens}", file=sys.stderr)
        return 1
    expected_risky_tokens = ("from", "in", "into", "over", "that", "this", "with")
    if risky_tokens != expected_risky_tokens:
        print(f"FAIL: risky location tokens changed: {risky_tokens}", file=sys.stderr)
        return 1
    location_cases = {
        "Atlanta, GA, USA; Boulder, CO, USA": "Atlanta, USA; Boulder, USA",
        "Remote - US; New York City; San Francisco; Seattle; Washington, DC": (
            "New York, USA; San Francisco, USA; Seattle, USA; USA; Washington, USA"
        ),
        "Remote in United States": "USA",
        "San Francisco, CA": "San Francisco, USA",
        "San Jose, CA, USA": "San Jose, USA",
        "CA, USA": "USA",
        "California, USA": "USA",
        "New York, USA": "New York, USA",
        "Washington, USA": "Washington, USA",
        "-, USA": "USA",
    }
    for raw, expected in location_cases.items():
        if not _expect(location_normalize.normalize_location_value(raw), expected, raw):
            return 1
    jd_location_cases = {
        "google locations": (
            "Note: By applying to this position you will have an opportunity to "
            "share your preferred working location from the following: Atlanta, "
            "GA, USA; Boulder, CO, USA .\nMinimum qualifications:",
            "Atlanta, USA; Boulder, USA",
        ),
        "openai remote us": (
            "Remote - US; New York City; San Francisco; Seattle; Washington, DC",
            "New York, USA; San Francisco, USA; Seattle, USA; USA; Washington, USA",
        ),
        "stripe remote united states": (
            "A remote location is defined as being 35 miles from one of our offices.\n"
            "Remote locations\nRemote in United States\nTeam\nMarketing",
            "USA",
        ),
        "risky prepositions are not countries": (
            "Remote locations\n"
            "In this role, work with partners from many teams over time.\n"
            "That context is not a job location.\n"
            "Team\nMarketing",
            "",
        ),
    }
    for label, (text, expected) in jd_location_cases.items():
        if not _expect(_infer_location_from_jd(text), expected, label):
            return 1
    view_plan = {
        "location_filter": {
            "target_cities": ["San Francisco"],
            "target_countries": ["USA"],
            "include_remote": True,
        },
    }
    view_location_cases = [
        ({"location": "", "remote_policy": "remote"}, (True, ""), "remote missing location"),
        ({"location": "Remote", "remote_policy": "remote"}, (True, ""), "remote generic location"),
        ({"location": "Worldwide", "remote_policy": "remote"}, (True, ""), "remote unknown country"),
        ({"location": "USA", "remote_policy": "remote"}, (True, ""), "remote target country"),
        (
            {"location": "San Francisco Bay Area", "remote_policy": "unknown"},
            (True, ""),
            "sf bay area matches san francisco target",
        ),
        (
            {"location": "Tokyo, Japan", "remote_policy": "remote"},
            (False, "location_mismatch"),
            "remote non-target country",
        ),
        (
            {"location": "London, United Kingdom", "remote_policy": "remote"},
            (False, "location_mismatch"),
            "remote non-target country uk",
        ),
        ({"location": "", "remote_policy": "unknown"}, (False, "missing_location"), "nonremote missing"),
    ]
    for row, expected, label in view_location_cases:
        actual = build_search_view._matches_location_filter(row, view_plan)
        if actual != expected:
            print(f"FAIL: {label}: expected {expected!r}, got {actual!r}", file=sys.stderr)
            return 1
    level_plan = {
        "level_filter": {
            "negative_terms": ["executive", "senior director"],
            "protected_phrases": [],
        }
    }
    sanitized, _ = build_search_view._sanitize_filter_plan(
        project=ROOT / "rolescout" / "fixtures" / "mock-project",
        plan={
            "schema": "rolescout-search-view-filter-plan-v1",
            "target_level": "senior manager, lead",
            "target_locations": ["San Francisco"],
            "location_filter": view_plan["location_filter"],
            "level_filter": level_plan["level_filter"],
        },
    )
    if "executive" in sanitized["level_filter"]["negative_terms"]:
        print("FAIL: executive should be removed from negative level terms", file=sys.stderr)
        return 1
    ok, reason = build_search_view._matches_level_filter(
        {"title": "Senior Strategy and Executive Operations Manager, Media"},
        sanitized,
    )
    if not ok:
        print(f"FAIL: executive operations title should not be excluded: {reason}", file=sys.stderr)
        return 1

    with tempfile.TemporaryDirectory(prefix="rolescout-search-test-") as td:
        project = Path(td) / "projects" / "tester--strategy"
        shutil.copytree(ROOT / "rolescout" / "fixtures" / "mock-project", project)
        _write(project / "project-meta.json", {
            "target_locations": ["San Francisco"],
            "focus_role": "Strategy, Business Development",
            "target_level": "lead",
            "target_companies": ["ExampleCo"],
            "negatives": [],
            "search_view_filter_mode": "deterministic",
            "schedules": [],
            "archived": False,
        })

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{port}"
            _write(project / "targets" / "company-universe.json", {
                "generated_at": "2026-07-10",
                "project": project.name,
                "preference_fingerprint": project_meta.preference_fingerprint(project),
                "buckets": [{
                    "bucket": "fixture",
                    "why_relevant": "fixture",
                    "companies": [{
                        "name": "ExampleCo",
                        "seed": True,
                        "rationale": "fixture company",
                    }],
                }],
                "excluded": [],
            })
            _write(project / "targets" / "source-plan.json", {
                "generated_at": "2026-07-10",
                "project": project.name,
                "company_set_fingerprint": _company_set_fingerprint([{"name": "ExampleCo"}]),
                "companies": [{
                    "name": "ExampleCo",
                    "sources": [{
                        "type": "official_careers",
                        "url": base + "/careers",
                        "status": "planned",
                    }],
                    "fallbacks_used": [],
                }],
            })
            rec = run_workflow(
                "search",
                project=project,
                telemetry_path=project / "data" / "runs.jsonl",
            )
            if rec.get("status") not in {"ok", "partial"}:
                print(f"FAIL: runner search status {rec.get('status')}: {rec}", file=sys.stderr)
                return 1
            summary_path = project / "targets" / "deterministic-search" / "summary.json"
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            if summary.get("kept_rows") != 1:
                print(f"FAIL: expected 1 kept row, got {summary}", file=sys.stderr)
                return 1
            if summary.get("llm_tokens_in") != 0 or summary.get("llm_tokens_out") != 0:
                print(f"FAIL: deterministic search should remain zero-token: {summary}", file=sys.stderr)
                return 1
            if summary.get("runtime_profile") != "fast":
                print(f"FAIL: default runtime profile should be fast: {summary}", file=sys.stderr)
                return 1
            if summary.get("jd_extraction_workers", 0) < 1:
                print(f"FAIL: expected concurrent JD extraction metrics: {summary}", file=sys.stderr)
                return 1
            if "browser_concurrency" in summary:
                print(f"FAIL: old env-style browser_concurrency key remains: {summary}", file=sys.stderr)
                return 1
            view_summary = json.loads(
                (project / "targets" / "search-view-summary.json").read_text(encoding="utf-8")
            )
            if view_summary.get("visible_rows") != 1:
                print(f"FAIL: expected 1 visible row, got {view_summary}", file=sys.stderr)
                return 1
            import sqlite3
            con = sqlite3.connect(project / "data" / "public-opportunities.db")
            try:
                persisted = con.execute("SELECT title FROM job_list").fetchall()
            finally:
                con.close()
            if ("Lead Strategy Manager",) not in persisted:
                print("FAIL: public opportunities store missing persisted row", file=sys.stderr)
                return 1
            snapshots = list((project / "targets" / "jobs").glob("*.json"))
            if len(snapshots) != 1:
                print("FAIL: expected one JD snapshot", file=sys.stderr)
                return 1
            snapshot = json.loads(snapshots[0].read_text(encoding="utf-8"))
            if snapshot.get("extraction_method") != "static_fetch_readable_dom":
                print(f"FAIL: expected static DOM extraction, got {snapshot}", file=sys.stderr)
                return 1
            if "Cookie Policy" in snapshot.get("jd_text", ""):
                print("FAIL: noisy header text leaked into JD snapshot", file=sys.stderr)
                return 1
            if any("playwright" in item for item in snapshot.get("fallback_history", [])):
                print("FAIL: browser fallback should not run when static DOM extraction succeeds", file=sys.stderr)
                return 1
        finally:
            server.shutdown()
            server.server_close()
    print("PASS: deterministic search self-test")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
