"""Deterministic cleanup and compact briefing for job descriptions."""

from __future__ import annotations

import hashlib
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

MUST_SECTION_RE = re.compile(
    r"^(?:minimum|basic|required|must[- ]have|essential)\s+"
    r"(?:qualifications?|requirements?|skills?)\s*:?$|^(?:requirements?|qualifications?)\s*:?$",
    re.I,
)
PREFERRED_SECTION_RE = re.compile(
    r"^(?:preferred|desired|nice[- ]to[- ]have|bonus)\s+"
    r"(?:qualifications?|requirements?|skills?)\s*:?$|^nice\s+to\s+have\s*:?$",
    re.I,
)
OTHER_SECTION_RE = re.compile(
    r"^(?:responsibilities|what you(?:'|’)ll do|about (?:the|this) role|"
    r"benefits|company description|job description|job information|industry|"
    r"about (?:tiktok|meta|openai|the company)|diversity(?:\s*&\s*inclusion)?|"
    r"logistics|additional information|representative projects?|how we're different|"
    r"come work with us!?|the anthropic economic index)\s*:?$",
    re.I,
)
PREFERRED_MARKER_RE = re.compile(
    r"\b(?:preferred|preferably|nice to have|ideally|bonus|a plus|advantageous)\b", re.I
)
MUST_MARKER_RE = re.compile(
    r"\b(?:must|required|minimum qualification|basic qualification|"
    r"requires?|need to have|we are looking for)\b", re.I
)
ESSENTIAL_CREDENTIAL_RE = re.compile(
    r"\b(?:ph\.?d\.?|doctorate|doctoral degree|master(?:'s)? degree|"
    r"bachelor(?:'s)? degree|licensed|licensure|professional license|"
    r"certified|certification|bar admission|admitted to (?:the )?bar|"
    r"cpa|chartered accountant|security clearance)\b",
    re.I,
)

YEARS_RE = re.compile(
    r"\b(?P<years>\d{1,2})(?:\s*[-–]\s*\d{1,2})?\s*\+?\s*years?\b", re.I
)
DEGREE_RE = re.compile(
    r"\b(?:ba\s*/\s*bs|bs\s*/\s*ba|b\.?(?:a|s)\.?|bachelor(?:'s)?|"
    r"master(?:'s)?|mba|ph\.?d\.?|doctorate)\b", re.I
)
LANGUAGE_RE = re.compile(
    r"\b(?:english|mandarin|chinese|korean|japanese|french|german|spanish|"
    r"thai|vietnamese|bahasa|arabic)\b", re.I
)
LOCATION_RE = re.compile(
    r"\b(?:onsite|on-site|hybrid|remote|relocat\w*|based in|located in|"
    r"work location)\b", re.I
)
AUTH_RE = re.compile(
    r"\b(?:work authori[sz]ation|authorized to work|visa(?:\s+(?:status|sponsorship))?|"
    r"(?:employment|work)\s+(?:visa\s+)?sponsorship|"
    r"sponsorship(?=.{0,50}\b(?:employment|visa|work authori[sz]ation)\b)|"
    r"security clearance|citizenship (?:is )?required|required citizenship|"
    r"must be (?:a |an )?\w+ citizen)\b", re.I
)
TRAVEL_RE = re.compile(r"\b(?:travel|shift|weekends?|overnight)\b", re.I)
MANAGEMENT_RE = re.compile(
    r"\b(?:people management|manage(?:d|s|ment)?\s+(?:a\s+)?team|direct reports?|"
    r"leadership experience)\b", re.I
)
LICENSE_RE = re.compile(
    r"\b(?:licen[cs](?:e|ed|ure)|certifi(?:ed|cation)|bar admission|"
    r"admitted to (?:the )?bar|cpa|chartered accountant)\b", re.I
)

REQUIREMENT_ATOMS_SCHEMA = "rolenavi-requirement-atoms-v9"


def clean_jd_text(value: str, *, limit: int | None = None) -> str:
    text = str(value or "")
    for src, dst in HTML_ENTITY_REPLACEMENTS.items():
        text = text.replace(src, dst)
    text = html.unescape(text).replace("\u00a0", " ")
    # Several careers pages serialize multiple bullets onto one physical line.
    # Preserve those boundaries before whitespace cleanup so an unrelated later
    # phrase (for example "sponsorship team") cannot reclassify or truncate an
    # earlier requirement.
    text = re.sub(r"[ \t]*[•●▪]\s*", "\n• ", text)
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


def classify_requirements(value: str, *, max_items: int = 16) -> dict[str, list[str]]:
    """Split explicit must/preferred requirements and hard credentials.

    Preferred sections are never promoted into must-have or essential output.
    When headings are absent, only sentences with explicit requirement markers
    enter the must lane; generic responsibilities are intentionally excluded.
    """
    text = clean_jd_text(value)
    must: list[str] = []
    preferred: list[str] = []
    mode = ""

    def add(target: list[str], item: str) -> None:
        item = re.sub(r"^\s*(?:\d+[.)]|[-*•])\s*", "", item).strip(" -*")
        if len(item) >= 12 and item.lower() not in {x.lower() for x in target}:
            target.append(item[:500])

    def add_classified(line: str, default: str) -> None:
        clauses = [part.strip() for part in re.split(r";\s+", line) if part.strip()]
        for clause in clauses or [line]:
            if PREFERRED_MARKER_RE.search(clause):
                add(preferred, clause)
            elif default == "must":
                add(must, clause)
            elif default == "preferred":
                add(preferred, clause)

    for raw in text.splitlines():
        line = re.sub(r"[*_#]", "", raw).strip()
        if not line:
            continue
        if PREFERRED_SECTION_RE.match(line):
            mode = "preferred"
            continue
        if MUST_SECTION_RE.match(line):
            mode = "must"
            continue
        if OTHER_SECTION_RE.match(line):
            mode = "ignore"
            continue

        if mode == "ignore":
            continue
        if mode == "preferred":
            add_classified(line, "preferred")
        elif mode == "must":
            add_classified(line, "must")

    # Some compact/JSON job pages lose section boundaries. Use only explicit
    # lexical markers as a conservative fallback.
    for sentence in compact_sentence_list(text, max_items=60, item_limit=500):
        if PREFERRED_MARKER_RE.search(sentence):
            add(preferred, sentence)
        elif MUST_MARKER_RE.search(sentence):
            add(must, sentence)

    must = must[:max_items]
    preferred = [item for item in preferred if item.lower() not in {x.lower() for x in must}]
    preferred = preferred[:max_items]
    essential = [item for item in must if ESSENTIAL_CREDENTIAL_RE.search(item)]
    return {
        "must_have": must,
        "preferred": preferred,
        "essential_qualifications": essential,
    }


def requirement_atoms(value: str) -> dict[str, Any]:
    """Build lossless, priority-aware requirement candidates from arbitrary JD text.

    This is intentionally high recall. It preserves source quotes and explicit
    obligation cues; the scoring model owns semantic candidate-to-requirement
    matching. Responsibilities never consume the minimum-qualification budget.
    """
    text = clean_jd_text(value)
    atoms: list[dict[str, Any]] = []
    mode = ""

    def category(line: str) -> str:
        if AUTH_RE.search(line):
            return "work_authorization"
        if LOCATION_RE.search(line):
            return "location"
        if LICENSE_RE.search(line):
            return "license"
        if DEGREE_RE.search(line):
            return "degree"
        if LANGUAGE_RE.search(line):
            return "language"
        if TRAVEL_RE.search(line):
            return "travel_schedule"
        if MANAGEMENT_RE.search(line):
            return "management"
        if YEARS_RE.search(line):
            return "experience"
        return "skill_domain"

    def add(line: str, obligation: str) -> None:
        quote = re.sub(r"^\s*(?:\d+[.)]|[-*â€¢])\s*", "", line).strip(" -*")
        if len(quote) < 8:
            return
        cat = category(quote)
        # These constraints remain eligibility gates even when an employer
        # places them under Responsibilities rather than Qualifications.
        if obligation == "responsibility" and cat in {
            "work_authorization", "location", "license", "travel_schedule"
        }:
            obligation = "required"
        if obligation in {"preferred", "nice_to_have"}:
            importance = "bonus" if obligation == "nice_to_have" else "supporting"
        elif obligation == "responsibility":
            importance = "central"
        elif cat in {"work_authorization", "location", "license", "travel_schedule"}:
            importance = "eligibility"
        else:
            importance = "central"
        years = YEARS_RE.search(quote)
        digest = hashlib.sha1(
            f"{obligation}\0{quote.lower()}".encode("utf-8")
        ).hexdigest()[:10]
        atom: dict[str, Any] = {
            "requirement_id": f"REQ-{digest}",
            "source_quote": quote[:500],
            "category": cat,
            "obligation": obligation,
            "importance": importance,
            "substitutable": obligation not in {"minimum_required"},
            "confidence": "high" if obligation != "informational" else "medium",
        }
        if years:
            atom["years"] = int(years.group("years"))
        atoms.append(atom)

    for raw in text.splitlines():
        line = re.sub(r"[*_#]", "", raw).strip()
        if not line:
            continue
        low = line.lower().rstrip(":").strip()
        if PREFERRED_SECTION_RE.match(line):
            mode = "preferred"
            continue
        if re.match(r"^(?:minimum|basic)\s+(?:qualifications?|requirements?|skills?)$", low):
            mode = "minimum_required"
            continue
        if re.match(r"^(?:required|must[- ]have|essential)\s+"
                    r"(?:qualifications?|requirements?|skills?)$", low):
            mode = "required"
            continue
        if MUST_SECTION_RE.match(line) or re.match(
            r"^(?:what (?:we're|we are) looking for|what you bring|"
            r"you might thrive in this role if|who you are)$",
            low,
        ):
            mode = "required"
            continue
        if re.search(r"\bresponsibilities\b$", low) or re.match(
            r"^(?:what you(?:'|â€™)ll do|about (?:the|this) role)$", low
        ):
            mode = "responsibility"
            continue
        if OTHER_SECTION_RE.match(line):
            mode = "ignore"
            continue

        if mode == "ignore":
            continue

        obligation = mode
        if PREFERRED_MARKER_RE.search(line):
            obligation = "preferred"
        # Long company/about paragraphs often contain a stray "required" far
        # from a qualification. Section modes above remain format-independent;
        # this lexical fallback is deliberately limited to bounded lines.
        elif not obligation and len(line) <= 300 and MUST_MARKER_RE.search(line):
            obligation = "required"
        elif not obligation and (
            YEARS_RE.search(line) or DEGREE_RE.search(line) or AUTH_RE.search(line)
            or LICENSE_RE.search(line) or LANGUAGE_RE.search(line)
        ):
            obligation = "informational"
        if obligation:
            add(line, obligation)

    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for atom in atoms:
        key = (atom["obligation"], atom["source_quote"].lower())
        if key in seen:
            continue
        seen.add(key)
        deduped.append(atom)

    priority = {"eligibility": 0, "central": 1, "supporting": 2, "bonus": 3}
    scoring = [
        atom for atom in deduped
        if atom["obligation"] in {"minimum_required", "required"}
        and atom["importance"] in {"eligibility", "central"}
    ]
    scoring.sort(key=lambda atom: (
        priority.get(str(atom["importance"]), 9),
        0 if atom.get("years") is not None else 1,
        str(atom["requirement_id"]),
    ))
    preferred = [
        atom for atom in deduped
        if atom["obligation"] in {"preferred", "nice_to_have"}
    ]
    return {
        "schema": REQUIREMENT_ATOMS_SCHEMA,
        "atoms": deduped,
        # Do not silently discard the ninth (or later) required qualification.
        # Output-aware batching in the runner controls model payload size.
        "scoring_requirements": scoring,
        "preferred_requirements": preferred,
    }


def requirement_coverage_issues(value: str, normalized: dict[str, Any]) -> list[str]:
    """Verify explicit deterministic signals survived normalization."""
    text = clean_jd_text(value)
    relevant: list[str] = []
    mode = ""
    for raw in text.splitlines():
        line = re.sub(r"[*_#]", "", raw).strip()
        low = line.lower().rstrip(":").strip()
        if PREFERRED_SECTION_RE.match(line):
            mode = "preferred"
            continue
        if re.match(r"^(?:minimum|basic)\s+(?:qualifications?|requirements?|skills?)$", low):
            mode = "minimum_required"
            continue
        if (
            MUST_SECTION_RE.match(line)
            or re.match(
                r"^(?:what (?:we're|we are) looking for|what you bring|"
                r"you might thrive in this role if|who you are)$",
                low,
            )
        ):
            mode = "required"
            continue
        if re.search(r"\bresponsibilities\b$", low) or re.match(
            r"^(?:what you(?:'|â€™)ll do|about (?:the|this) role)$", low
        ):
            mode = "responsibility"
            continue
        if OTHER_SECTION_RE.match(line):
            mode = "ignore"
            continue
        if mode == "ignore":
            continue
        if mode or (
            len(line) <= 300 and (
                MUST_MARKER_RE.search(line) or YEARS_RE.search(line)
                or DEGREE_RE.search(line) or LICENSE_RE.search(line)
                or LANGUAGE_RE.search(line) or AUTH_RE.search(line)
            )
        ):
            relevant.append(line)
    relevant_text = "\n".join(relevant)
    quotes = "\n".join(
        str(item.get("source_quote", ""))
        for item in normalized.get("atoms", [])
        if isinstance(item, dict)
    )
    issues: list[str] = []
    signals = {
        "years": YEARS_RE,
        "degree": DEGREE_RE,
        "license": LICENSE_RE,
        "language": LANGUAGE_RE,
        "work_authorization": AUTH_RE,
    }
    for name, pattern in signals.items():
        if pattern.search(relevant_text) and not pattern.search(quotes):
            issues.append(f"explicit {name} signal missing from requirement atoms")
    return issues


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
    essential = sections.get("essential_qualifications", [])
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
        "essential_qualifications": (
            [str(item) for item in essential]
            if isinstance(essential, list)
            else compact_sentence_list(str(essential), max_items=5)
        ),
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
