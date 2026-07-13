#!/usr/bin/env python3
"""Fetch a public URL with the Python standard library — no third-party install.

Why this exists: runs must not assume `requests` is available (RoleNavi ships
zero runtime dependencies), and agents should not hand-roll a fetch script every
run. Use this for plain public JSON/HTML fetches (ATS boards, careers APIs):

  python scripts/fetch_url.py "https://boards-api.greenhouse.io/v1/boards/acme/jobs"
  python scripts/fetch_url.py "<url>" --json --out project/data/source.json
  python scripts/fetch_url.py "<url>" --timeout 20 --header "Accept: application/json"

Output is always UTF-8 (so bullets/CJK never crash on Windows cp949/cp1252).
JSON responses print a compact machine summary to stdout; raw bodies should be
written with --out. This keeps Windows/Codex pipes stable on large ATS feeds.
Reading public pages only — never send credentials, submit, or message anyone.
For JavaScript-rendered pages this returns the raw shell; use a browser runtime.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

# Make stdout/stderr UTF-8 regardless of the OS console encoding.
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

DEFAULT_UA = ("Mozilla/5.0 (compatible; RoleNavi/0.1; +local-first job-search "
              "research; reads public pages only)")
DEFAULT_MAX_STDOUT_BYTES = 65536


def fetch(url: str, timeout: float, headers: dict[str, str]) -> tuple[str, str]:
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        ctype = resp.headers.get("Content-Type", "")
        charset = resp.headers.get_content_charset() or "utf-8"
        body = resp.read().decode(charset, errors="replace")
    return body, ctype


def _json_summary(url: str, body: str, ctype: str, saved_to: Path | None) -> dict:
    payload = json.loads(body)
    summary = {
        "status": "ok",
        "source_url": url,
        "content_type": ctype,
        "bytes": len(body.encode("utf-8", errors="replace")),
        "sha256": hashlib.sha256(body.encode("utf-8", errors="replace")).hexdigest(),
        "saved_to": str(saved_to) if saved_to else "",
        "json_kind": "array" if isinstance(payload, list) else "object" if isinstance(payload, dict) else type(payload).__name__,
        "result_count": len(payload) if isinstance(payload, list) else None,
        "top_level_keys": sorted(payload.keys())[:40] if isinstance(payload, dict) else [],
    }
    if isinstance(payload, dict):
        for key in ("jobs", "postings", "results", "data", "items"):
            value = payload.get(key)
            if isinstance(value, list):
                summary["result_count"] = len(value)
                summary["result_key"] = key
                break
    return summary


def _write_stdout(text: str) -> bool:
    try:
        print(text)
        return True
    except (BrokenPipeError, OSError):
        return False


def _bounded_body(body: str, max_bytes: int) -> tuple[str, bool]:
    encoded = body.encode("utf-8", errors="replace")
    if max_bytes <= 0 or len(encoded) <= max_bytes:
        return body, False
    trimmed = encoded[:max_bytes].decode("utf-8", errors="replace")
    return trimmed + "\n[rolenavi fetch_url: output truncated; use --out for full body]\n", True


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Fetch a public URL (stdlib only).")
    ap.add_argument("url")
    ap.add_argument("--json", action="store_true",
                    help="parse JSON and print a compact summary instead of the full body")
    ap.add_argument("--summary-json", action="store_true",
                    help="print compact JSON summary for any response")
    ap.add_argument("--raw", action="store_true",
                    help="print raw response body, bounded by --max-stdout-bytes")
    ap.add_argument("--out", type=Path,
                    help="write the full raw response body to this UTF-8 file")
    ap.add_argument("--max-stdout-bytes", type=int, default=DEFAULT_MAX_STDOUT_BYTES,
                    help="maximum raw body bytes to print before truncating")
    ap.add_argument("--timeout", type=float, default=30.0)
    ap.add_argument("--header", action="append", default=[],
                    help="extra request header 'Name: value' (repeatable)")
    ap.add_argument("--user-agent", default=DEFAULT_UA)
    args = ap.parse_args(argv)

    headers = {"User-Agent": args.user_agent}
    for h in args.header:
        if ":" in h:
            k, v = h.split(":", 1)
            headers[k.strip()] = v.strip()

    try:
        body, ctype = fetch(args.url, args.timeout, headers)
    except urllib.error.HTTPError as e:
        print(f"ERROR: HTTP {e.code} {e.reason} for {args.url}", file=sys.stderr)
        return 1
    except (urllib.error.URLError, OSError, ValueError) as e:
        print(f"ERROR: fetch failed for {args.url}: {e}", file=sys.stderr)
        return 1

    saved_to = None
    if args.out:
        saved_to = args.out
        saved_to.parent.mkdir(parents=True, exist_ok=True)
        saved_to.write_text(body, encoding="utf-8")

    looks_json = args.json or "json" in ctype.lower()
    if looks_json and not args.raw:
        try:
            summary = _json_summary(args.url, body, ctype, saved_to)
            return 0 if _write_stdout(json.dumps(summary, ensure_ascii=False)) else 0
        except json.JSONDecodeError:
            pass  # not valid JSON — fall through to raw

    if args.summary_json and not args.raw:
        summary = {
            "status": "ok",
            "source_url": args.url,
            "content_type": ctype,
            "bytes": len(body.encode("utf-8", errors="replace")),
            "sha256": hashlib.sha256(body.encode("utf-8", errors="replace")).hexdigest(),
            "saved_to": str(saved_to) if saved_to else "",
        }
        return 0 if _write_stdout(json.dumps(summary, ensure_ascii=False)) else 0

    out, truncated = _bounded_body(body, args.max_stdout_bytes)
    if truncated:
        print("WARN: response body truncated on stdout; use --out for full body",
              file=sys.stderr)
    _write_stdout(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
