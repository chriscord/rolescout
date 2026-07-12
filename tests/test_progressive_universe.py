from __future__ import annotations

import json
from pathlib import Path

import pytest

from rolescout import project_meta, universe


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "projects" / "person--focus"
    project.mkdir(parents=True)
    (project / "project.json").write_text(
        json.dumps({"person": "person", "focus": "focus", "status": "active"}),
        encoding="utf-8",
    )
    project_meta.update(
        project,
        target_locations=["Singapore"],
        focus_role="Product, Strategy",
        target_companies=["Example Seed", "AI or data startups"],
    )
    return project


def test_explicit_seed_is_searchable_before_descriptor_expansion(tmp_path: Path):
    project = _project(tmp_path)
    payload = universe.materialize_seed_universe(project)
    companies = [
        company["name"]
        for bucket in payload["buckets"]
        for company in bucket["companies"]
    ]
    assert companies == ["Example Seed"]
    assert payload["state"] == "expanding"
    assert {item["kind"] for item in payload["pending_inputs"]} == {
        "seed_peer_expansion", "descriptor_expansion"
    }


def test_single_coordinator_merges_typed_proposals_and_rejects_stale(tmp_path: Path):
    project = _project(tmp_path)
    revision = project_meta.load(project)["preference_revision"]
    proposals = [{
        "schema": "rolescout-universe-proposal-v1",
        "preference_revision": revision,
        "input": "Example Seed",
        "kind": "seed_peer_expansion",
        "proposed_companies": [{
            "name": "Runtime Peer",
            "relationship": "direct_competitor",
            "rationale": "Competes for the same target-location product talent.",
            "evidence": "runtime market-map inference",
            "priority": "high",
            "confidence": "medium",
        }],
        "excluded": [],
        "omissions": [],
    }, {
        "schema": "rolescout-universe-proposal-v1",
        "preference_revision": revision,
        "input": "AI or data startups",
        "kind": "descriptor_expansion",
        "proposed_companies": [{
            "name": "Descriptor Employer",
            "relationship": "funded_entrant",
            "rationale": "Matches the descriptor and target-location role family.",
            "evidence": "runtime market-map inference",
            "priority": "medium",
            "confidence": "medium",
        }],
        "excluded": [],
        "omissions": [],
    }]
    merged = universe.merge_proposals(project, proposals, expected_revision=revision)
    names = {
        company["name"]
        for bucket in merged["buckets"]
        for company in bucket["companies"]
    }
    assert names == {"Example Seed", "Runtime Peer", "Descriptor Employer"}
    assert merged["state"] == "ready"

    project_meta.update(project, target_locations=["Tokyo"])
    with pytest.raises(ValueError, match="stale universe proposals"):
        universe.merge_proposals(project, proposals, expected_revision=revision)
