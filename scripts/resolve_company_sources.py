#!/usr/bin/env python3
"""Build a deterministic first-pass source plan for one or more companies.

The agent still decides the company universe and judges postings. This helper
only prevents a common mechanical failure: blindly guessing ATS slugs before
checking the official/registered careers source.

Usage:
  python scripts/resolve_company_sources.py "ExampleCo" --json
  python scripts/resolve_company_sources.py "ExampleCo" --registry references/search-source-registry.yaml
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path
from urllib.parse import quote_plus

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REGISTRY = ROOT / "references" / "search-source-registry.yaml"


def _configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def normalized_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", name.lower())


def _strip_comment(value: str) -> str:
    in_quote = ""
    out: list[str] = []
    i = 0
    while i < len(value):
        ch = value[i]
        if ch in ("'", '"') and (i == 0 or value[i - 1] != "\\"):
            in_quote = "" if in_quote == ch else ch if not in_quote else in_quote
        if ch == "#" and not in_quote:
            break
        out.append(ch)
        i += 1
    return "".join(out).strip()


def _parse_scalar(value: str):
    value = _strip_comment(value)
    if not value:
        return ""
    if value[0] in ("'", '"', "[", "{"):
        try:
            return ast.literal_eval(value)
        except (ValueError, SyntaxError):
            pass
    return value.strip("'\"")


def load_registered_registry(registry_path: Path | None = None) -> dict[str, dict]:
    """Parse maintained company career entries from the plain YAML registry.

    RoleScout intentionally avoids a YAML runtime dependency. The registry shape
    is constrained enough that a small section parser is safer than requiring
    agents to parse the same file with ad-hoc Python. Returns both
    self_hosted_careers and major_company_careers entries.
    """
    path = registry_path or DEFAULT_REGISTRY
    text = path.read_text(encoding="utf-8")
    entries: dict[str, dict] = {}
    active_section = ""
    in_companies = False
    current: dict | None = None
    supported_sections = {"self_hosted_careers", "major_company_careers"}

    def flush() -> None:
        nonlocal current
        if current and current.get("name"):
            entries.setdefault(normalized_name(str(current["name"])), current)
            aliases = current.get("aliases") or []
            if isinstance(aliases, str):
                aliases = [aliases]
            for alias in aliases:
                if str(alias).strip():
                    entries.setdefault(normalized_name(str(alias)), current)
        current = None

    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if not raw.startswith(" ") and stripped.endswith(":"):
            flush()
            section = stripped[:-1]
            if section in supported_sections:
                active_section = section
                in_companies = False
                continue
            active_section = ""
            in_companies = False
            continue
        if not active_section:
            continue
        if stripped == "companies:":
            in_companies = True
            continue
        if not in_companies:
            continue
        match = re.match(r"\s*-\s+name:\s*(.+)$", raw)
        if match:
            flush()
            current = {
                "name": _parse_scalar(match.group(1)),
                "registry_section": active_section,
            }
            continue
        if current is None:
            continue
        kv = re.match(r"\s+([A-Za-z_][A-Za-z0-9_]*):\s*(.*)$", raw)
        if kv:
            current[kv.group(1)] = _parse_scalar(kv.group(2))
    flush()
    return entries


def load_self_hosted_registry(registry_path: Path | None = None) -> dict[str, dict]:
    """Backward-compatible alias for the combined maintained registry."""
    return load_registered_registry(registry_path)


def _slug_guess(company: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", company.lower())


def is_category_seed(company: str) -> bool:
    """True for market/category descriptions that must not become ATS slugs."""
    text = " ".join(str(company or "").lower().split())
    plural_category = re.search(
        r"\b(startups|scaleups|companies|employers|firms|organizations|organisations)\b",
        text,
    )
    return bool(plural_category and (" or " in text or " and " in text or "," in text
                                     or text.startswith(("ai ", "tech ", "fintech "))))


def _registered_sources(company: str, entry: dict) -> list[dict]:
    sources: list[dict] = []
    render = str(entry.get("render", "")).strip()
    source_kind = str(entry.get("source_kind", "")).strip()
    careers_url = str(entry.get("careers_search_url", "") or entry.get("listing_url", "")).strip()
    location_template = str(entry.get("location_search_url_template", "")).strip()
    keyword_location_template = str(entry.get("keyword_location_search_url_template", "")).strip()
    location_param_name = str(entry.get("location_param_name", "")).strip()
    location_value_style = str(entry.get("location_value_style", "")).strip()
    location_multi_value = str(entry.get("location_multi_value", "")).strip()
    location_examples = entry.get("location_examples", "")
    json_api = str(entry.get("json_api", "")).strip()
    posting_url = str(entry.get("posting_url", "") or entry.get("detail_prefix", "")
                      or entry.get("detail_pattern", "")).strip()
    raw_ats_url = str(entry.get("raw_ats_url", "")).strip()
    if careers_url:
        source = {
            "type": "official_careers",
            "url": careers_url,
            "status": "planned",
            "render": render,
            "source_kind": source_kind,
        }
        if location_template:
            source["location_search_url_template"] = location_template
            source["note"] = "instantiate {country_or_city} from project target locations"
        if keyword_location_template:
            source["keyword_location_search_url_template"] = keyword_location_template
        if location_param_name:
            source["location_param_name"] = location_param_name
        if location_value_style:
            source["location_value_style"] = location_value_style
        if location_multi_value:
            source["location_multi_value"] = location_multi_value
        if location_examples:
            source["location_examples"] = location_examples
        sources.append(source)
    if json_api:
        sources.append({
            "type": "official_careers_api",
            "url": json_api,
            "status": "planned",
            "render": "json_api",
        })
    if posting_url:
        sources.append({
            "type": "official_posting_pattern",
            "url": posting_url,
            "status": "planned",
        })
    alternates = entry.get("alternate_detail_prefixes") or []
    if isinstance(alternates, str):
        alternates = [alternates]
    for alternate in alternates:
        sources.append({
            "type": "official_posting_pattern",
            "url": str(alternate),
            "status": "planned",
            "note": "alternate detail route",
        })
    if raw_ats_url:
        sources.append({
            "type": "official_careers_mirror",
            "url": raw_ats_url,
            "status": "planned",
            "note": "raw ATS URL maintained in registry; prefer branded source_url when available",
        })
    mirrors = entry.get("mirrors") or []
    if isinstance(mirrors, str):
        mirrors = [mirrors]
    for mirror in mirrors:
        sources.append({
            "type": "official_careers_mirror",
            "url": str(mirror),
            "status": "planned",
        })
    sources.append({
        "type": "web_discovery",
        "query": f'"{company}" careers jobs',
        "status": "planned",
        "note": "official careers verification/discovery fallback",
    })
    return sources


def _unknown_sources(company: str) -> list[dict]:
    encoded = quote_plus(f"{company} careers jobs")
    token = _slug_guess(company)
    return [
        {
            "type": "official_discovery",
            "query": f'"{company}" careers jobs',
            "search_url": f"https://www.google.com/search?q={encoded}",
            "status": "planned",
        },
        {
            "type": "guessed_ats_probe",
            "status": "planned",
            "note": "fallback after official careers discovery finds no canonical source",
            "urls": [
                f"https://boards.greenhouse.io/{token}",
                f"https://jobs.lever.co/{token}",
                f"https://jobs.ashbyhq.com/{token}",
            ],
        },
        {
            "type": "web_discovery",
            "query": f'site:careers.* "{company}" jobs',
            "status": "planned",
        },
    ]


def source_plan_for_company(company: str, registry_path: Path | None = None) -> dict:
    registry = load_registered_registry(registry_path)
    entry = registry.get(normalized_name(company))
    category_seed = is_category_seed(company)
    if category_seed:
        sources = [{
            "type": "company_expansion_required",
            "query": company,
            "status": "planned",
            "note": "category/market seed; expand to named employers before source resolution",
        }]
        notes = "category seed; skipped guessed ATS probes until expanded to named employers"
    elif entry:
        sources = _registered_sources(company, entry)
        notes = ("registered official careers source; verify registry URLs first; "
                 "do not execute guessed ATS probes unless discovery proves no canonical source")
    else:
        sources = _unknown_sources(company)
        notes = "unknown source; discover official careers before ATS fallback probes"
    if not category_seed:
        sources.append({
            "type": "LinkedIn Jobs",
            "status": "planned",
            "note": "mandatory lead-owned pass after non-login sources",
        })
    return {
        "name": company,
        "sources": sources,
        "fallbacks_used": [],
        "notes": notes,
        "category_seed": category_seed,
    }


def main(argv: list[str] | None = None) -> int:
    _configure_stdio()
    ap = argparse.ArgumentParser(description="Resolve official-first job sources.")
    ap.add_argument("companies", nargs="+")
    ap.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    ap.add_argument("--json", action="store_true", help="emit machine JSON only")
    args = ap.parse_args(argv)

    plans = [source_plan_for_company(company, args.registry)
             for company in args.companies]
    payload = {"companies": plans}
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        for plan in plans:
            print(f"{plan['name']}: {plan['notes']}")
            for src in plan["sources"]:
                label = src.get("url") or src.get("query") or ", ".join(src.get("urls", []))
                print(f"  - {src['type']}: {label}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
