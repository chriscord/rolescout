"""Central model-input gateway: allowlist, minimize, redact, and fingerprint."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from .classification import allowed_classes

MAX_TEXT_BYTES = 24_000
MAX_PACKET_BYTES = 120_000

# Keys are policy-bearing. Unknown context is not forwarded.
COMMON_KEYS = frozenset({"task", "targets", "run_intent", "profile_ready", "profile_stale"})
WORKFLOW_KEYS: dict[str, frozenset[str]] = {
    "profile-intake": frozenset({"profile_source_packet"}),
    "capability-ledger": frozenset({"capability_ledger_packet"}),
    "opportunity-plan": frozenset({"universe_seed_catalog"}),
    "universe-expand": frozenset({"universe_task"}),
    "search": frozenset({"search_phase", "search_view_filter", "search_shard",
                         "search_retry", "runner_context_packet"}),
    "score": frozenset({"score_batch"}),
    "prep-strategy": frozenset({"runner_context_packet"}),
    "prep-resume": frozenset({"runner_context_packet", "resume_group_packet"}),
    "prep-linkedin": frozenset({"runner_context_packet", "linkedin_group_packet"}),
    "story-bank": frozenset({"runner_context_packet"}),
    "prep-interview": frozenset({"runner_context_packet", "interview_role_packet",
                                 "interview_stage", "prep_interview_quality_retry",
                                 "prep_interview_artifact_retry"}),
    "apply": frozenset({"runner_context_packet"}),
}

LOCAL_ONLY_KEYS = frozenset({
    "linkedin_url", "linkedin_source_path", "profile_dir", "project", "person",
    "contacts", "contact", "email", "phone", "application_state", "tracker",
    "actual_compensation", "salary_history", "work_authorization", "visa",
    "private_notes", "notes_private", "instructions",
})

KEY_DATA_CLASS = {
    "decision_policy": "target_preferences",
    "scoring_policy": "target_preferences",
    "linkedin_current_md": "linkedin_content",
    "candidate_profile_md": "candidate_profile",
    "evidence_map_md": "candidate_evidence",
    "capability_ledger": "candidate_evidence",
    "baseline_resume": "resume",
    "baseline_extracted_md": "resume",
    "resume_files": "resume",
    "resume_group_files": "resume",
    "existing_group_resume_files": "resume",
    "story_bank_json": "derived_career_artifact",
    "application_packets": "derived_career_artifact",
    "application_route_audit": "public_source",
    "processed_jd_brief": "jd",
    "recommended_resume_path": "derived_career_artifact",
    "recommended_linkedin_path": "derived_career_artifact",
    "existing_packet": "derived_career_artifact",
}

_PRIVATE_LINE = re.compile(
    r"(?i)(?:^|\b)(?:e-?mail|phone|mobile|contact|linkedin\s*(?:url|profile)|"
    r"work\s*authori[sz]ation|visa\s*status|salary\s*history|current\s*comp(?:ensation)?|"
    r"actual\s*comp(?:ensation)?|application\s*status)\s*[:|]"
)
_EMAIL = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
_LINKEDIN_URL = re.compile(r"(?i)https?://(?:[a-z]{2,3}\.)?(?:www\.)?linkedin\.com/\S+")
_PUBLIC_URL = re.compile(r"(?i)https?://[^\s<>()\[\]{}]+")
_PHONE = re.compile(r"(?<!\w)(?:\+?\d[\d ()-]{7,}\d)(?!\w)")
_CANDIDATE_AUTH_LINE = re.compile(
    r"(?i)\b(?:citizen(?:ship)?|permanent\s+resident|\bPR\b|work\s*authori[sz](?:ed|ation)?|"
    r"visa|sponsorship)\b"
)


@dataclass(frozen=True)
class PromptAudit:
    workflow: str
    data_classes: tuple[str, ...]
    removed_fields: tuple[str, ...]
    input_bytes: int
    fingerprint: str


def _redact_text(text: str, *, raw_profile_source: bool = False,
                 candidate_source: bool = False) -> str:
    value = str(text or "")
    if not raw_profile_source:
        lines = [line for line in value.splitlines()
                 if not _PRIVATE_LINE.search(line)
                 and not (candidate_source and _CANDIDATE_AUTH_LINE.search(line))]
        value = "\n".join(lines)
        value = _EMAIL.sub("[contact redacted]", value)
        value = _LINKEDIN_URL.sub("[LinkedIn URL redacted]", value)
        # ATS/job UUIDs and numeric posting IDs resemble phone numbers. Protect
        # already-public URLs while applying the phone detector to surrounding
        # candidate text, then restore them byte-for-byte.
        urls: list[str] = []

        def protect_url(match: re.Match) -> str:
            urls.append(match.group(0))
            return f"__ROLESCOUT_PUBLIC_URL_{len(urls) - 1}__"

        value = _PUBLIC_URL.sub(protect_url, value)
        value = _PHONE.sub("[phone redacted]", value)
        for index, url in enumerate(urls):
            value = value.replace(f"__ROLESCOUT_PUBLIC_URL_{index}__", url)
    encoded = value.encode("utf-8")
    if len(encoded) > MAX_TEXT_BYTES:
        value = encoded[:MAX_TEXT_BYTES].decode("utf-8", errors="ignore") + "\n[truncated]"
    return value


def _clean(value: Any, removed: list[str], path: str, *, allowed_data: frozenset[str],
           raw_profile_source: bool = False, inherited_data_class: str | None = None) -> Any:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            name = str(key)
            child_path = f"{path}.{name}" if path else name
            if name.lower() in LOCAL_ONLY_KEYS:
                removed.append(child_path)
                continue
            data_class = KEY_DATA_CLASS.get(name.lower())
            if data_class and data_class not in allowed_data:
                removed.append(child_path)
                continue
            effective_data_class = data_class or inherited_data_class
            out[name] = _clean(item, removed, child_path, allowed_data=allowed_data,
                               raw_profile_source=raw_profile_source,
                               inherited_data_class=effective_data_class)
        return out
    if isinstance(value, list):
        return [_clean(item, removed, f"{path}[]", allowed_data=allowed_data,
                       raw_profile_source=raw_profile_source,
                       inherited_data_class=inherited_data_class)
                for item in value[:100]]
    if isinstance(value, str):
        return _redact_text(
            value,
            raw_profile_source=raw_profile_source,
            candidate_source=inherited_data_class in {
                "resume", "candidate_profile", "candidate_evidence", "linkedin_content",
            },
        )
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)


def prepare_prompt_context(workflow: str, context: dict) -> tuple[dict, PromptAudit]:
    """Return the only context permitted to cross the provider boundary.

    Profile intake is the sole stage allowed to pass raw source text. It still
    receives only runner-extracted content, never filesystem paths.
    """
    allowed = COMMON_KEYS | WORKFLOW_KEYS.get(workflow, frozenset())
    allowed_data = allowed_classes(workflow)
    removed: list[str] = []
    selected: dict[str, Any] = {}
    for key, value in copy.deepcopy(context).items():
        if key not in allowed:
            removed.append(str(key))
            continue
        selected[key] = _clean(
            value,
            removed,
            str(key),
            allowed_data=allowed_data,
            # Even profile source text is stripped of contacts, LinkedIn URLs,
            # work-authorization lines, and compensation-history lines.
            raw_profile_source=False,
        )
    raw = json.dumps(selected, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    if len(raw.encode("utf-8")) > MAX_PACKET_BYTES:
        raise ValueError(
            f"model input packet exceeds {MAX_PACKET_BYTES} bytes after minimization"
        )
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    audit = PromptAudit(
        workflow=workflow,
        data_classes=tuple(sorted(allowed_data)),
        removed_fields=tuple(sorted(set(removed))),
        input_bytes=len(raw.encode("utf-8")),
        fingerprint=digest,
    )
    return selected, audit
