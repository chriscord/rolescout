#!/usr/bin/env python3
"""Validate prep-linkedin review artifacts mechanically."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


REQUIRED_SCORE_SECTIONS = {
    "headline": "Headline",
    "about": "About",
    "experienceentries": "Experience entries",
    "skills": "Skills",
    "education": "Education",
    "activity": "Activity",
}
UNSCORED_SECTIONS = {"featured", "licenses", "licensescertifications"}


def _configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def _key(value: str) -> str:
    return "".join(ch for ch in value.lower() if ch.isalnum())


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _review_files(target: Path) -> list[Path]:
    if target.is_file():
        return [target]
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


def _part2_blocks(text: str) -> dict[str, str]:
    marker = re.search(r"^##\s+Part\s+2\b.*$", text, re.I | re.M)
    if not marker:
        return {}
    tail = text[marker.end():]
    blocks: dict[str, str] = {}
    matches = list(re.finditer(r"^###\s+(.+?)\s*$", tail, re.M))
    for i, match in enumerate(matches):
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(tail)
        blocks[_key(match.group(1))] = tail[start:end]
    return blocks


def validate_file(path: Path) -> list[str]:
    errors: list[str] = []
    try:
        text = _read(path)
    except OSError as e:
        return [f"{path}: cannot read: {e}"]

    label = str(path)
    if not re.search(r"^##\s+Part\s+1\b", text, re.I | re.M):
        errors.append(f"{label}: missing Part 1 heading")
    if not re.search(r"^##\s+Part\s+2\b", text, re.I | re.M):
        errors.append(f"{label}: missing Part 2 heading")

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

    blocks = _part2_blocks(text)
    for key, section in REQUIRED_SCORE_SECTIONS.items():
        block = blocks.get(key)
        if not block:
            errors.append(f"{label}: Part 2 missing proposal section '{section}'")
            continue
        if not re.search(r"\*\*Current:?\*\*", block, re.I):
            errors.append(f"{label}: proposal '{section}' missing Current block")
        if not re.search(r"\*\*(Add|Proposed|Change):?\*\*", block, re.I):
            errors.append(f"{label}: proposal '{section}' missing Add/Proposed/Change block")

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
