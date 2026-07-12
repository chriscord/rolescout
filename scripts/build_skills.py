#!/usr/bin/env python3
"""Build deterministic .skill archives with source digests."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXED_TIME = (1980, 1, 1, 0, 0, 0)


def package_bytes(skill_dir: Path) -> bytes:
    name = skill_dir.name
    source = ((skill_dir / "SKILL.md").read_text(encoding="utf-8")
              .replace("\r\n", "\n").replace("\r", "\n").encode("utf-8"))
    manifest = json.dumps({
        "schema": "rolescout-skill-package-v1",
        "skill": name,
        "source_sha256": hashlib.sha256(source).hexdigest(),
    }, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
    output = io.BytesIO()
    # Stored entries avoid platform-specific deflate output while packages stay tiny.
    with zipfile.ZipFile(output, "w", zipfile.ZIP_STORED) as archive:
        for member, data in ((f"{name}/SKILL.md", source),
                             (f"{name}/.source.json", manifest)):
            info = zipfile.ZipInfo(member, FIXED_TIME)
            info.compress_type = zipfile.ZIP_STORED
            info.create_system = 3
            info.create_version = 20
            info.extract_version = 20
            info.flag_bits = 0
            info.external_attr = 0o100644 << 16
            archive.writestr(info, data)
    return output.getvalue()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    dist = ROOT / "dist"
    dist.mkdir(exist_ok=True)
    stale: list[str] = []
    for skill_dir in sorted((ROOT / ".agents" / "skills").iterdir()):
        if not (skill_dir / "SKILL.md").exists():
            continue
        expected = package_bytes(skill_dir)
        target = dist / f"{skill_dir.name}.skill"
        if args.check:
            if not target.exists() or target.read_bytes() != expected:
                stale.append(skill_dir.name)
        else:
            target.write_bytes(expected)
            print(target.relative_to(ROOT))
    if stale:
        print("FAIL: stale skill packages: " + ", ".join(stale))
        return 1
    if args.check:
        print("PASS: skill packages are deterministic and current")
    return 0


if __name__ == "__main__":
    sys.exit(main())
