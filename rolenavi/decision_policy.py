"""Canonical, model-safe decision policy derived from standing instructions.

Generated profile facts live in candidate-profile.md/evidence-map.md. This file
contains only user-authored decision preferences and is the stable contract for
score and prep workflows: profiles/<person>/decision-policy.json.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

POLICY_NAME = "decision-policy.json"
SCHEMA = "rolenavi-decision-policy-v2"


def _preference_text(instructions: str) -> str:
    tagged = re.findall(r"<preference>(.*?)</preference>", instructions, re.I | re.S)
    if tagged:
        return "\n\n".join(part.strip() for part in tagged if part.strip())
    # Remove explicitly profile-only blocks. Untagged instructions are treated
    # as user-authored workflow preferences, preserving existing installations.
    return re.sub(r"<user_profile>.*?</user_profile>", "", instructions,
                  flags=re.I | re.S).strip()


def build(profile_dir: Path, instructions: str | None = None) -> dict[str, Any]:
    if instructions is None:
        try:
            meta = json.loads((profile_dir / "profile-meta.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            meta = {}
        instructions = str(meta.get("instructions", ""))
    preference = _preference_text(str(instructions or ""))
    lower = preference.lower()
    no_backward = bool(re.search(
        r"(?:must\s+not|never|forbid|prohibit|dealbreak\w*).*backward|"
        r"backward.*(?:must\s+not|never|forbid|prohibit|dealbreak\w*)",
        lower,
    ))
    engineering_exception = bool(
        "engineering" in lower and re.search(r"waiv|exception|exempt", lower)
    )
    digest = hashlib.sha256(preference.encode("utf-8")).hexdigest()
    payload = {
        "schema": SCHEMA,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source_fingerprint": digest,
        "policy_text": preference,
        "constraints": {
            "no_backward_career_progression": no_backward,
            "engineering_role_exception": engineering_exception,
        },
        "policies": ([{
            "id": "no_backward_career_progression",
            "criterion": "career_trajectory",
            "enforcement": "dealbreaker",
            "rule": preference,
            "exceptions": (
                ["engineering_role_exception"] if engineering_exception else []
            ),
        }] if no_backward else []),
    }
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / POLICY_NAME).write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return payload


def load(profile_dir: Path | None) -> dict[str, Any]:
    if profile_dir is None:
        return {}
    path = profile_dir / POLICY_NAME
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return build(profile_dir)
    if not isinstance(payload, dict) or payload.get("schema") != SCHEMA:
        return build(profile_dir)
    return payload
