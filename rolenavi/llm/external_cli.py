"""External agent CLI provider.

This backend lets RoleNavi call a local CLI that the user has already
authenticated outside RoleNavi. It is intentionally generic: RoleNavi never
sees provider credentials, subscription tokens, or plan keys.

Configure with:

    ROLENAVI_PROVIDER=cli
    ROLENAVI_LLM_CMD='your-agent-cli run --model {model} --effort {effort} {prompt}'

The prompt is sent to stdin; argv prompt expansion is rejected by default.
`{root}` and `{project}` resolve to a disposable staging directory. Arbitrary
CLIs are developer-only because RoleNavi cannot verify their tool sandbox.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import time
from pathlib import Path

from ..paths import RoleNaviError
from . import model_profiles, prompts
from .runtime import provider_environment, staged_working_directory

TIMEOUT_S = int(os.environ.get("ROLENAVI_CLI_TIMEOUT_S", "1800"))


def _template() -> str:
    return os.environ.get("ROLENAVI_LLM_CMD", "").strip()


def _split_template() -> list[str]:
    template = _template()
    if not template:
        raise RoleNaviError(
            "ROLENAVI_PROVIDER=cli requires ROLENAVI_LLM_CMD, for example: "
            "ROLENAVI_LLM_CMD='opencode run {prompt}'")
    try:
        return shlex.split(template)
    except ValueError as e:
        raise RoleNaviError(f"invalid ROLENAVI_LLM_CMD: {e}") from e


def binary() -> str | None:
    try:
        args = _split_template()
    except RoleNaviError:
        return None
    if not args:
        return None
    exe = args[0]
    return exe if Path(exe).exists() else shutil.which(exe)


def _build_command(prompt: str, project: Path | None = None,
                   working_dir: Path | None = None,
                   workflow: str | None = None) -> tuple[list[str], str | None, dict[str, str]]:
    root = working_dir or Path.cwd()
    project_text = str(working_dir or "")
    profile = model_profiles.external_cli_profile_for(workflow)
    effort = profile.get("effort", "")
    cmd: list[str] = []
    prompt_in_argv = False
    for arg in _split_template():
        if "{prompt}" in arg:
            if os.environ.get("ROLENAVI_ALLOW_PROMPT_ARGV") != "1":
                raise RoleNaviError(
                    "{prompt} in ROLENAVI_LLM_CMD exposes private packet text in the "
                    "process list. Remove it to send the prompt through stdin, or set "
                    "ROLENAVI_ALLOW_PROMPT_ARGV=1 for reviewed developer-only use."
                )
            prompt_in_argv = True
            arg = arg.replace("{prompt}", prompt)
        arg = (arg.replace("{root}", str(root))
               .replace("{project}", project_text)
               .replace("{model}", profile["model"])
               .replace("{effort}", effort))
        cmd.append(arg)
    return cmd, None if prompt_in_argv else prompt, profile


def _cli_name() -> str:
    configured = os.environ.get("ROLENAVI_LLM_NAME", "").strip()
    if configured:
        return configured
    try:
        args = _split_template()
    except RoleNaviError:
        return "external-cli"
    return Path(args[0]).name if args else "external-cli"


class ExternalCliProvider:
    name = "cli"

    def __init__(self) -> None:
        if os.environ.get("ROLENAVI_ENABLE_UNSANDBOXED_CLI") != "1":
            raise RoleNaviError(
                "external CLI providers are developer-only because RoleNavi cannot "
                "verify their filesystem/tool sandbox. Set "
                "ROLENAVI_ENABLE_UNSANDBOXED_CLI=1 only after reviewing that provider."
            )
        exe = binary()
        if not exe:
            raise RoleNaviError(
                "external CLI provider configured, but the command executable was not found. "
                "Check ROLENAVI_LLM_CMD.")
        self.exe = exe

    def model_config(self, workflow: str | None = None) -> dict:
        profile = model_profiles.external_cli_profile_for(workflow)
        return {"provider": _cli_name(),
                "model": profile["model"],
                "effort": profile["effort"],
                "settings_file": profile["settings_file"],
                "auth": "external-cli-managed"}

    def complete(self, prompt: str) -> str:
        with staged_working_directory("complete") as stage:
            cmd, stdin_prompt, profile = _build_command(
                prompt, working_dir=stage, workflow="complete")
            env = provider_environment({"ROLENAVI_TASK_MODEL": profile["model"],
                                        "ROLENAVI_TASK_EFFORT": profile.get("effort", "")})
            r = subprocess.run(cmd, input=stdin_prompt, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=TIMEOUT_S,
                               cwd=stage, env=env)
        if r.returncode != 0:
            raise RoleNaviError(f"{_cli_name()} failed (rc={r.returncode}): "
                                 f"{(r.stderr or r.stdout).strip()[:400]}")
        return r.stdout.strip()

    def run(self, workflow: str, context: dict, on_progress=None,
            model_workflow: str | None = None) -> dict:
        prompt, audit = prompts.workflow_prompt_with_audit(workflow, context)
        project = Path(context["project"])
        profile_key = model_workflow or workflow
        stage_cm = staged_working_directory(workflow)
        stage = stage_cm.__enter__()
        cmd, stdin_prompt, profile = _build_command(
            prompt, project=project, working_dir=stage, workflow=profile_key)
        env = provider_environment({"ROLENAVI_TASK_MODEL": profile["model"],
                                    "ROLENAVI_TASK_EFFORT": profile.get("effort", "")})

        t0 = time.monotonic()
        proc = subprocess.Popen(
            cmd,
            cwd=stage,
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
                    raise RoleNaviError(
                        f"{_cli_name()} exceeded ROLENAVI_CLI_TIMEOUT_S={TIMEOUT_S}s")
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
            stage_cm.__exit__(None, None, None)

        if proc.returncode != 0:
            tail = "\n".join(raw_lines[-12:])
            raise RoleNaviError(f"{_cli_name()} failed (rc={proc.returncode}): "
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
        final_text = raw_lines[-1] if raw_lines else ""
        result = {"type": "result", "summary": final_text[:2000], "content": final_text}
        events.append(result)
        return {"workflow": workflow,
                "model_config": self.model_config(profile_key),
                "streamed": True,
                "usage": {"cost_usd": 0.0, "tokens_in": 0, "tokens_out": 0,
                          "num_turns": 0,
                          "input_bytes": audit.input_bytes,
                          "prompt_fingerprint": audit.fingerprint,
                          "data_classes": list(audit.data_classes),
                          "note": "usage is managed by the external CLI"},
                "events": events}
