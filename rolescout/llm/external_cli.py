"""External agent CLI provider.

This backend lets RoleScout call a local CLI that the user has already
authenticated outside RoleScout. It is intentionally generic: RoleScout never
sees provider credentials, subscription tokens, or plan keys.

Configure with:

    ROLESCOUT_PROVIDER=cli
    ROLESCOUT_LLM_CMD='your-agent-cli run --model {model} --effort {effort} {prompt}'

If the command template does not contain a `{prompt}` token, the prompt is sent
to stdin. `{root}`, `{project}`, `{model}`, and `{effort}` are also replaced
when present.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import time
from pathlib import Path

from ..paths import RoleScoutError, repo_root
from . import model_profiles
from . import prompts

TIMEOUT_S = int(os.environ.get("ROLESCOUT_CLI_TIMEOUT_S", "1800"))


def _template() -> str:
    return os.environ.get("ROLESCOUT_LLM_CMD", "").strip()


def _split_template() -> list[str]:
    template = _template()
    if not template:
        raise RoleScoutError(
            "ROLESCOUT_PROVIDER=cli requires ROLESCOUT_LLM_CMD, for example: "
            "ROLESCOUT_LLM_CMD='opencode run {prompt}'")
    try:
        return shlex.split(template)
    except ValueError as e:
        raise RoleScoutError(f"invalid ROLESCOUT_LLM_CMD: {e}") from e


def binary() -> str | None:
    try:
        args = _split_template()
    except RoleScoutError:
        return None
    if not args:
        return None
    exe = args[0]
    return exe if Path(exe).exists() else shutil.which(exe)


def _build_command(prompt: str, project: Path | None = None,
                   workflow: str | None = None) -> tuple[list[str], str | None, dict[str, str]]:
    root = repo_root()
    project_text = str(project or "")
    profile = model_profiles.external_cli_profile_for(workflow)
    effort = profile.get("effort", "")
    cmd: list[str] = []
    prompt_in_argv = False
    for arg in _split_template():
        if "{prompt}" in arg:
            prompt_in_argv = True
            arg = arg.replace("{prompt}", prompt)
        arg = (arg.replace("{root}", str(root))
               .replace("{project}", project_text)
               .replace("{model}", profile["model"])
               .replace("{effort}", effort))
        cmd.append(arg)
    return cmd, None if prompt_in_argv else prompt, profile


def _cli_name() -> str:
    configured = os.environ.get("ROLESCOUT_LLM_NAME", "").strip()
    if configured:
        return configured
    try:
        args = _split_template()
    except RoleScoutError:
        return "external-cli"
    return Path(args[0]).name if args else "external-cli"


class ExternalCliProvider:
    name = "cli"

    def __init__(self) -> None:
        exe = binary()
        if not exe:
            raise RoleScoutError(
                "external CLI provider configured, but the command executable was not found. "
                "Check ROLESCOUT_LLM_CMD.")
        self.exe = exe

    def model_config(self, workflow: str | None = None) -> dict:
        profile = model_profiles.external_cli_profile_for(workflow)
        return {"provider": _cli_name(),
                "model": profile["model"],
                "effort": profile["effort"],
                "settings_file": profile["settings_file"],
                "auth": "external-cli-managed"}

    def complete(self, prompt: str) -> str:
        cmd, stdin_prompt, profile = _build_command(prompt, workflow="complete")
        env = {**os.environ, "ROLESCOUT_TASK_MODEL": profile["model"],
               "ROLESCOUT_TASK_EFFORT": profile.get("effort", ""),
               "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
        r = subprocess.run(cmd, input=stdin_prompt, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=TIMEOUT_S,
                           cwd=repo_root(), env=env)
        if r.returncode != 0:
            raise RoleScoutError(f"{_cli_name()} failed (rc={r.returncode}): "
                                 f"{(r.stderr or r.stdout).strip()[:400]}")
        return r.stdout.strip()

    def run(self, workflow: str, context: dict, on_progress=None,
            model_workflow: str | None = None) -> dict:
        prompt = prompts.workflow_prompt(workflow, context)
        project = Path(context["project"])
        profile_key = model_workflow or workflow
        cmd, stdin_prompt, profile = _build_command(prompt, project=project, workflow=profile_key)
        env = {**os.environ, "RECRUITING_PROJECT_DIR": str(project),
               "ROLESCOUT_TASK_MODEL": profile["model"],
               "ROLESCOUT_TASK_EFFORT": profile.get("effort", ""),
               "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}

        t0 = time.monotonic()
        proc = subprocess.Popen(
            cmd,
            cwd=repo_root(),
            stdin=subprocess.PIPE if stdin_prompt is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
        if stdin_prompt is not None and proc.stdin is not None:
            proc.stdin.write(stdin_prompt)
            proc.stdin.close()

        raw_lines: list[str] = []
        assert proc.stdout is not None
        try:
            for line in proc.stdout:
                if time.monotonic() - t0 > TIMEOUT_S:
                    proc.kill()
                    raise RoleScoutError(
                        f"{_cli_name()} exceeded ROLESCOUT_CLI_TIMEOUT_S={TIMEOUT_S}s")
                line = line.rstrip("\n")
                if not line.strip():
                    continue
                raw_lines.append(line)
                if on_progress:
                    on_progress(line.splitlines()[0][:300])
            proc.wait(timeout=30)
        finally:
            try:
                proc.stdout.close()
            except OSError:
                pass
            if proc.poll() is None:
                proc.kill()

        if proc.returncode != 0:
            tail = "\n".join(raw_lines[-12:])
            raise RoleScoutError(f"{_cli_name()} failed (rc={proc.returncode}): "
                                 f"{tail[:1000]}")

        events: list[dict] = []
        for text in raw_lines[:-1]:
            first = text.splitlines()[0][:200]
            events.append({"type": "progress", "text": first})
            if text.startswith("APPROVAL_REQUIRED:"):
                events.append({"type": "external_action",
                               "action": "agent_requested",
                               "target": text[len("APPROVAL_REQUIRED:"):].strip()[:300],
                               "content": text,
                               "on_approve": [],
                               "on_deny": []})
        events.append({"type": "result",
                       "summary": (raw_lines[-1] if raw_lines else "")[:2000]})
        return {"workflow": workflow,
                "model_config": self.model_config(profile_key),
                "streamed": True,
                "usage": {"cost_usd": 0.0, "tokens_in": 0, "tokens_out": 0,
                          "num_turns": 0,
                          "note": "usage is managed by the external CLI"},
                "events": events}
