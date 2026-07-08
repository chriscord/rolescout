"""Mock provider — canned envelopes proving the local pipeline without network.

Envelopes live in rolescout/llm/envelopes/<workflow>.json. They are honest test
doubles: their store rows pass the real validators and their research logs
satisfy grade_run's Layer-2 checks. Costs are recorded as 0.0 and the model id
marks the run as canned.
"""

from __future__ import annotations

import json
from pathlib import Path

ENVELOPES_DIR = Path(__file__).parent / "envelopes"


class MockProvider:
    name = "mock"

    def model_config(self) -> dict:
        return {"provider": "mock", "model": "canned-envelope-v1"}

    def run(self, workflow: str, context: dict, on_progress=None,
            model_workflow: str | None = None) -> dict:
        """Return the canned envelope for a workflow. `context`/`on_progress` are
        accepted for interface parity with the live providers."""
        del model_workflow
        p = ENVELOPES_DIR / f"{workflow}.json"
        if not p.exists():
            raise FileNotFoundError(f"no canned envelope for workflow '{workflow}' ({p})")
        env = json.loads(p.read_text(encoding="utf-8"))
        env.setdefault("model_config", self.model_config())
        return env
