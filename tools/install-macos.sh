#!/usr/bin/env bash
# One-command macOS installer for RoleNavi.
set -euo pipefail

REPO_URL="${ROLENAVI_REPO_URL:-https://github.com/chriscord/rolenavi.git}"
INSTALL_DIR="${ROLENAVI_INSTALL_DIR:-$HOME/RoleNavi}"

if [[ "${1:-}" != "--from-checkout" ]]; then
  if ! command -v git >/dev/null 2>&1; then
    echo "ERROR: Git is required. Install Xcode Command Line Tools, then rerun:"
    echo "  xcode-select --install"
    exit 1
  fi
  if [[ -e "$INSTALL_DIR" ]]; then
    echo "ERROR: install directory already exists: $INSTALL_DIR"
    echo "Set ROLENAVI_INSTALL_DIR to choose another location."
    exit 1
  fi
  git clone --depth 1 "$REPO_URL" "$INSTALL_DIR"
  exec "$INSTALL_DIR/tools/install-macos.sh" --from-checkout
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY=""
for candidate in python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$candidate" >/dev/null 2>&1 && \
     "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
    PY="$candidate"
    break
  fi
done

if [[ -z "$PY" ]] && command -v brew >/dev/null 2>&1; then
  brew install python@3.12
  PY="python3.12"
fi
if [[ -z "$PY" ]]; then
  echo "ERROR: Python 3.10+ is required. Install it with Homebrew, then rerun:"
  echo "  brew install python@3.12"
  exit 1
fi

cd "$ROOT"
"$PY" -m venv .venv
./.venv/bin/python -m pip install --upgrade pip setuptools
./.venv/bin/python -m pip install -e ".[xlsx]"
./.venv/bin/rolenavi --version

echo
echo "RoleNavi installed in $ROOT"
echo "  activate: source .venv/bin/activate"
echo "  next:     npm install -g @openai/codex && codex login"
echo "  verify:   rolenavi doctor"
