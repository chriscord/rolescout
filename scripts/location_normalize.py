"""Normalize job locations for job_list rows.

The storage format is a semicolon-separated list of location tags. A tag is
either a city-state/country-only value such as `Singapore`, or `{City}, {Country}`
for ordinary city locations. Use `USA`, not `US` or `United States`.
"""

from __future__ import annotations

import re

COUNTRY_ALIASES = {
    "us": "USA",
    "u.s.": "USA",
    "u.s.a.": "USA",
    "usa": "USA",
    "united states": "USA",
    "united states of america": "USA",
    "sg": "Singapore",
    "singapore": "Singapore",
    "kr": "South Korea",
    "kor": "South Korea",
    "south korea": "South Korea",
    "korea": "South Korea",
    "republic of korea": "South Korea",
    "australia": "Australia",
    "japan": "Japan",
    "mexico": "Mexico",
}

CITY_COUNTRIES = {
    "san francisco": "USA",
    "pangyo": "South Korea",
    "seoul": "South Korea",
    "sydney": "Australia",
    "tokyo": "Japan",
    "mexico city": "Mexico",
}

LOCATION_PRIORITY = {
    "Singapore": 0,
    "Seoul, South Korea": 1,
}


def _clean_piece(text: str) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    text = re.sub(r"\s+Locations?$", "", text, flags=re.I).strip()
    text = re.sub(r"\s+preferred$", "", text, flags=re.I).strip()
    text = re.sub(r"^(remote|hybrid|onsite)\s+", "", text, flags=re.I).strip()
    return text


def _title_city(text: str) -> str:
    return " ".join(p.capitalize() if not p.isupper() else p for p in text.split())


def _country(text: str) -> str:
    return COUNTRY_ALIASES.get(text.lower().strip(), _title_city(text.strip()))


def _tag(piece: str) -> str:
    piece = _clean_piece(piece)
    if not piece:
        return ""

    pref = re.match(
        r"^(SG|US|USA|United States|KR|KOR|South Korea|Korea)\s*[-–]\s*(.+)$",
        piece, flags=re.I)
    if pref:
        country = _country(pref.group(1))
        city = _clean_piece(pref.group(2))
        if country == "Singapore" or city.lower() == "singapore":
            return "Singapore"
        return f"{_title_city(city)}, {country}"

    trailing = re.match(r"^(.+?)\s+(US|USA|SG|KR|KOR)$", piece, flags=re.I)
    if trailing:
        city = _clean_piece(trailing.group(1)).rstrip(",").strip()
        country = _country(trailing.group(2))
        if country == "Singapore" or city.lower() == "singapore":
            return "Singapore"
        return f"{_title_city(city)}, {country}"

    parts = [_clean_piece(p) for p in piece.split(",")]
    parts = [p for p in parts if p]
    if len(parts) >= 2:
        low_parts = [p.lower() for p in parts]
        if any(p == "singapore" or p == "sg" for p in low_parts):
            return "Singapore"
        city = parts[0]
        country = _country(parts[-1])
        if city.lower() in COUNTRY_ALIASES and len(parts) == 2:
            return _country(city)
        return f"{_title_city(city)}, {country}"

    low = piece.lower()
    if low in COUNTRY_ALIASES:
        return _country(piece)
    if low in CITY_COUNTRIES:
        country = CITY_COUNTRIES[low]
        if country == "Singapore":
            return "Singapore"
        return f"{_title_city(piece)}, {country}"
    return _title_city(piece)


def normalize_location_value(value: str) -> str:
    """Return normalized location tags joined by `; `."""
    value = _clean_piece(value)
    if not value:
        return ""
    pieces = re.split(r"\s*;\s*", value)
    tags = []
    seen = set()
    for piece in pieces:
        tag = _tag(piece)
        if not tag:
            continue
        key = tag.lower()
        if key not in seen:
            seen.add(key)
            tags.append(tag)
    tags.sort(key=lambda t: (LOCATION_PRIORITY.get(t, 50), t.lower()))
    return "; ".join(tags)


def normalize_job_rows(rows: list[dict]) -> list[dict]:
    out = []
    for row in rows:
        nr = dict(row)
        if "location" in nr:
            nr["location"] = normalize_location_value(nr.get("location", ""))
        out.append(nr)
    return out
