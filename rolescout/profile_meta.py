"""Person-level profile metadata (profiles/<person>/profile-meta.json).

Small, optional facts the CLI collects at intake that aren't documents:
currently the LinkedIn URL. Stored next to the profile so every channel
(plugin, CLI, fork) and every skill sees the same source of truth. Sensitive
by policy (AGENTS.md §privacy) — lives only inside the repo's profiles/.
"""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

META_NAME = "profile-meta.json"

_LINKEDIN_RE = re.compile(r"^https://([a-z]{2,3}\.)?linkedin\.com/[^\s]+$", re.I)


def normalize_linkedin_url(url: str) -> str:
    """Lenient normalize; raises ValueError when it clearly isn't a LinkedIn URL."""
    url = url.strip().rstrip("/")
    if not url:
        return ""
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    url = re.sub(r"^http://", "https://", url, flags=re.I)
    url = re.sub(r"^https://www\.", "https://", url, flags=re.I)
    if not _LINKEDIN_RE.match(url):
        raise ValueError(f"not a linkedin.com URL: {url!r} "
                         "(expected e.g. https://linkedin.com/in/<handle>)")
    return url


def load(profile_dir: Path) -> dict:
    p = profile_dir / META_NAME
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def save(profile_dir: Path, **fields) -> Path:
    profile_dir.mkdir(parents=True, exist_ok=True)
    meta = load(profile_dir)
    meta.update({k: v for k, v in fields.items() if v})
    meta["updated_at"] = date.today().isoformat()
    p = profile_dir / META_NAME
    p.write_text(json.dumps(meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return p


def linkedin_url(profile_dir: Path | None) -> str:
    return load(profile_dir).get("linkedin_url", "") if profile_dir else ""


def instructions(profile_dir: Path | None) -> str:
    """User's standing instructions — injected into
    every live workflow prompt."""
    return load(profile_dir).get("instructions", "") if profile_dir else ""


RESUME_SUFFIXES = {".pdf", ".docx", ".doc", ".md", ".txt", ".html"}
_GENERATED = {"candidate-profile.md", "evidence-map.md", META_NAME}


def material_files(profile_dir: Path | None) -> list[dict]:
    """User-supplied source materials in the profile folder (resumes, notes…)."""
    if profile_dir is None or not profile_dir.is_dir():
        return []
    out = []
    for f in sorted(profile_dir.iterdir()):
        if (f.is_file() and f.suffix.lower() in RESUME_SUFFIXES
                and f.name not in _GENERATED):
            out.append({"name": f.name, "size": f.stat().st_size,
                        "mtime": int(f.stat().st_mtime)})
    return out


def list_persons(root: Path) -> list[dict]:
    out = []
    for d in sorted((root / "profiles").glob("*/")):
        if not d.is_dir() or d.name.startswith("."):
            continue
        meta = load(d)
        out.append({"person": d.name,
                    "name": meta.get("name", ""),
                    "linkedin_url": meta.get("linkedin_url", ""),
                    "instructions": meta.get("instructions", ""),
                    "has_profile": (d / "candidate-profile.md").exists(),
                    "materials": material_files(d)})
    return out
