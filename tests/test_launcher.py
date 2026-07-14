from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(os.name == "nt", reason="Unix launcher contract")
@pytest.mark.parametrize(
    ("arguments", "expected_arguments"),
    [([], ["web"]), (["doctor"], ["doctor"]), (["--version"], ["--version"])],
)
def test_start_launcher_hides_venv_and_forwards_arguments(
    tmp_path: Path, arguments: list[str], expected_arguments: list[str]
) -> None:
    launcher = tmp_path / "start"
    shutil.copy2(ROOT / "start", launcher)
    launcher.chmod(0o755)

    binary = tmp_path / ".venv" / "bin" / "rolenavi"
    binary.parent.mkdir(parents=True)
    binary.write_text(
        "#!/usr/bin/env bash\n"
        "printf 'cwd=%s\\n' \"$PWD\"\n"
        "printf 'arg=%s\\n' \"$@\"\n",
        encoding="utf-8",
    )
    binary.chmod(0o755)

    result = subprocess.run(
        [str(launcher), *arguments],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout.splitlines() == [
        f"cwd={tmp_path}",
        *(f"arg={argument}" for argument in expected_arguments),
    ]
