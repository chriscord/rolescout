"""Deterministic provider-first job search.

This module intentionally implements RoleScout-native search mechanics rather
than a clone of another repository. It uses the existing project metadata,
source-plan artifacts, validators, and SQLite persistence scripts, while moving
the slow crawl/parse work out of LLM agents.
"""

from __future__ import annotations

import concurrent.futures
import html
import http.client
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from .. import core, project_meta

normalize_job_url = core.load("normalize_job_url")
job_url_policy = core.load("job_url_policy")
location_normalize = core.load("location_normalize")
resolve_company_sources = core.load("resolve_company_sources")
schema_defs = core.load("schema_defs")
jd_text_cleaner = core.load("jd_text_cleaner")


DEFAULT_TIMEOUT_S = 12
MAX_RESPONSE_BYTES = 25_000_000
MIN_JD_TEXT_CHARS = 200
JD_TEXT_CAP = 12_000
DEFAULT_CANDIDATES_PER_SOURCE = 120
DEFAULT_DETAIL_FETCHES_PER_SOURCE = 40
DEFAULT_SEARCH_RUNTIME_PROFILE = "fast"
ROLE_SYNONYMS = {
    "strategy": {
        "strategy", "strategic", "bizops", "business operations",
        "business ops", "chief of staff", "planning", "market intelligence",
        "corporate strategy",
    },
    "investment": {
        "investment", "investor", "investing", "venture", "corporate development",
        "corp dev", "m&a", "mergers", "acquisitions", "portfolio",
    },
    "business development": {
        "business development", "bd", "partnership", "partnerships",
        "alliances", "ecosystem", "channels", "gtm", "go-to-market",
    },
}
DEFAULT_ROLE_TERMS = {
    "strategy", "strategic", "business development", "partnership",
    "partnerships", "corporate development", "bizops", "business operations",
    "investment", "alliances",
}
REMOTE_TERMS = ("remote", "hybrid", "distributed")
SKIP_REASON = "constraint_violation"
CONTENT_ROOT_ATTR_TOKENS = (
    "job", "posting", "position", "description", "content", "detail",
    "responsibilities", "qualifications", "requisition", "opportunity",
)
JD_SIGNAL_TERMS = (
    "responsibilities", "requirements", "qualifications", "experience",
    "about the role", "about this role", "what you", "you will", "we are looking",
    "preferred", "minimum", "salary", "compensation", "benefits", "apply",
)
NON_JOB_TITLE_PATTERNS = (
    r"^all teams$",
    r"^early careers$",
    r"^employee awards$",
    r"^engineering$",
    r"^here$",
    r"^interviewing$",
    r"^life at ",
    r"^sales$",
    r"^university$",
    r"^view this page",
)
CLOSED_POSTING_TERMS = (
    "job is no longer available",
    "posting is no longer available",
    "position is no longer available",
    "no longer accepting applications",
    "this job has expired",
    "job has expired",
    "position has been filled",
    "job not found",
)


@dataclass(frozen=True)
class ProviderRuntimePolicy:
    candidate_limit: int | None = None
    detail_fetch_limit: int | None = None
    browser_allowed: bool = True


@dataclass(frozen=True)
class SearchRuntimePolicy:
    name: str
    timeout_s: int
    source_workers: int
    jd_workers: int
    browser_slots: int
    candidate_limit: int = DEFAULT_CANDIDATES_PER_SOURCE
    detail_fetch_limit: int = DEFAULT_DETAIL_FETCHES_PER_SOURCE
    provider_policies: dict[str, ProviderRuntimePolicy] = field(default_factory=dict)

    def provider(self, provider: str) -> ProviderRuntimePolicy:
        return self.provider_policies.get(provider, ProviderRuntimePolicy())

    def candidate_limit_for(self, provider: str) -> int:
        override = self.provider(provider).candidate_limit
        return max(1, override if override is not None else self.candidate_limit)

    def detail_fetch_limit_for(self, provider: str) -> int:
        override = self.provider(provider).detail_fetch_limit
        return max(1, override if override is not None else self.detail_fetch_limit)

    def browser_allowed_for(self, provider: str) -> bool:
        return self.provider(provider).browser_allowed


SEARCH_RUNTIME_PROFILES: dict[str, SearchRuntimePolicy] = {
    "polite": SearchRuntimePolicy(
        name="polite",
        timeout_s=15,
        source_workers=4,
        jd_workers=4,
        browser_slots=1,
        candidate_limit=80,
        detail_fetch_limit=24,
        provider_policies={
            "official_html": ProviderRuntimePolicy(detail_fetch_limit=24),
            "workday": ProviderRuntimePolicy(candidate_limit=80, detail_fetch_limit=24),
        },
    ),
    "standard": SearchRuntimePolicy(
        name="standard",
        timeout_s=12,
        source_workers=8,
        jd_workers=6,
        browser_slots=2,
        candidate_limit=120,
        detail_fetch_limit=40,
        provider_policies={
            "official_html": ProviderRuntimePolicy(detail_fetch_limit=40),
            "workday": ProviderRuntimePolicy(candidate_limit=120, detail_fetch_limit=40),
        },
    ),
    "fast": SearchRuntimePolicy(
        name="fast",
        timeout_s=12,
        source_workers=12,
        jd_workers=10,
        browser_slots=2,
        candidate_limit=160,
        detail_fetch_limit=50,
        provider_policies={
            "official_html": ProviderRuntimePolicy(detail_fetch_limit=50),
            "workday": ProviderRuntimePolicy(candidate_limit=160, detail_fetch_limit=50),
        },
    ),
    "deep": SearchRuntimePolicy(
        name="deep",
        timeout_s=18,
        source_workers=10,
        jd_workers=10,
        browser_slots=3,
        candidate_limit=240,
        detail_fetch_limit=80,
        provider_policies={
            "official_html": ProviderRuntimePolicy(detail_fetch_limit=80),
            "workday": ProviderRuntimePolicy(candidate_limit=240, detail_fetch_limit=80),
        },
    ),
}


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _today() -> str:
    return date.today().isoformat()


def _norm(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _norm_key(value: str) -> str:
    return "".join(ch for ch in str(value or "").lower() if ch.isalnum())


def _slug(value: str, fallback: str = "item") -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(value or "").lower()).strip("-")
    return slug[:60].strip("-") or fallback


def _strip_tags(value: str) -> str:
    return jd_text_cleaner.clean_jd_text(value)


def _compact_text(value: str) -> str:
    text = _strip_tags(value)
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
    lines = [line for line in lines if line]
    return _cap_jd_text("\n".join(lines))


def _cap_jd_text(text: str) -> str:
    text = str(text or "").strip()
    if len(text) <= JD_TEXT_CAP:
        return text
    cut = text[:JD_TEXT_CAP]
    last_break = max(cut.rfind("\n"), cut.rfind(". "))
    if last_break > int(JD_TEXT_CAP * 0.75):
        cut = cut[:last_break + 1]
    return cut.rstrip()


def _jd_signal_count(text: str) -> int:
    lower = str(text or "").lower()
    return sum(1 for term in JD_SIGNAL_TERMS if term in lower)


def _jd_content_score(text: str) -> int:
    lower = str(text or "").lower()
    score = min(len(text), JD_TEXT_CAP)
    score += _jd_signal_count(lower) * 500
    score -= sum(
        350 for term in (
            "privacy policy", "cookie", "job alert", "similar jobs",
            "share this job", "sign in", "create account",
        )
        if term in lower
    )
    return score


def _looks_like_jd_text(text: str, title: str = "") -> bool:
    text = str(text or "")
    if len(text) < MIN_JD_TEXT_CHARS:
        return False
    if _jd_signal_count(text) >= 2:
        return True
    title_terms = [
        term for term in re.split(r"[^a-z0-9]+", title.lower())
        if len(term) >= 4
    ]
    lower = text.lower()
    title_hits = sum(1 for term in set(title_terms) if term in lower)
    return title_hits >= 2 and _jd_signal_count(text) >= 1


def _liveness_status(text: str) -> str:
    lower = str(text or "").lower()
    if any(term in lower for term in CLOSED_POSTING_TERMS):
        return "closed_or_expired"
    return "open_or_accessible"


def _json_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value or "")


def _sentences(text: str) -> list[str]:
    pieces: list[str] = []
    for raw in re.split(r"[\n\r]+|(?<=[.!?])\s+", text):
        item = re.sub(r"\s+", " ", raw).strip(" -\t")
        if 35 <= len(item) <= 320:
            pieces.append(item)
    return pieces


def _first_unique(items: list[str], limit: int) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        key = item.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
        if len(out) >= limit:
            break
    return out


def _field(value: Any, *names: str) -> Any:
    cur = value
    for name in names:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(name)
    return cur


class _LinkExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[dict[str, str]] = []
        self._href = ""
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        attrs_dict = {k.lower(): v or "" for k, v in attrs}
        self._href = attrs_dict.get("href", "")
        self._text = []

    def handle_data(self, data: str) -> None:
        if self._href:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a" and self._href:
            text = _norm(" ".join(self._text))
            self.links.append({"href": self._href, "text": text})
            self._href = ""
            self._text = []


class _ReadableTextExtractor(HTMLParser):
    """Small stdlib extractor for readable JD text from static HTML."""

    noise_tags = {"script", "style", "noscript", "svg", "nav", "header", "footer", "form", "aside"}
    block_tags = {
        "article", "br", "dd", "div", "dt", "h1", "h2", "h3", "h4",
        "h5", "h6", "li", "main", "p", "section", "table", "td", "th", "tr",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._stack: list[str] = []
        self._skip_depth = 0
        self._all_parts: list[str] = []
        self._blocks: list[dict[str, Any]] = []
        self._active_blocks: list[int] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attrs_dict = {k.lower(): v or "" for k, v in attrs}
        self._stack.append(tag)
        if tag in self.noise_tags:
            self._skip_depth += 1
            return
        if tag == "br":
            self._append("\n")
        if self._is_content_root(tag, attrs_dict):
            block = {"tag": tag, "depth": len(self._stack), "parts": []}
            self._blocks.append(block)
            self._active_blocks.append(len(self._blocks) - 1)

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        self._append(data)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if not self._stack:
            return
        current_depth = len(self._stack)
        if not self._skip_depth and tag in self.block_tags:
            self._append("\n")
        self._active_blocks = [
            idx for idx in self._active_blocks
            if not (
                self._blocks[idx]["depth"] == current_depth
                and self._blocks[idx]["tag"] == tag
            )
        ]
        if tag in self.noise_tags and self._skip_depth:
            self._skip_depth -= 1
        if self._stack[-1] == tag:
            self._stack.pop()
        elif tag in self._stack:
            self._stack.pop(self._stack.index(tag))

    def _append(self, text: str) -> None:
        if not text:
            return
        self._all_parts.append(text)
        for idx in self._active_blocks:
            self._blocks[idx]["parts"].append(text)

    def _is_content_root(self, tag: str, attrs: dict[str, str]) -> bool:
        if tag in {"main", "article"}:
            return True
        if attrs.get("role", "").lower() == "main":
            return True
        attr_text = " ".join(
            attrs.get(name, "") for name in ("id", "class", "data-qa", "data-testid")
        ).lower()
        return any(token in attr_text for token in CONTENT_ROOT_ATTR_TOKENS)

    def best_text(self) -> str:
        body = _compact_text(" ".join(self._all_parts))
        candidates = [
            _compact_text(" ".join(block["parts"]))
            for block in self._blocks
        ]
        candidates = [text for text in candidates if len(text) >= MIN_JD_TEXT_CHARS]
        if not candidates:
            return body
        best = max(candidates, key=_jd_content_score)
        if _looks_like_jd_text(best) and _jd_signal_count(best) >= 2:
            return best
        if _jd_content_score(best) >= max(1, int(_jd_content_score(body) * 0.45)):
            return best
        return body


def _readable_html_text(value: str) -> str:
    parser = _ReadableTextExtractor()
    try:
        parser.feed(str(value or ""))
        parser.close()
    except Exception:
        return _compact_text(value)
    return parser.best_text()


@dataclass
class SearchIntent:
    target_locations: list[str]
    focus_role: str
    target_level: str
    target_companies: list[str]
    negatives: list[str]
    role_terms: set[str] = field(default_factory=set)
    location_terms: set[str] = field(default_factory=set)


@dataclass
class ProviderCandidate:
    company: str
    title: str
    url: str
    location: str = ""
    remote_policy: str = "unknown"
    provider: str = ""
    provider_job_id: str = ""
    description: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class DiscoveryResult:
    task_id: str
    provider: str
    company: str
    source_url: str
    status: str
    candidates: list[ProviderCandidate] = field(default_factory=list)
    error: str = ""
    method: str = ""
    elapsed_s: float = 0.0


@dataclass
class SearchResult:
    status: str
    summary: dict[str, Any]
    output_path: Path


@dataclass
class CandidateWork:
    seq: int
    result: DiscoveryResult
    candidate: ProviderCandidate
    canonical_url: str


@dataclass
class ExtractedCandidate:
    work: CandidateWork
    jd_text: str
    extraction_method: str
    fallbacks: list[str]


class FetchError(RuntimeError):
    def __init__(self, message: str, status: str = "failed_retryable") -> None:
        super().__init__(message)
        self.status = status


class HttpClient:
    def __init__(self, timeout_s: int = DEFAULT_TIMEOUT_S) -> None:
        self.timeout_s = timeout_s
        self.user_agent = (
            "RoleScout/1.0 deterministic job search "
            "(local-first; contact user via configured project)"
        )

    def request(
        self,
        url: str,
        *,
        method: str = "GET",
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[str, str]:
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "application/json,text/html,application/xml,text/xml,*/*",
            **(headers or {}),
        }
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                raw = resp.read(MAX_RESPONSE_BYTES)
                content_type = resp.headers.get("content-type", "")
        except urllib.error.HTTPError as exc:
            status = "blocked_auth" if exc.code in (401, 403) else "failed_retryable"
            raise FetchError(f"http_{exc.code}: {url}", status) from exc
        except urllib.error.URLError as exc:
            reason = str(getattr(exc, "reason", exc))
            raise FetchError(f"url_error: {reason}: {url}", "failed_retryable") from exc
        except http.client.IncompleteRead as exc:
            raise FetchError(
                f"incomplete_read: {len(getattr(exc, 'partial', b''))} bytes read: {url}",
                "failed_retryable",
            ) from exc
        except TimeoutError as exc:
            raise FetchError(f"timeout: {url}", "failed_retryable") from exc
        encoding = "utf-8"
        match = re.search(r"charset=([^;\s]+)", content_type, re.I)
        if match:
            encoding = match.group(1)
        return raw.decode(encoding, errors="replace"), content_type

    def get_text(self, url: str) -> str:
        return self.request(url)[0]

    def get_json(self, url: str) -> Any:
        text = self.get_text(url)
        return json.loads(text)

    def post_json(self, url: str, payload: dict[str, Any]) -> Any:
        data = json.dumps(payload).encode("utf-8")
        text, _ = self.request(
            url,
            method="POST",
            data=data,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        return json.loads(text)


class ProviderAdapter:
    provider = "generic"

    def can_handle(self, url: str, source: dict[str, Any]) -> bool:
        return False

    def discover(
        self,
        task: dict[str, Any],
        client: HttpClient,
        intent: SearchIntent,
    ) -> DiscoveryResult:
        raise NotImplementedError


class GreenhouseAdapter(ProviderAdapter):
    provider = "greenhouse"

    def can_handle(self, url: str, source: dict[str, Any]) -> bool:
        host = urllib.parse.urlsplit(url).netloc.lower()
        return "greenhouse.io" in host

    def _token(self, url: str) -> str:
        parts = urllib.parse.urlsplit(url)
        if "boards-api.greenhouse.io" in parts.netloc:
            match = re.search(r"/boards/([^/]+)/jobs", parts.path)
            return match.group(1) if match else ""
        bits = [p for p in parts.path.split("/") if p]
        return bits[0] if bits else ""

    def discover(self, task: dict[str, Any], client: HttpClient,
                 intent: SearchIntent) -> DiscoveryResult:
        token = self._token(task["url"])
        if not token:
            raise FetchError("greenhouse token not found", "failed_terminal")
        api = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"
        payload = client.get_json(api)
        jobs = payload.get("jobs", []) if isinstance(payload, dict) else []
        candidates = []
        for job in jobs[:_candidate_limit(task)]:
            if not isinstance(job, dict):
                continue
            loc = _field(job, "location", "name") or ""
            url = str(job.get("absolute_url") or "").strip()
            candidates.append(ProviderCandidate(
                company=task["company"],
                title=_norm(job.get("title", "")),
                url=url,
                location=_norm(loc),
                provider=self.provider,
                provider_job_id=str(job.get("id") or job.get("internal_job_id") or ""),
                description=str(job.get("content") or ""),
                raw=job,
            ))
        return DiscoveryResult(task["id"], self.provider, task["company"], task["url"],
                               "scanned", candidates, method=api)


class LeverAdapter(ProviderAdapter):
    provider = "lever"

    def can_handle(self, url: str, source: dict[str, Any]) -> bool:
        return urllib.parse.urlsplit(url).netloc.lower() == "jobs.lever.co"

    def discover(self, task: dict[str, Any], client: HttpClient,
                 intent: SearchIntent) -> DiscoveryResult:
        bits = [p for p in urllib.parse.urlsplit(task["url"]).path.split("/") if p]
        if not bits:
            raise FetchError("lever account not found", "failed_terminal")
        token = bits[0]
        api = f"https://api.lever.co/v0/postings/{token}?mode=json"
        jobs = client.get_json(api)
        if not isinstance(jobs, list):
            jobs = []
        candidates = []
        for job in jobs[:_candidate_limit(task)]:
            if not isinstance(job, dict):
                continue
            cats = job.get("categories") if isinstance(job.get("categories"), dict) else {}
            location = _norm(cats.get("location", ""))
            candidates.append(ProviderCandidate(
                company=task["company"],
                title=_norm(job.get("text", "")),
                url=str(job.get("hostedUrl") or job.get("applyUrl") or ""),
                location=location,
                remote_policy=_remote_policy(location + " " + _json_text(job)),
                provider=self.provider,
                provider_job_id=str(job.get("id") or ""),
                description=str(job.get("descriptionPlain") or job.get("description") or ""),
                raw=job,
            ))
        return DiscoveryResult(task["id"], self.provider, task["company"], task["url"],
                               "scanned", candidates, method=api)


class AshbyAdapter(ProviderAdapter):
    provider = "ashby"

    def can_handle(self, url: str, source: dict[str, Any]) -> bool:
        host = urllib.parse.urlsplit(url).netloc.lower()
        return host == "jobs.ashbyhq.com" or "ashbyhq.com" in host

    def discover(self, task: dict[str, Any], client: HttpClient,
                 intent: SearchIntent) -> DiscoveryResult:
        bits = [p for p in urllib.parse.urlsplit(task["url"]).path.split("/") if p]
        if not bits:
            raise FetchError("ashby org not found", "failed_terminal")
        org = bits[0]
        api = f"https://api.ashbyhq.com/posting-api/job-board/{org}"
        payload = client.get_json(api)
        jobs = payload.get("jobs", []) if isinstance(payload, dict) else []
        candidates = []
        for job in jobs[:_candidate_limit(task)]:
            if not isinstance(job, dict):
                continue
            job_id = str(job.get("id") or job.get("jobId") or "")
            url = str(job.get("jobUrl") or job.get("url") or "").strip()
            if not url and job_id:
                url = f"https://jobs.ashbyhq.com/{org}/{job_id}"
            candidates.append(ProviderCandidate(
                company=task["company"],
                title=_norm(job.get("title", "")),
                url=url,
                location=_norm(job.get("locationName") or job.get("location") or ""),
                remote_policy=_remote_policy(_json_text(job)),
                provider=self.provider,
                provider_job_id=job_id,
                description=str(job.get("descriptionHtml") or job.get("description") or ""),
                raw=job,
            ))
        return DiscoveryResult(task["id"], self.provider, task["company"], task["url"],
                               "scanned", candidates, method=api)


class SmartRecruitersAdapter(ProviderAdapter):
    provider = "smartrecruiters"

    def can_handle(self, url: str, source: dict[str, Any]) -> bool:
        host = urllib.parse.urlsplit(url).netloc.lower()
        return "smartrecruiters.com" in host

    def discover(self, task: dict[str, Any], client: HttpClient,
                 intent: SearchIntent) -> DiscoveryResult:
        bits = [p for p in urllib.parse.urlsplit(task["url"]).path.split("/") if p]
        company_token = bits[0] if bits else re.sub(r"[^A-Za-z0-9]", "", task["company"])
        api = f"https://api.smartrecruiters.com/v1/companies/{company_token}/postings"
        payload = client.get_json(api)
        jobs = payload.get("content", []) if isinstance(payload, dict) else []
        candidates = []
        for job in jobs[:_candidate_limit(task)]:
            if not isinstance(job, dict):
                continue
            job_id = str(job.get("id") or "")
            detail = {}
            if job_id:
                try:
                    detail = client.get_json(
                        f"https://api.smartrecruiters.com/v1/companies/"
                        f"{company_token}/postings/{job_id}"
                    )
                except (FetchError, json.JSONDecodeError):
                    detail = {}
            loc = job.get("location") if isinstance(job.get("location"), dict) else {}
            url = str(job.get("ref") or "")
            if not url and job_id:
                url = f"https://jobs.smartrecruiters.com/{company_token}/{job_id}"
            desc = _json_text(detail.get("jobAd", {}).get("sections", {})) if isinstance(detail, dict) else ""
            candidates.append(ProviderCandidate(
                company=task["company"],
                title=_norm(job.get("name", "")),
                url=url,
                location=_norm(loc.get("city") or loc.get("country") or ""),
                remote_policy=_remote_policy(_json_text(job) + _json_text(detail)),
                provider=self.provider,
                provider_job_id=job_id,
                description=desc,
                raw={"summary": job, "detail": detail},
            ))
        return DiscoveryResult(task["id"], self.provider, task["company"], task["url"],
                               "scanned", candidates, method=api)


class WorkdayAdapter(ProviderAdapter):
    provider = "workday"

    def can_handle(self, url: str, source: dict[str, Any]) -> bool:
        host = urllib.parse.urlsplit(url).netloc.lower()
        return "myworkdayjobs.com" in host

    def _tenant_site(self, url: str) -> tuple[str, str]:
        parts = urllib.parse.urlsplit(url)
        host_left = parts.netloc.split(".", 1)[0]
        tenant = host_left.split("_", 1)[0]
        bits = [p for p in parts.path.split("/") if p]
        bits = [p for p in bits if not re.match(r"^[a-z]{2}(-[A-Z]{2})?$", p)]
        site = bits[0] if bits else "External"
        return tenant, site

    def discover(self, task: dict[str, Any], client: HttpClient,
                 intent: SearchIntent) -> DiscoveryResult:
        tenant, site = self._tenant_site(task["url"])
        parts = urllib.parse.urlsplit(task["url"])
        api = f"https://{parts.netloc}/wday/cxs/{tenant}/{site}/jobs"
        jobs: list[dict[str, Any]] = []
        for offset in range(0, _candidate_limit(task), 20):
            payload = client.post_json(
                api,
                {"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": ""},
            )
            batch = payload.get("jobPostings", []) if isinstance(payload, dict) else []
            if not batch:
                break
            jobs.extend(j for j in batch if isinstance(j, dict))
            if len(batch) < 20:
                break
        candidates = []
        base = f"https://{parts.netloc}/{site}"
        for job in jobs[:_candidate_limit(task)]:
            external = str(job.get("externalPath") or job.get("bulletFields", [""])[0] or "")
            url = urllib.parse.urljoin(base + "/", external.lstrip("/"))
            candidates.append(ProviderCandidate(
                company=task["company"],
                title=_norm(job.get("title", "")),
                url=url,
                location=_norm(job.get("locationsText") or " ".join(job.get("locations", []))),
                remote_policy=_remote_policy(_json_text(job)),
                provider=self.provider,
                provider_job_id=str(job.get("jobPostingId") or job.get("id") or ""),
                description="",
                raw=job,
            ))
        return DiscoveryResult(task["id"], self.provider, task["company"], task["url"],
                               "scanned", candidates, method=api)


class PersonioAdapter(ProviderAdapter):
    provider = "personio"

    def can_handle(self, url: str, source: dict[str, Any]) -> bool:
        host = urllib.parse.urlsplit(url).netloc.lower()
        return "jobs.personio." in host

    def discover(self, task: dict[str, Any], client: HttpClient,
                 intent: SearchIntent) -> DiscoveryResult:
        parts = urllib.parse.urlsplit(task["url"])
        feed = urllib.parse.urlunsplit((parts.scheme or "https", parts.netloc, "/xml", "", ""))
        text = client.get_text(feed)
        root = ET.fromstring(text)
        candidates = []
        for pos in root.findall(".//position")[:_candidate_limit(task)]:
            def find_text(name: str) -> str:
                node = pos.find(name)
                return _norm(node.text if node is not None else "")
            job_id = find_text("id") or find_text("requisition_id")
            title = find_text("name")
            loc = find_text("office") or find_text("location")
            desc = " ".join(_norm(" ".join(elem.itertext())) for elem in pos.findall(".//jobDescription"))
            url_node = pos.find("job_url")
            url = _norm(url_node.text if url_node is not None else "")
            if not url and job_id:
                url = urllib.parse.urlunsplit(
                    (parts.scheme or "https", parts.netloc, f"/job/{job_id}", "", "")
                )
            candidates.append(ProviderCandidate(
                company=task["company"], title=title, url=url, location=loc,
                remote_policy=_remote_policy(loc + " " + desc), provider=self.provider,
                provider_job_id=job_id, description=desc, raw={"xml_id": job_id},
            ))
        return DiscoveryResult(task["id"], self.provider, task["company"], task["url"],
                               "scanned", candidates, method=feed)


class RecruiteeAdapter(ProviderAdapter):
    provider = "recruitee"

    def can_handle(self, url: str, source: dict[str, Any]) -> bool:
        return "recruitee.com" in urllib.parse.urlsplit(url).netloc.lower()

    def discover(self, task: dict[str, Any], client: HttpClient,
                 intent: SearchIntent) -> DiscoveryResult:
        parts = urllib.parse.urlsplit(task["url"])
        api = urllib.parse.urlunsplit((parts.scheme or "https", parts.netloc, "/api/offers/", "", ""))
        payload = client.get_json(api)
        jobs = payload.get("offers", payload if isinstance(payload, list) else [])
        candidates = []
        for job in jobs[:_candidate_limit(task)] if isinstance(jobs, list) else []:
            if not isinstance(job, dict):
                continue
            locs = job.get("locations") if isinstance(job.get("locations"), list) else []
            loc = "; ".join(_norm(loc.get("name", "")) for loc in locs if isinstance(loc, dict))
            candidates.append(ProviderCandidate(
                company=task["company"],
                title=_norm(job.get("title", "")),
                url=str(job.get("careers_url") or job.get("url") or ""),
                location=loc,
                remote_policy=_remote_policy(_json_text(job)),
                provider=self.provider,
                provider_job_id=str(job.get("id") or ""),
                description=str(job.get("description") or ""),
                raw=job,
            ))
        return DiscoveryResult(task["id"], self.provider, task["company"], task["url"],
                               "scanned", candidates, method=api)


class TeamtailorAdapter(ProviderAdapter):
    provider = "teamtailor"

    def can_handle(self, url: str, source: dict[str, Any]) -> bool:
        return "teamtailor.com" in urllib.parse.urlsplit(url).netloc.lower()

    def discover(self, task: dict[str, Any], client: HttpClient,
                 intent: SearchIntent) -> DiscoveryResult:
        parts = urllib.parse.urlsplit(task["url"])
        feed = urllib.parse.urlunsplit((parts.scheme or "https", parts.netloc, "/jobs.rss", "", ""))
        text = client.get_text(feed)
        root = ET.fromstring(text)
        candidates = []
        for item in root.findall(".//item")[:_candidate_limit(task)]:
            def find_text(name: str) -> str:
                node = item.find(name)
                return _norm(node.text if node is not None else "")
            title = find_text("title")
            url = find_text("link")
            desc = find_text("description")
            candidates.append(ProviderCandidate(
                company=task["company"], title=title, url=url, location="",
                remote_policy=_remote_policy(desc), provider=self.provider,
                provider_job_id=url.rsplit("/", 1)[-1], description=desc,
                raw={"rss_link": url},
            ))
        return DiscoveryResult(task["id"], self.provider, task["company"], task["url"],
                               "scanned", candidates, method=feed)


class ICIMSAdapter(ProviderAdapter):
    provider = "icims"

    def can_handle(self, url: str, source: dict[str, Any]) -> bool:
        host = urllib.parse.urlsplit(url).netloc.lower()
        return "jibeapply.com" in host or "icims.com" in host

    def discover(self, task: dict[str, Any], client: HttpClient,
                 intent: SearchIntent) -> DiscoveryResult:
        parts = urllib.parse.urlsplit(task["url"])
        api_path = "/api/jobs"
        payload = client.get_json(urllib.parse.urlunsplit(
            (parts.scheme or "https", parts.netloc, api_path, "page=1", "")
        ))
        jobs = payload.get("jobs", payload.get("data", [])) if isinstance(payload, dict) else []
        candidates = []
        for job in jobs[:_candidate_limit(task)] if isinstance(jobs, list) else []:
            if not isinstance(job, dict):
                continue
            url = str(job.get("url") or job.get("canonicalUrl") or job.get("applyUrl") or "")
            if url and url.startswith("/"):
                url = urllib.parse.urljoin(task["url"], url)
            candidates.append(ProviderCandidate(
                company=task["company"],
                title=_norm(job.get("title") or job.get("name") or ""),
                url=url,
                location=_norm(job.get("location") or job.get("city") or ""),
                remote_policy=_remote_policy(_json_text(job)),
                provider=self.provider,
                provider_job_id=str(job.get("id") or job.get("jobId") or ""),
                description=str(job.get("description") or ""),
                raw=job,
            ))
        return DiscoveryResult(task["id"], self.provider, task["company"], task["url"],
                               "scanned", candidates, method=api_path)


class GenericHtmlAdapter(ProviderAdapter):
    provider = "official_html"

    def can_handle(self, url: str, source: dict[str, Any]) -> bool:
        source_type = str(source.get("type", "")).lower()
        return "official" in source_type or "career" in source_type

    def discover(self, task: dict[str, Any], client: HttpClient,
                 intent: SearchIntent) -> DiscoveryResult:
        text = client.get_text(task["url"])
        parser = _LinkExtractor()
        parser.feed(text)
        candidates: list[ProviderCandidate] = []
        seen: set[str] = set()
        for link in parser.links:
            href = _clean_candidate_href(urllib.parse.urljoin(task["url"], link["href"]))
            if href in seen:
                continue
            seen.add(href)
            if not _looks_job_link(href, link.get("text", "")):
                continue
            title = _norm(link.get("text", "")) or _title_from_url(href)
            candidates.append(ProviderCandidate(
                company=task["company"],
                title=title,
                url=href,
                location="",
                remote_policy="unknown",
                provider=self.provider,
                description="",
                raw={"anchor_text": title},
            ))
            if len(candidates) >= _detail_fetch_limit(task):
                break
        if not candidates and str(task.get("source", {}).get("render", "")).lower() in {"js", "json_api"}:
            candidates = _browser_listing_candidates(task)
        return DiscoveryResult(task["id"], self.provider, task["company"], task["url"],
                               "scanned", candidates, method="static_html")


ADAPTERS: list[ProviderAdapter] = [
    GreenhouseAdapter(),
    LeverAdapter(),
    AshbyAdapter(),
    WorkdayAdapter(),
    SmartRecruitersAdapter(),
    PersonioAdapter(),
    RecruiteeAdapter(),
    TeamtailorAdapter(),
    ICIMSAdapter(),
    GenericHtmlAdapter(),
]


def _runtime_policy(task: dict[str, Any]) -> SearchRuntimePolicy:
    value = task.get("runtime_policy")
    if isinstance(value, SearchRuntimePolicy):
        return value
    return SEARCH_RUNTIME_PROFILES[DEFAULT_SEARCH_RUNTIME_PROFILE]


def _candidate_limit(task: dict[str, Any]) -> int:
    return _runtime_policy(task).candidate_limit_for(str(task.get("provider", "")))


def _detail_fetch_limit(task: dict[str, Any]) -> int:
    return _runtime_policy(task).detail_fetch_limit_for(str(task.get("provider", "")))


def _resolve_runtime_policy(project: Path, requested_profile: str | None = None) -> SearchRuntimePolicy:
    raw = requested_profile
    if not raw:
        raw = str(project_meta.load(project).get(
            "search_runtime_profile", DEFAULT_SEARCH_RUNTIME_PROFILE
        ))
    key = str(raw or DEFAULT_SEARCH_RUNTIME_PROFILE).strip().lower()
    return SEARCH_RUNTIME_PROFILES.get(key, SEARCH_RUNTIME_PROFILES[DEFAULT_SEARCH_RUNTIME_PROFILE])


def _remote_policy(text: str) -> str:
    lower = str(text or "").lower()
    if "hybrid" in lower:
        return "hybrid"
    if "remote" in lower:
        return "remote"
    if "onsite" in lower or "on-site" in lower or "office" in lower:
        return "onsite"
    return "unknown"


def _looks_non_job_title(text: str) -> bool:
    title = _norm(text).lower()
    if not title:
        return False
    return any(re.search(pattern, title) for pattern in NON_JOB_TITLE_PATTERNS)


def _clean_candidate_href(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    path = re.sub(
        r"(/about/careers/applications/jobs/results)/(?:jobs/results/)+",
        r"\1/",
        parts.path,
        flags=re.I,
    )
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, path, parts.query, parts.fragment))


def _looks_job_link(url: str, text: str) -> bool:
    lower = f"{url} {text}".lower()
    if _looks_non_job_title(text):
        return False
    if any(bad in lower for bad in (
        "login", "privacy", "terms", "cookie", "benefits", "candidate-resources",
        "employee-awards", "life-at-stripe", "recruitment-fraud", "resources/interview",
    )):
        return False
    return job_url_policy.is_direct_posting_url(url)


def _browser_listing_candidates(task: dict[str, Any]) -> list[ProviderCandidate]:
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
    except ImportError:
        return []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.goto(
                    task["url"],
                    wait_until="networkidle",
                    timeout=max(5000, _runtime_policy(task).timeout_s * 1000),
                )
                links = page.evaluate(
                    """() => Array.from(document.querySelectorAll("a[href]"))
                        .slice(0, 600)
                        .map((a) => ({
                            href: a.href || "",
                            text: (a.innerText || a.textContent || "").trim()
                        }))"""
                )
            finally:
                browser.close()
    except Exception:
        return []
    candidates: list[ProviderCandidate] = []
    seen: set[str] = set()
    for link in links if isinstance(links, list) else []:
        if not isinstance(link, dict):
            continue
        href = _clean_candidate_href(str(link.get("href") or "").strip())
        title = _norm(str(link.get("text") or "")) or _title_from_url(href)
        if not href or href in seen or not _looks_job_link(href, title):
            continue
        seen.add(href)
        candidates.append(ProviderCandidate(
            company=task["company"],
            title=title,
            url=href,
            location="",
            remote_policy="unknown",
            provider="official_html",
            description="",
            raw={"anchor_text": title, "browser_listing": True},
        ))
        if len(candidates) >= _detail_fetch_limit(task):
            break
    return candidates


def _title_from_url(url: str) -> str:
    path = urllib.parse.urlsplit(url).path.rstrip("/")
    part = path.rsplit("/", 1)[-1]
    part = re.sub(r"^\d+[-_]*", "", part)
    part = re.sub(r"[-_]+", " ", part)
    return _norm(part.title())


def _intent(project: Path) -> SearchIntent:
    meta = project_meta.load(project)
    focus = str(meta.get("focus_role", ""))
    terms = set(DEFAULT_ROLE_TERMS if focus else [])
    lower_focus = focus.lower()
    for key, values in ROLE_SYNONYMS.items():
        if key in lower_focus:
            terms.update(values)
    for piece in re.split(r"[,/|;]+|\band\b", lower_focus):
        piece = piece.strip()
        if len(piece) >= 3:
            terms.add(piece)
    locations = [str(x) for x in meta.get("target_locations", []) if str(x).strip()]
    loc_terms: set[str] = set()
    for loc in locations:
        lower = loc.lower().strip()
        loc_terms.add(lower)
        if lower in {"sf", "san francisco"}:
            loc_terms.update({"san francisco", "bay area", "california", "ca", "united states"})
        if lower in {"la", "l.a.", "los angeles"}:
            loc_terms.update({"los angeles", "santa monica", "culver city", "california", "ca"})
        if "san mateo" in lower:
            loc_terms.update({"san mateo", "bay area", "california", "ca"})
    return SearchIntent(
        target_locations=locations,
        focus_role=focus,
        target_level=str(meta.get("target_level", "")),
        target_companies=[str(x) for x in meta.get("target_companies", []) if str(x).strip()],
        negatives=[str(x) for x in meta.get("negatives", []) if str(x).strip()],
        role_terms={t for t in terms if len(t) >= 2},
        location_terms={t for t in loc_terms if t},
    )


def _load_universe(project: Path, intent: SearchIntent) -> dict[str, Any]:
    path = project / "targets" / "company-universe.json"
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            if isinstance(data, dict) and data.get("buckets"):
                return data
        except json.JSONDecodeError:
            pass
    companies = [
        {
            "name": company,
            "seed": True,
            "rationale": "Declared target company seed from project metadata.",
            "evidence": "project-meta.json target_companies",
            "priority": "high",
        }
        for company in intent.target_companies
    ]
    data = {
        "generated_at": _today(),
        "project": project.name,
        "expansion_mode": "seed-only-deterministic-baseline",
        "expansion_note": (
            "Deterministic baseline uses declared companies and existing universe "
            "artifacts. Run an LLM-assisted market-map phase separately for broad "
            "adjacent-company expansion."
        ),
        "buckets": [{
            "bucket": "declared-target-companies",
            "why_relevant": "User-declared target companies for deterministic baseline search.",
            "expansion_note": "Seed-only deterministic baseline.",
            "companies": companies,
        }],
        "excluded": [],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return data


def _universe_companies(universe: dict[str, Any], intent: SearchIntent) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for bucket in universe.get("buckets", []) if isinstance(universe, dict) else []:
        for company in bucket.get("companies", []) if isinstance(bucket, dict) else []:
            if not isinstance(company, dict) or not company.get("name"):
                continue
            key = _norm_key(company["name"])
            if key in seen:
                continue
            seen.add(key)
            out.append(dict(company))
    for company in intent.target_companies:
        key = _norm_key(company)
        if key not in seen:
            seen.add(key)
            out.append({"name": company, "seed": True,
                        "rationale": "Declared target company seed."})
    return out


def _load_or_build_source_plan(project: Path, companies: list[dict[str, Any]]) -> dict[str, Any]:
    path = project / "targets" / "source-plan.json"
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            if isinstance(data, dict) and isinstance(data.get("companies"), list):
                return data
        except json.JSONDecodeError:
            pass
    plans = [
        resolve_company_sources.source_plan_for_company(str(company.get("name", "")))
        for company in companies
        if str(company.get("name", "")).strip()
    ]
    data = {
        "generated_at": _today(),
        "project": project.name,
        "phase_scope": "deterministic search generated source plan",
        "source_order": [
            "registered official careers source",
            "structured ATS/provider API",
            "official static careers HTML",
            "LinkedIn optional supplemental pass",
        ],
        "companies": plans,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return data


def _adapter_for(url: str, source: dict[str, Any]) -> ProviderAdapter | None:
    for adapter in ADAPTERS:
        if adapter.can_handle(url, source):
            return adapter
    return None


def _source_urls(source: dict[str, Any]) -> list[str]:
    urls: list[str] = []
    if source.get("url"):
        urls.append(str(source["url"]))
    for url in source.get("urls", []) if isinstance(source.get("urls"), list) else []:
        urls.append(str(url))
    out: list[str] = []
    seen: set[str] = set()
    for url in urls:
        clean = url.strip()
        if not clean or clean in seen or not clean.startswith("http"):
            continue
        seen.add(clean)
        out.append(clean)
    return out


def _build_tasks(
    plan: dict[str, Any],
    runtime_policy: SearchRuntimePolicy,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    tasks: list[dict[str, Any]] = []
    unsupported: list[dict[str, Any]] = []
    for company in plan.get("companies", []) if isinstance(plan, dict) else []:
        if not isinstance(company, dict):
            continue
        company_name = str(company.get("name", "")).strip()
        if not company_name:
            continue
        for source_index, source in enumerate(company.get("sources", [])):
            if not isinstance(source, dict):
                continue
            source_type = str(source.get("type", ""))
            if "linkedin" in source_type.lower():
                continue
            urls = _source_urls(source)
            if not urls:
                unsupported.append({
                    "company": company_name,
                    "source": source,
                    "reason": "no executable URL in source entry",
                })
                continue
            for url_index, url in enumerate(urls):
                if "<" in url or ">" in url:
                    unsupported.append({
                        "company": company_name,
                        "source": source,
                        "url": url,
                        "reason": "templated URL requires a provider-specific expander",
                    })
                    continue
                adapter = _adapter_for(url, source)
                if adapter is None:
                    unsupported.append({
                        "company": company_name,
                        "source": source,
                        "url": url,
                        "reason": "no deterministic provider adapter matched this URL",
                    })
                    continue
                task_id = (
                    f"{_slug(company_name)}--{adapter.provider}--"
                    f"{source_index + 1:02d}-{url_index + 1:02d}"
                )
                tasks.append({
                    "id": task_id,
                    "company": company_name,
                    "source": source,
                    "source_index": source_index,
                    "url": url,
                    "provider": adapter.provider,
                    "adapter": adapter,
                    "runtime_policy": runtime_policy,
                })
    return tasks, unsupported


def _discover_task(
    task: dict[str, Any],
    intent: SearchIntent,
    runtime_policy: SearchRuntimePolicy,
) -> DiscoveryResult:
    client = HttpClient(timeout_s=runtime_policy.timeout_s)
    t0 = time.monotonic()
    try:
        result = task["adapter"].discover(task, client, intent)
        result.elapsed_s = round(time.monotonic() - t0, 3)
        if not result.candidates and result.status == "scanned":
            result.status = "no_match"
        return result
    except FetchError as exc:
        return DiscoveryResult(
            task["id"], task["provider"], task["company"], task["url"],
            exc.status, [], str(exc), elapsed_s=round(time.monotonic() - t0, 3),
        )
    except (json.JSONDecodeError, ET.ParseError, OSError, ValueError) as exc:
        return DiscoveryResult(
            task["id"], task["provider"], task["company"], task["url"],
            "failed_retryable", [], f"{type(exc).__name__}: {exc}",
            elapsed_s=round(time.monotonic() - t0, 3),
        )


def _matches_role(candidate: ProviderCandidate, jd_text: str, intent: SearchIntent) -> bool:
    if not intent.role_terms:
        return True
    hay = f"{candidate.title} {candidate.description} {jd_text}".lower()
    return any(term in hay for term in intent.role_terms)


def _matches_location(candidate: ProviderCandidate, jd_text: str,
                      intent: SearchIntent) -> bool:
    if not intent.location_terms:
        return True
    hay = f"{candidate.location} {candidate.title} {jd_text}".lower()
    if any(term in hay for term in REMOTE_TERMS):
        return True
    return any(term in hay for term in intent.location_terms)


def _extract_jd(
    candidate: ProviderCandidate,
    runtime_policy: SearchRuntimePolicy,
    browser_semaphore: threading.BoundedSemaphore,
) -> tuple[str, str, list[str]]:
    fallbacks: list[str] = []
    native = _compact_text(candidate.description)
    if len(native) >= MIN_JD_TEXT_CHARS:
        fallbacks.append("provider_native_description")
        return native, "provider_native_description", fallbacks
    fallbacks.append("provider_native_description_empty_or_short")
    if not candidate.url:
        return "", "missing_direct_url", fallbacks
    client = HttpClient(timeout_s=runtime_policy.timeout_s)
    provider_text, provider_method = _provider_detail_text(candidate, client)
    if provider_method:
        fallbacks.append(provider_method)
    if len(provider_text) >= MIN_JD_TEXT_CHARS and _looks_like_jd_text(provider_text, candidate.title):
        return provider_text, provider_method or "provider_detail_json", fallbacks
    try:
        html_text = client.get_text(candidate.url)
        fallbacks.append("static_fetch")
        text = _readable_html_text(html_text)
        if len(text) >= MIN_JD_TEXT_CHARS and _looks_like_jd_text(text, candidate.title):
            return text, "static_fetch_readable_dom", fallbacks
        fallbacks.append("static_readable_text_short_or_non_jd")
        plain_text = _compact_text(html_text)
        if (
            plain_text != text
            and len(plain_text) >= MIN_JD_TEXT_CHARS
            and _looks_like_jd_text(plain_text, candidate.title)
        ):
            return plain_text, "static_fetch_full_text", fallbacks
        fallbacks.append("compact_html_text_short")
    except FetchError as exc:
        fallbacks.append(str(exc))
    if not runtime_policy.browser_allowed_for(candidate.provider):
        fallbacks.append("browser_skipped_by_runtime_policy")
        return "", "jd_text_too_short", fallbacks
    browser_text, browser_method = _browser_extract(
        candidate.url,
        runtime_policy,
        browser_semaphore,
    )
    fallbacks.append(browser_method)
    if len(browser_text) >= MIN_JD_TEXT_CHARS and _looks_like_jd_text(browser_text, candidate.title):
        return browser_text, "browser_rendered_readable_dom", fallbacks
    return "", "jd_text_too_short", fallbacks


def _provider_detail_text(candidate: ProviderCandidate, client: HttpClient) -> tuple[str, str]:
    if candidate.provider == "workday":
        url = _workday_detail_url(candidate)
        if not url:
            return "", ""
        try:
            payload = client.get_json(url)
        except (FetchError, json.JSONDecodeError):
            return "", "workday_detail_json_failed"
        text = _compact_text("\n".join(_strings_for_keys(
            payload,
            {
                "jobDescription", "jobDescriptionPlain", "jobDescriptionText",
                "description", "qualifications", "responsibilities",
                "jobPostingInfo", "jobRequisition", "externalUrl",
            },
        )))
        return text, "workday_detail_json"
    return "", ""


def _workday_detail_url(candidate: ProviderCandidate) -> str:
    parts = urllib.parse.urlsplit(candidate.url)
    if "myworkdayjobs.com" not in parts.netloc.lower():
        return ""
    host_left = parts.netloc.split(".", 1)[0]
    tenant = host_left.split("_", 1)[0]
    bits = [p for p in parts.path.split("/") if p]
    bits = [p for p in bits if not re.match(r"^[a-z]{2}(-[A-Z]{2})?$", p)]
    if not bits:
        return ""
    site = bits[0]
    external = str(candidate.raw.get("externalPath") or "")
    if not external and len(bits) > 1:
        external = "/" + "/".join(bits[1:])
    if not external:
        return ""
    path = f"/wday/cxs/{tenant}/{site}/{external.lstrip('/')}"
    return urllib.parse.urlunsplit((parts.scheme or "https", parts.netloc, path, "", ""))


def _strings_for_keys(value: Any, keys: set[str]) -> list[str]:
    out: list[str] = []

    def visit(node: Any, parent_key: str = "") -> None:
        if isinstance(node, dict):
            for key, child in node.items():
                key_text = str(key)
                if isinstance(child, str) and key_text in keys:
                    out.append(child)
                else:
                    visit(child, key_text)
        elif isinstance(node, list):
            for child in node:
                visit(child, parent_key)
        elif isinstance(node, str) and parent_key in keys:
            out.append(node)

    visit(value)
    return out


def _browser_extract(
    url: str,
    runtime_policy: SearchRuntimePolicy,
    browser_semaphore: threading.BoundedSemaphore,
) -> tuple[str, str]:
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
    except ImportError:
        return "", "browser runtime unavailable"
    try:
        with browser_semaphore:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                try:
                    page = browser.new_page()
                    page.goto(
                        url,
                        wait_until="networkidle",
                        timeout=max(5000, runtime_policy.timeout_s * 1000),
                    )
                    text = page.evaluate(
                        """() => {
                            const clone = document.body ? document.body.cloneNode(true) : null;
                            if (!clone) return "";
                            clone.querySelectorAll(
                                "script,style,noscript,svg,nav,header,footer,form,aside"
                            ).forEach((node) => node.remove());
                            const selectors = [
                                "main", "[role='main']", "article",
                                "[data-qa*='job' i]", "[data-testid*='job' i]",
                                "[class*='job' i]", "[id*='job' i]",
                                "[class*='posting' i]", "[id*='posting' i]",
                                "[class*='description' i]", "[id*='description' i]",
                                "[class*='content' i]", "[id*='content' i]"
                            ];
                            const signals = [
                                "responsibilities", "requirements", "qualifications",
                                "experience", "about the role", "what you", "you will",
                                "preferred", "compensation", "benefits", "apply"
                            ];
                            const textOf = (node) => (node.innerText || node.textContent || "").trim();
                            const score = (text) => {
                                const lower = text.toLowerCase();
                                let value = Math.min(text.length, 12000);
                                for (const signal of signals) {
                                    if (lower.includes(signal)) value += 500;
                                }
                                for (const noise of ["privacy policy", "cookie", "job alert", "similar jobs"]) {
                                    if (lower.includes(noise)) value -= 350;
                                }
                                return value;
                            };
                            const candidates = [];
                            for (const selector of selectors) {
                                for (const node of clone.querySelectorAll(selector)) {
                                    const text = textOf(node);
                                    if (text.length >= 200) candidates.push(text);
                                }
                            }
                            if (candidates.length) {
                                candidates.sort((a, b) => score(b) - score(a));
                                return candidates[0];
                            }
                            return textOf(clone);
                        }"""
                    )
                    return _compact_text(text), "playwright_chromium_dom"
                finally:
                    browser.close()
    except Exception as exc:
        return "", f"browser render failed: {type(exc).__name__}: {str(exc)[:180]}"


def _requirements(jd_text: str, preferred: bool = False) -> str:
    if preferred:
        keywords = ("preferred", "nice to have", "bonus", "plus", "ideally")
    else:
        keywords = (
            "require", "qualification", "experience", "years", "ability",
            "proven", "must", "responsible", "lead", "manage", "partner",
            "strategy", "strategic",
        )
    matches = [
        s for s in _sentences(jd_text)
        if any(k in s.lower() for k in keywords)
    ]
    return "; ".join(_first_unique(matches, 8))


def _summary(jd_text: str) -> str:
    sentences = _first_unique(_sentences(jd_text), 3)
    if sentences:
        return " ".join(sentences)[:900]
    return jd_text[:900]


def _looks_like_location_fragment(value: str) -> bool:
    text = _norm(value)
    if not text or len(text) > 260:
        return False
    lower = text.lower()
    if re.search(r"\b(remote|hybrid|onsite|on-site)\b", lower):
        return True
    city_countries = getattr(location_normalize, "CITY_COUNTRIES", {})
    country_aliases = getattr(location_normalize, "COUNTRY_ALIASES", {})
    risky_alias_tokens = set(getattr(
        location_normalize, "RISKY_LOCATION_ALIAS_TOKENS", ()
    ))
    if any(re.search(r"\b" + re.escape(city) + r"\b", lower) for city in city_countries):
        return True
    safe_country_aliases = {
        alias for alias in country_aliases
        if alias not in risky_alias_tokens
        and (len(alias) > 2 or alias in {"us", "usa", "uk", "uae", "sg", "kr", "kor"})
    }
    return any(
        re.search(r"\b" + re.escape(alias) + r"\b", lower)
        for alias in safe_country_aliases
    )


def _trim_location_fragment(value: str) -> str:
    text = _norm(value)
    text = re.split(
        r"\b(minimum qualifications|preferred qualifications|responsibilities|"
        r"requirements|team|job type|apply for this role|about the job)\b",
        text,
        maxsplit=1,
        flags=re.I,
    )[0]
    return text.strip(" .")


def _explicit_location_fragments(jd_text: str) -> list[str]:
    lines = [_norm(line) for line in str(jd_text or "").splitlines()]
    lines = [line for line in lines if line]
    out: list[str] = []
    label_re = re.compile(
        r"^(?:job\s+)?(?:locations?|remote locations?|office locations?|"
        r"work location|place)\s*[:\-]?\s*(.*)$",
        re.I,
    )
    for idx, line in enumerate(lines[:180]):
        label = label_re.match(line)
        if label:
            tail = _trim_location_fragment(label.group(1))
            if _looks_like_location_fragment(tail):
                out.append(tail)
            for nxt in lines[idx + 1:idx + 4]:
                if _looks_like_location_fragment(nxt):
                    out.append(_trim_location_fragment(nxt))
                    break
            continue

        preferred = re.search(
            r"(?:preferred working location from the following|"
            r"preferred work location from the following|"
            r"work location from the following)\s*[:\-]?\s*(.+)$",
            line,
            re.I,
        )
        if preferred:
            out.append(_trim_location_fragment(preferred.group(1)))
            continue

        based = re.search(
            r"\b(?:based in|located in|office in)\s+([^.;]+)",
            line,
            re.I,
        )
        if based:
            fragment = _trim_location_fragment(based.group(1))
            if _looks_like_location_fragment(fragment):
                out.append(fragment)

        remote = re.search(
            r"\b(?:remote|hybrid|onsite|on-site)\b\s*"
            r"(?:[-–—,:]|\bin\b|\bwithin\b|\bbased\s+in\b)\s*"
            r"([A-Za-z][A-Za-z .,\-/]*(?:\s*;\s*[A-Za-z][A-Za-z .,\-/]*)*)",
            line,
            re.I,
        )
        if remote:
            out.append(_trim_location_fragment(remote.group(0)))
    return _first_unique([item for item in out if item], 8)


def _infer_location_from_jd(jd_text: str) -> str:
    text = str(jd_text or "")
    city_countries = getattr(location_normalize, "CITY_COUNTRIES", {})
    candidates: list[str] = []
    for fragment in _explicit_location_fragments(text):
        normalized = location_normalize.normalize_location_value(fragment)
        if normalized:
            candidates.extend(piece.strip() for piece in normalized.split(";") if piece.strip())
    if candidates:
        return location_normalize.normalize_location_value("; ".join(_first_unique(candidates, 8)))

    location_lines = [
        line.strip()
        for line in text.splitlines()
        if re.search(r"\b(location|based in|office)\b", line, re.I)
    ][:8]
    haystacks = location_lines or [text[:2500]]
    for hay in haystacks:
        lower = hay.lower()
        for city, country in city_countries.items():
            if re.search(r"\b" + re.escape(city) + r"\b", lower):
                candidates.append(
                    city if country == "Singapore"
                    else f"{city.title()}, {country}"
                )
        if candidates:
            break
    return location_normalize.normalize_location_value("; ".join(_first_unique(candidates, 8)))


def _row_from_candidate(candidate: ProviderCandidate, jd_text: str) -> dict[str, str]:
    jd_text = jd_text_cleaner.clean_jd_text(jd_text, limit=JD_TEXT_CAP)
    info = normalize_job_url.build(candidate.url, candidate.company, candidate.title)
    location = (
        location_normalize.normalize_location_value(candidate.location)
        or _infer_location_from_jd(jd_text)
    )
    row = {
        "job_id": info["job_id"],
        "captured_at": _today(),
        "company": candidate.company,
        "title": candidate.title,
        "job_group": "",
        "location": location,
        "remote_policy": candidate.remote_policy or "unknown",
        "source_url": info["canonical_url"],
        "job_page_url": info["canonical_url"],
        "posting_status": "open",
        "seniority": "",
        "must_have_requirements": _requirements(jd_text),
        "nice_to_have_requirements": _requirements(jd_text, preferred=True),
        "jd_summary": _summary(jd_text),
        "fit_score": "",
        "priority": "",
        "notes": (
            f"Deterministic capture via {candidate.provider}; "
            "fit scoring not run during baseline discovery."
        ),
        "last_seen_at": _today(),
    }
    return {key: str(row.get(key, "")) for key in schema_defs.JOB_LIST_COLUMNS}


def _snapshot(
    candidate: ProviderCandidate,
    row: dict[str, str],
    jd_text: str,
    extraction_method: str,
    source_task_id: str,
    fallbacks: list[str],
) -> dict[str, Any]:
    jd_text = jd_text_cleaner.clean_jd_text(jd_text, limit=JD_TEXT_CAP)
    return {
        "schema": "rolescout-jd-snapshot-v1",
        "job_id": row["job_id"],
        "company": candidate.company,
        "title": candidate.title,
        "canonical_url": row["source_url"],
        "source_url": row["source_url"],
        "provider": candidate.provider,
        "provider_job_id": candidate.provider_job_id,
        "source_task_id": source_task_id,
        "extracted_at": _now_utc(),
        "extraction_method": extraction_method,
        "liveness_status": "open_or_accessible",
        "raw_text": jd_text,
        "jd_text": jd_text,
        "structured_sections": {
            "requirements": _requirements(jd_text),
            "preferred": _requirements(jd_text, preferred=True),
        },
        "fallback_history": fallbacks,
        "warnings": [],
    }


def _is_unresolved_blocker(method: str, fallbacks: list[str]) -> bool:
    text = " ".join([method, *fallbacks]).lower()
    return any(token in text for token in (
        "403", "401", "blocked", "auth", "timeout", "captcha", "rate limit",
        "429", "browser unavailable", "browser runtime unavailable", "js shell",
    ))


def _candidate_log_kept(row: dict[str, str], candidate: ProviderCandidate,
                        source_task_id: str) -> dict[str, Any]:
    return {
        **row,
        "decision": "kept",
        "reason": "Verified direct posting URL and JD snapshot captured deterministically.",
        "reason_code": "",
        "source_task_id": source_task_id,
        "provider": candidate.provider,
    }


def _candidate_log_skipped(company: str, provider: str, count: int,
                           reason: str) -> dict[str, Any]:
    return {
        "company": company,
        "title": f"{count} out-of-scope posting(s)",
        "decision": "skipped",
        "reason": reason,
        "reason_code": SKIP_REASON,
        "count": count,
        "provider": provider,
    }


def _candidate_log_failed(candidate: ProviderCandidate, source_task_id: str,
                          method: str, fallbacks: list[str]) -> dict[str, Any]:
    pending = _is_unresolved_blocker(method, fallbacks)
    return {
        "company": candidate.company,
        "title": candidate.title,
        "source_url": candidate.url,
        "job_page_url": candidate.url,
        "decision": "pending_fallback" if pending else "failed_capture",
        "reason": (
            f"JD extraction blocked by unresolved capture/tooling blocker: {method}"
            if pending else f"JD extraction failed after deterministic attempts: {method}"
        ),
        "reason_code": "run_interrupted" if pending else "capture_error",
        "fallbacks_attempted": fallbacks[:8],
        "source_task_id": source_task_id,
        "provider": candidate.provider,
    }


def _candidate_log_skipped_candidate(
    candidate: ProviderCandidate,
    source_task_id: str,
    reason: str,
    reason_code: str = SKIP_REASON,
) -> dict[str, Any]:
    return {
        "company": candidate.company,
        "title": candidate.title,
        "source_url": candidate.url,
        "job_page_url": candidate.url,
        "decision": "skipped",
        "reason": reason,
        "reason_code": reason_code,
        "source_task_id": source_task_id,
        "provider": candidate.provider,
    }


def _no_postings_candidate(company: str, provider: str, task_id: str) -> dict[str, Any]:
    return {
        "company": company,
        "title": "No matching postings found",
        "decision": "no_postings_found",
        "reason": "Provider scan returned no in-scope postings for the project constraints.",
        "reason_code": "no_postings",
        "source_task_id": task_id,
        "provider": provider,
    }


def _query_from_result(result: DiscoveryResult) -> dict[str, Any]:
    return {
        "scope": "board_enumeration",
        "company": result.company,
        "provider": result.provider,
        "source_task_id": result.task_id,
        "q": result.method or result.source_url,
        "source_url": result.source_url,
        "results_seen": len(result.candidates),
        "observed": result.status if not result.error else f"{result.status}: {result.error}",
        "runner_owned": True,
    }


def _legacy_source_status(status: str) -> str:
    if status in {"scanned"}:
        return "ok"
    if status in {"no_match", "empty"}:
        return "empty"
    if status in {"blocked", "blocked_auth", "blocked_tooling"}:
        return "blocked"
    if status in {"failed_retryable", "failed_terminal"}:
        return "failed"
    return "failed"


def _update_source_plan(
    project: Path,
    plan: dict[str, Any],
    results: list[DiscoveryResult],
    unsupported: list[dict[str, Any]],
) -> None:
    result_by_company: dict[str, list[DiscoveryResult]] = {}
    for result in results:
        result_by_company.setdefault(_norm_key(result.company), []).append(result)
    unsupported_by_company: dict[str, list[dict[str, Any]]] = {}
    for item in unsupported:
        unsupported_by_company.setdefault(_norm_key(item.get("company", "")), []).append(item)

    for company in plan.get("companies", []) if isinstance(plan, dict) else []:
        if not isinstance(company, dict):
            continue
        key = _norm_key(company.get("name", ""))
        company_results = result_by_company.get(key, [])
        company_unsupported = unsupported_by_company.get(key, [])
        fallbacks = list(company.get("fallbacks_used", []))
        for result in company_results:
            fallbacks.append(
                f"{result.provider}: {result.status}"
                + (f" ({result.error})" if result.error else "")
            )
        for item in company_unsupported:
            fallbacks.append(
                f"unsupported source: {item.get('reason', '')}"
                + (f" ({item.get('url')})" if item.get("url") else "")
            )
        company["fallbacks_used"] = _first_unique([str(x) for x in fallbacks if str(x).strip()], 20)

        for source in company.get("sources", []):
            if not isinstance(source, dict):
                continue
            if "linkedin" in str(source.get("type", "")).lower():
                source["status"] = "planned"
                source["note"] = "optional supplemental source; not required for baseline search"
                continue
            urls = set(_source_urls(source))
            matched = [
                result for result in company_results
                if result.source_url in urls or not urls
            ]
            if matched:
                if any(r.status == "scanned" and r.candidates for r in matched):
                    source["status"] = "ok"
                elif all(r.status in {"no_match", "empty"} for r in matched):
                    source["status"] = "empty"
                elif any(r.status.startswith("blocked") for r in matched):
                    source["status"] = "blocked"
                else:
                    source["status"] = "failed"
            elif any(item.get("source") is source for item in company_unsupported):
                source["status"] = "blocked"
                source["note"] = "No deterministic adapter or executable URL for this source yet."
            elif source.get("status") == "planned":
                source["status"] = "blocked"
                source["note"] = "Not selected by deterministic source task generation."

    path = project / "targets" / "source-plan.json"
    path.write_text(json.dumps(plan, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8")


def _persist_rows(project: Path, rows: list[dict[str, str]]) -> tuple[int, str]:
    if not rows:
        return 0, "no rows to persist"
    incoming = project / "data" / "_deterministic_search_rows.json"
    _write_json(incoming, rows)
    result = core.run_script("persist_job_rows", str(incoming), "--project", str(project),
                             env={**os.environ, "RECRUITING_PROJECT_DIR": str(project)})
    try:
        incoming.unlink(missing_ok=True)
    except OSError:
        pass
    return result.returncode, (result.stdout + result.stderr).strip()


def _extract_candidate_work(
    work: CandidateWork,
    runtime_policy: SearchRuntimePolicy,
    browser_semaphore: threading.BoundedSemaphore,
) -> ExtractedCandidate:
    jd_text, extraction_method, fallbacks = _extract_jd(
        work.candidate,
        runtime_policy,
        browser_semaphore,
    )
    return ExtractedCandidate(work, jd_text, extraction_method, fallbacks)


def run_search(
    project: Path,
    *,
    emit=None,
    runtime_profile: str | None = None,
) -> SearchResult:
    """Run deterministic search and write RoleScout-compatible artifacts."""
    project = Path(project)
    if emit is None:
        emit = print
    runtime_policy = _resolve_runtime_policy(project, runtime_profile)
    browser_semaphore = threading.BoundedSemaphore(runtime_policy.browser_slots)
    targets = project / "targets"
    targets.mkdir(parents=True, exist_ok=True)
    (targets / "jobs").mkdir(parents=True, exist_ok=True)
    details_dir = targets / "deterministic-search"
    details_dir.mkdir(parents=True, exist_ok=True)

    intent = _intent(project)
    universe = _load_universe(project, intent)
    companies = _universe_companies(universe, intent)
    plan = _load_or_build_source_plan(project, companies)
    tasks, unsupported = _build_tasks(plan, runtime_policy)
    emit(
        "deterministic_search: "
        f"profile={runtime_policy.name} companies={len(companies)} "
        f"tasks={len(tasks)} unsupported_sources={len(unsupported)}"
    )

    task_records: list[dict[str, Any]] = []
    results: list[DiscoveryResult] = []
    if tasks:
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=max(1, min(runtime_policy.source_workers, len(tasks)))
        ) as pool:
            future_map = {
                pool.submit(_discover_task, task, intent, runtime_policy): task
                for task in tasks
            }
            for fut in concurrent.futures.as_completed(future_map):
                result = fut.result()
                results.append(result)
                emit(
                    f"deterministic_search: {result.company} {result.provider} "
                    f"{result.status} candidates={len(result.candidates)}"
                )
    for item in unsupported:
        task_records.append({
            "task_id": "",
            "company": item.get("company", ""),
            "provider": "unsupported",
            "source_url": item.get("url", ""),
            "status": "blocked_tooling",
            "reason": item.get("reason", ""),
            "source": item.get("source", {}),
        })
    queries = [_query_from_result(result) for result in sorted(results, key=lambda r: r.task_id)]
    candidates_log: list[dict[str, Any]] = []
    rows: list[dict[str, str]] = []
    snapshots: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    skipped_rollups: dict[tuple[str, str], int] = {}
    pending_extractions: list[CandidateWork] = []

    for result in sorted(results, key=lambda r: r.task_id):
        task_records.append({
            "task_id": result.task_id,
            "company": result.company,
            "provider": result.provider,
            "source_url": result.source_url,
            "status": result.status,
            "candidate_count": len(result.candidates),
            "error": result.error,
            "method": result.method,
            "elapsed_s": result.elapsed_s,
        })
        if not result.candidates:
            candidates_log.append(_no_postings_candidate(result.company, result.provider, result.task_id))
            continue
        for candidate in result.candidates:
            if not candidate.title or not candidate.url:
                skipped_rollups[(result.company, result.provider)] = (
                    skipped_rollups.get((result.company, result.provider), 0) + 1
                )
                continue
            original_url = candidate.url
            try:
                canonical = normalize_job_url.canonicalize(original_url)
            except ValueError:
                candidates_log.append(_candidate_log_failed(
                    candidate, result.task_id, "invalid_url", ["provider_url_parse"]
                ))
                continue
            if canonical in seen_urls:
                candidates_log.append({
                    "company": candidate.company,
                    "title": candidate.title,
                    "source_url": canonical,
                    "job_page_url": canonical,
                    "decision": "skipped",
                    "reason": "Duplicate canonical posting URL already captured.",
                    "reason_code": "duplicate",
                    "source_task_id": result.task_id,
                    "provider": candidate.provider,
                })
                continue
            seen_urls.add(canonical)
            pending_extractions.append(CandidateWork(
                len(pending_extractions), result, candidate, canonical
            ))

    extracted: list[ExtractedCandidate] = []
    if pending_extractions:
        emit(
            "deterministic_search: jd_extraction "
            f"candidates={len(pending_extractions)} "
            f"workers={min(runtime_policy.jd_workers, len(pending_extractions))} "
            f"browser_slots={runtime_policy.browser_slots}"
        )
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=max(1, min(runtime_policy.jd_workers, len(pending_extractions)))
        ) as pool:
            future_map = {
                pool.submit(_extract_candidate_work, work, runtime_policy, browser_semaphore): work
                for work in pending_extractions
            }
            for fut in concurrent.futures.as_completed(future_map):
                work = future_map[fut]
                try:
                    extracted.append(fut.result())
                except Exception as exc:
                    candidates_log.append(_candidate_log_failed(
                        work.candidate,
                        work.result.task_id,
                        f"extract_worker_exception:{type(exc).__name__}",
                        [str(exc)[:300]],
                    ))

    for extracted_candidate in sorted(extracted, key=lambda item: item.work.seq):
        result = extracted_candidate.work.result
        candidate = extracted_candidate.work.candidate
        canonical = extracted_candidate.work.canonical_url
        jd_text = extracted_candidate.jd_text
        extraction_method = extracted_candidate.extraction_method
        fallbacks = extracted_candidate.fallbacks
        candidate.url = canonical
        if not _matches_role(candidate, jd_text, intent) or not _matches_location(candidate, jd_text, intent):
            skipped_rollups[(result.company, result.provider)] = (
                skipped_rollups.get((result.company, result.provider), 0) + 1
            )
            continue
        if not job_url_policy.is_direct_posting_url(candidate.url):
            candidates_log.append(_candidate_log_skipped_candidate(
                candidate,
                result.task_id,
                "Candidate URL is a listing/search/non-posting URL, not a direct job posting.",
            ))
            continue
        if _liveness_status(jd_text) != "open_or_accessible":
            candidates_log.append(_candidate_log_failed(
                candidate, result.task_id, "posting_closed_or_expired", fallbacks
            ))
            continue
        if len(jd_text) < MIN_JD_TEXT_CHARS:
            candidates_log.append(_candidate_log_failed(
                candidate, result.task_id, extraction_method, fallbacks
            ))
            continue
        row = _row_from_candidate(candidate, jd_text)
        snapshot = _snapshot(candidate, row, jd_text, extraction_method,
                             result.task_id, fallbacks)
        rows.append(row)
        snapshots.append(snapshot)
        candidates_log.append(_candidate_log_kept(row, candidate, result.task_id))

    for (company, provider), count in sorted(skipped_rollups.items()):
        if count:
            candidates_log.append(_candidate_log_skipped(
                company, provider, count,
                "Posting(s) did not match target role/location constraints or lacked required metadata.",
            ))

    # Ensure every universe company has at least one auditable log entry.
    logged_companies = {_norm_key(c.get("company", "")) for c in candidates_log}
    for company in companies:
        name = str(company.get("name", "")).strip()
        if name and _norm_key(name) not in logged_companies:
            candidates_log.append({
                "company": name,
                "title": "No executable deterministic source completed",
                "decision": "no_postings_found",
                "reason": (
                    "No deterministic provider task completed for this company in the "
                    "baseline scan; see source-plan and source-tasks for blocked sources."
                ),
                "reason_code": "no_postings",
            })

    _update_source_plan(project, plan, results, unsupported)
    for snapshot in snapshots:
        _write_json(targets / "jobs" / f"{snapshot['job_id']}.json", snapshot)

    research_log = {
        "schema": "research-log-v1",
        "search_engine": "deterministic-provider-v1",
        "generated_at": _now_utc(),
        "merge_status": "ok",
        "parts": ["deterministic-provider-v1"],
        "failed_parts": [],
        "warnings": [],
        "queries": queries,
        "candidates": candidates_log,
    }
    _write_json(targets / "research-log.json", research_log)
    _write_json(details_dir / "source-tasks.json", {
        "schema": "rolescout-source-tasks-v1",
        "generated_at": _now_utc(),
        "tasks": task_records,
    })
    _write_json(details_dir / "candidates.json", {
        "schema": "rolescout-deterministic-candidates-v1",
        "generated_at": _now_utc(),
        "kept_rows": rows,
        "snapshots": [s["job_id"] for s in snapshots],
    })
    thesis = targets / "opportunity-thesis.md"
    if not thesis.exists():
        thesis.write_text(
            "# Opportunity Thesis\n\n"
            "Deterministic baseline search uses project-declared targets and any "
            "existing company-universe artifact. LLM market-map expansion and fit "
            "judgment are separate post-capture steps.\n",
            encoding="utf-8",
        )

    persist_rc, persist_out = _persist_rows(project, rows)
    emit("deterministic_search: persist_job_rows " + ("OK" if persist_rc == 0 else "REFUSED"))
    if persist_out:
        emit(persist_out[:1200])

    coverage = core.run_script("generate_coverage_audit", str(project),
                               env={**os.environ, "RECRUITING_PROJECT_DIR": str(project)})
    if coverage.stdout.strip():
        emit(coverage.stdout.strip())
    if coverage.stderr.strip():
        emit(coverage.stderr.strip())

    view = core.run_script("build_search_view", str(project),
                           env={**os.environ, "RECRUITING_PROJECT_DIR": str(project)})
    view_output = (view.stdout + view.stderr).strip()
    emit("deterministic_search: build_search_view " + ("OK" if view.returncode == 0 else "FAILED"))
    if view_output:
        emit(view_output[:1200])

    summary = {
        "schema": "rolescout-deterministic-search-summary-v1",
        "generated_at": _now_utc(),
        "project": project.name,
        "companies": len(companies),
        "source_tasks": len(tasks),
        "unsupported_sources": len(unsupported),
        "runtime_profile": runtime_policy.name,
        "source_workers": min(runtime_policy.source_workers, len(tasks)) if tasks else 0,
        "provider_results": len(results),
        "candidates_seen": sum(len(result.candidates) for result in results),
        "jd_extraction_candidates": len(pending_extractions),
        "jd_extraction_workers": (
            min(runtime_policy.jd_workers, len(pending_extractions))
            if pending_extractions else 0
        ),
        "browser_slots": runtime_policy.browser_slots,
        "kept_rows": len(rows),
        "snapshots": len(snapshots),
        "persist_returncode": persist_rc,
        "coverage_returncode": coverage.returncode,
        "view_returncode": view.returncode,
        "llm_tokens_in": 0,
        "llm_tokens_out": 0,
        "notes": [
            "Baseline search is deterministic and provider-first.",
            "Fit scoring, grouping, resume tailoring, and interview prep are downstream steps.",
            "LinkedIn Jobs is optional supplemental coverage and is not a baseline blocker.",
        ],
    }
    out_path = details_dir / "summary.json"
    _write_json(out_path, summary)
    if persist_rc != 0:
        status = "failed"
    elif rows:
        status = "ok"
    else:
        status = "partial"
    return SearchResult(status=status, summary=summary, output_path=out_path)


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Run deterministic RoleScout search.")
    parser.add_argument("project", type=Path)
    parser.add_argument(
        "--profile",
        choices=sorted(SEARCH_RUNTIME_PROFILES),
        help=(
            "search runtime profile; defaults to project-meta.json "
            f"search_runtime_profile or {DEFAULT_SEARCH_RUNTIME_PROFILE}"
        ),
    )
    parser.add_argument("--json", action="store_true",
                        help="print only the final summary JSON")
    args = parser.parse_args(argv)
    lines: list[str] = []

    def emit(text: str) -> None:
        if args.json:
            lines.append(text)
        else:
            print(text, flush=True)

    result = run_search(args.project, emit=emit, runtime_profile=args.profile)
    if args.json:
        print(json.dumps(result.summary, indent=2, ensure_ascii=False))
    if result.status == "failed":
        return 1
    if result.status == "partial":
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
