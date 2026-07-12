#!/usr/bin/env python3
"""Validate apply workflow application instruction packets."""

from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import store_io


REQUIRED_HEADINGS = [
    "Position summary",
    "Current posting state",
    "Application route",
    "Required materials",
    "Field-by-field guidance",
    "Sensitive fields",
    "Step-by-step user instructions",
    "What to save after submission",
    "Tracker update recommendation",
]
SENSITIVE_TERMS = (
    "compensation", "salary", "work authorization", "visa", "demographic",
    "legal", "address", "phone", "reference",
)
ROUTE_TERMS = (
    "greenhouse", "lever", "workday", "ashby", "smartrecruiters",
    "application form", "careers", "account", "login", "sign-in",
    "sign in", "email", "referral",
)


def _configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def _packets(project: Path) -> list[Path]:
    return sorted((project / "applications").glob("*/application-instructions.md"))


def _tracker_rows(project: Path) -> list[dict]:
    return store_io.read_project_rows(project, "tracker")


def _path_text(path: Path, project: Path) -> str:
    try:
        return path.relative_to(project).as_posix()
    except ValueError:
        return path.as_posix()


def _has_heading(text: str, heading: str) -> bool:
    pattern = r"^#{1,3}\s+" + re.escape(heading)
    return bool(re.search(pattern, text, re.I | re.M))


def _tracker_mentions(rows: list[dict], rel: str) -> bool:
    rel_lower = rel.lower()
    return any(
        rel_lower in " ".join(str(v or "") for v in row.values()).replace("\\", "/").lower()
        for row in rows
    )


def validate_packet(path: Path, project: Path, tracker_rows: list[dict]) -> list[str]:
    errors: list[str] = []
    rel = _path_text(path, project)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        return [f"{rel}: cannot read: {e}"]
    lower = text.lower()

    for heading in REQUIRED_HEADINGS:
        if not _has_heading(text, heading):
            errors.append(f"{rel}: missing required section '{heading}'")
    if not any(term in lower for term in SENSITIVE_TERMS):
        errors.append(f"{rel}: Sensitive fields section must name user-manual sensitive fields")
    if not any(term in lower for term in ROUTE_TERMS):
        errors.append(f"{rel}: Application route section must identify route/vendor/login/email/referral state")
    if not re.search(r"capture (?:completeness|boundary)|required upload", text, re.I):
        errors.append(f"{rel}: packet must disclose route-capture completeness/boundary")
    if not re.search(r"\b(?:do not|never|not)\b.{0,80}\bsubmit", text, re.I | re.S):
        errors.append(f"{rel}: packet must state that RoleScout does not submit the application")
    if not re.search(r"https?://", text):
        errors.append(f"{rel}: packet must include the verified posting/application URL")
    if re.search(r"https?://[^\s]*\[(?:phone|contact) redacted\]", text, re.I):
        errors.append(f"{rel}: verified posting/application URL was corrupted by redaction")
    if re.search(r"https?://\S+/apply(?:\s|$)", text) and "guessed" in lower:
        errors.append(f"{rel}: guessed /apply URLs are not valid application route evidence")
    if not _tracker_mentions(tracker_rows, rel):
        errors.append(f"{rel}: private pipeline does not reference this instruction file")
    return errors


def main(argv: list[str] | None = None) -> int:
    _configure_stdio()
    parser = argparse.ArgumentParser(description="Validate application instruction packets.")
    parser.add_argument("project", type=Path)
    args = parser.parse_args(argv)
    packets = _packets(args.project)
    if not packets:
        print(f"FAIL: no application-instructions.md files found under {args.project / 'applications'}")
        return 1
    rows = _tracker_rows(args.project)
    errors: list[str] = []
    if not rows:
        errors.append("private pipeline missing or empty; application packets must have tracker rows")
    for packet in packets:
        errors.extend(validate_packet(packet, args.project, rows))
    if errors:
        print(f"FAIL: {len(errors)} application packet issue(s)")
        for error in errors:
            print(f" - {error}")
        return 1
    print(f"PASS: {len(packets)} application instruction packet(s) mechanically valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
