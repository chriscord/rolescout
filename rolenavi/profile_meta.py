"""Person-level profile metadata (profiles/<person>/profile-meta.json).

Small, optional facts the CLI collects at intake that aren't documents:
currently the LinkedIn URL. Stored next to the profile so every channel
(plugin, CLI, fork) and every skill sees the same source of truth. Sensitive
by policy (AGENTS.md §privacy) — lives only inside the repo's profiles/.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
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
    _atomic_json(p, meta)
    from . import decision_policy
    decision_policy.build(profile_dir, str(meta.get("instructions", "")))
    return p


def replace(profile_dir: Path, **fields) -> Path:
    """Set supplied metadata fields exactly, including explicit empty values."""
    profile_dir.mkdir(parents=True, exist_ok=True)
    meta = load(profile_dir)
    meta.update(fields)
    meta["updated_at"] = date.today().isoformat()
    path = profile_dir / META_NAME
    _atomic_json(path, meta)
    from . import decision_policy
    decision_policy.build(profile_dir, str(meta.get("instructions", "")))
    return path


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    os.close(fd)
    tmp = Path(raw)
    try:
        tmp.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def linkedin_url(profile_dir: Path | None) -> str:
    return load(profile_dir).get("linkedin_url", "") if profile_dir else ""


def instructions(profile_dir: Path | None) -> str:
    """Raw standing instructions; decision-policy.json is the model-safe contract."""
    return load(profile_dir).get("instructions", "") if profile_dir else ""


RESUME_SUFFIXES = {".pdf", ".docx", ".md", ".txt", ".html"}
_GENERATED = {
    "candidate-profile.md",
    "evidence-map.md",
    "linkedin-current.md",
    "linkedin-analysis.md",
    "story-bank.md",
    "story-bank.json",
    "decision-policy.json",
    META_NAME,
}


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


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def material_hashes(profile_dir: Path | None) -> dict[str, str]:
    if profile_dir is None:
        return {}
    out: dict[str, str] = {}
    for item in material_files(profile_dir):
        path = profile_dir / str(item["name"])
        try:
            out[path.name] = file_sha256(path)
        except OSError:
            continue
    return out


def linkedin_content_fingerprint(profile_dir: Path | None) -> str:
    if profile_dir is None:
        return ""
    path = profile_dir / "linkedin-current.md"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    marker = "## Visible LinkedIn Profile Text"
    stable = text.split(marker, 1)[1].strip() if marker in text else text.strip()
    return hashlib.sha256(stable.encode("utf-8")).hexdigest() if stable else ""


def source_fingerprint(profile_dir: Path) -> str:
    meta = load(profile_dir)
    payload = {
        # Artifact meaning depends on source bytes, not on the local filename.
        # Renaming an identical resume must not spend another model call.
        "materials": sorted(material_hashes(profile_dir).values()),
        "linkedin_url": str(meta.get("linkedin_url", "")),
        "linkedin_content": linkedin_content_fingerprint(profile_dir),
        "name": str(meta.get("name", "")),
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def profile_is_current(profile_dir: Path) -> bool:
    meta = load(profile_dir)
    return bool(
        (profile_dir / "candidate-profile.md").exists()
        and (profile_dir / "evidence-map.md").exists()
        and meta.get("profile_build_fingerprint") == source_fingerprint(profile_dir)
    )


def mark_profile_built(profile_dir: Path) -> str:
    fingerprint = source_fingerprint(profile_dir)
    replace(profile_dir, profile_build_fingerprint=fingerprint)
    return fingerprint


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
                    "profile_current": profile_is_current(d),
                    "linkedin_synced": bool(linkedin_content_fingerprint(d)),
                    "materials": material_files(d)})
    return out
