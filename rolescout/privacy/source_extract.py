"""Deterministic, bounded text extraction for profile-intake model packets."""

from __future__ import annotations

import html
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree

TEXT_SUFFIXES = {".md", ".txt", ".html", ".htm"}
MAX_SOURCE_BYTES = 40_000
MAX_TOTAL_BYTES = 100_000


def _bounded(text: str, limit: int = MAX_SOURCE_BYTES) -> str:
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    return raw[:limit].decode("utf-8", errors="ignore") + "\n[truncated]"


def _docx_text(path: Path) -> str:
    with zipfile.ZipFile(path) as archive:
        root = ElementTree.fromstring(archive.read("word/document.xml"))
    parts: list[str] = []
    for elem in root.iter():
        tag = elem.tag.rsplit("}", 1)[-1]
        if tag == "t" and elem.text:
            parts.append(elem.text)
        elif tag in {"p", "br"}:
            parts.append("\n")
    return re.sub(r"\n{3,}", "\n\n", "".join(parts)).strip()


def _pdf_text(path: Path) -> str:
    try:
        from pypdf import PdfReader  # type: ignore[import-not-found]
    except ImportError:
        return ""
    reader = PdfReader(str(path))
    return "\n\n".join((page.extract_text() or "") for page in reader.pages)


def extract_text(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in TEXT_SUFFIXES:
        text = path.read_text(encoding="utf-8", errors="replace")
        if suffix in {".html", ".htm"}:
            text = html.unescape(re.sub(r"<[^>]+>", " ", text))
            text = re.sub(r"[ \t]+", " ", text)
        return _bounded(text)
    if suffix == ".docx":
        return _bounded(_docx_text(path))
    if suffix == ".pdf":
        return _bounded(_pdf_text(path))
    return ""


def profile_source_packet(profile_dir: Path) -> dict:
    """Extract only approved source files; never include filesystem paths."""
    generated = {
        "candidate-profile.md", "evidence-map.md", "linkedin-analysis.md",
        "profile-meta.json", "decision-policy.json", "story-bank.md", "story-bank.json",
    }
    documents: list[dict[str, str | int]] = []
    total = 0
    for path in sorted(profile_dir.iterdir()):
        if not path.is_file() or path.name in generated:
            continue
        if path.suffix.lower() not in {".md", ".txt", ".html", ".htm", ".docx", ".pdf"}:
            continue
        try:
            text = extract_text(path)
        except (OSError, ValueError, zipfile.BadZipFile, ElementTree.ParseError):
            text = ""
        if not text:
            documents.append({"source": path.name, "text": "[text extraction unavailable]",
                              "size": path.stat().st_size})
            continue
        encoded = text.encode("utf-8")
        if total + len(encoded) > MAX_TOTAL_BYTES:
            remain = max(0, MAX_TOTAL_BYTES - total)
            text = encoded[:remain].decode("utf-8", errors="ignore") + "\n[truncated]"
            encoded = text.encode("utf-8")
        documents.append({"source": path.name, "text": text, "size": path.stat().st_size})
        total += len(encoded)
        if total >= MAX_TOTAL_BYTES:
            break
    return {"schema": "rolescout-profile-source-packet-v1", "documents": documents}
