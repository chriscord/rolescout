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
import os
import shutil
import subprocess
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


def _word_executable() -> str:
    candidates = (
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) /
        "Microsoft Office" / "root" / "Office16" / "WINWORD.EXE",
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) /
        "Microsoft Office" / "root" / "Office16" / "WINWORD.EXE",
    )
    return next((str(path) for path in candidates if path.is_file()), "")


def _word_page_counts(paths: list[Path]) -> dict[str, int]:
    """Use installed Word's pagination engine without saving or mutating files."""
    env = dict(os.environ)
    env["ROLESCOUT_DOCX_PATHS"] = json.dumps(
        [str(path.resolve()) for path in paths], ensure_ascii=False)
    command = r'''
$ErrorActionPreference = "Stop"
$paths = [Environment]::GetEnvironmentVariable("ROLESCOUT_DOCX_PATHS") |
  ConvertFrom-Json
$word = $null
$results = @{}
try {
  $word = New-Object -ComObject Word.Application
  $word.Visible = $false
  $word.DisplayAlerts = 0
  $word.Options.SaveNormalPrompt = $false
  foreach ($path in $paths) {
    $doc = $null
    try {
      $doc = $word.Documents.Open($path, $false, $true)
      $doc.Repaginate()
      $results[$path] = [int]$doc.ComputeStatistics(2)
    } finally {
      if ($doc -ne $null) {
        $doc.Close([ref]0)
        [void][Runtime.InteropServices.Marshal]::ReleaseComObject($doc)
      }
    }
  }
  $results | ConvertTo-Json -Compress
} finally {
  if ($word -ne $null) {
    $word.Quit()
    [void][Runtime.InteropServices.Marshal]::ReleaseComObject($word)
  }
  [GC]::Collect()
  [GC]::WaitForPendingFinalizers()
}
'''
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env, timeout=90, check=False,
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout).strip()[:1000])
    data = json.loads(result.stdout.strip() or "{}")
    return {str(key): int(value) for key, value in data.items()}


def dependency_status() -> dict:
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    pdftoppm = shutil.which("pdftoppm")
    pdf2image = _has_module("pdf2image")
    word = _word_executable()
    missing = []
    if not soffice:
        missing.append("LibreOffice/soffice")
    if not pdftoppm:
        missing.append("Poppler/pdftoppm")
    if not pdf2image:
        missing.append("Python package pdf2image")
    native_stack = not missing
    return {
        "ok": native_stack or bool(word),
        "missing": missing,
        "soffice": soffice or "",
        "pdftoppm": pdftoppm or "",
        "python": sys.executable,
        "pdf2image": pdf2image,
        "word": word,
        "renderer": "native-pdf-stack" if native_stack else
                    "microsoft-word" if word else "",
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

    page_counts: dict[str, int] = {}
    if status["ok"] and status["renderer"] == "microsoft-word" and args.docx:
        try:
            page_counts = _word_page_counts(args.docx)
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
            status["ok"] = False
            status.setdefault("missing", []).append(f"Word pagination failed: {exc}")
        else:
            status["page_counts"] = page_counts
            not_one_page = {
                path: pages for path, pages in page_counts.items() if pages != 1
            }
            if not_one_page:
                status["ok"] = False
                status["page_count_failures"] = not_one_page

    if args.as_json:
        print(json.dumps(status, indent=2, ensure_ascii=False))
    elif status["ok"]:
        if page_counts:
            summary = ", ".join(
                f"{Path(path).name}={pages} page(s)"
                for path, pages in page_counts.items())
            print(f"PASS: Microsoft Word pagination verified ({summary})")
        else:
            print(f"PASS: render dependencies available ({status['renderer']})")
    else:
        failures = status.get("page_count_failures", {})
        if failures:
            summary = ", ".join(
                f"{Path(path).name}={pages} page(s)"
                for path, pages in failures.items())
            print("FAIL: resume must be exactly one page: " + summary)
        else:
            print("BLOCKED: render dependencies unavailable: "
                  + ", ".join(status["missing"]))

    return 0 if status["ok"] else 1 if status.get("page_count_failures") else 2


if __name__ == "__main__":
    raise SystemExit(main())
