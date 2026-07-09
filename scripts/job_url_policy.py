"""Direct posting URL and JD-quality checks shared by validators."""

from __future__ import annotations

import re
from urllib.parse import urlsplit


LISTING_PATHS = {
    "/careers",
    "/jobs",
    "/job",
    "/job-search",
    "/jobs/search",
    "/about/careers/applications/jobs/results",
}
SEARCH_QUERY_KEYS = {
    "q", "query", "keyword", "keywords", "search", "location", "locations",
    "page", "sort_by", "employment_type", "company", "hl", "jlo",
}
UNVERIFIED_JD_TOKENS = (
    "not verified",
    "unverified forced",
    "could not verify",
    "could not be fetched",
    "posting/jd verification remains pending",
    "official posting/jd verification remains pending",
    "pending source retry",
    "verification remains pending",
)


def _host(url: str) -> str:
    host = urlsplit(str(url or "")).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def _path(url: str) -> str:
    path = re.sub(r"/+$", "", urlsplit(str(url or "")).path.lower())
    return path or "/"


def _query_keys(url: str) -> set[str]:
    query = urlsplit(str(url or "")).query.lower()
    return {part.split("=", 1)[0] for part in query.split("&") if part}


def is_google_direct_posting_url(url: str) -> bool:
    if _host(url) != "google.com":
        return False
    path = _path(url)
    return bool(re.match(
        r"^/about/careers/applications/jobs/results/\d+[a-z0-9-]*$",
        path,
    ))


def is_servicenow_direct_posting_url(url: str) -> bool:
    if _host(url) != "careers.servicenow.com":
        return False
    return bool(re.match(r"^/jobs/\d+/.+", _path(url)))


def looks_like_listing_or_search_url(url: str) -> bool:
    url = str(url or "").strip()
    if not url:
        return False
    host = _host(url)
    path = _path(url)
    if host == "google.com" and path == "/about/careers/applications/jobs/results":
        return True
    if host == "careers.servicenow.com" and path in {"/jobs", "/jobs/"}:
        return True
    if path in LISTING_PATHS:
        return True
    if "search" in path and not re.search(r"/\d+|/[0-9a-f]{8,}", path):
        return True
    if _query_keys(url) & SEARCH_QUERY_KEYS and not re.search(r"/\d+|/[0-9a-f]{8,}", path):
        return True
    return False


def is_direct_posting_url(url: str) -> bool:
    url = str(url or "").strip()
    if not re.match(r"^https?://", url):
        return False
    if is_google_direct_posting_url(url) or is_servicenow_direct_posting_url(url):
        return True
    if looks_like_listing_or_search_url(url):
        return False
    path = _path(url)
    return bool(re.search(
        r"/(jobs?|careers|positions?|openings?|details?)/.+|/\d{6,}/.+|/[0-9a-f]{8,}",
        path,
    ))


def row_has_direct_posting_url(row: dict) -> bool:
    return any(
        is_direct_posting_url(str(row.get(field, "")))
        for field in ("job_page_url", "source_url", "url")
    )


def unverified_jd_placeholder(row: dict) -> str:
    fields = (
        "must_have_requirements", "nice_to_have_requirements", "jd_summary",
        "notes", "reason",
    )
    text = " ".join(str(row.get(field, "")) for field in fields).lower()
    for token in UNVERIFIED_JD_TOKENS:
        if token in text:
            return token
    return ""
