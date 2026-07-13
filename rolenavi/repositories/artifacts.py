"""Content-addressed artifact provenance index."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else ""


def default_dependencies(root: Path) -> list[Path]:
    candidates = [
        root / "project.json", root / "project-meta.json", root / "data" / "focused-jobs.json",
        root / "candidate-profile.md", root / "evidence-map.md",
    ]
    return [path for path in candidates if path.is_file()]


def record(root: Path, logical_id: str, artifact: Path, workflow: str,
           dependencies: list[Path] | None = None,
           contract_version: str = "rolenavi-stage-contract-v2",
           input_fingerprint: str = "", model: str = "") -> None:
    index_path = root / "artifacts" / "manifest.json"
    try:
        doc = json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else {}
    except (OSError, json.JSONDecodeError):
        doc = {}
    entries = doc.setdefault("artifacts", {})
    deps = dependencies if dependencies is not None else default_dependencies(root)
    entries[logical_id] = {
        "path": logical_id,
        "content_sha256": _hash(artifact),
        "dependencies": {path.relative_to(root).as_posix(): _hash(path) for path in deps},
        "workflow": workflow,
        "contract_version": contract_version,
        "input_fingerprint": input_fingerprint,
        "model": model,
        "validator_version": "rolenavi-release-gate-v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    doc["schema"] = "rolenavi-artifact-index-v1"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix="manifest.", suffix=".json.tmp", dir=index_path.parent)
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        tmp.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(tmp, index_path)
    finally:
        tmp.unlink(missing_ok=True)


def fresh(root: Path, logical_id: str) -> bool:
    index_path = root / "artifacts" / "manifest.json"
    try:
        entry = json.loads(index_path.read_text(encoding="utf-8"))["artifacts"][logical_id]
    except (OSError, KeyError, json.JSONDecodeError, TypeError):
        return False
    artifact = root / entry.get("path", "")
    if _hash(artifact) != entry.get("content_sha256"):
        return False
    for rel, expected in entry.get("dependencies", {}).items():
        if _hash(root / rel) != expected:
            return False
    return True
