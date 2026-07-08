"""rolescout.llm — provider isolation layer (dev-plan §0).

Backends (all optional; deps live ONLY in their own module):
  codex   OpenAI Codex CLI, user's ChatGPT subscription via `codex login`
          (`codex.py` - no API key, RoleScout never sees credentials)
  cli     any local agent CLI command you already authenticated yourself
          (`external_cli.py` - configured by ROLESCOUT_LLM_CMD)
  mock    canned envelopes; zero deps, zero accounts, zero network (`mock.py`)

Selection (ROLESCOUT_PROVIDER overrides; otherwise automatic):
  LLM_MOCK=1 / --mock             -> mock
  ROLESCOUT_PROVIDER=codex|cli    -> that backend (errors if unusable)
  ROLESCOUT_LLM_CMD set           -> cli
  `codex` binary on PATH          -> codex
  otherwise                       -> mock

An *envelope* is the provider-neutral output contract of a workflow run:
  {workflow, model_config: {...}, usage: {...}, events: [...]}
Event types: progress, artifact, store_write, external_action (with on_approve/
on_deny event lists), result. The runner executes events through the prototype's
validate-before-write pipeline; providers only produce them.
"""

from __future__ import annotations

import os
import shutil

from ..paths import RoleScoutError

_ALIASES = {"codex": "codex", "openai": "codex",
            "cli": "cli", "external": "cli", "custom": "cli",
            "mock": "mock"}


def provider_choice(force_mock: bool = False) -> str:
    """'mock' | 'codex' | 'cli' per the selection rules above."""
    if force_mock or os.environ.get("LLM_MOCK") == "1":
        return "mock"
    explicit = os.environ.get("ROLESCOUT_PROVIDER", "").strip().lower()
    if explicit:
        if explicit not in _ALIASES:
            raise RoleScoutError(
                f"ROLESCOUT_PROVIDER={explicit!r} unknown (use codex, cli, or mock)")
        return _ALIASES[explicit]
    if os.environ.get("ROLESCOUT_LLM_CMD"):
        return "cli"
    if shutil.which(os.environ.get("CODEX_BIN", "codex")):
        return "codex"
    return "mock"


def mode(force_mock: bool = False) -> str:
    """'mock' or 'live' (backward-compatible summary of provider_choice)."""
    return "mock" if provider_choice(force_mock) == "mock" else "live"


def get_provider(force_mock: bool = False):
    choice = provider_choice(force_mock)
    if choice == "mock":
        from .mock import MockProvider
        return MockProvider()
    if choice == "codex":
        from .codex import CodexProvider
        return CodexProvider()
    from .external_cli import ExternalCliProvider
    return ExternalCliProvider()
