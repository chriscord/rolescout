"""Direct posting URL and JD-quality checks shared by validators."""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlsplit


LISTING_PATHS = {
    "/careers",
    "/jobs",
    "/job",
    "/job-search",
    "/jobs/search",
    "/about/careers/applications/jobs/results",
}
NON_POSTING_PATH_TOKENS = {
    "accommodation", "accommodations", "all-jobs", "benefits", "candidate",
    "culture", "early-careers", "earlycareers", "eeo", "employee-awards",
    "engineering", "fraud", "how-we-hire", "interview", "interviewing",
    "life", "life-at", "life-at-stripe", "privacy", "resources", "sales",
    "students", "teams", "terms", "university",
}
SEARCH_QUERY_KEYS = {
    "q", "query", "keyword", "keywords", "search", "location", "locations",
    "page", "sort_by", "employment_type", "company", "hl", "jlo",
}
DIRECT_POSTING_QUERY_KEYS = {
    "gh_jid", "id", "jid", "job_id", "jobid", "job",
    "posting_id", "req", "req_id", "requisition", "requisition_id",
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


def _query_params(url: str) -> dict[str, str]:
    return {key: value for key, value in parse_qsl(urlsplit(str(url or "")).query)}


def _has_direct_posting_query_id(url: str) -> bool:
    path = _path(url)
    if not re.search(r"\b(job|jobs|position|positions|opening|openings|posting)\b", path):
        return False
    for key, value in _query_params(url).items():
        if key.lower() in DIRECT_POSTING_QUERY_KEYS and re.search(
            r"\d{5,}|[0-9a-f-]{12,}", value, re.I
        ):
            return True
    return False


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


def is_known_provider_direct_posting_url(url: str) -> bool:
    host = _host(url)
    path = _path(url)
    parts = [p for p in path.split("/") if p]
    lower_parts = [p.lower() for p in parts]
    if any(part in NON_POSTING_PATH_TOKENS for part in lower_parts):
        return False
    if host == "jobs.lever.co":
        return len(parts) >= 2 and bool(re.search(r"[0-9a-f]{8,}|[a-f0-9-]{20,}", parts[-1]))
    if host == "jobs.ashbyhq.com":
        return len(parts) >= 2 and bool(re.search(r"[0-9a-f-]{8,}|[a-z0-9-]{16,}", parts[-1], re.I))
    if host in {"boards.greenhouse.io", "job-boards.greenhouse.io"}:
        return len(parts) >= 3 and parts[-2] == "jobs" and bool(re.search(r"\d{6,}", parts[-1]))
    if host == "stripe.com":
        return len(parts) >= 4 and parts[:2] == ["jobs", "listing"] and bool(re.search(r"\d{5,}", parts[-1]))
    if host == "metacareers.com":
        return len(parts) >= 2 and parts[0] == "jobs" and bool(re.search(r"\d{5,}", parts[1]))
    if host == "uber.com":
        return len(parts) >= 3 and parts[:2] == ["careers", "list"] and bool(re.search(r"\d{4,}", parts[2]))
    if host.endswith(".recruitee.com"):
        return len(parts) >= 2 and parts[0] in {"o", "offers", "jobs"}
    if host.endswith(".teamtailor.com"):
        return len(parts) >= 2 and parts[0] == "jobs"
    if "smartrecruiters.com" in host:
        return len(parts) >= 2 and bool(re.search(r"\d{6,}|[0-9a-f-]{12,}", parts[-1], re.I))
    if "jobs.personio." in host:
        return "job" in parts and len(parts) >= 2
    if "myworkdayjobs.com" in host:
        return any(part.lower() == "job" for part in parts) or bool(
            re.search(r"jr\d+|r-\d+|req\d+", path, re.I)
        )
    return False


def looks_like_listing_or_search_url(url: str) -> bool:
    url = str(url or "").strip()
    if not url:
        return False
    host = _host(url)
    path = _path(url)
    parts = [p for p in path.split("/") if p]
    if any(part.lower() in NON_POSTING_PATH_TOKENS for part in parts):
        return True
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
    if _has_direct_posting_query_id(url):
        return True
    if (is_google_direct_posting_url(url) or is_servicenow_direct_posting_url(url)
            or is_known_provider_direct_posting_url(url)):
        return True
    if looks_like_listing_or_search_url(url):
        return False
    path = _path(url)
    parts = [p for p in path.split("/") if p]
    if any(part.lower() in NON_POSTING_PATH_TOKENS for part in parts):
        return False
    if re.search(r"/careers?/[^/]+$", path) and not re.search(r"\d{5,}|[0-9a-f-]{20,}", path, re.I):
        return False
    return bool(re.search(
        r"/(jobs?|positions?|openings?|details?)/.+\d{5,}|/\d{6,}/.+|/[0-9a-f]{8,}",
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
