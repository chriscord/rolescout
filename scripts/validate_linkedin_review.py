#!/usr/bin/env python3
"""Validate prep-linkedin review artifacts mechanically."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import store_io  # noqa: E402

REQUIRED_SCORE_SECTIONS = {
    "headline": "Headline",
    "about": "About",
    "experienceentries": "Experience entries",
    "skills": "Skills",
    "education": "Education",
}
UNSCORED_SECTIONS = {"activity", "featured", "licenses", "licensescertifications"}


def _configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def _key(value: str) -> str:
    return "".join(ch for ch in value.lower() if ch.isalnum())


def _slug(value: str, fallback: str = "group") -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(value or "").lower()).strip("-")
    return (slug[:48].strip("-") or fallback)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _focused_groups(project: Path) -> list[str]:
    focus_path = project / "data" / "focused-jobs.json"
    try:
        ids = json.loads(focus_path.read_text(encoding="utf-8-sig")).get("job_ids", [])
    except (OSError, json.JSONDecodeError, ValueError):
        return []
    wanted = {str(job_id) for job_id in ids if str(job_id or "").strip()}
    if not wanted:
        return []
    groups: list[str] = []
    seen: set[str] = set()
    for row in store_io.read_project_rows(project, "job_list"):
        if row.get("job_id") not in wanted:
            continue
        slug = _slug(row.get("job_group", ""), "")
        if slug and slug not in seen:
            seen.add(slug)
            groups.append(slug)
    return groups


def _review_files(target: Path) -> list[Path]:
    if target.is_file():
        return [target]
    if (target / "linkedin").exists():
        groups = _focused_groups(target)
        if groups:
            return [target / "linkedin" / group / "linkedin-review.md" for group in groups]
    linkedin_dir = target / "linkedin"
    if target.name == "linkedin":
        linkedin_dir = target
    if not linkedin_dir.exists():
        return []
    return sorted(linkedin_dir.glob("*/linkedin-review.md"))


def _table_rows(text: str) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line.startswith("|") or "---" in line:
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 2:
            continue
        if _key(cells[0]) == "section":
            continue
        if re.search(r"\d+(?:\.\d+)?\s*/\s*5", cells[1]):
            rows.append((cells[0], cells[1]))
    return rows


def _proposal_blocks(text: str) -> dict[str, str]:
    blocks: dict[str, str] = {}
    matches = list(re.finditer(r"^###\s+(.+?)\s*$", text, re.M))
    for i, match in enumerate(matches):
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        blocks[_key(match.group(1))] = text[start:end]
    return blocks


def _fenced_value(block: str, label: str) -> str:
    inline = re.search(
        rf"```\s*{re.escape(label)}\s*\n(.*?)\n```",
        block,
        re.I | re.S,
    )
    if inline:
        return inline.group(1).strip()
    match = re.search(
        rf"(?:\*\*\s*{re.escape(label)}\s*:?\s*\*\*|"
        rf"^\s*{re.escape(label)}\s*:?\s*$)\s*\n"
        r"```(?:text)?\s*\n(.*?)\n```",
        block,
        re.I | re.M | re.S,
    )
    return match.group(1).strip() if match else ""


def _proposed_content_errors(section_key: str, proposed: str) -> list[str]:
    errors: list[str] = []
    lines = [line.strip() for line in proposed.splitlines() if line.strip()]
    advisory = re.compile(
        r"^(?:for\s+(?:the\s+)?(?:current\s+)?role\b|prioriti[sz]e\b|"
        r"consider\s+adding\b|add\s+only\b|if\s+(?:available|linkedin)\b|"
        r"keep\s+(?:the\s+.+?\s+)?as-is\b|do\s+not\b|recommend(?:ed)?\b)",
        re.I,
    )
    if any(advisory.search(line) for line in lines):
        errors.append("Proposed block contains advisory prose instead of final LinkedIn content")
    if section_key == "experienceentries":
        if not any(re.match(r"^[-*•‣]\s+\S", line) for line in lines):
            errors.append("Experience Proposed block must contain copy-ready bullet lines")
    elif section_key == "skills":
        if len(lines) < 3:
            errors.append("Skills Proposed block must contain at least three skill lines")
        if any("," in line or len(line) > 80 for line in lines):
            errors.append("Skills Proposed block must use one concise skill name per line")
    elif section_key == "education" and len(lines) < 2:
        errors.append("Education Proposed block must contain complete final entries")
    return errors


def validate_file(path: Path) -> list[str]:
    errors: list[str] = []
    try:
        text = _read(path)
    except OSError as e:
        return [f"{path}: cannot read: {e}"]

    label = str(path)
    score_rows = _table_rows(text)
    seen: dict[str, str] = {}
    for section, _score in score_rows:
        key = _key(section)
        if key in UNSCORED_SECTIONS:
            errors.append(f"{label}: '{section}' must not be a scored LinkedIn section")
        elif key in REQUIRED_SCORE_SECTIONS:
            seen[key] = section
        else:
            errors.append(f"{label}: unexpected scored LinkedIn section '{section}'")

    for key, section in REQUIRED_SCORE_SECTIONS.items():
        if key not in seen:
            errors.append(f"{label}: missing scored section '{section}'")

    lower = text.lower()
    if "overall score:" not in lower:
        errors.append(f"{label}: missing parser-friendly 'Overall score:' line")
    if "experience x3" not in lower and "experience \u00d73" not in lower:
        errors.append(f"{label}: overall score line must show Experience x3 weighting")

    blocks = _proposal_blocks(text)
    for key, section in REQUIRED_SCORE_SECTIONS.items():
        block = blocks.get(key)
        if not block:
            errors.append(f"{label}: missing proposal section '{section}'")
            continue
        current = _fenced_value(block, "Current")
        proposed = _fenced_value(block, "Proposed")
        if not current:
            errors.append(f"{label}: proposal '{section}' missing fenced Current block")
        if not proposed:
            errors.append(f"{label}: proposal '{section}' missing fenced Proposed block")
        else:
            for detail in _proposed_content_errors(key, proposed):
                errors.append(f"{label}: proposal '{section}' {detail}")

    return errors


def main(argv: list[str] | None = None) -> int:
    _configure_stdio()
    parser = argparse.ArgumentParser(description="Validate LinkedIn review artifacts.")
    parser.add_argument("target", type=Path, help="Project dir, linkedin dir, or linkedin-review.md")
    args = parser.parse_args(argv)

    files = _review_files(args.target)
    if not files:
        print(f"FAIL: no linkedin-review.md files found under {args.target}")
        return 1

    errors: list[str] = []
    for path in files:
        errors.extend(validate_file(path))

    if errors:
        print(f"FAIL: {len(errors)} LinkedIn review issue(s)")
        for error in errors:
            print(f" - {error}")
        return 1
    print(f"PASS: {len(files)} LinkedIn review file(s) mechanically valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
