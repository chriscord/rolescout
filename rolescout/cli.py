"""rolescout CLI — public runtime entry point.

Commands: init, doctor, run, web. Dev/eval commands stay in the
private development repository and are not shipped in the public runtime.
"""

from __future__ import annotations

import argparse
import os
import sys

from . import CLI_NAME, PRODUCT_NAME, __version__
from .paths import RoleScoutError

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except AttributeError:
    pass


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog=CLI_NAME,
        description=f"{PRODUCT_NAME} — local-first AI recruiting workflow")
    ap.add_argument("--version", action="version", version=f"{CLI_NAME} {__version__}")
    sub = ap.add_subparsers(dest="cmd", metavar="command")

    p = sub.add_parser("init", help="create/activate/list search projects (person × focus)")
    p.add_argument("--person", help="person code (lowercase slug)")
    p.add_argument("--focus", help="search focus (lowercase slug)")
    p.add_argument("--activate", metavar="CODE", help="switch to existing <person>--<focus>")
    p.add_argument("--list", action="store_true", help="list projects")
    p.add_argument("--linkedin-url", metavar="URL",
                   help="optional: your LinkedIn profile URL (stored in "
                        "profiles/<person>/profile-meta.json; used by prep/positioning)")
    p.add_argument("--name", help="your display name (profile meta)")
    p.add_argument("--instructions", help="standing instructions injected into every run")
    p.add_argument("--locations", help="target locations, comma-separated (REQUIRED for a "
                                       "new project — grounds every search)")
    p.add_argument("--role", help="focus role, e.g. 'AI product manager'")
    p.add_argument("--level", help="target level, e.g. senior / staff / director")
    p.add_argument("--companies", help="target company seeds, comma-separated "
                                       "(the agent also explores similar companies)")
    p.add_argument("--comp-range", help="target comp range (sensitive; local only)")
    p.add_argument("--negatives", help="excludes, comma-separated (companies/titles/industries)")

    sub.add_parser("doctor", help="environment & install health check")

    p = sub.add_parser("run", help="run a workflow headlessly")
    p.add_argument("workflow", choices=["profile-intake", "search", "score", "prep",
                                        "prep-strategy", "prep-resume", "prep-linkedin",
                                        "prep-interview", "story-bank", "apply"])
    p.add_argument("--project", help="project code (default: active project)")
    p.add_argument("--person", help="person code for profile-intake")
    p.add_argument("--task", help="free-text task focus passed to the workflow")
    p.add_argument("--mock", action="store_true", help="force mock mode (LLM_MOCK=1)")
    p.add_argument("--provider", choices=["codex", "cli", "mock"],
                   help="LLM backend for this run (default: codex when available, else mock)")
    p.add_argument("--llm-cmd",
                   help="external agent CLI command template for --provider cli; use "
                        "{prompt}, {root}, {project}, {model}, and {effort} placeholders "
                        "as needed")
    p.add_argument("--llm-name",
                   help="display name for --provider cli telemetry, e.g. glm or opencode")
    p.add_argument("--llm-model",
                   help="model label for --provider cli templates and telemetry "
                        "(search discovery is deterministic unless legacy search is enabled)")
    p.add_argument("--llm-effort",
                   help="effort label for --provider cli templates and telemetry "
                        "(search discovery is deterministic unless legacy search is enabled)")
    p.add_argument("--max-turns", type=int, default=40)

    p = sub.add_parser("web", help="local web UI: run workflows and watch live progress")
    p.add_argument("--port", type=int, default=8787)
    p.add_argument("--no-open", action="store_true", help="don't open the browser")

    return ap


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    if not args.cmd:
        ap.print_help()
        return 0
    try:
        if getattr(args, "provider", None):
            os.environ["ROLESCOUT_PROVIDER"] = args.provider
        if getattr(args, "llm_cmd", None):
            os.environ["ROLESCOUT_LLM_CMD"] = args.llm_cmd
            os.environ.setdefault("ROLESCOUT_PROVIDER", "cli")
        if getattr(args, "llm_name", None):
            os.environ["ROLESCOUT_LLM_NAME"] = args.llm_name
        if getattr(args, "llm_model", None):
            os.environ["ROLESCOUT_LLM_MODEL"] = args.llm_model
        if getattr(args, "llm_effort", None):
            os.environ["ROLESCOUT_LLM_EFFORT"] = args.llm_effort
        if args.cmd == "init":
            from . import initcmd
            return initcmd.main(args)
        if args.cmd == "doctor":
            from . import doctor
            return doctor.main(args)
        if args.cmd == "run":
            from .runner import workflows
            return workflows.main(args)
        if args.cmd == "web":
            from .web import server as web_server
            return web_server.main(args)
        ap.error(f"unknown command {args.cmd}")
    except RoleScoutError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
