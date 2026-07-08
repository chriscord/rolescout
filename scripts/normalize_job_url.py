#!/usr/bin/env python3
"""Canonicalize a job posting URL and generate a dedupe-safe job_id.

Usage:
  python3 scripts/normalize_job_url.py --url URL [--company COMPANY --title TITLE]
  python3 scripts/normalize_job_url.py --json rows.json   # adds canonical_url/job_id to each row

Outputs JSON to stdout: {"canonical_url": ..., "url_hash": ..., "job_id": ...}
job_id format: <company-slug>--<title-slug>--<8-char-hash>. Exit 1 on invalid URL.
"""
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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url")
    ap.add_argument("--company", default="")
    ap.add_argument("--title", default="")
    ap.add_argument("--json", help="path to a JSON list of row dicts with source_url/company/title")
    args = ap.parse_args()
    try:
        if args.json:
            with open(args.json, encoding="utf-8") as f:
                rows = json.load(f)
            for row in rows:
                info = build(row.get("source_url", ""), row.get("company", ""), row.get("title", ""))
                row["source_url"] = info["canonical_url"]
                row.setdefault("job_id", info["job_id"])
            rows = normalize_job_rows(rows)
            print(json.dumps(rows, indent=2))
        elif args.url:
            print(json.dumps(build(args.url, args.company, args.title), indent=2))
        else:
            ap.error("provide --url or --json")
    except (ValueError, OSError, json.JSONDecodeError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
