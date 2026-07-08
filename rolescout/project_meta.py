"""Project-level target metadata (<project>/project-meta.json) — rolescout-owned.

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
  comp_range         str        optional (sensitive — never shared externally)
  negatives          list[str]  optional excludes (companies/titles/industries)
  schedules          list       reserved for the scheduler feature (not active)
  archived           bool
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

META_NAME = "project-meta.json"

LIST_FIELDS = ("target_locations", "target_companies", "negatives")
STR_FIELDS = ("focus_role", "target_level", "comp_range")
DEFAULTS = {
    "target_locations": [], "focus_role": "", "target_level": "",
    "target_companies": [], "comp_range": "", "negatives": [],
    "schedules": [], "archived": False,
}


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
    return meta


def update(project: Path, **fields) -> dict:
    """Merge + persist; list fields accept comma-strings or lists. Also mirrors
    archived state into project.json `status` (the prototype's own field) so
    `rolescout init --list` stays truthful."""
    meta = load(project)
    for k, v in fields.items():
        if v is None:
            continue
        if k in LIST_FIELDS:
            meta[k] = _parse_list(v)
        elif k == "archived":
            meta[k] = bool(v)
        else:
            meta[k] = str(v).strip()
    meta["updated_at"] = date.today().isoformat()
    (project / META_NAME).write_text(
        json.dumps(meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    if "archived" in fields:
        pj = project / "project.json"
        if pj.exists():
            try:
                doc = json.loads(pj.read_text(encoding="utf-8"))
                doc["status"] = "archived" if meta["archived"] else "active"
                pj.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
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
        lines.append(f"- Target comp range: {m['comp_range']} "
                     "(sensitive — never share externally without approval)")
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
