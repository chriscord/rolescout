#!/usr/bin/env bash
# One-command Linux installer for RoleNavi.
set -euo pipefail

REPO_URL="${ROLENAVI_REPO_URL:-https://github.com/chriscord/rolenavi.git}"
INSTALL_DIR="${ROLENAVI_INSTALL_DIR:-$HOME/rolenavi}"

install_python() {
  if command -v apt-get >/dev/null 2>&1; then
    sudo apt-get update
    sudo apt-get install -y python3 python3-venv
  elif command -v dnf >/dev/null 2>&1; then
    sudo dnf install -y python3
  elif command -v pacman >/dev/null 2>&1; then
    sudo pacman -Sy --noconfirm python
  else
    echo "ERROR: Python 3.10+ is required; install it with your distribution package manager."
    exit 1
  fi
}

if [[ "${1:-}" != "--from-checkout" ]]; then
  if ! command -v git >/dev/null 2>&1; then
    echo "ERROR: Git is required; install it with your distribution package manager."
    exit 1
  fi
  if [[ -e "$INSTALL_DIR" ]]; then
    echo "ERROR: install directory already exists: $INSTALL_DIR"
    echo "Set ROLENAVI_INSTALL_DIR to choose another location."
    exit 1
  fi
  git clone --depth 1 "$REPO_URL" "$INSTALL_DIR"
  exec "$INSTALL_DIR/tools/install-linux.sh" --from-checkout
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
if [[ -z "$PY" ]]; then
  install_python
  for candidate in python3.13 python3.12 python3.11 python3.10 python3; do
    if command -v "$candidate" >/dev/null 2>&1 && \
       "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
      PY="$candidate"
      break
    fi
  done
fi
if [[ -z "$PY" ]]; then
  echo "ERROR: installed Python is older than 3.10. Install Python 3.10+ and rerun."
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
