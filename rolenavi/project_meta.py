"""Project-level target metadata (<project>/project-meta.json) — rolenavi-owned.

`project.json` stays the prototype's file (person/focus/status); this sidecar
holds the search TARGETS the user declares at project creation and can update
mid-session (CLI flags or web UI). Live runs inject these into every workflow
prompt so agent behavior always reflects the current declared intent.

Fields:
  target_locations   list[str]  (required at creation — grounds every search)
  focus_role         str        optional
  target_level       str        optional (e.g. senior / staff / director)
  target_companies   list[str]  optional seeds — the agent explores SIMILAR
                                companies too, never only these
  comp_range         str        optional target preference (model-allowed)
  search_runtime_profile str    optional: polite / standard / fast / deep
  search_view_filter_mode str   optional: llm / deterministic
  negatives          list[str]  optional excludes (companies/titles/industries)
  schedules          list       reserved for the scheduler feature (not active)
  archived           bool
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

META_NAME = "project-meta.json"

LIST_FIELDS = ("target_locations", "target_companies", "negatives")
STR_FIELDS = (
    "focus_role", "target_level", "comp_range", "search_runtime_profile",
    "search_view_filter_mode",
)
DEFAULTS = {
    "target_locations": [], "focus_role": "", "target_level": "",
    "target_companies": [], "comp_range": "", "search_runtime_profile": "fast",
    "search_view_filter_mode": "llm", "negatives": [],
    "schedules": [], "archived": False,
    "preference_revision": 0, "preference_fingerprint": "", "updated_at": "",
}

PREFERENCE_FIELDS = (*LIST_FIELDS, *STR_FIELDS)


def _preference_payload(meta: dict) -> dict:
    return {key: meta.get(key, DEFAULTS[key]) for key in PREFERENCE_FIELDS}


def preference_fingerprint(meta_or_project: dict | Path) -> str:
    """Stable fingerprint for every field that can change search intent."""
    meta = load(meta_or_project) if isinstance(meta_or_project, Path) else meta_or_project
    raw = json.dumps(_preference_payload(meta), sort_keys=True, ensure_ascii=False,
                     separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _write_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _parse_list(v) -> list[str]:
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    return [s.strip() for s in str(v or "").split(",") if s.strip()]


def load(project: Path) -> dict:
    p = project / META_NAME
    meta = dict(DEFAULTS)
    if p.exists():
        try:
            meta.update(json.loads(p.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            pass
    meta["preference_fingerprint"] = preference_fingerprint(meta)
    return meta


def update(project: Path, **fields) -> dict:
    """Merge + persist; list fields accept comma-strings or lists. Also mirrors
    archived state into project.json `status` (the prototype's own field) so
    `rolenavi init --list` stays truthful."""
    meta = load(project)
    before = preference_fingerprint(meta)
    for k, v in fields.items():
        if v is None:
            continue
        if k in LIST_FIELDS:
            meta[k] = _parse_list(v)
        elif k == "archived":
            meta[k] = bool(v)
        else:
            meta[k] = str(v).strip()
    after = preference_fingerprint(meta)
    if after != before:
        meta["preference_revision"] = int(meta.get("preference_revision", 0) or 0) + 1
    meta["preference_fingerprint"] = after
    meta["updated_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    _write_atomic(project / META_NAME, meta)

    if "archived" in fields:
        pj = project / "project.json"
        if pj.exists():
            try:
                doc = json.loads(pj.read_text(encoding="utf-8"))
                doc["status"] = "archived" if meta["archived"] else "active"
                _write_atomic(pj, doc)
            except (json.JSONDecodeError, OSError):
                pass
    return meta


def targets_text(project: Path) -> str:
    """Prompt-ready summary of declared targets ('' when nothing declared)."""
    m = load(project)
    lines = []
    if m["target_locations"]:
        lines.append(f"- Target locations (hard constraint): {', '.join(m['target_locations'])}")
    if m["focus_role"]:
        lines.append(f"- Focus role: {m['focus_role']}")
    if m["target_level"]:
        lines.append(f"- Target level: {m['target_level']}")
    if m["target_companies"]:
        lines.append(f"- Target companies (seeds — also explore SIMILAR companies): "
                     f"{', '.join(m['target_companies'])}")
    if m["comp_range"]:
        lines.append(f"- Target comp range preference: {m['comp_range']}")
    if m["negatives"]:
        lines.append(f"- Excludes (hard): {', '.join(m['negatives'])}")
    return "\n".join(lines)


def summary(project: Path) -> str:
    m = load(project)
    bits = []
    if m["target_locations"]:
        bits.append(", ".join(m["target_locations"][:3]))
    if m["focus_role"]:
        bits.append(m["focus_role"])
    if m["target_level"]:
        bits.append(m["target_level"])
    return " · ".join(bits)


def universe_status(project: Path) -> dict:
    """Return whether the model-built company universe matches current preferences."""
    path = project / "targets" / "company-universe.json"
    expected = preference_fingerprint(project)
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {"ready": False, "reason": "company universe has not been built",
                "expected_fingerprint": expected}
    def category_seed(value: str) -> bool:
        text = " ".join(str(value or "").lower().split())
        plural = re.search(
            r"\b(startups|scaleups|companies|employers|firms|organizations|organisations)\b",
            text,
        )
        return bool(plural and (" or " in text or " and " in text or "," in text
                                or text.startswith(("ai ", "tech ", "fintech "))))

    companies = []
    for bucket in payload.get("buckets", []) if isinstance(payload, dict) else []:
        if isinstance(bucket, dict):
            companies.extend(
                item for item in bucket.get("companies", [])
                if isinstance(item, dict) and str(item.get("name", "")).strip()
            )
    actual = str(payload.get("preference_fingerprint", "")) if isinstance(payload, dict) else ""
    if actual != expected:
        return {"ready": False, "reason": "company universe is stale",
                "expected_fingerprint": expected, "actual_fingerprint": actual,
                "companies": len(companies)}
    if not companies:
        return {"ready": False, "reason": "company universe contains no named employers",
                "expected_fingerprint": expected, "actual_fingerprint": actual,
                "companies": 0}
    invalid_categories = [
        str(item.get("name", "")).strip() for item in companies
        if category_seed(str(item.get("name", "")))
    ]
    if invalid_categories:
        return {"ready": False, "reason": "company universe contains unexpanded descriptors",
                "expected_fingerprint": expected, "actual_fingerprint": actual,
                "companies": len(companies)}
    expanded = {
        str(item.get("input", "")).strip().lower()
        for item in payload.get("expanded_descriptors", [])
        if isinstance(item, dict)
    }
    missing_descriptors = [
        target for target in load(project).get("target_companies", [])
        if category_seed(target) and str(target).strip().lower() not in expanded
    ]
    if missing_descriptors:
        return {"ready": False, "reason": "category targets have not been expanded",
                "expected_fingerprint": expected, "actual_fingerprint": actual,
                "companies": len(companies)}
    state = str(payload.get("state", "ready"))
    return {"ready": True, "reason": "", "state": state,
            "expected_fingerprint": expected,
            "actual_fingerprint": actual, "companies": len(companies)}
