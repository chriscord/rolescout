from __future__ import annotations

from rolescout.runner import workflows
from scripts import jd_text_cleaner


def test_must_preferred_and_essential_are_separated():
    jd = """
Minimum Qualifications:
- PhD in machine learning is required.
- Five years of production Python experience.

Preferred Qualifications:
- Publications at NeurIPS are preferred.
- CPA certification is a plus.
"""
    result = jd_text_cleaner.classify_requirements(jd)
    assert any("PhD" in item for item in result["must_have"])
    assert any("NeurIPS" in item for item in result["preferred"])
    assert any("PhD" in item for item in result["essential_qualifications"])
    assert not any("CPA" in item for item in result["essential_qualifications"])


def test_preferred_phd_never_becomes_essential():
    result = jd_text_cleaner.classify_requirements(
        "Preferred Qualifications:\nPhD in economics is preferred."
    )
    assert result["essential_qualifications"] == []
    assert any("PhD" in item for item in result["preferred"])


def test_missing_required_phd_forces_essential_dealbreaker():
    accepted = {
        "job-1": {
            "ratings": {"essential_qualification": 3},
            "rationale": {"essential_qualification": "Uncertain."},
        }
    }
    workflows._apply_deterministic_qualification_gates(
        accepted,
        {"job-1": {"essential_qualifications": "PhD in machine learning is required."}},
        {"candidate_profile_md": "BBA in Business Administration", "evidence_map_md": ""},
    )
    assert accepted["job-1"]["ratings"]["essential_qualification"] == 1


def test_model_policy_violation_is_enforced_as_career_dealbreaker():
    accepted = {
        "job-1": {
            "ratings": {"career_trajectory": 3, "essential_qualification": 5},
            "rationale": {"career_trajectory": "Ambiguous."},
            "policy_evaluations": [{
                "policy_id": "no_backward_career_progression",
                "outcome": "violated",
                "confidence": "high",
                "evidence": "Scope and level are below the candidate's evidenced baseline.",
            }],
        }
    }
    workflows._apply_policy_evaluation_enforcement(
        accepted,
        {
            "decision_policy": {
                "policies": [{
                    "id": "no_backward_career_progression",
                    "criterion": "career_trajectory",
                    "enforcement": "dealbreaker",
                }],
            },
        },
    )
    assert accepted["job-1"]["ratings"]["career_trajectory"] == 1
    assert "below the candidate" in accepted["job-1"]["rationale"]["career_trajectory"]


def test_uncertain_model_policy_outcome_is_not_a_dealbreaker():
    accepted = {
        "job-1": {
            "ratings": {"career_trajectory": 1, "essential_qualification": 5},
            "rationale": {"career_trajectory": "Model assessment."},
            "policy_evaluations": [{
                "policy_id": "no_backward_career_progression",
                "outcome": "uncertain",
                "confidence": "low",
                "evidence": "Compensation and actual scope are not available.",
            }],
        }
    }
    workflows._apply_policy_evaluation_enforcement(
        accepted,
        {
            "decision_policy": {
                "policies": [{
                    "id": "no_backward_career_progression",
                    "criterion": "career_trajectory",
                    "enforcement": "dealbreaker",
                }],
            },
        },
    )
    assert accepted["job-1"]["ratings"]["career_trajectory"] == 3


def test_score_validator_requires_all_active_policy_evaluations():
    ratings = [{
        "job_id": "job-1",
        "ratings": {"career_trajectory": 3},
        "rationale": {"career_trajectory": "Ambiguous."},
    }]
    accepted, missing, issues = workflows._validate_score_batch_ratings(
        ratings,
        {"job-1"},
        {"career_trajectory"},
        {"no_backward_career_progression": {"criterion": "career_trajectory"}},
    )
    assert accepted == {}
    assert missing == {"job-1"}
    assert "incomplete_rating=1" in issues


def test_minimum_qualifications_survive_long_responsibilities_section():
    responsibilities = "\n".join(
        f"{index}. Lead product responsibility number {index} across teams."
        for index in range(1, 25)
    )
    jd = f"""
Required Skills:
Product Manager Responsibilities:
{responsibilities}
Minimum Qualifications:
25. 12+ years of experience in Product Management and/or Product Design
26. BA/BS in Computer Science or related field
Preferred Qualifications:
Experience in consumer technology is preferred
"""
    normalized = jd_text_cleaner.requirement_atoms(jd)
    scoring_quotes = " ".join(
        item["source_quote"] for item in normalized["scoring_requirements"]
    )
    assert "12+ years" in scoring_quotes
    assert "BA/BS" in scoring_quotes
    assert all(
        item["obligation"] == "preferred"
        for item in normalized["atoms"]
        if "consumer technology" in item["source_quote"]
    )
    assert jd_text_cleaner.requirement_coverage_issues(jd, normalized) == []


def test_all_required_qualifications_are_retained_beyond_eight():
    jd = "Minimum Qualifications:\n" + "\n".join(
        f"{index}+ years of required domain {index} experience"
        for index in range(1, 11)
    )
    normalized = jd_text_cleaner.requirement_atoms(jd)
    assert len(normalized["scoring_requirements"]) == 10


def test_long_about_paragraph_is_not_promoted_to_a_requirement():
    about = (
        "At Example, we build products and work across many teams. "
        "The organization is required to comply with internal standards. " * 5
    )
    jd = about + "\nMinimum Qualifications:\n12+ years of Product Management experience"

    normalized = jd_text_cleaner.requirement_atoms(jd)
    quotes = [item["source_quote"] for item in normalized["scoring_requirements"]]

    assert quotes == ["12+ years of Product Management experience"]


def test_responsibility_eligibility_constraint_is_scored():
    normalized = jd_text_cleaner.requirement_atoms(
        "Responsibilities:\nAbility to travel up to 30% is required."
    )
    requirement = normalized["scoring_requirements"][0]
    assert requirement["category"] == "travel_schedule"
    assert requirement["obligation"] == "required"
    assert requirement["importance"] == "eligibility"


def test_eeo_citizenship_reference_is_not_work_authorization():
    normalized = jd_text_cleaner.requirement_atoms(
        "We do not discriminate based on citizenship, veteran status, or disability."
    )
    assert normalized["atoms"] == []


def test_event_sponsorship_is_not_misclassified_as_visa_sponsorship():
    jd = """Responsibilities
• Design and manage expo placements for attendee visibility. • Collaborate with the sponsorship team on partner activations.
Minimum requirements
• 6+ years managing and executing tradeshows
"""
    normalized = jd_text_cleaner.requirement_atoms(jd)
    assert not any(
        item["category"] == "work_authorization" for item in normalized["atoms"]
    )
    assert any(
        "sponsorship team" in item["source_quote"]
        and item["obligation"] == "responsibility"
        for item in normalized["atoms"]
    )
    assert jd_text_cleaner.requirement_coverage_issues(jd, normalized) == []


def test_actual_employment_sponsorship_remains_an_eligibility_requirement():
    jd = "Responsibilities\nWill you now or in the future require sponsorship for employment visa status?"
    normalized = jd_text_cleaner.requirement_atoms(jd)
    requirement = normalized["scoring_requirements"][0]
    assert requirement["category"] == "work_authorization"
    assert requirement["importance"] == "eligibility"


def test_normalization_failures_are_isolated_by_job():
    rows = [
        {"job_id": "good", "requirement_coverage_issues": []},
        {"job_id": "bad", "requirement_coverage_issues": ["explicit years signal missing"]},
    ]
    assert workflows._normalization_errors_by_job(rows) == {
        "bad": ["requirement normalization: explicit years signal missing"]
    }


def test_unmet_central_minimum_caps_fit_likelihood_and_gate():
    accepted = {
        "job-1": {
            "ratings": {
                "role_fit": 4,
                "likelihood": 4,
                "minimum_requirement": 5,
            },
            "rationale": {},
            "requirement_evaluations": [{
                "requirement_id": "REQ-years",
                "coverage": "unmet",
                "confidence": "high",
                "direct_months": 12,
                "adjacent_months": 60,
                "evidence_ids": ["EV-001"],
                "reason": "Twelve years of direct product management is not evidenced.",
            }],
        }
    }
    workflows._apply_requirement_evaluation_enforcement(
        accepted,
        {"job-1": {"requirements": [{
            "requirement_id": "REQ-years",
            "obligation": "minimum_required",
            "importance": "central",
        }]}},
    )
    assert accepted["job-1"]["ratings"]["role_fit"] == 2
    assert accepted["job-1"]["ratings"]["likelihood"] == 2
    assert accepted["job-1"]["ratings"]["minimum_requirement"] == 1


def test_required_central_and_unknown_eligibility_are_capped():
    accepted = {
        "required": {
            "ratings": {"role_fit": 5, "likelihood": 5, "minimum_requirement": 5},
            "rationale": {},
            "requirement_evaluations": [{
                "requirement_id": "REQ-required", "coverage": "unmet",
                "reason": "Required tenure is not evidenced.",
            }],
        },
        "eligibility": {
            "ratings": {"role_fit": 5, "likelihood": 5, "minimum_requirement": 5},
            "rationale": {},
            "requirement_evaluations": [{
                "requirement_id": "REQ-eligibility", "coverage": "unknown",
                "reason": "Work authorization evidence is unavailable.",
            }],
        },
    }
    workflows._apply_requirement_evaluation_enforcement(
        accepted,
        {
            "required": {"requirements": [{
                "requirement_id": "REQ-required", "obligation": "required",
                "importance": "central",
            }]},
            "eligibility": {"requirements": [{
                "requirement_id": "REQ-eligibility", "obligation": "required",
                "importance": "eligibility",
            }]},
        },
    )
    assert accepted["required"]["ratings"]["role_fit"] == 2
    assert accepted["required"]["ratings"]["likelihood"] == 2
    assert accepted["required"]["ratings"]["minimum_requirement"] == 1
    assert accepted["eligibility"]["ratings"]["role_fit"] == 3
    assert accepted["eligibility"]["ratings"]["likelihood"] == 3
    assert accepted["eligibility"]["ratings"]["minimum_requirement"] == 3


def test_requirement_batching_uses_output_complexity_not_only_job_count():
    jobs = [
        {"job_id": f"job-{index}", "requirements": [{}] * 9}
        for index in range(3)
    ]
    batches = workflows._make_score_batches(
        jobs, max_jobs=4, max_requirements=16, max_chars=100_000
    )
    assert [len(batch) for batch in batches] == [1, 1, 1]


def test_capability_ledger_requires_matching_source_fingerprint():
    payload = {
        "schema": "rolescout-capability-ledger-v1",
        "source_fingerprint": "correct",
        "entries": [{
            "experience_id": "EXP-001",
            "function": "Product management",
            "coverage_type": "direct",
            "start": "2021-01",
            "end": "2022-01",
            "evidence_ids": ["EV-001"],
            "scope": "Owned a product roadmap.",
        }],
    }
    assert workflows._validate_capability_ledger(payload, "wrong") is None
    assert workflows._validate_capability_ledger(payload, "correct") is not None
