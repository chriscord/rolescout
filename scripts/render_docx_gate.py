#!/usr/bin/env python3
"""Preflight DOCX render dependencies without crashing the run.

This script deliberately does not attempt a fragile inline render. It gives the
agent a deterministic gate: PASS when the local runtime can render DOCX -> PDF ->
PNG, BLOCKED when structural DOCX checks should be recorded instead.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import sys
from pathlib import Path


def _configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def _has_module(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except ModuleNotFoundError:
        return False


def dependency_status() -> dict:
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    pdftoppm = shutil.which("pdftoppm")
    pdf2image = _has_module("pdf2image")
    missing = []
    if not soffice:
        missing.append("LibreOffice/soffice")
    if not pdftoppm:
        missing.append("Poppler/pdftoppm")
    if not pdf2image:
        missing.append("Python package pdf2image")
    return {
        "ok": not missing,
        "missing": missing,
        "soffice": soffice or "",
        "pdftoppm": pdftoppm or "",
        "python": sys.executable,
        "pdf2image": pdf2image,
    }


def main(argv: list[str] | None = None) -> int:
    _configure_stdio()
    parser = argparse.ArgumentParser(description="Check DOCX render gate dependencies.")
    parser.add_argument("docx", nargs="*", type=Path, help="DOCX files to gate")
    parser.add_argument("--check-only", action="store_true",
                        help="only report dependency state")
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)

    status = dependency_status()
    missing_files = [str(p) for p in args.docx if not p.exists()]
    if missing_files:
        status["ok"] = False
        status.setdefault("missing", []).extend(
            f"DOCX file {path}" for path in missing_files)

    if args.as_json:
        print(json.dumps(status, indent=2, ensure_ascii=False))
    elif status["ok"]:
        print("PASS: render dependencies available")
    else:
        print("BLOCKED: render dependencies unavailable: "
              + ", ".join(status["missing"]))

    return 0 if status["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
