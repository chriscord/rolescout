#!/usr/bin/env bash
# One-command macOS installer for RoleNavi.
set -euo pipefail

REPO_URL="${ROLENAVI_REPO_URL:-https://github.com/chriscord/rolenavi.git}"
INSTALL_DIR="${ROLENAVI_INSTALL_DIR:-$HOME/RoleNavi}"

if [[ "${1:-}" == "--from-checkout" ]]; then
  ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
else
  if ! command -v git >/dev/null 2>&1; then
    echo "ERROR: Git is required. Install Xcode Command Line Tools, then rerun:"
    echo "  xcode-select --install"
    exit 1
  fi
  if [[ -e "$INSTALL_DIR" ]]; then
    if [[ -L "$INSTALL_DIR" ]] ||
       [[ ! -d "$INSTALL_DIR/.git" ]] ||
       [[ ! -f "$INSTALL_DIR/tools/install-macos.sh" ]] ||
       [[ "$(git -C "$INSTALL_DIR" remote get-url origin 2>/dev/null || true)" != "$REPO_URL" ]]; then
      echo "ERROR: install directory exists but is not the expected RoleNavi checkout: $INSTALL_DIR"
      echo "Move it aside or set ROLENAVI_INSTALL_DIR to choose another location."
      exit 1
    fi
    echo "Resuming the existing RoleNavi checkout in $INSTALL_DIR"
  else
    git clone --depth 1 "$REPO_URL" "$INSTALL_DIR"
  fi
  ROOT="$(cd "$INSTALL_DIR" && pwd)"
fi

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
  if command -v python3.12 >/dev/null 2>&1 &&
     python3.12 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
    PY="python3.12"
  fi
fi
if [[ -z "$PY" ]]; then
  echo "ERROR: Python 3.10+ is required. Install it with Homebrew, then rerun:"
  echo "  brew install python@3.12"
  exit 1
fi

cd "$ROOT"
"$PY" -m venv .venv
"$ROOT/.venv/bin/python" -m pip install --upgrade pip setuptools
"$ROOT/.venv/bin/python" -m pip install -e ".[xlsx]"
"$ROOT/.venv/bin/rolenavi" --version

echo
echo "RoleNavi installed in $ROOT"
echo "  activate: source .venv/bin/activate"
echo "  next:     npm install -g @openai/codex && codex login"
echo "  verify:   rolenavi doctor"
