"""Read-only application-route inspection for the ``apply`` workflow.

The auditor may load public posting/application pages and inspect their form
configuration.  It never enters candidate data, uploads a file, clicks a
progress/submit control, authenticates, or creates an account.
"""

from __future__ import annotations

import html
import json
import re
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


class _LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[dict[str, str]] = []
        self._href = ""
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        self._href = dict(attrs).get("href") or ""
        self._text = []

    def handle_data(self, data: str) -> None:
        if self._href:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a" and self._href:
            self.links.append({
                "href": html.unescape(self._href).strip(),
                "text": re.sub(r"\s+", " ", html.unescape(" ".join(self._text))).strip(),
            })
            self._href = ""
            self._text = []


def _plain(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", value or ""))).strip()


def _fetch(url: str, timeout: int = 30) -> tuple[str, str]:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset, errors="replace"), response.geturl()


def _embedded_json(source: str, marker: str) -> Any:
    index = source.find(marker)
    if index < 0:
        return None
    index += len(marker)
    try:
        value, _ = json.JSONDecoder().raw_decode(source[index:])
        return value
    except json.JSONDecodeError:
        return None


def _application_link(posting_url: str, source: str) -> str:
    parser = _LinkParser()
    parser.feed(source)
    ranked: list[tuple[int, str]] = []
    for link in parser.links:
        href = urllib.parse.urljoin(posting_url, link["href"])
        text = link["text"].lower()
        host = urllib.parse.urlparse(href).netloc.lower()
        score = 0
        if "apply" in text:
            score += 5
        if any(vendor in host for vendor in (
            "ashbyhq.com", "smartrecruiters.com", "greenhouse.io", "lever.co", "myworkdayjobs.com"
        )):
            score += 4
        if href.rstrip("/").endswith("/application"):
            score += 3
        if score:
            ranked.append((score, href))
    return max(ranked, default=(0, ""))[1]


def _ashby_audit(posting_url: str, application_url: str = "") -> dict[str, Any]:
    if not application_url:
        application_url = posting_url.rstrip("/") + "/application"
    source, final_url = _fetch(application_url)
    form = _embedded_json(source, '"applicationForm":')
    if not isinstance(form, dict):
        raise ValueError("Ashby applicationForm was not present on the verified application page")
    fields: list[dict[str, Any]] = []
    for entry in form.get("fieldEntries", []):
        if not isinstance(entry, dict) or not isinstance(entry.get("field"), dict):
            continue
        field = entry["field"]
        title = str(field.get("title") or field.get("humanReadablePath") or "").strip()
        if not title:
            continue
        fields.append({
            "label": title,
            "required": bool(entry.get("isRequired")),
            "type": str(field.get("type", "")),
            "description": _plain(str(entry.get("descriptionHtml", ""))),
        })
    limit = _plain(str(_embedded_json(source, '"applicationLimitCalloutHtml":') or ""))
    return {
        "vendor": "Ashby",
        "application_url": final_url,
        "posting_state": "open" if fields else "unknown",
        "account_requirement": "No login/account control observed on the public form.",
        "terminal_action": "Submit Application",
        "capture_completeness": "complete",
        "capture_boundary": (
            "All fields exposed by the public single-page form were captured. "
            "No values were entered and Submit Application was not clicked."
        ),
        "application_limit_notice": limit,
        "fields": fields,
    }


def _smartrecruiters_fields(config: dict[str, Any]) -> list[dict[str, Any]]:
    labels = {
        "firstAndLastName": "First and last name",
        "email": "Email and email confirmation",
        "placeOfResidence": "Current city / place of residence",
        "phoneNumber": "Phone number",
        "experience": "Experience entries",
        "education": "Education entries",
        "socialProfiles": "Social profiles / personal website",
        "resume": "Resume attachment",
        "messageToHiringManager": "Message to the hiring manager",
    }
    fields: list[dict[str, Any]] = []
    field_sets = config.get("fieldSets", {}) if isinstance(config, dict) else {}
    for key, label in labels.items():
        item = field_sets.get(key, {}) if isinstance(field_sets, dict) else {}
        if not isinstance(item, dict) or not item.get("visible"):
            continue
        detail = ""
        if key == "placeOfResidence":
            detail = str((item.get("configuration") or {}).get("locationType", "")).lower()
        elif key == "socialProfiles":
            visible = [name for name, child in item.items()
                       if isinstance(child, dict) and child.get("visible")]
            detail = ", ".join(visible)
        elif key == "education" and isinstance(item.get("institution"), dict):
            detail = "institution required within each optional education entry"
        fields.append({
            "label": label,
            "required": bool(item.get("required")),
            "type": "file" if key == "resume" else "structured",
            "description": detail,
        })
    return fields


def _smartrecruiters_browser_audit(application_url: str, timeout_ms: int = 60_000) -> dict[str, Any]:
    """Capture the public one-click configuration without interacting with the form."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - environment-dependent fallback
        raise RuntimeError("Playwright is not installed; install RoleScout's browser extra") from exc

    captured: dict[str, Any] = {}
    final_url = application_url
    title = ""
    profile = Path(tempfile.gettempdir()) / "rolescout-application-audit-browser"
    profile.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            str(profile),
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
            viewport={"width": 1280, "height": 900},
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()

            def on_response(response) -> None:
                if "/oneclick-ui/api/" in response.url and response.url.endswith("/config"):
                    try:
                        value = response.json()
                    except Exception:
                        return
                    if isinstance(value, dict):
                        captured.update(value)

            page.on("response", on_response)
            page.goto(application_url, wait_until="domcontentloaded", timeout=timeout_ms)
            page.wait_for_timeout(6_000)
            final_url = page.url
            title = page.title()
        finally:
            context.close()
    if not captured:
        raise RuntimeError("SmartRecruiters public form configuration was not captured")
    fields = _smartrecruiters_fields(captured)
    return {
        "vendor": "SmartRecruiters",
        "application_url": final_url,
        "posting_state": "open" if fields else "unknown",
        "page_title": title,
        "account_requirement": "No login/account control observed on the public Easy Apply page.",
        "terminal_action": "Next, followed by the terminal review/submit step",
        "capture_completeness": "partial_at_required_upload",
        "capture_boundary": (
            "The public first-page schema was captured from SmartRecruiters. Resume upload is "
            "required before Next; RoleScout did not upload a file, transmit candidate data, or "
            "advance the form. Any later screening questions must be confirmed manually after "
            "the user attaches the selected resume."
        ),
        "application_experience_snapshot_id": str(
            captured.get("applicationExperienceSnapshotId", "")
        ),
        "fields": fields,
    }


def audit_application_route(posting_url: str, *, allow_browser: bool = True) -> dict[str, Any]:
    """Inspect a posting and its public application route without making a submission."""
    result: dict[str, Any] = {
        "schema": "rolescout-application-route-audit-v1",
        "posting_url": posting_url,
        "posting_state": "unknown",
        "vendor": "unknown",
        "application_url": "",
        "fields": [],
        "capture_completeness": "failed",
        "capture_boundary": "",
        "errors": [],
    }
    try:
        posting_source, final_posting_url = _fetch(posting_url)
        result["posting_url"] = final_posting_url
        application_url = _application_link(final_posting_url, posting_source)
        host = urllib.parse.urlparse(final_posting_url).netloc.lower()
        if "ashbyhq.com" in host:
            result.update(_ashby_audit(final_posting_url, application_url))
        elif application_url and "smartrecruiters.com" in urllib.parse.urlparse(application_url).netloc.lower():
            if not allow_browser:
                raise RuntimeError("SmartRecruiters requires the rendered-browser auditor")
            result.update(_smartrecruiters_browser_audit(application_url))
        else:
            result.update({
                "application_url": application_url,
                "vendor": urllib.parse.urlparse(application_url or final_posting_url).netloc,
                "posting_state": "open" if application_url else "unknown",
                "capture_completeness": "route_only" if application_url else "failed",
                "capture_boundary": (
                    "The public application link was verified, but no supported ATS form adapter "
                    "was available. No candidate data was entered and no control was clicked."
                ),
            })
    except (OSError, ValueError, RuntimeError, urllib.error.URLError) as exc:
        result["errors"].append(f"{type(exc).__name__}: {exc}")
        if not result["capture_boundary"]:
            result["capture_boundary"] = (
                "Route inspection failed before any candidate data was entered or transmitted."
            )
    return result
