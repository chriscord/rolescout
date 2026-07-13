from __future__ import annotations

import json

import pytest

from rolenavi.paths import RoleNaviError
from rolenavi.runner import workflows


@pytest.mark.parametrize("path", [
    "../escape.md", "..\\escape.md", "/absolute.md", "C:\\private\\absolute.md",
    "//server/share.md", "", ".", "folder/../../escape.md",
])
def test_artifact_path_rejects_traversal_and_absolute_paths(path: str):
    with pytest.raises(RoleNaviError):
        workflows._safe_artifact_rel(path)


def test_artifact_path_accepts_safe_unicode_and_normalizes_separator():
    _, display = workflows._safe_artifact_rel("résumés\\그룹\\draft.md")
    assert display == "résumés/그룹/draft.md"


def test_typed_payload_rejects_unknown_fields_and_malformed_json():
    payload = {"schema": "rolenavi-artifact-output-v1", "artifacts": [],
               "store_writes": [], "unexpected": True}
    assert workflows._extract_runner_artifact_payload(
        "ROLENAVI_ARTIFACT_OUTPUT_JSON:\n" + json.dumps(payload)) is None
    assert workflows._extract_runner_artifact_payload(
        "ROLENAVI_ARTIFACT_OUTPUT_JSON:\n{bad") is None
