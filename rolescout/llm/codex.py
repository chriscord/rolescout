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
import queue
import shlex
import shutil
import subprocess
import threading
import time

from ..paths import RoleScoutError, repo_root
from . import model_profiles
from . import prompts

DEFAULT_EXEC_ARGS = ["--sandbox", "workspace-write", "--json"]
TIMEOUT_S = int(os.environ.get("CODEX_TIMEOUT_S", "1800"))
PINNED_CONFIG_KEYS = {"model", "model_reasoning_effort"}
ISOLATION_FLAGS = ["--ignore-user-config", "--ignore-rules"]
ISOLATION_VALUE_FLAGS = {"--profile", "-p"}


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

    def model_config(self, workflow: str | None = None,
                     profile: dict | None = None) -> dict:
        profile = profile or model_profiles.codex_profile_for(workflow)
        return {"provider": "codex-cli",
                "model": profile["model"],
                "effort": profile["effort"],
                "settings_file": profile["settings_file"],
                "auth": "chatgpt-subscription"}

    # ---- low-level exec ----

    @staticmethod
    def _strip_pinned_model_args(args: list[str]) -> list[str]:
        """Remove env-supplied args that would override RoleScout's run contract."""
        out: list[str] = []
        i = 0
        while i < len(args):
            arg = args[i]
            if arg in ISOLATION_FLAGS:
                i += 1
                continue
            if arg in ISOLATION_VALUE_FLAGS:
                i += 2
                continue
            if arg.startswith("--profile=") or arg.startswith("-p="):
                i += 1
                continue
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
                      sandbox_write: bool = True,
                      profile: dict | None = None) -> list[str]:
        args = os.environ.get("CODEX_EXEC_ARGS")
        extra = shlex.split(args) if args else list(DEFAULT_EXEC_ARGS)
        extra = self._strip_pinned_model_args(extra)
        extra += ISOLATION_FLAGS
        if not sandbox_write:
            extra = ["read-only" if a == "workspace-write" else a for a in extra]
        profile_args = (model_profiles.codex_cli_args_for_profile(profile)
                        if profile is not None
                        else model_profiles.codex_cli_args(workflow)[1])
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

    @staticmethod
    def _retryable_model_error(message: str) -> bool:
        text = message.lower()
        return ("capacity" in text or "try a different model" in text
                or "model is overloaded" in text or "temporarily unavailable" in text)

    def run(self, workflow: str, context: dict, on_progress=None,
            model_workflow: str | None = None) -> dict:
        """Streaming execution: every codex output line reaches `on_progress` the
        moment it appears (the CLI/web UI show live activity instead of a blinking
        cursor); the envelope is assembled from the same stream at the end."""
        prompt = prompts.workflow_prompt(workflow, context)
        profile_key = model_workflow or workflow
        profiles = model_profiles.codex_profile_variants(profile_key)
        last_error: RoleScoutError | None = None
        for idx, profile in enumerate(profiles):
            try:
                return self._run_once(workflow, context, prompt, profile, on_progress)
            except RoleScoutError as e:
                last_error = e
                if idx >= len(profiles) - 1 or not self._retryable_model_error(str(e)):
                    raise
                fallback = profiles[idx + 1]
                if on_progress:
                    on_progress(
                        "model fallback: "
                        f"{profile['model']}/{profile.get('effort', '')} failed with capacity; "
                        f"retrying {fallback['model']}/{fallback.get('effort', '')}"
                    )
        raise last_error or RoleScoutError("codex exec failed before starting")

    def _run_once(self, workflow: str, context: dict, prompt: str,
                  profile: dict, on_progress=None) -> dict:
        cmd = self._exec_command(workflow=workflow, profile=profile)
        # PYTHONUTF8/PYTHONIOENCODING force UTF-8 in the agent's Python subprocesses so
        # writing non-ASCII (e.g. "•") doesn't crash on Windows cp949/cp1252 consoles.
        env = {**os.environ, "RECRUITING_PROJECT_DIR": str(context["project"]),
               "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}

        t0 = time.monotonic()
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                stdin=subprocess.PIPE, text=True,
                                encoding="utf-8", errors="replace", env=env)
        jsonl: list[dict] = []
        raw_lines: list[str] = []
        assert proc.stdout is not None
        stdout_queue: queue.Queue[str | BaseException | None] = queue.Queue()

        def read_stdout() -> None:
            try:
                assert proc.stdout is not None
                for item in proc.stdout:
                    stdout_queue.put(item)
            except BaseException as exc:
                stdout_queue.put(exc)
            finally:
                stdout_queue.put(None)

        reader = threading.Thread(target=read_stdout, daemon=True)
        reader.start()
        try:
            if proc.stdin is not None:
                try:
                    proc.stdin.write(prompt)
                    proc.stdin.close()
                except OSError:
                    pass
            while True:
                elapsed = time.monotonic() - t0
                if elapsed > TIMEOUT_S:
                    proc.kill()
                    raise RoleScoutError(f"codex exec exceeded CODEX_TIMEOUT_S={TIMEOUT_S}s")
                try:
                    item = stdout_queue.get(timeout=min(0.25, max(0.01, TIMEOUT_S - elapsed)))
                except queue.Empty:
                    continue
                if item is None:
                    break
                if isinstance(item, BaseException):
                    raise RoleScoutError(f"codex stdout read failed: {item}")
                line = item
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
        final_text = texts[-1] if texts else ""
        result = {"type": "result", "summary": final_text[:2000], "content": final_text}
        events.append(result)
        return {"workflow": workflow,
                "model_config": self.model_config(workflow, profile),
                "streamed": True,  # progress already shown live; don't re-print
                "usage": {"cost_usd": 0.0, "tokens_in": tokens_in,
                          "tokens_out": tokens_out, "num_turns": 0,
                          "note": "chatgpt-subscription: no per-run dollar cost exposed"},
                "events": events}
