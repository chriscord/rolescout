#!/usr/bin/env python3
"""Validate prep-interview artifacts mechanically."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from rolescout.interview_paths import role_slug

REQUIRED_SECTIONS = [
    "Self Introduction",
    "Job Requirements",
    "Adversarial Questions",
    "The Whys",
    "Behavioral Questions",
    "Glossary",
    "News",
    "Questions to Ask",
    "Sources",
]
REQUIRED_WHY_QUESTIONS = [
    "Why this industry",
    "Why this company",
    "Why this position",
    "Why you",
]
REQUIRED_WHY_VERSIONS = ("v1", "v2", "v3")
STORY_KEYS = {
    "id", "title", "source", "situation", "task", "action", "result",
    "best_for", "ev_refs",
}
FUNCTION_AS_INDUSTRY_PHRASES = (
    "strategy and gtm operations",
    "strategy & operations",
    "strategy and operations",
    "strategic finance and corporate development",
    "ai partnerships and business development",
    "business development",
    "corporate development",
    "turns strategy into operating systems",
)
GENERIC_WHY_PHRASES = (
    "current company moment makes operating judgment matter",
    "where the next constraint sits",
    "role sits close to real operating tradeoffs",
    "the position appears to need both synthesis and execution",
    "this role matches my through-line",
)
GENERIC_CONTEXT_TERMS = {
    "gtm", "go-to-market", "operating cadence", "workflow automation",
    "north star metric", "strategy", "operations", "business development",
    "strategic finance", "corporate development", "ai ecosystem",
}
INDUSTRY_MARKET_TERMS = (
    "enterprise ai", "enterprise genai", "cloud ai", "platform economy",
    "superapp", "consumer technology", "consumer ai", "financial services",
    "life sciences", "retail", "developer platform", "southeast asia",
)


def _configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def _key(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip()).lower()


def _why_question_key(value: str) -> str:
    """Normalize harmless heading variants to the four contract concepts."""
    key = re.sub(r"\s+v[123]$", "", _key(value))
    key = re.sub(r"^why\s+", "", key)
    key = re.sub(r"^this\s+", "", key)
    aliases = {
        "industry": "why this industry",
        "company": "why this company",
        "position": "why this position",
        "role": "why this position",
        "job": "why this position",
        "candidate": "why you",
        "you": "why you",
        "me": "why you",
    }
    if "candidate" in key or key.startswith("me "):
        return "why you"
    if key in aliases:
        return aliases[key]
    # Company-specific labels such as "Why Meta" or "Why Google" are a
    # natural rendering of the contract's "Why this company" concept.
    original = _key(value)
    if original.startswith("why ") and key not in {"now", "change"}:
        return "why this company"
    return original


def _slug(text: str, fallback: str = "role") -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(text or "").lower()).strip("-")
    return (slug[:48].strip("-") or fallback)


def _focused_prep_files(project: Path) -> list[Path] | None:
    context_path = project / "interviews" / "interview-context.json"
    if not context_path.exists():
        return None
    try:
        data = json.loads(context_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    roles = data.get("roles") if isinstance(data, dict) else None
    if not isinstance(roles, list):
        return None
    scoped_ids = data.get("prep_scope_job_ids") if isinstance(data, dict) else None
    scoped = set(str(item) for item in scoped_ids) if isinstance(scoped_ids, list) else None
    files: list[Path] = []
    for role in roles:
        if not isinstance(role, dict):
            continue
        if scoped is not None and str(role.get("job_id", "")) not in scoped:
            continue
        slug = role_slug(role)
        files.append(project / "interviews" / slug / "prep-notes.md")
    return files


def _prep_files(target: Path) -> list[Path]:
    if target.is_file():
        return [target]
    if (target / "interviews").exists():
        focused = _focused_prep_files(target)
        if focused is not None:
            return focused
    interviews = target / "interviews"
    if target.name == "interviews":
        interviews = target
    if not interviews.exists():
        return []
    return sorted(interviews.glob("*/prep-notes.md"))


def _story_bank_for(target: Path) -> Path | None:
    if target.is_file():
        for parent in [target.parent, *target.parents]:
            candidate = parent / "story-bank.json"
            if candidate.exists():
                return candidate
        return None
    interviews = target / "interviews"
    if target.name == "interviews":
        interviews = target
    return interviews / "story-bank.json"


def _sections(text: str) -> list[tuple[str, int, int]]:
    matches = list(re.finditer(r"^##\s+(.+?)\s*$", text, re.M))
    out: list[tuple[str, int, int]] = []
    for i, match in enumerate(matches):
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        out.append((match.group(1).strip(), start, end))
    return out


def _section_body(text: str, name: str) -> str:
    key = _key(name)
    for section_name, start, end in _sections(text):
        if _key(section_name) == key:
            return text[start:end]
    return ""


def _table_rows(body: str) -> list[list[str]]:
    rows: list[list[str]] = []
    for line in body.splitlines():
        if not line.lstrip().startswith("|"):
            continue
        stripped = line.strip()
        if re.fullmatch(r"\|[\s\-:|]+\|", stripped):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        rows.append(cells)
    if rows:
        header = [_key(cell) for cell in rows[0]]
        if header[:2] in (["why-question", "version"], ["why question", "version"]):
            rows = rows[1:]
    return rows


def _why_rows(text: str) -> list[tuple[str, str, str]]:
    rows: list[tuple[str, str, str]] = []
    table = _table_rows(_section_body(text, "The Whys"))
    if table and table[0] and _key(table[0][0]) == "version":
        # Also accept the compact horizontal form:
        # Version | Why Industry | Why Company | Why Position | Why Candidate
        canonical_order = [_key(item) for item in REQUIRED_WHY_QUESTIONS]
        headers = canonical_order[:len(table[0]) - 1]
        accepted = {_key(item) for item in REQUIRED_WHY_QUESTIONS}
        for cells in table[1:]:
            if not cells:
                continue
            version = cells[0].strip()
            for question, answer in zip(headers, cells[1:]):
                if question in accepted:
                    rows.append((question, version, answer.strip()))
        return rows
    if (table and len(table[0]) >= 4
            and all(_key(cell) in REQUIRED_WHY_VERSIONS
                    for cell in table[0][1:4])):
        # Also accept the transposed compact form:
        # Why/Topic | V1 | V2 | V3 | Evidence
        canonical_order = [_key(item) for item in REQUIRED_WHY_QUESTIONS]
        for index, cells in enumerate(table[1:]):
            if len(cells) < 4:
                continue
            question = _why_question_key(cells[0])
            if question not in canonical_order and index < len(canonical_order):
                question = canonical_order[index]
            if question not in canonical_order:
                continue
            for version, answer in zip(REQUIRED_WHY_VERSIONS, cells[1:4]):
                rows.append((question, version.upper(), answer.strip()))
        return rows
    for cells in table:
        if len(cells) >= 3:
            embedded_version = re.search(r"\b(v[123])\s*$", _key(cells[0]))
            question = _why_question_key(cells[0])
            if question in {_key(item) for item in REQUIRED_WHY_QUESTIONS}:
                supplied_version = _key(cells[1])
                version = (embedded_version.group(1).upper()
                           if embedded_version and supplied_version not in REQUIRED_WHY_VERSIONS
                           else cells[1].strip())
                rows.append((question, version, cells[2].strip()))
    return rows


def _glossary_terms(text: str) -> list[str]:
    terms: list[str] = []
    for cells in _table_rows(_section_body(text, "Glossary")):
        if not cells:
            continue
        term = _key(cells[0])
        if term and len(term) >= 4 and term not in GENERIC_CONTEXT_TERMS:
            terms.append(term)
    return terms


def _mentions_any(answer: str, terms: list[str]) -> bool:
    haystack = _key(answer)
    return any(term and term in haystack for term in terms)


def _quality_issues(path: Path, text: str) -> list[str]:
    quality: list[str] = []
    why_rows = _why_rows(text)
    seen = {(_why_question_key(question), _key(version[:2]))
            for question, version, _answer in why_rows}
    for question in REQUIRED_WHY_QUESTIONS:
        qkey = _why_question_key(question)
        for version in REQUIRED_WHY_VERSIONS:
            if (qkey, version) not in seen:
                quality.append(f"{path}: The Whys missing {question} {version.upper()}")

    context_terms = _glossary_terms(text)
    for question, version, answer in why_rows:
        qkey = _key(question)
        lowered = _key(answer)
        if qkey == "why this industry":
            functionish = any(phrase in lowered for phrase in FUNCTION_AS_INDUSTRY_PHRASES)
            market_grounded = (any(term in lowered for term in INDUSTRY_MARKET_TERMS)
                               or _mentions_any(answer, context_terms))
            if functionish and not market_grounded:
                quality.append(
                    f"{path}: Why this industry {version} describes a job function "
                    "instead of the web-researched company/product market")
        if qkey in {"why this company", "why this position"}:
            if any(phrase in lowered for phrase in GENERIC_WHY_PHRASES):
                quality.append(
                    f"{path}: {question} {version} uses a generic template; retry "
                    "with company, industry thesis, JD, and news context")
    return quality


def validate_prep_file(path: Path) -> tuple[list[str], list[str], list[tuple[str, str, str]]]:
    errors: list[str] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        return [f"{path}: cannot read: {e}"], [], []

    sections = _sections(text)
    names = [_key(name) for name, _start, _end in sections]
    required_keys = [_key(name) for name in REQUIRED_SECTIONS]
    label = str(path)

    last_index = -1
    for display, key in zip(REQUIRED_SECTIONS, required_keys):
        if key not in names:
            errors.append(f"{label}: missing required section '{display}'")
            continue
        idx = names.index(key)
        if idx < last_index:
            errors.append(f"{label}: section '{display}' is out of order")
        last_index = idx

    by_key = {_key(name): text[start:end] for name, start, end in sections}
    for display, key in zip(REQUIRED_SECTIONS, required_keys):
        body = by_key.get(key)
        if body is None:
            continue
        if "|" not in body:
            errors.append(f"{label}: section '{display}' must contain a markdown table")

    return errors, _quality_issues(path, text), _why_rows(text)


def validate_story_bank(path: Path | None) -> list[str]:
    if path is None:
        return ["story-bank.json missing"]
    if not path.exists():
        return [f"{path}: story-bank.json missing"]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return [f"{path}: story-bank.json unreadable: {e}"]
    entries = data.get("entries")
    if not isinstance(entries, list):
        return [f"{path}: story-bank.json entries must be a list"]
    errors: list[str] = []
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            errors.append(f"{path}: story entry {i} must be an object")
            continue
        missing = sorted(STORY_KEYS - set(entry))
        if missing:
            errors.append(f"{path}: story entry {i} missing {', '.join(missing)}")
    return errors


def main(argv: list[str] | None = None) -> int:
    _configure_stdio()
    parser = argparse.ArgumentParser(description="Validate interview prep artifacts.")
    parser.add_argument("target", type=Path, help="Project dir, interviews dir, or prep-notes.md")
    args = parser.parse_args(argv)

    files = _prep_files(args.target)
    if not files:
        print(f"FAIL: no prep-notes.md files found under {args.target}")
        return 1

    errors: list[str] = []
    quality: list[str] = []
    all_whys: list[tuple[Path, str, str, str]] = []
    errors.extend(validate_story_bank(_story_bank_for(args.target)))
    for path in files:
        file_errors, file_quality, file_whys = validate_prep_file(path)
        errors.extend(file_errors)
        quality.extend(file_quality)
        all_whys.extend((path, question, version, answer)
                        for question, version, answer in file_whys)

    if errors:
        print(f"FAIL: {len(errors)} interview prep issue(s)")
        for error in errors:
            print(f" - {error}")
        return 1
    repeated: dict[tuple[str, str, str], set[str]] = {}
    for path, question, version, answer in all_whys:
        if _key(question) not in {
            "why this industry", "why this company", "why this position",
        }:
            continue
        norm = re.sub(r"\b[A-Z][A-Za-z0-9&.-]+\b", "{name}", answer)
        norm = _key(norm)
        repeated.setdefault((_key(question), _key(version[:2]), norm), set()).add(str(path))
    for question, version, norm in sorted(repeated):
        paths = repeated[(question, version, norm)]
        if len(paths) >= 2 and len(norm) > 80:
            quality.append(
                f"The Whys repeated {question} {version.upper()} answer across "
                f"{len(paths)} files; retry with per-position industry thesis")
    if quality:
        print(f"QUALITY: {len(quality)} interview prep quality issue(s)")
        for issue in quality:
            print(f" - {issue}")
        return 2
    print(f"PASS: {len(files)} interview prep file(s) mechanically valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
