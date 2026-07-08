"""Codex CLI provider — uses the user's ChatGPT/Codex SUBSCRIPTION, no API key.

Auth model: the user signs in once with `codex login` (ChatGPT account); the CLI
then bills usage to their subscription. RoleScout never sees credentials — it
only shells out to the locally installed `codex` binary.

Install: `npm install -g @openai/codex` (or `brew install codex`), then
`codex login`. Select this provider with `ROLESCOUT_PROVIDER=codex` (it is also
the automatic default when `codex` is on PATH).

Execution: `codex exec` (non-interactive) inside the repo with
RECRUITING_PROJECT_DIR pinned — the agent reads skills, runs the validators, and
writes through the same pipeline every backend uses. General flags are
overridable via CODEX_EXEC_ARGS because CLI versions differ; model and reasoning
effort are pinned by RoleScout workflow profiles.

Honesty note: subscription usage exposes no dollar cost; cost_usd is recorded as
0.0 with the model config marking auth=chatgpt-subscription, and token counts
are parsed from `--json` events when available, else 0.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess

from ..paths import RoleScoutError, repo_root
from . import model_profiles
from . import prompts

DEFAULT_EXEC_ARGS = ["--sandbox", "workspace-write", "--json"]
TIMEOUT_S = int(os.environ.get("CODEX_TIMEOUT_S", "1800"))
PINNED_CONFIG_KEYS = {"model", "model_reasoning_effort"}


def binary() -> str | None:
    return shutil.which(os.environ.get("CODEX_BIN", "codex"))


def logged_in() -> bool | None:
    """True/False when determinable; None when the CLI doesn't support the check."""
    exe = binary()
    if not exe:
        return False
    try:
        r = subprocess.run([exe, "login", "status"], capture_output=True,
                           text=True, encoding="utf-8", errors="replace",
                           timeout=30)
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return None


class CodexProvider:
    name = "codex"

    def __init__(self) -> None:
        exe = binary()
        if not exe:
            raise RoleScoutError(
                "ROLESCOUT_PROVIDER=codex but the `codex` CLI is not on PATH — "
                "install it (`npm install -g @openai/codex` or `brew install codex`) "
                "and sign in with `codex login` (uses your ChatGPT subscription)")
        self.exe = exe
        status = logged_in()
        if status is False:
            raise RoleScoutError(
                "codex CLI found but not signed in — run `codex login` "
                "(ChatGPT account; usage bills to your subscription)")
        # status None: old CLI without `login status` — proceed, exec will surface auth errors

    def model_config(self, workflow: str | None = None) -> dict:
        profile = model_profiles.codex_profile_for(workflow)
        return {"provider": "codex-cli",
                "model": profile["model"],
                "effort": profile["effort"],
                "settings_file": profile["settings_file"],
                "auth": "chatgpt-subscription"}

    # ---- low-level exec ----

    @staticmethod
    def _strip_pinned_model_args(args: list[str]) -> list[str]:
        """Remove model/effort args from CODEX_EXEC_ARGS so workflow profiles win."""
        out: list[str] = []
        i = 0
        while i < len(args):
            arg = args[i]
            if arg in ("--model", "-m"):
                i += 2
                continue
            if arg.startswith("--model=") or arg.startswith("-m="):
                i += 1
                continue
            if arg in ("--config", "-c"):
                value = args[i + 1] if i + 1 < len(args) else ""
                key = value.split("=", 1)[0]
                if key in PINNED_CONFIG_KEYS:
                    i += 2
                    continue
                out.append(arg)
                if i + 1 < len(args):
                    out.append(args[i + 1])
                    i += 2
                else:
                    i += 1
                continue
            if arg.startswith("--config="):
                value = arg.split("=", 1)[1]
                key = value.split("=", 1)[0]
                if key in PINNED_CONFIG_KEYS:
                    i += 1
                    continue
            out.append(arg)
            i += 1
        return out

    def _exec_command(self, workflow: str | None = None,
                      sandbox_write: bool = True) -> list[str]:
        args = os.environ.get("CODEX_EXEC_ARGS")
        extra = shlex.split(args) if args else list(DEFAULT_EXEC_ARGS)
        extra = self._strip_pinned_model_args(extra)
        if not sandbox_write:
            extra = ["read-only" if a == "workspace-write" else a for a in extra]
        _, profile_args = model_profiles.codex_cli_args(workflow)
        extra += profile_args
        # Prompt is passed on stdin. Passing the full RoleScout prompt as argv
        # exceeds Windows' command-line limit on real search runs.
        return [self.exe, "exec", "--cd", str(repo_root()), *extra, "-"]

    def _exec(self, prompt: str, workflow: str | None = None,
              sandbox_write: bool = True) -> tuple[str, list[dict]]:
        """Run codex exec; returns (raw_stdout, parsed_jsonl_events_if_any)."""
        cmd = self._exec_command(workflow=workflow, sandbox_write=sandbox_write)
        r = subprocess.run(cmd, capture_output=True, text=True,
                           input=prompt,
                           encoding="utf-8", errors="replace", timeout=TIMEOUT_S,
                           env={**os.environ, "PYTHONUTF8": "1",
                                "PYTHONIOENCODING": "utf-8"})
        if r.returncode != 0:
            detail = (r.stderr or r.stdout).strip() or "no output"
            raise RoleScoutError(f"codex exec failed (rc={r.returncode}): "
                                 f"{detail[:400]}")
        events = []
        for line in r.stdout.splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        return r.stdout, events

    @staticmethod
    def _texts_from_jsonl(events: list[dict]) -> list[str]:
        """Pull agent-visible text out of codex --json events, defensively."""
        texts = []
        for ev in events:
            if not isinstance(ev, dict):
                continue
            item = ev.get("item") if isinstance(ev.get("item"), dict) else {}
            for key in ("text", "message", "content", "last_agent_message"):
                v = ev.get(key) or item.get(key)
                if isinstance(v, str) and v.strip():
                    texts.append(v.strip())
                    break
        return texts

    def complete(self, prompt: str) -> str:
        """Single completion (bench B0/B1, judging) — read-only sandbox."""
        raw, events = self._exec(prompt, workflow="complete", sandbox_write=False)
        texts = self._texts_from_jsonl(events)
        return texts[-1] if texts else raw

    # ---- workflow run (envelope contract; streams line-by-line) ----

    def run(self, workflow: str, context: dict, on_progress=None) -> dict:
        """Streaming execution: every codex output line reaches `on_progress` the
        moment it appears (the CLI/web UI show live activity instead of a blinking
        cursor); the envelope is assembled from the same stream at the end."""
        prompt = prompts.workflow_prompt(workflow, context)
        cmd = self._exec_command(workflow=workflow)
        # PYTHONUTF8/PYTHONIOENCODING force UTF-8 in the agent's Python subprocesses so
        # writing non-ASCII (e.g. "•") doesn't crash on Windows cp949/cp1252 consoles.
        env = {**os.environ, "RECRUITING_PROJECT_DIR": str(context["project"]),
               "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}

        import time
        t0 = time.monotonic()
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                stdin=subprocess.PIPE, text=True,
                                encoding="utf-8", errors="replace", env=env)
        jsonl: list[dict] = []
        raw_lines: list[str] = []
        assert proc.stdout is not None
        try:
            if proc.stdin is not None:
                try:
                    proc.stdin.write(prompt)
                    proc.stdin.close()
                except OSError:
                    pass
            for line in proc.stdout:
                if time.monotonic() - t0 > TIMEOUT_S:
                    proc.kill()
                    raise RoleScoutError(f"codex exec exceeded CODEX_TIMEOUT_S={TIMEOUT_S}s")
                line = line.rstrip("\n")
                if not line.strip():
                    continue
                raw_lines.append(line)
                text = line
                if line.lstrip().startswith("{"):
                    try:
                        ev = json.loads(line)
                        jsonl.append(ev)
                        parsed = self._texts_from_jsonl([ev])
                        text = parsed[0] if parsed else ""
                    except json.JSONDecodeError:
                        pass
                if text and on_progress:
                    on_progress(text.splitlines()[0][:300])
            proc.wait(timeout=30)
        finally:
            try:
                proc.stdout.close()
            except OSError:
                pass
            if proc.poll() is None:
                proc.kill()
        if proc.returncode != 0:
            detail = "\n".join(raw_lines[-12:]) or "no output"
            raise RoleScoutError(f"codex exec failed (rc={proc.returncode}): "
                                 f"{detail[:1000]}")

        texts = self._texts_from_jsonl(jsonl) or raw_lines
        events: list[dict] = []
        tokens_in = tokens_out = 0
        for ev in jsonl:
            usage = ev.get("usage") if isinstance(ev, dict) else None
            if isinstance(usage, dict):
                tokens_in = usage.get("input_tokens", tokens_in) or tokens_in
                tokens_out = usage.get("output_tokens", tokens_out) or tokens_out
        for text in texts[:-1]:
            first = text.splitlines()[0][:200]
            events.append({"type": "progress", "text": first})
            if text.startswith("APPROVAL_REQUIRED:"):
                events.append({"type": "external_action", "action": "agent_requested",
                               "target": text[len("APPROVAL_REQUIRED:"):].strip()[:300],
                               "content": text, "on_approve": [], "on_deny": []})
        events.append({"type": "result",
                       "summary": (texts[-1] if texts else "")[:2000]})
        return {"workflow": workflow,
                "model_config": self.model_config(workflow),
                "streamed": True,  # progress already shown live; don't re-print
                "usage": {"cost_usd": 0.0, "tokens_in": tokens_in,
                          "tokens_out": tokens_out, "num_turns": 0,
                          "note": "chatgpt-subscription: no per-run dollar cost exposed"},
                "events": events}
