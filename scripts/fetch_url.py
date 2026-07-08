#!/usr/bin/env python3
"""Fetch a public URL with the Python standard library — no third-party install.

Why this exists: runs must not assume `requests` is available (RoleScout ships
zero runtime dependencies), and agents should not hand-roll a fetch script every
run. Use this for plain public JSON/HTML fetches (ATS boards, careers APIs):

  python scripts/fetch_url.py "https://boards-api.greenhouse.io/v1/boards/acme/jobs"
  python scripts/fetch_url.py "<url>" --json          # pretty-print JSON
  python scripts/fetch_url.py "<url>" --timeout 20 --header "Accept: application/json"

Output is always UTF-8 (so bullets/CJK never crash on Windows cp949/cp1252).
Reading public pages only — never send credentials, submit, or message anyone.
For JavaScript-rendered pages this returns the raw shell; use a browser runtime.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request

# Make stdout/stderr UTF-8 regardless of the OS console encoding.
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

DEFAULT_UA = ("Mozilla/5.0 (compatible; RoleScout/0.1; +local-first job-search "
              "research; reads public pages only)")


def fetch(url: str, timeout: float, headers: dict[str, str]) -> tuple[str, str]:
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        ctype = resp.headers.get("Content-Type", "")
        charset = resp.headers.get_content_charset() or "utf-8"
        body = resp.read().decode(charset, errors="replace")
    return body, ctype


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Fetch a public URL (stdlib only).")
    ap.add_argument("url")
    ap.add_argument("--json", action="store_true",
                    help="pretty-print the response as JSON")
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

    if args.json or "json" in ctype.lower():
        try:
            print(json.dumps(json.loads(body), indent=2, ensure_ascii=False))
            return 0
        except json.JSONDecodeError:
            pass  # not valid JSON — fall through to raw
    print(body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
