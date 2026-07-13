"""Revisioned, atomic employer-universe state owned by the runner."""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import project_meta

UNIVERSE_PATH = Path("targets/company-universe.json")


def is_descriptor(value: str) -> bool:
    text = " ".join(str(value or "").lower().split())
    return bool(
        re.search(
            r"\b(?:startups?|scaleups?|companies|employers|firms|organizations|"
            r"organisations|platforms?|labs?|fintech|insurtech)\b",
            text,
        )
        and (" or " in text or " and " in text or "," in text
             or text.startswith(("ai ", "data ", "tech ", "fintech ")))
    )


def dependency_fingerprint(meta: dict[str, Any]) -> str:
    payload = {
        key: meta.get(key)
        for key in ("target_locations", "focus_role", "target_companies", "negatives")
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _write_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def materialize_seed_universe(project: Path) -> dict[str, Any]:
    """Commit an immediately searchable seed projection for current preferences."""
    meta = project_meta.load(project)
    path = project / UNIVERSE_PATH
    previous: dict[str, Any] = {}
    try:
        previous = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        pass
    dep = dependency_fingerprint(meta)
    reuse_previous = previous.get("universe_dependency_fingerprint") == dep
    exact = [
        str(value).strip() for value in meta.get("target_companies", [])
        if str(value).strip() and not is_descriptor(str(value))
    ]
    descriptors = [
        str(value).strip() for value in meta.get("target_companies", [])
        if str(value).strip() and is_descriptor(str(value))
    ]
    buckets: list[dict[str, Any]] = []
    for seed in exact:
        companies = [{
            "name": seed,
            "seed": True,
            "derived_from": seed,
            "relationship": "declared_seed",
            "rationale": "User-declared employer seed; searchable immediately.",
            "evidence": "declared target",
            "priority": "high",
        }]
        if reuse_previous:
            for bucket in previous.get("buckets", []):
                for company in bucket.get("companies", []) if isinstance(bucket, dict) else []:
                    if (
                        isinstance(company, dict)
                        and not company.get("seed")
                        and str(company.get("derived_from", "")).lower() == seed.lower()
                    ):
                        companies.append(company)
        buckets.append({
            "bucket": f"seed:{seed}",
            "why_relevant": f"Declared employer seed and its validated close peers: {seed}.",
            "companies": companies,
        })
    payload: dict[str, Any] = {
        "schema": "rolenavi-progressive-universe-v1",
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "state": "expanding" if exact or descriptors else "seed_ready",
        "expansion_mode": "progressive-background",
        "preference_revision": int(meta.get("preference_revision", 0) or 0),
        "preference_fingerprint": project_meta.preference_fingerprint(meta),
        "universe_dependency_fingerprint": dep,
        "buckets": buckets,
        "expanded_descriptors": [],
        "pending_inputs": [
            {"input": seed, "kind": "seed_peer_expansion", "status": "pending"}
            for seed in exact
        ] + [
            {"input": descriptor, "kind": "descriptor_expansion", "status": "pending"}
            for descriptor in descriptors
        ],
        "excluded": [],
        "omissions_review": {"status": "pending", "items": []},
    }
    _write_atomic(path, payload)
    return payload


def merge_proposals(
    project: Path,
    proposals: list[dict[str, Any]],
    *,
    expected_revision: int,
) -> dict[str, Any]:
    """Single-writer merge for proposal-only model workers."""
    meta = project_meta.load(project)
    current_revision = int(meta.get("preference_revision", 0) or 0)
    if current_revision != expected_revision:
        raise ValueError(
            f"stale universe proposals: revision {expected_revision}, current {current_revision}"
        )
    base = materialize_seed_universe(project)
    bucket_by_input = {
        str(bucket.get("bucket", "")).removeprefix("seed:").lower(): bucket
        for bucket in base.get("buckets", [])
        if isinstance(bucket, dict)
    }
    seen = {
        re.sub(r"\W+", "", str(company.get("name", "")).lower())
        for bucket in base.get("buckets", [])
        for company in bucket.get("companies", [])
        if isinstance(company, dict)
    }
    expanded_descriptors: list[dict[str, Any]] = []
    completed: set[str] = set()
    excluded: list[dict[str, str]] = []
    for proposal in proposals:
        if int(proposal.get("preference_revision", -1)) != expected_revision:
            continue
        source = str(proposal.get("input", "")).strip()
        kind = str(proposal.get("kind", "")).strip()
        if not source or kind not in {"seed_peer_expansion", "descriptor_expansion"}:
            continue
        completed.add(source.lower())
        target_bucket = bucket_by_input.get(source.lower())
        if target_bucket is None:
            target_bucket = {
                "bucket": f"descriptor:{source}",
                "why_relevant": f"Employers derived from user descriptor: {source}.",
                "companies": [],
            }
            base["buckets"].append(target_bucket)
        added: list[str] = []
        for company in proposal.get("proposed_companies", []):
            if not isinstance(company, dict):
                continue
            name = str(company.get("name", "")).strip()
            key = re.sub(r"\W+", "", name.lower())
            if not name or is_descriptor(name) or key in seen:
                continue
            rationale = str(company.get("rationale", "")).strip()
            relationship = str(company.get("relationship", "")).strip()
            if not rationale or not relationship:
                continue
            seen.add(key)
            added.append(name)
            target_bucket["companies"].append({
                "name": name,
                "seed": False,
                "derived_from": source,
                "relationship": relationship,
                "rationale": rationale,
                "evidence": str(company.get("evidence", "")).strip() or "model market-map inference",
                "priority": str(company.get("priority", "medium")),
                "confidence": str(company.get("confidence", "medium")),
            })
        if kind == "descriptor_expansion":
            expanded_descriptors.append({"input": source, "employers": added})
        for item in proposal.get("excluded", []):
            if isinstance(item, dict) and item.get("name_or_bucket") and item.get("reason"):
                excluded.append({
                    "name_or_bucket": str(item["name_or_bucket"]),
                    "reason": str(item["reason"]),
                })
    base["expanded_descriptors"] = expanded_descriptors
    base["excluded"] = excluded
    base["pending_inputs"] = [
        {**item, "status": "complete" if str(item.get("input", "")).lower() in completed else "failed"}
        for item in base.get("pending_inputs", [])
    ]
    base["state"] = (
        "ready" if all(item.get("status") == "complete" for item in base["pending_inputs"])
        else "partial"
    )
    base["omissions_review"] = {
        "status": "complete",
        "items": [
            item for proposal in proposals for item in proposal.get("omissions", [])
            if isinstance(item, dict)
        ],
    }
    _write_atomic(project / UNIVERSE_PATH, base)
    return base
