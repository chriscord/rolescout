"""One-time provider notice plus per-workflow data-use text."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from ..paths import home_dir
from .classification import workflow_disclosure

NOTICE_VERSION = 1


def disclosure_lines(workflow: str, provider: str) -> list[str]:
    marker = home_dir() / f"provider-disclosure-v{NOTICE_VERSION}.json"
    lines: list[str] = []
    if not marker.exists():
        lines.append(
            "Privacy notice: RoleScout stores inputs/outputs locally, but a live run sends "
            f"the workflow-approved packet to {provider} for model processing. Provider "
            "retention and training terms are governed by that provider/account."
        )
        marker.write_text(json.dumps({
            "version": NOTICE_VERSION,
            "shown_at": datetime.now(timezone.utc).isoformat(),
            "provider": provider,
        }, indent=2) + "\n", encoding="utf-8")
    lines.append("Data used — " + workflow_disclosure(workflow, provider))
    return lines
