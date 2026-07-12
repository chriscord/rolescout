from __future__ import annotations

import json

from rolescout import application_audit
from rolescout.privacy.prompt_gateway import prepare_prompt_context
from rolescout.runner import workflows


def test_apply_selection_resolves_requested_roles_without_shared_title_false_positives(monkeypatch):
    rows = [
        {"job_id": "oa-vc", "company": "OpenAI", "title": "VC Partnerships Lead, APAC"},
        {"job_id": "oa-deploy", "company": "OpenAI", "title": "AI Deployment Engineering Manager, Startups"},
        {"job_id": "oa-technical", "company": "OpenAI", "title": "Technical Deployment Lead - Singapore"},
        {"job_id": "grab-analytics", "company": "Grab", "title": "Senior Manager, Business Analytics (Sales Operations)"},
        {"job_id": "grab-product", "company": "Grab", "title": "Principal Product Manager, AI Enablement"},
    ]
    monkeypatch.setattr(workflows, "_focused_job_rows", lambda _project: rows)
    intent = {"requested_companies": ["OpenAI", "Grab"]}
    selected = workflows._select_application_jobs(
        None,
        "Apply OpenAI VC, AI Deployment Engineering Manager (Startup), "
        "Grab Senior Manager Business Analytics.",
        intent,
    )
    assert [row["job_id"] for row in selected] == ["oa-vc", "oa-deploy", "grab-analytics"]


def test_ashby_audit_extracts_every_public_form_field(monkeypatch):
    application_form = {
        "fieldEntries": [
            {
                "field": {"title": "Legal Name", "type": "String"},
                "isRequired": True,
                "descriptionHtml": "<p>Use the name on your ID.</p>",
            },
            {
                "field": {"title": "Additional Information", "type": "LongText"},
                "isRequired": False,
            },
        ]
    }
    source = (
        '<a href="/openai/role/application">Application</a>'
        '"applicationLimitCalloutHtml":"<p>Maximum five applications.</p>",'
        '"applicationForm":' + json.dumps(application_form) + ","
    )
    monkeypatch.setattr(application_audit, "_fetch", lambda url, timeout=30: (source, url))
    audit = application_audit.audit_application_route(
        "https://jobs.ashbyhq.com/openai/role", allow_browser=False
    )
    assert audit["vendor"] == "Ashby"
    assert audit["capture_completeness"] == "complete"
    assert [(field["label"], field["required"]) for field in audit["fields"]] == [
        ("Legal Name", True), ("Additional Information", False)
    ]
    assert "five applications" in audit["application_limit_notice"].lower()


def test_smartrecruiters_config_marks_required_upload_boundary():
    fields = application_audit._smartrecruiters_fields({
        "fieldSets": {
            "firstAndLastName": {"visible": True, "required": True},
            "resume": {"visible": True, "required": True},
            "messageToHiringManager": {"visible": True, "required": False},
        }
    })
    assert [field["label"] for field in fields] == [
        "First and last name", "Resume attachment", "Message to the hiring manager"
    ]
    assert fields[1]["required"] is True


def test_apply_prompt_gateway_allows_minimized_profile_evidence_and_route_audit():
    clean, audit = prepare_prompt_context("apply", {
        "runner_context_packet": {
            "candidate_profile_md": "Evidence-backed product leadership",
            "evidence_map_md": "EV-001 deployment evidence",
            "baseline_resume": {"baseline_extracted_md": "Resume content"},
            "application_route_audit": {"vendor": "Ashby", "fields": []},
        }
    })
    text = json.dumps(clean)
    assert "product leadership" in text
    assert "deployment evidence" in text
    assert "Ashby" in text
    assert {"candidate_profile", "candidate_evidence", "resume", "public_source"}.issubset(
        set(audit.data_classes)
    )


def test_apply_gateway_preserves_numeric_ats_urls_but_removes_candidate_auth_facts():
    url = "https://jobs.ashbyhq.com/openai/483598bf-c608-4250-9680-cf7ec4737f6a/application"
    clean, _ = prepare_prompt_context("apply", {
        "runner_context_packet": {
            "application_route_audit": {"application_url": url},
            "candidate_profile_md": "Builder evidence\nSingapore PR / Korean citizen\nProduct leadership",
        }
    })
    encoded = json.dumps(clean)
    assert url in encoded
    assert "[phone redacted]" not in encoded
    assert "Singapore PR" not in encoded
    assert "Korean citizen" not in encoded
    assert "Product leadership" in encoded


def test_application_artifact_paths_are_short_and_collision_resistant():
    first = workflows._application_artifact_path({
        "job_id": "openai--ai-deployment-engineering-manager-startups--11111111"
    })
    second = workflows._application_artifact_path({
        "job_id": "openai--ai-deployment-engineering-manager-startups--22222222"
    })
    assert first != second
    assert len(first) < 100
    assert first.endswith("/application-instructions.md")
