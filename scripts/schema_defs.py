"""Shared schema constants for the recruiting pipeline.

Mirrors references/recruiting-sheet-schema.md. If you change one, change both.
"""

JOB_LIST_COLUMNS = [
    "job_id", "captured_at", "company", "title", "job_group", "location",
    "remote_policy", "source_url", "job_page_url", "posting_status",
    "seniority", "must_have_requirements", "nice_to_have_requirements",
    "jd_summary", "fit_score", "priority", "notes", "last_seen_at",
]

TRACKER_COLUMNS = [
    "application_id", "job_id", "company", "title", "job_group", "status",
    "applied_at", "resume_version", "linkedin_version", "contact",
    "next_action", "next_action_due", "last_updated", "outcome", "notes",
]

STATUSES = [
    "to_apply", "applied", "to_interview_1", "to_interview_2",
    "to_interview_3", "offer", "accepted", "rejected", "withdrawn", "paused",
]

ALLOWED_TRANSITIONS = {
    "to_apply": {"applied", "rejected", "withdrawn", "paused"},
    "applied": {"to_interview_1", "rejected", "withdrawn", "paused"},
    "to_interview_1": {"to_interview_2", "rejected", "withdrawn", "paused"},
    "to_interview_2": {"to_interview_3", "offer", "rejected", "withdrawn", "paused"},
    "to_interview_3": {"offer", "rejected", "withdrawn", "paused"},
    "offer": {"accepted", "rejected", "withdrawn"},
    "paused": {"to_apply", "applied", "to_interview_1", "to_interview_2",
               "to_interview_3", "withdrawn"},
    "accepted": set(),
    "rejected": set(),
    "withdrawn": set(),
}

ENTRY_STATUSES = {"to_apply", "applied"}
REMOTE_POLICIES = {"onsite", "hybrid", "remote", "unknown"}
POSTING_STATUSES = {"open", "closed", "removed", "unknown"}
PRIORITIES = {"high", "medium", "low", ""}
OUTCOMES = {"", "offer_accepted", "offer_declined", "rejected", "withdrawn"}
ISO_DATE_RE = r"^\d{4}-\d{2}-\d{2}$"

# research-log decision vocabulary (references/search-workflow.md) — shared by
# validate_research_artifacts.py and grade_run.py so the contract can't drift.
RESEARCH_DECISIONS = {"kept", "skipped", "failed_capture", "no_postings_found",
                      "pending_fallback"}
RESEARCH_REASON_CODES = {"constraint_violation", "seniority_mismatch", "low_fit", "duplicate",
                         "closed", "off_focus", "capture_error", "no_postings",
                         "run_interrupted", ""}
