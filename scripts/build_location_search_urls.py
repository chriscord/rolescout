#!/usr/bin/env python3
"""Build location-filtered careers search URL candidates from registry hints.

The helper is deliberately generic: it does not encode a company-specific
fallback machine. Registry entries may provide a location URL template and a
small value-style hint; this script expands those hints into primary and
fallback URL variants so the LLM can observe which format a site accepts.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from urllib.parse import quote


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from location_normalize import normalize_location_value  # noqa: E402
import resolve_company_sources  # noqa: E402


PLACEHOLDERS = ("{country_or_city}", "{location}", "<city>", "<location>")

CANONICAL_LOCATION_LABELS = {
    "san francisco": ["San Francisco, CA, USA", "San Francisco, USA"],
    "sf": ["San Francisco, CA, USA", "San Francisco"],
    "seoul": ["Seoul, South Korea"],
    "korea": ["Korea, Republic of", "South Korea"],
    "south korea": ["Korea, Republic of", "South Korea"],
    "republic of korea": ["Korea, Republic of", "South Korea"],
    "singapore": ["Singapore"],
    "new york": ["New York, NY, USA", "New York, USA"],
    "london": ["London, UK", "London, United Kingdom"],
    "tokyo": ["Tokyo, Japan"],
}


def _configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _key(value: str) -> str:
    return _clean(value).lower()


def _dedupe(values: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        cleaned = _clean(value)
        if not cleaned:
            continue
        key = cleaned.lower()
        if key not in seen:
            seen.add(key)
            out.append(cleaned)
    return out


def _example_map(examples) -> dict[str, str]:
    out: dict[str, str] = {}
    if not isinstance(examples, list):
        return out
    for item in examples:
        if not isinstance(item, dict):
            continue
        source = _clean(item.get("input", ""))
        value = _clean(item.get("value", ""))
        if source and value:
            out[_key(source)] = value
    return out


def location_value_candidates(location: str, *,
                              value_style: str = "canonical_display_label",
                              examples=None) -> list[str]:
    """Return candidate display labels for one target location.

    The first value is the best registry/helper guess. Later values are fallback
    variants for LLM observation when a careers site ignores a location filter.
    """
    raw = _clean(location)
    if not raw:
        return []
    examples_by_input = _example_map(examples)
    values: list[str] = []
    if _key(raw) in examples_by_input:
        values.append(examples_by_input[_key(raw)])
    if value_style == "canonical_display_label":
        values.extend(CANONICAL_LOCATION_LABELS.get(_key(raw), []))
        normalized = normalize_location_value(raw)
        if normalized:
            values.extend(part.strip() for part in normalized.split(";"))
    values.append(raw)
    return _dedupe(values)


def _encode(value: str, encoding: str) -> str:
    if encoding in {"none", "raw"}:
        return value
    # Careers URLs usually expect `%20`, not form `+`.
    return quote(value, safe="")


def _replace_placeholder(template: str, replacement: str) -> str:
    for placeholder in PLACEHOLDERS:
        if placeholder in template:
            return template.replace(placeholder, replacement)
    joiner = "&" if "?" in template else "?"
    return f"{template}{joiner}location={replacement}"


def build_url(template: str, values: list[str], *,
              param_name: str = "location",
              encoding: str = "urlencoded",
              multi_value: str = "repeat_param") -> str:
    encoded = [_encode(value, encoding) for value in values if _clean(value)]
    if not encoded:
        return template
    if multi_value == "comma":
        replacement = ",".join(encoded)
    elif multi_value == "semicolon":
        replacement = ";".join(encoded)
    elif multi_value == "repeat_param":
        replacement = f"&{param_name}=".join(encoded)
    else:
        replacement = encoded[0]
    return _replace_placeholder(template, replacement)


def build_urls(template: str, locations: list[str], *,
               param_name: str = "location",
               value_style: str = "canonical_display_label",
               multi_value: str = "repeat_param",
               encoding: str = "urlencoded",
               examples=None) -> list[dict]:
    per_location = [
        location_value_candidates(location, value_style=value_style, examples=examples)
        for location in locations
    ]
    per_location = [values for values in per_location if values]
    urls: list[dict] = []
    if not per_location:
        return urls

    primary_values = [values[0] for values in per_location]
    urls.append({
        "variant": "primary_all_locations",
        "values": primary_values,
        "url": build_url(template, primary_values, param_name=param_name,
                         encoding=encoding, multi_value=multi_value),
    })

    for loc_index, values in enumerate(per_location, start=1):
        for value_index, value in enumerate(values, start=1):
            urls.append({
                "variant": f"location_{loc_index}_value_{value_index}",
                "values": [value],
                "url": build_url(template, [value], param_name=param_name,
                                 encoding=encoding, multi_value=multi_value),
            })

    out: list[dict] = []
    seen: set[str] = set()
    for item in urls:
        if item["url"] in seen:
            continue
        seen.add(item["url"])
        out.append(item)
    return out


def _source_examples(source: dict):
    examples = source.get("location_examples", [])
    return examples if isinstance(examples, list) else []


def build_for_company(company: str, locations: list[str]) -> dict:
    plan = resolve_company_sources.source_plan_for_company(company)
    url_items: list[dict] = []
    for source in plan.get("sources", []):
        if not isinstance(source, dict):
            continue
        template = str(source.get("location_search_url_template", "") or "").strip()
        if not template:
            continue
        source_urls = build_urls(
            template,
            locations,
            param_name=str(source.get("location_param_name", "") or "location"),
            value_style=str(source.get("location_value_style", "")
                            or "canonical_display_label"),
            multi_value=str(source.get("location_multi_value", "") or "repeat_param"),
            examples=_source_examples(source),
        )
        for item in source_urls:
            enriched = dict(item)
            enriched["source_type"] = source.get("type", "")
            enriched["template"] = template
            url_items.append(enriched)
    return {
        "company": company,
        "locations": locations,
        "urls": url_items,
    }


def main(argv: list[str] | None = None) -> int:
    _configure_stdio()
    parser = argparse.ArgumentParser(
        description="Build location-filtered careers search URL candidates.")
    parser.add_argument("--company", help="company name to resolve from registry")
    parser.add_argument("--template", help="explicit URL template")
    parser.add_argument("--location", action="append", default=[],
                        help="target location; repeatable")
    parser.add_argument("--param-name", default="location")
    parser.add_argument("--value-style", default="canonical_display_label",
                        choices=["canonical_display_label", "raw"])
    parser.add_argument("--multi-value", default="repeat_param",
                        choices=["repeat_param", "comma", "semicolon", "first_only"])
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if not args.location:
        print("ERROR: at least one --location is required", file=sys.stderr)
        return 1
    if args.company:
        payload = build_for_company(args.company, args.location)
    elif args.template:
        payload = {
            "company": "",
            "locations": args.location,
            "urls": build_urls(
                args.template,
                args.location,
                param_name=args.param_name,
                value_style=args.value_style,
                multi_value=args.multi_value,
            ),
        }
    else:
        print("ERROR: provide --company or --template", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        for item in payload["urls"]:
            print(f"{item['variant']}: {item['url']}")
    return 0 if payload["urls"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
