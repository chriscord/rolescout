#!/usr/bin/env python3
"""Observe LinkedIn Jobs availability and record the attempt in research-log.

This helper is runner-owned: it avoids asking a Codex search agent to spawn a
browser from inside its own sandbox. It never enters credentials, submits forms,
messages anyone, or exports bulk data. When the browser path is unavailable it
records a structured connector_error instead of letting the search agent invent
ad-hoc browser code.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path


LINKEDIN_JOBS_URL = "https://www.linkedin.com/jobs/"


def _configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def has_playwright() -> bool:
    try:
        return importlib.util.find_spec("playwright.sync_api") is not None
    except ModuleNotFoundError:
        return False


def classify_observation(url: str, title: str, text: str) -> str:
    url_lower = str(url or "").lower()
    title_lower = str(title or "").lower()
    text_lower = str(text or "").lower()
    lower = "\n".join([url_lower, title_lower, text_lower])
    if "checkpoint" in lower or "security verification" in lower or "captcha" in lower:
        return "verification_prompt"

    login_route = any(marker in url_lower for marker in (
        "/login", "/uas/login", "session_redirect",
    ))
    jobs_route = "linkedin.com/jobs" in url_lower
    jobs_signal = (
        jobs_route
        and (
            "jobs" in title_lower
            or "/jobs/search" in url_lower
            or re.search(r"\b\d[\d,]*(?:\+)?\s+(?:jobs?|results?)\b", text_lower)
            or "recommended jobs" in text_lower
            or "jobs in " in text_lower
        )
    )
    if jobs_signal and not login_route:
        return "jobs_page_ok"

    login_form = (
        ("email or phone" in text_lower and "password" in text_lower)
        or "sign in to linkedin" in text_lower
        or "join linkedin to" in text_lower
    )
    if login_route or login_form:
        return "signed_out"
    if "linkedin.com" in url_lower:
        return "authwall"
    return "connector_error: unexpected navigation result"


def _research_log_path(project: Path) -> Path:
    return project / "targets" / "research-log.json"


def _load_log(path: Path) -> dict:
    if not path.exists():
        return {
            "schema": "research-log-v1",
            "queries": [],
            "candidates": [],
        }
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("research-log.json must be an object")
    data.setdefault("schema", "research-log-v1")
    data.setdefault("queries", [])
    data.setdefault("candidates", [])
    if not isinstance(data["queries"], list):
        raise ValueError("research-log queries must be a list")
    return data


def record_observation(project: Path, observed: str, note: str = "") -> None:
    path = _research_log_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = _load_log(path)
    data["queries"].append({
        "scope": "LinkedIn Jobs",
        "attempt": "navigation",
        "observed": observed,
        "q": LINKEDIN_JOBS_URL,
        "results_seen": 0,
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "note": note,
    })
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8")


def probe_with_playwright(project: Path, timeout_ms: int) -> str:
    from playwright.sync_api import sync_playwright

    user_data = Path(os.environ.get(
        "ROLENAVI_LINKEDIN_BROWSER_PROFILE",
        Path.home() / ".rolenavi" / "browser" / "linkedin-jobs",
    )).expanduser()
    user_data.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch_persistent_context(
            str(user_data),
            headless=False,
            viewport={"width": 1280, "height": 900},
        )
        try:
            page = browser.pages[0] if browser.pages else browser.new_page()
            page.goto(LINKEDIN_JOBS_URL, wait_until="domcontentloaded",
                      timeout=timeout_ms)
            page.wait_for_timeout(1500)
            text = page.locator("body").inner_text(timeout=5000)
            observed = classify_observation(page.url, page.title(), text)
            record_observation(project, observed, "playwright runner probe")
            return observed
        finally:
            browser.close()


def main(argv: list[str] | None = None) -> int:
    _configure_stdio()
    ap = argparse.ArgumentParser(description="Record LinkedIn Jobs probe status.")
    ap.add_argument("project", type=Path)
    ap.add_argument("--timeout-ms", type=int, default=30000)
    ap.add_argument("--record-error",
                    help="record this connector_error instead of probing")
    args = ap.parse_args(argv)

    try:
        if args.record_error:
            observed = f"connector_error: {args.record_error}"
            record_observation(args.project, observed)
            print(f"OK: LinkedIn Jobs probe recorded {observed}")
            return 2
        if not has_playwright():
            observed = "connector_error: Playwright unavailable in runner environment"
            record_observation(args.project, observed)
            print(f"OK: LinkedIn Jobs probe recorded {observed}")
            return 2
        observed = probe_with_playwright(args.project, args.timeout_ms)
        print(f"OK: LinkedIn Jobs probe observed {observed}")
        return 0 if observed == "jobs_page_ok" else 2
    except Exception as e:
        observed = f"connector_error: {type(e).__name__}: {e}"
        try:
            record_observation(args.project, observed)
        except Exception as record_error:
            print(f"ERROR: probe failed and could not record: {record_error}",
                  file=sys.stderr)
            return 1
        print(f"OK: LinkedIn Jobs probe recorded {observed}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
