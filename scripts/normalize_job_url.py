#!/usr/bin/env python3
"""Canonicalize a job posting URL and generate a dedupe-safe job_id.

Usage:
  python3 scripts/normalize_job_url.py --url URL [--company COMPANY --title TITLE]
  python3 scripts/normalize_job_url.py URL COMPANY TITLE
  python3 scripts/normalize_job_url.py COMPANY TITLE URL
  python3 scripts/normalize_job_url.py --json rows.json   # adds canonical_url/job_id to each row

Outputs JSON to stdout: {"canonical_url": ..., "url_hash": ..., "job_id": ...}
job_id format: <company-slug>--<title-slug>--<8-char-hash>. Exit 1 on invalid URL.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

from location_normalize import normalize_job_rows

TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "gclid", "fbclid", "ref", "referral", "src", "source", "trk", "trkid",
    "refid", "gh_src", "lever-source", "icid", "mc_cid", "mc_eid",
    # LinkedIn jobs URLs (same class as ref/src above; keeps dedupe canonical)
    "trackingid", "ebp", "origin", "lipi", "midtoken", "midsig", "trkemail",
}


def canonicalize(url: str) -> str:
    url = url.strip()
    if not re.match(r"^https?://", url):
        raise ValueError(f"not an http(s) URL: {url!r}")
    parts = urlsplit(url)
    netloc = parts.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    path = re.sub(r"/+$", "", parts.path) or "/"
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if k.lower() not in TRACKING_PARAMS]
    return urlunsplit(("https", netloc, path, urlencode(query), ""))


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return slug[:40] or "unknown"


def build(url: str, company: str = "", title: str = "") -> dict:
    canonical = canonicalize(url)
    url_hash = hashlib.sha256(canonical.encode()).hexdigest()[:8]
    return {
        "canonical_url": canonical,
        "url_hash": url_hash,
        "job_id": f"{slugify(company)}--{slugify(title)}--{url_hash}",
    }


def _parse_positionals(values: list[str]) -> tuple[str, str, str]:
    if not values:
        return "", "", ""
    url_indexes = [i for i, value in enumerate(values)
                   if re.match(r"^https?://", value.strip())]
    if not url_indexes:
        raise ValueError("positional form requires one http(s) URL")
    idx = url_indexes[0]
    url = values[idx]
    before = values[:idx]
    after = values[idx + 1:]
    if idx == 0:
        company = after[0] if after else ""
        title = " ".join(after[1:]) if len(after) > 1 else ""
    else:
        company = before[0] if before else ""
        title = " ".join(before[1:] + after)
    return url, company, title


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("values", nargs="*",
                    help="forgiving positional form: URL COMPANY TITLE or COMPANY TITLE URL")
    ap.add_argument("--url")
    ap.add_argument("--company", default="")
    ap.add_argument("--title", default="")
    ap.add_argument("--json", help="path to a JSON list of row dicts with source_url/company/title")
    args = ap.parse_args(argv)
    try:
        if args.json:
            with open(args.json, encoding="utf-8") as f:
                rows = json.load(f)
            for row in rows:
                info = build(row.get("source_url", ""), row.get("company", ""), row.get("title", ""))
                row["source_url"] = info["canonical_url"]
                row.setdefault("job_id", info["job_id"])
            rows = normalize_job_rows(rows)
            print(json.dumps(rows, indent=2, ensure_ascii=False))
        else:
            url = args.url
            company = args.company
            title = args.title
            if not url and args.values:
                url, pos_company, pos_title = _parse_positionals(args.values)
                company = company or pos_company
                title = title or pos_title
            if url:
                print(json.dumps(build(url, company, title), indent=2, ensure_ascii=False))
            else:
                ap.error("provide --url, --json, or positional URL/company/title")
    except (ValueError, OSError, json.JSONDecodeError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
