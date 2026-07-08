"""Workflow-specific model profiles for local LLM CLI providers."""

from __future__ import annotations

import json
import os
from importlib import resources
from pathlib import Path
from typing import Any

from ..paths import RoleScoutError, home_dir

CONFIG_FILE = "model-profiles.json"
CURRENT_VERSION = 3
ALLOWED_CODEX_EFFORTS = {"minimal", "low", "medium", "high", "xhigh"}

FALLBACK_PROFILES: dict[str, Any] = {
    "version": CURRENT_VERSION,
    "codex": {
        "default": {"model": "gpt-5.5", "effort": "medium"},
        "workflows": {
            "profile-intake": {"model": "gpt-5.5", "effort": "high"},
            "search": {"model": "gpt-5.5", "effort": "medium"},
            "score": {"model": "gpt-5.5", "effort": "medium"},
            "prep": {"model": "gpt-5.5", "effort": "high"},
            "prep-strategy": {"model": "gpt-5.5", "effort": "xhigh"},
            "prep-resume": {"model": "gpt-5.5", "effort": "high"},
            "prep-linkedin": {"model": "gpt-5.5", "effort": "high"},
            "prep-interview": {"model": "gpt-5.5", "effort": "high"},
            "apply": {"model": "gpt-5.5", "effort": "medium"},
            "complete": {"model": "gpt-5.5", "effort": "medium"},
        },
    },
    "external_cli": {
        "default": {"model": "gpt-5.5", "effort": "medium"},
        "workflows": {
            "profile-intake": {"model": "gpt-5.5", "effort": "high"},
            "search": {"model": "gpt-5.5", "effort": "medium"},
            "score": {"model": "gpt-5.5", "effort": "medium"},
            "prep": {"model": "gpt-5.5", "effort": "high"},
            "prep-strategy": {"model": "gpt-5.5", "effort": "xhigh"},
            "prep-resume": {"model": "gpt-5.5", "effort": "high"},
            "prep-linkedin": {"model": "gpt-5.5", "effort": "high"},
            "prep-interview": {"model": "gpt-5.5", "effort": "high"},
            "apply": {"model": "gpt-5.5", "effort": "medium"},
            "complete": {"model": "gpt-5.5", "effort": "medium"},
        },
    },
}


def model_profile_path() -> Path:
    override = os.environ.get("ROLESCOUT_MODEL_PROFILES")
    return Path(override).expanduser() if override else home_dir() / CONFIG_FILE


def _bundled_profiles() -> dict[str, Any]:
    try:
        text = resources.files(__package__).joinpath(CONFIG_FILE).read_text(encoding="utf-8")
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except (FileNotFoundError, ModuleNotFoundError, json.JSONDecodeError, OSError):
        pass
    return json.loads(json.dumps(FALLBACK_PROFILES))


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        existing = merged.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            merged[key] = _deep_merge(existing, value)
        else:
            merged[key] = value
    return merged


def _write_default(path: Path, profiles: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(profiles, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8")


def _migrate_known_defaults(custom: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    version = custom.get("version", 0)
    try:
        version_num = int(version)
    except (TypeError, ValueError):
        version_num = 0
    if version_num >= CURRENT_VERSION:
        return custom, False

    migrated = json.loads(json.dumps(custom))
    for provider in ("codex", "external_cli"):
        provider_settings = migrated.get(provider)
        if not isinstance(provider_settings, dict):
            continue
        workflows = provider_settings.get("workflows")
        if not isinstance(workflows, dict):
            continue
        search = workflows.get("search")
        if isinstance(search, dict) and search.get("effort") == "low":
            search["effort"] = "medium"
        prep_strategy = workflows.get("prep-strategy")
        if isinstance(prep_strategy, dict) and prep_strategy.get("effort") == "high":
            prep_strategy["effort"] = "xhigh"
        workflows.setdefault("profile-intake", {"model": "gpt-5.5", "effort": "high"})
    migrated["version"] = CURRENT_VERSION
    return migrated, migrated != custom


def load_profiles() -> tuple[dict[str, Any], Path]:
    base = _bundled_profiles()
    path = model_profile_path()
    if not path.exists():
        _write_default(path, base)
        return base, path
    try:
        custom = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise RoleScoutError(f"model profile config is not valid JSON: {path} ({e})") from e
    except OSError as e:
        raise RoleScoutError(f"cannot read model profile config: {path} ({e})") from e
    if not isinstance(custom, dict):
        raise RoleScoutError(f"model profile config must be a JSON object: {path}")
    custom, changed = _migrate_known_defaults(custom)
    if changed:
        _write_default(path, custom)
    return _deep_merge(base, custom), path


def _profile_for(provider: str, workflow: str | None, *,
                 model_env: str, effort_env: str,
                 validate_effort: bool) -> dict[str, str]:
    profiles, path = load_profiles()
    provider_settings = profiles.get(provider)
    if not isinstance(provider_settings, dict) and provider != "codex":
        provider_settings = profiles.get("codex")
    if not isinstance(provider_settings, dict):
        raise RoleScoutError(f"model profile config is missing {provider} settings: {path}")

    default = provider_settings.get("default", {})
    workflows = provider_settings.get("workflows", {})
    if not isinstance(default, dict) or not isinstance(workflows, dict):
        raise RoleScoutError(f"model profile config has invalid {provider} settings: {path}")

    selected = workflows.get(workflow or "complete", {})
    if selected is None:
        selected = {}
    if not isinstance(selected, dict):
        raise RoleScoutError(
            f"model profile for workflow {workflow!r} must be an object: {path}")

    merged = {**default, **selected}
    model = str(os.environ.get(model_env) or merged.get("model") or "").strip()
    effort = str(os.environ.get(effort_env) or merged.get("effort") or "").strip()
    effort = effort.lower()
    if not model:
        raise RoleScoutError(f"model profile for workflow {workflow!r} is missing model: {path}")
    if validate_effort and effort and effort not in ALLOWED_CODEX_EFFORTS:
        allowed = ", ".join(sorted(ALLOWED_CODEX_EFFORTS))
        raise RoleScoutError(
            f"model profile for workflow {workflow!r} has unsupported effort "
            f"{effort!r}; use one of: {allowed}")
    return {"model": model, "effort": effort, "settings_file": str(path)}


def codex_profile_for(workflow: str | None) -> dict[str, str]:
    return _profile_for("codex", workflow, model_env="ROLESCOUT_CODEX_MODEL",
                        effort_env="ROLESCOUT_CODEX_EFFORT", validate_effort=True)


def external_cli_profile_for(workflow: str | None) -> dict[str, str]:
    return _profile_for("external_cli", workflow, model_env="ROLESCOUT_LLM_MODEL",
                        effort_env="ROLESCOUT_LLM_EFFORT", validate_effort=False)


def codex_cli_args(workflow: str | None) -> tuple[dict[str, str], list[str]]:
    profile = codex_profile_for(workflow)
    args = ["--model", profile["model"]]
    if profile.get("effort"):
        args += ["-c", f'model_reasoning_effort="{profile["effort"]}"']
    return profile, args
