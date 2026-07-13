"""Stable, collision-safe paths for per-position interview artifacts."""

from __future__ import annotations

import hashlib
import re


def role_slug(role: dict) -> str:
    """Return a readable <=48-char slug with a stable position-identity suffix."""
    raw = f"{role.get('company', '')}-{role.get('title', '')}"
    base = re.sub(r"[^a-z0-9]+", "-", raw.lower()).strip("-")[:48].strip("-") or "role"
    identity = str(role.get("job_id") or role.get("source_url") or base).strip()
    match = re.search(r"(?:^|--|-)([0-9a-f]{8,})$", identity, re.I)
    digest = (match.group(1)[:8].lower() if match else
              hashlib.sha256(identity.encode("utf-8")).hexdigest()[:8])
    return f"{base[:39].rstrip('-')}-{digest}"
