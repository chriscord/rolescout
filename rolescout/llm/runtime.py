"""Provider process isolation helpers shared by all live backends."""

from __future__ import annotations

import os
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path

from ..paths import home_dir

_BASE_ENV_KEYS = {
    "PATH", "PATHEXT", "SystemRoot", "WINDIR", "COMSPEC", "HOME", "USERPROFILE",
    "APPDATA", "LOCALAPPDATA", "TEMP", "TMP", "TMPDIR", "CODEX_HOME",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
}


def provider_environment(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Build a documented allowlist instead of inheriting the parent environment."""
    keys = set(_BASE_ENV_KEYS)
    configured = os.environ.get("ROLESCOUT_PROVIDER_ENV_ALLOWLIST", "")
    keys.update(item.strip() for item in configured.split(",") if item.strip())
    env = {key: os.environ[key] for key in keys if key in os.environ}
    env.update({"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"})
    if extra:
        env.update({str(k): str(v) for k, v in extra.items()})
    return env


@contextmanager
def staged_working_directory(workflow: str):
    """Yield a disposable neutral cwd containing no profile/project files."""
    base = home_dir() / "runtime" / "staging"
    base.mkdir(parents=True, exist_ok=True)
    path = Path(tempfile.mkdtemp(prefix=f"{workflow}-", dir=base))
    try:
        (path / "README.txt").write_text(
            "RoleScout isolated model stage. All approved inputs are in the prompt. "
            "Filesystem and shell access are not part of the workflow contract.\n",
            encoding="utf-8",
        )
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)
