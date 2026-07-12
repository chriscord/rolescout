#!/usr/bin/env python3
"""Release-time private-identity, PII, placeholder, and skill-package scan.

Private profile names are compared without printing them; findings use a short
SHA-256 fingerprint. Synthetic fixture identities are explicitly allowlisted.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEXT_SUFFIXES = {".py", ".md", ".json", ".toml", ".yaml", ".yml", ".html", ".txt"}
ALLOWLIST = {"sample candidate", "example person", "alex example"}
PLACEHOLDER = re.compile("<<" + r"HUMAN_INPUT:[^>]+>>")


def _private_names() -> set[str]:
    names: set[str] = set()
    for meta in (ROOT / "profiles").glob("*/profile-meta.json"):
        try:
            name = str(json.loads(meta.read_text(encoding="utf-8")).get("name", "")).strip()
        except (OSError, json.JSONDecodeError):
            continue
        if len(name.split()) >= 2 and name.lower() not in ALLOWLIST:
            names.add(name)
    return names


def _tracked_files() -> list[Path]:
    result = subprocess.run(["git", "ls-files", "-z", "--cached", "--others",
                             "--exclude-standard"], cwd=ROOT,
                            capture_output=True, check=True)
    return [ROOT / value.decode("utf-8", errors="replace")
            for value in result.stdout.split(b"\0") if value]


def _fingerprint(value: str) -> str:
    return hashlib.sha256(value.casefold().encode("utf-8")).hexdigest()[:12]


def _scan_bytes(label: str, data: bytes, names: set[str], findings: list[str]) -> None:
    text = data.decode("utf-8", errors="ignore")
    lower = text.casefold()
    for name in names:
        if name.casefold() in lower:
            findings.append(f"private-identity:{_fingerprint(name)}:{label}")
    if PLACEHOLDER.search(text):
        findings.append(f"unresolved-placeholder:{label}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--history", action="store_true")
    args = parser.parse_args(argv)
    names = _private_names()
    findings: list[str] = []
    for path in _tracked_files():
        if path.is_file() and path.suffix.lower() in TEXT_SUFFIXES:
            _scan_bytes(path.relative_to(ROOT).as_posix(), path.read_bytes(), names, findings)
    for package in (ROOT / "dist").glob("*.skill"):
        try:
            with zipfile.ZipFile(package) as archive:
                for member in archive.namelist():
                    _scan_bytes(f"{package.name}:{member}", archive.read(member), names, findings)
        except (OSError, zipfile.BadZipFile):
            findings.append(f"invalid-skill-package:{package.name}")
    if args.history:
        for name in names:
            result = subprocess.run(
                ["git", "log", "--all", "--format=%H", f"-S{name}", "--"],
                cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace")
            if result.stdout.strip():
                findings.append(f"private-identity-history:{_fingerprint(name)}")
    if findings:
        print(f"FAIL: {len(findings)} release privacy finding(s)")
        for finding in sorted(set(findings)):
            print("  - " + finding)
        return 1
    print("PASS: release sources and skill packages contain no private profile identities or placeholders")
    return 0


if __name__ == "__main__":
    sys.exit(main())
