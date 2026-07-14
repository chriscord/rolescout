#!/usr/bin/env bash
# One-command setup: find Python 3.10+, create .venv, install RoleNavi editable.
# Exists because macOS's default python3 is often the Xcode CLT 3.9 with an old pip
# that can't read pyproject metadata (installs as "UNKNOWN", then fails on perms).
set -u
cd "$(dirname "$0")/.."

PY=""
for cand in python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$cand" >/dev/null 2>&1 && \
     "$cand" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then
    PY="$cand"; break
  fi
done
if [ -z "$PY" ]; then
  echo "ERROR: Python 3.10+ not found on PATH."
  echo "  Your 'python3' is likely the Xcode Command Line Tools 3.9 — it cannot install this package."
  echo "  Install a newer Python first, then re-run tools/setup.sh:"
  echo "    brew install python@3.12        # Homebrew"
  echo "    or download from https://www.python.org/downloads/"
  exit 1
fi
echo "using $PY ($("$PY" -V 2>&1))"

"$PY" -m venv .venv || { echo "ERROR: venv creation failed"; exit 1; }
./.venv/bin/pip install -q --upgrade pip setuptools || { echo "ERROR: pip upgrade failed"; exit 1; }
./.venv/bin/pip install -q -e ".[xlsx]" || { echo "ERROR: install failed"; exit 1; }
./.venv/bin/rolenavi --version || exit 1

echo
echo "OK — installed into .venv (system Python untouched)"
echo "  start UI:  ./start"
echo "  verify:    ./start doctor"
echo "  live runs: install Codex CLI, then run: codex login"
echo "  other CLIs: ./start run search --provider cli --llm-cmd 'your-agent {prompt}'"
