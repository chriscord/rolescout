from __future__ import annotations

import json
from pathlib import Path

from rolescout import project_meta
from rolescout.search import deterministic


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "projects" / "sample--test"
    project.mkdir(parents=True)
    (project / "project.json").write_text('{"person":"sample","focus":"test"}\n')
    return project


def test_preference_revision_invalidates_filter_plan(tmp_path: Path, monkeypatch):
    project = _project(tmp_path)
    monkeypatch.setenv("RECRUITING_PROJECT_DIR", str(project))
    first = project_meta.update(project, target_locations="Singapore", target_level="Lead")

    from scripts import build_search_view
    old = build_search_view.default_filter_plan(project)
    plan_path = project / "targets" / "search-view-filter-plan.json"
    plan_path.parent.mkdir(parents=True)
    plan_path.write_text(json.dumps(old), encoding="utf-8")

    second = project_meta.update(project, target_locations="Singapore, Tokyo",
                                 target_level="Director")
    assert second["preference_revision"] == first["preference_revision"] + 1
    assert second["preference_fingerprint"] != first["preference_fingerprint"]

    refreshed = build_search_view._load_or_create_plan(project)
    assert refreshed["target_level"] == "Director"
    assert refreshed["target_locations"] == ["Singapore", "Tokyo"]
    assert refreshed["preference_fingerprint"] == second["preference_fingerprint"]


def test_visibility_refresh_never_unfocuses_hidden_jobs(tmp_path: Path, monkeypatch):
    project = _project(tmp_path)
    monkeypatch.setenv("RECRUITING_PROJECT_DIR", str(project))
    project_meta.update(project, target_locations="Singapore", target_level="Lead")

    from scripts import build_search_view, store_io
    con = store_io.connect()
    try:
        store_io.upsert("job_list", [{
            "job_id": "job--1", "captured_at": "2026-07-11", "company": "Example",
            "title": "Product Lead", "location": "San Francisco, USA",
            "source_url": "https://example.com/jobs/1", "posting_status": "open",
        }], con)
        con.commit()
    finally:
        con.close()
    focus_path = project / "data" / "focused-jobs.json"
    focus_path.write_text(json.dumps({"job_ids": ["job--1"]}), encoding="utf-8")

    summary = build_search_view.build_view(project)
    assert summary["visible_rows"] == 0
    assert json.loads(focus_path.read_text())["job_ids"] == ["job--1"]


def test_universe_and_source_plan_follow_current_inputs(tmp_path: Path, monkeypatch):
    project = _project(tmp_path)
    project_meta.update(project, target_locations="Singapore", focus_role="Product",
                        target_companies="Alpha")
    first_intent = deterministic._intent(project)
    first_universe = deterministic._load_universe(project, first_intent)

    project_meta.update(project, focus_role="Business Development")
    second_intent = deterministic._intent(project)
    second_universe = deterministic._load_universe(project, second_intent)
    assert second_universe["preference_fingerprint"] != first_universe["preference_fingerprint"]

    calls: list[str] = []

    def source_plan(name: str) -> dict:
        calls.append(name)
        return {"name": name, "sources": []}

    monkeypatch.setattr(deterministic.resolve_company_sources,
                        "source_plan_for_company", source_plan)
    deterministic._load_or_build_source_plan(project, [{"name": "Alpha"}])
    deterministic._load_or_build_source_plan(project, [{"name": "Beta"}])
    assert calls == ["Alpha", "Beta"]


def _singapore_intent() -> deterministic.SearchIntent:
    return deterministic.SearchIntent(
        target_locations=["Singapore"],
        focus_role="Strategy",
        target_level="Manager",
        target_companies=[],
        negatives=[],
        role_terms={"strategy"},
        location_terms={"singapore", "sg"},
    )


def test_structured_location_wins_over_incidental_jd_mentions():
    intent = _singapore_intent()
    candidate = deterministic.ProviderCandidate(
        company="Example",
        title="Strategy Manager",
        url="https://example.com/jobs/1",
        location="San Francisco, CA, USA",
    )
    jd = "You will collaborate daily with teams in Singapore and London."
    assert not deterministic._matches_location(candidate, jd, intent)


def test_missing_structured_location_uses_only_explicit_jd_location():
    intent = _singapore_intent()
    candidate = deterministic.ProviderCandidate(
        company="Example",
        title="Strategy Manager",
        url="https://example.com/jobs/1",
    )
    assert deterministic._matches_location(
        candidate, "Location: Singapore\nResponsibilities: build strategy.", intent)
    assert not deterministic._matches_location(
        candidate, "Collaborate with our Singapore customers on strategy.", intent)


def test_location_filter_happens_before_global_board_candidate_cap():
    intent = _singapore_intent()
    jobs = [
        {"id": str(index), "title": "Strategy Manager", "location": "New York, USA"}
        for index in range(200)
    ]
    jobs.append({"id": "sg-role", "title": "Strategy Manager", "location": "Singapore"})
    scoped = deterministic._prepare_structured_jobs(
        jobs,
        intent,
        location_getter=lambda job: job["location"],
        title_getter=lambda job: job["title"],
    )
    assert [job["id"] for job in scoped[:160]] == ["sg-role"]


def test_location_template_is_instantiated_from_current_preferences():
    urls = deterministic._source_urls({
        "url": "https://example.com/jobs",
        "location_search_url_template": "https://example.com/jobs?location={location}",
    }, _singapore_intent())
    assert urls == ["https://example.com/jobs?location=Singapore"]


def test_lifecycle_manifest_uses_complete_provider_listing():
    candidate = deterministic.ProviderCandidate(
        company="Example",
        title="Strategy Manager",
        url="https://example.com/jobs/1",
    )
    result = deterministic.DiscoveryResult(
        "source-1", "provider", "Example", "https://example.com/jobs", "scanned",
        [candidate], authoritative=True,
        listing_urls=["https://example.com/jobs/1", "https://example.com/jobs/2"],
    )
    manifest = deterministic._lifecycle_manifest([result], "run-1")
    assert len(manifest["sources"][0]["seen_job_ids"]) == 2


def test_registry_resolves_grab_meta_aliases_and_category_seeds():
    resolver = deterministic.resolve_company_sources
    grab = resolver.source_plan_for_company("Grab")
    grab_urls = {source.get("url") for source in grab["sources"]}
    assert "https://www.grab.careers/en/jobs/" in grab_urls
    assert "https://careers.smartrecruiters.com/Grab" in grab_urls

    meta = resolver.source_plan_for_company("Meta")
    assert any(source.get("url") == "https://www.metacareers.com/"
               for source in meta["sources"])

    govtech = resolver.source_plan_for_company("GovTech")
    assert any(source.get("url") == "https://www.tech.gov.sg/careers/"
               for source in govtech["sources"])

    category = resolver.source_plan_for_company("AI or data startups")
    assert category["category_seed"] is True
    assert not any(source["type"] == "guessed_ats_probe" for source in category["sources"])


def test_google_adapter_paginates_before_role_prioritization():
    def page_html(start: int, strategy_at: int | None = None) -> str:
        links = []
        for index in range(start, start + 20):
            title = "Strategy and Operations Principal" if index == strategy_at else f"Engineer {index}"
            links.append(
                f'<a href="/about/careers/applications/jobs/results/{index}-{title.lower().replace(" ", "-")}/">'
                f"{title}</a>"
            )
        return "<html><body><div>40 jobs matched</div>" + "".join(links) + "</body></html>"

    class Client:
        def get_text(self, url: str) -> str:
            return page_html(1) if "page=1" in url else page_html(21, strategy_at=37)

    adapter = deterministic.GoogleCareersAdapter()
    task = {
        "id": "google", "company": "Google",
        "url": "https://www.google.com/about/careers/applications/jobs/results?location=Singapore",
        "provider": adapter.provider,
        "runtime_policy": deterministic.SEARCH_RUNTIME_PROFILES["fast"],
    }
    result = adapter.discover(task, Client(), _singapore_intent())
    assert result.pagination_complete is True
    assert result.pages_fetched == 2
    assert result.advertised_total == 40
    assert len(result.listing_urls) == 40
    assert result.candidates[0].title == "Strategy and Operations Principal"


def test_meta_adapter_uses_structured_paginated_api():
    pages = {
        1: {
            "jobs": [{
                "guid": "ABCDEF1234567890",
                "title_exact": "Strategy Manager",
                "location_exact": "Singapore, SGP",
                "description": "Required Skills:\nMinimum Qualifications:\n8 years strategy experience.",
            }],
            "pagination": {"page": 1, "total": 2, "total_pages": 2,
                           "has_more_pages": True},
        },
        2: {
            "jobs": [{
                "guid": "ABCDEF1234567891",
                "title_exact": "Product Manager",
                "location_exact": "Singapore, SGP",
                "description": "Minimum Qualifications:\n5 years product experience.",
            }],
            "pagination": {"page": 2, "total": 2, "total_pages": 2,
                           "has_more_pages": False},
        },
    }

    class Client:
        def request(self, url: str, headers=None):
            page = 2 if "page=2" in url else 1
            assert headers["X-Origin"] == "metacareers.dejobs.org"
            return json.dumps(pages[page]), "application/json"

    adapter = deterministic.MetaCareersAdapter()
    task = {
        "id": "meta", "company": "Meta", "url": "https://www.metacareers.com/",
        "runtime_policy": deterministic.SEARCH_RUNTIME_PROFILES["fast"],
    }
    result = adapter.discover(task, Client(), _singapore_intent())
    assert result.pagination_complete is True
    assert result.pages_fetched == 2
    assert result.advertised_total == 2
    assert len(result.candidates) == 2
    assert result.candidates[0].provider == "meta_careers"


def test_incomplete_source_is_never_logged_as_no_postings():
    result = deterministic.DiscoveryResult(
        "meta", "meta_careers", "Meta", "https://www.metacareers.com/",
        "scanned", [], authoritative=False, pagination_complete=False,
    )
    entry = deterministic._incomplete_source_candidate(result)
    assert entry["decision"] == "pending_fallback"
    assert entry["reason_code"] == "coverage_incomplete"


def test_generic_html_adapter_follows_explicit_pagination():
    pages = {
        "https://example.com/careers": (
            '<a href="https://jobs.smartrecruiters.com/Example/744000135416399-strategy-manager">Strategy Manager</a>'
            '<a rel="next" href="/careers?page=2">Next</a>'
        ),
        "https://example.com/careers?page=2": (
            '<a href="https://jobs.smartrecruiters.com/Example/744000135416400-product-lead">Product Lead</a>'
        ),
    }

    class Client:
        def get_text(self, url: str) -> str:
            return pages[url]

    adapter = deterministic.GenericHtmlAdapter()
    task = {
        "id": "example", "company": "Example", "url": "https://example.com/careers",
        "source": {"type": "official_careers", "render": "server_html"},
        "provider": adapter.provider,
        "runtime_policy": deterministic.SEARCH_RUNTIME_PROFILES["fast"],
    }
    result = adapter.discover(task, Client(), _singapore_intent())
    assert result.pagination_complete is True
    assert result.pages_fetched == 2
    assert len(result.listing_urls) == 2


def test_universe_status_requires_current_preference_fingerprint(tmp_path: Path):
    project = _project(tmp_path)
    project_meta.update(project, target_locations="Singapore", focus_role="Strategy")
    path = project / "targets" / "company-universe.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({
        "preference_fingerprint": project_meta.preference_fingerprint(project),
        "buckets": [{"companies": [{"name": "Example", "rationale": "Target market"}]}],
    }), encoding="utf-8")
    assert project_meta.universe_status(project)["ready"] is True
    project_meta.update(project, focus_role="Product")
    assert project_meta.universe_status(project)["ready"] is False


def test_seed_only_fallback_never_treats_category_as_employer(tmp_path: Path):
    project = _project(tmp_path)
    project_meta.update(
        project, target_locations="Singapore", focus_role="Strategy",
        target_companies="Google, AI or data startups",
    )
    intent = deterministic._intent(project)
    universe = deterministic._load_universe(project, intent)
    names = [
        company["name"]
        for bucket in universe["buckets"]
        for company in bucket["companies"]
    ]
    assert names == ["Google"]
    assert universe["unresolved_descriptors"] == ["AI or data startups"]
