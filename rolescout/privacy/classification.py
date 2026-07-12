"""Deny-by-default data classification for model and export boundaries.

The registry is deliberately executable policy rather than documentation.  A
workflow may receive a class only when it is listed here; individual prompt
fields are further restricted by :mod:`rolescout.privacy.prompt_gateway`.
"""

from __future__ import annotations

from enum import Enum


class DataClass(str, Enum):
    PUBLIC = "public"
    MODEL_ALLOWED = "model_allowed"
    LOCAL_ONLY = "local_only"


FIELD_CLASSIFICATION: dict[str, DataClass] = {
    # Public opportunity evidence.
    "job": DataClass.PUBLIC,
    "job_posting": DataClass.PUBLIC,
    "jd": DataClass.PUBLIC,
    "company": DataClass.PUBLIC,
    "public_source": DataClass.PUBLIC,
    # Search preferences. Target compensation is explicitly not compensation history.
    "target_preferences": DataClass.MODEL_ALLOWED,
    "target_compensation": DataClass.MODEL_ALLOWED,
    "run_instruction": DataClass.MODEL_ALLOWED,
    # Person-derived material: allowed only for workflows that explicitly need it.
    "resume": DataClass.MODEL_ALLOWED,
    "candidate_profile": DataClass.MODEL_ALLOWED,
    "candidate_evidence": DataClass.MODEL_ALLOWED,
    "linkedin_content": DataClass.MODEL_ALLOWED,
    "derived_career_artifact": DataClass.MODEL_ALLOWED,
    # Default-denied fields.
    "linkedin_url": DataClass.LOCAL_ONLY,
    "contact": DataClass.LOCAL_ONLY,
    "application_state": DataClass.LOCAL_ONLY,
    "actual_compensation": DataClass.LOCAL_ONLY,
    "work_authorization": DataClass.LOCAL_ONLY,
    "private_note": DataClass.LOCAL_ONLY,
}


WORKFLOW_DATA_CLASSES: dict[str, frozenset[str]] = {
    "profile-intake": frozenset({"resume", "linkedin_content", "run_instruction"}),
    "capability-ledger": frozenset({"candidate_profile", "candidate_evidence"}),
    "opportunity-plan": frozenset({"target_preferences", "target_compensation", "run_instruction"}),
    "universe-expand": frozenset({"target_preferences", "run_instruction"}),
    "search": frozenset({"job", "job_posting", "jd", "company", "public_source",
                         "target_preferences", "target_compensation", "run_instruction"}),
    "score": frozenset({"job", "job_posting", "jd", "target_preferences",
                        "target_compensation", "candidate_profile", "candidate_evidence"}),
    "prep-strategy": frozenset({"job", "jd", "target_preferences", "target_compensation",
                                "candidate_profile", "candidate_evidence",
                                "derived_career_artifact"}),
    "prep": frozenset({"job", "jd", "public_source", "target_preferences",
                       "target_compensation", "candidate_profile", "candidate_evidence",
                       "resume", "linkedin_content", "derived_career_artifact"}),
    "prep-resume": frozenset({"job", "jd", "target_preferences", "candidate_profile",
                              "candidate_evidence", "resume", "derived_career_artifact"}),
    "prep-linkedin": frozenset({"job", "jd", "target_preferences", "candidate_profile",
                                "candidate_evidence", "linkedin_content", "resume",
                                "derived_career_artifact"}),
    "story-bank": frozenset({"candidate_profile", "candidate_evidence", "resume",
                             "derived_career_artifact"}),
    "prep-interview": frozenset({"job", "jd", "public_source", "candidate_profile",
                                 "candidate_evidence", "resume", "derived_career_artifact"}),
    "apply": frozenset({"job", "job_posting", "jd", "public_source",
                         "target_preferences", "candidate_profile", "candidate_evidence",
                         "resume", "derived_career_artifact"}),
}


def allowed_classes(workflow: str) -> frozenset[str]:
    """Return the exact data-class allowlist for a model stage."""
    return WORKFLOW_DATA_CLASSES.get(workflow, frozenset({"public_source"}))


def workflow_disclosure(workflow: str, provider: str = "selected model provider") -> str:
    """Concise, truthful disclosure suitable for CLI and UI presentation."""
    classes = allowed_classes(workflow)
    labels = {
        "resume": "resume/source text",
        "linkedin_content": "captured LinkedIn content (never the profile URL)",
        "candidate_profile": "minimized candidate-profile excerpts",
        "candidate_evidence": "minimized evidence excerpts",
        "derived_career_artifact": "relevant generated career artifacts",
        "job": "public job facts",
        "job_posting": "public posting facts",
        "jd": "job-description evidence",
        "company": "public company facts",
        "public_source": "public-source evidence",
        "target_preferences": "search preferences",
        "target_compensation": "target compensation preference",
        "run_instruction": "this run's instruction",
    }
    used = ", ".join(labels[c] for c in sorted(classes) if c in labels) or "public facts only"
    return (
        f"{workflow}: {used} may be processed by {provider}. Files and outputs remain "
        "local, but live model processing is not local-only. Contacts, application state, "
        "compensation history, work authorization, LinkedIn URLs, and unrelated private "
        "notes are excluded by default."
    )
