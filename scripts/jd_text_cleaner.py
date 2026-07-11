"""Deterministic cleanup and compact briefing for job descriptions."""

from __future__ import annotations

import html
import re
from typing import Any

HTML_BLOCK_TAGS = (
    "br", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "p",
    "section", "tr",
)
HTML_DROP_TAGS = ("iframe", "noscript", "script", "style", "svg")
HTML_ENTITY_REPLACEMENTS = {
    "&amp;": "&",
    "&apos;": "'",
    "&colon;": ":",
    "&comma;": ",",
    "&emsp;": " ",
    "&ensp;": " ",
    "&gt;": ">",
    "&hellip;": "...",
    "&lt;": "<",
    "&mdash;": "-",
    "&ndash;": "-",
    "&nbsp;": " ",
    "&quot;": '"',
    "&semi;": ";",
}
BOILERPLATE_PATTERNS = (
    r"\b(cookie policy|privacy policy|terms of use)\b",
    r"\b(sign in|create account|job alerts|similar jobs|apply now)\b",
    r"\b(we are an equal opportunity|reasonable accommodation)\b",
)


def clean_jd_text(value: str, *, limit: int | None = None) -> str:
    text = str(value or "")
    for src, dst in HTML_ENTITY_REPLACEMENTS.items():
        text = text.replace(src, dst)
    text = html.unescape(text).replace("\u00a0", " ")
    for tag in HTML_DROP_TAGS:
        text = re.sub(rf"(?is)<{tag}\b.*?</{tag}>", " ", text)
    block = "|".join(re.escape(tag) for tag in HTML_BLOCK_TAGS)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(rf"(?i)</(?:{block})\s*>", "\n", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    lines = [re.sub(r"\s+", " ", line).strip(" -") for line in text.splitlines()]
    out: list[str] = []
    seen: set[str] = set()
    for line in lines:
        if not line or len(line) < 3:
            continue
        low = line.lower()
        if len(line) < 140 and any(re.search(pattern, low) for pattern in BOILERPLATE_PATTERNS):
            continue
        key = re.sub(r"\W+", "", low)
        if key in seen:
            continue
        seen.add(key)
        out.append(line)
    text = "\n".join(out).strip()
    if limit and len(text) > limit:
        cut = text[:limit]
        last = max(cut.rfind("\n"), cut.rfind(". "), cut.rfind("; "))
        if last > int(limit * 0.65):
            cut = cut[:last + 1]
        text = cut.rstrip()
    return text


def compact_sentence_list(value: str, *, max_items: int = 8,
                          item_limit: int = 220) -> list[str]:
    text = clean_jd_text(value)
    candidates = re.split(r"(?:\n+|(?<=[.;:])\s+)", text)
    out: list[str] = []
    seen: set[str] = set()
    for item in candidates:
        item = re.sub(r"\s+", " ", item).strip(" -")
        if len(item) < 20:
            continue
        key = item.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(item[:item_limit].rstrip())
        if len(out) >= max_items:
            break
    return out


def jd_interview_brief(row: dict[str, Any], snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
    snapshot = snapshot or {}
    sections = snapshot.get("structured_sections", {})
    if not isinstance(sections, dict):
        sections = {}
    jd_text = (
        snapshot.get("jd_text")
        or snapshot.get("raw_text")
        or row.get("jd_summary")
        or row.get("must_have_requirements")
        or ""
    )
    jd_clean = clean_jd_text(str(jd_text), limit=6000)
    must = row.get("must_have_requirements") or sections.get("requirements", "")
    nice = row.get("nice_to_have_requirements") or sections.get("preferred", "")
    return {
        "job_id": row.get("job_id") or snapshot.get("job_id", ""),
        "company": row.get("company") or snapshot.get("company", ""),
        "title": row.get("title") or snapshot.get("title", ""),
        "location": row.get("location") or snapshot.get("location", ""),
        "source_url": row.get("job_page_url") or row.get("source_url") or snapshot.get("source_url", ""),
        "summary": " ".join(compact_sentence_list(row.get("jd_summary") or jd_clean, max_items=3, item_limit=260)),
        "responsibilities": compact_sentence_list(jd_clean, max_items=8),
        "must_have_requirements": compact_sentence_list(str(must), max_items=8),
        "nice_to_have_requirements": compact_sentence_list(str(nice), max_items=5),
        "key_phrases": _key_phrases(jd_clean),
    }


def _key_phrases(text: str) -> list[str]:
    phrases: list[str] = []
    patterns = (
        r"\b(?:go[- ]to[- ]market|GTM)\b",
        r"\b(?:business operations|strategy operations|strategic finance)\b",
        r"\b(?:partnerships?|business development|corporate development)\b",
        r"\b(?:AI|machine learning|developer platform|enterprise)\b",
        r"\b(?:media|gaming|consumer revenue|marketplace|growth)\b",
    )
    lowered = {p.lower() for p in phrases}
    for pattern in patterns:
        for match in re.finditer(pattern, text, flags=re.I):
            value = match.group(0).strip()
            if value.lower() not in lowered:
                phrases.append(value)
                lowered.add(value.lower())
            if len(phrases) >= 12:
                return phrases
    return phrases
