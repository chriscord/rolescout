"""Normalize job locations for job_list rows.

The storage format is a semicolon-separated list of location tags. A tag is
either a city-state/country-only value such as `Singapore`, or `{City}, {Country}`
for ordinary city locations. Use `USA`, not `US` or `United States`.
"""

from __future__ import annotations

import re

COUNTRY_ALIASES = {
    "americas": "Americas",
    "apac": "APAC",
    "br": "Brazil",
    "brazil": "Brazil",
    "ca": "Canada",
    "canada": "Canada",
    "china": "China",
    "cn": "China",
    "de": "Germany",
    "emea": "EMEA",
    "europe": "Europe",
    "fr": "France",
    "france": "France",
    "germany": "Germany",
    "hk": "Hong Kong",
    "hong kong": "Hong Kong",
    "in": "India",
    "india": "India",
    "ireland": "Ireland",
    "it": "Italy",
    "italy": "Italy",
    "latam": "LATAM",
    "mexico": "Mexico",
    "mx": "Mexico",
    "netherlands": "Netherlands",
    "nl": "Netherlands",
    "portugal": "Portugal",
    "pt": "Portugal",
    "spain": "Spain",
    "south korea": "South Korea",
    "taiwan": "Taiwan",
    "tw": "Taiwan",
    "uae": "UAE",
    "united arab emirates": "UAE",
    "united kingdom": "United Kingdom",
    "uk": "United Kingdom",
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
}

RISKY_LOCATION_ALIAS_TOKENS = (
    "from",
    "in",
    "into",
    "over",
    "that",
    "this",
    "with",
)

CITY_COUNTRIES = {
    "abu dhabi": "UAE",
    "amsterdam": "Netherlands",
    "atlanta": "USA",
    "austin": "USA",
    "bangalore": "India",
    "barcelona": "Spain",
    "beijing": "China",
    "bengaluru": "India",
    "berkeley": "USA",
    "berlin": "Germany",
    "boston": "USA",
    "boulder": "USA",
    "chennai": "India",
    "chicago": "USA",
    "culver city": "USA",
    "cupertino": "USA",
    "delhi": "India",
    "dubai": "UAE",
    "dublin": "Ireland",
    "fremont": "USA",
    "hong kong": "Hong Kong",
    "hyderabad": "India",
    "lisbon": "Portugal",
    "london": "United Kingdom",
    "los angeles": "USA",
    "madrid": "Spain",
    "melbourne": "Australia",
    "mexico city": "Mexico",
    "milan": "Italy",
    "mountain view": "USA",
    "mumbai": "India",
    "new jersey": "USA",
    "new york": "USA",
    "oakland": "USA",
    "palo alto": "USA",
    "pangyo": "South Korea",
    "paris": "France",
    "porto": "Portugal",
    "pune": "India",
    "redwood city": "USA",
    "rio de janeiro": "Brazil",
    "san diego": "USA",
    "san francisco": "USA",
    "san jose": "USA",
    "san mateo": "USA",
    "santa clara": "USA",
    "santa monica": "USA",
    "seoul": "South Korea",
    "seattle": "USA",
    "shanghai": "China",
    "shenzhen": "China",
    "singapore": "Singapore",
    "south san francisco": "USA",
    "sunnyvale": "USA",
    "sydney": "Australia",
    "taipei": "Taiwan",
    "tokyo": "Japan",
    "toronto": "Canada",
    "vancouver": "Canada",
    "warsaw": "Poland",
    "washington": "USA",
}

CITY_ALIASES = {
    "bay area": "san francisco",
    "la": "los angeles",
    "l.a.": "los angeles",
    "nyc": "new york",
    "new york city": "new york",
    "san francisco bay area": "san francisco",
    "sf": "san francisco",
    "sf bay area": "san francisco",
    "silicon valley": "san francisco",
    "washington d.c": "washington",
    "washington dc": "washington",
    "washington d.c.": "washington",
}

US_STATE_CODES = {
    "ak", "al", "ar", "az", "ca", "co", "ct", "dc", "de", "fl", "ga",
    "hi", "ia", "id", "il", "in", "ks", "ky", "la", "ma", "md", "me",
    "mi", "mn", "mo", "ms", "mt", "nc", "nd", "ne", "nh", "nj", "nm",
    "nv", "ny", "oh", "ok", "or", "pa", "ri", "sc", "sd", "tn", "tx",
    "ut", "va", "vt", "wa", "wi", "wv", "wy",
}

US_STATE_NAMES = {
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado",
    "connecticut", "delaware", "district of columbia", "florida", "georgia",
    "hawaii", "idaho", "illinois", "indiana", "iowa", "kansas", "kentucky",
    "louisiana", "maine", "maryland", "massachusetts", "michigan",
    "minnesota", "mississippi", "missouri", "montana", "nebraska", "nevada",
    "new hampshire", "new jersey", "new mexico", "new york",
    "north carolina", "north dakota", "ohio", "oklahoma", "oregon",
    "pennsylvania", "rhode island", "south carolina", "south dakota",
    "tennessee", "texas", "utah", "vermont", "virginia", "washington",
    "west virginia", "wisconsin", "wyoming",
}

LOCATION_PRIORITY = {
    "Singapore": 0,
    "Seoul, South Korea": 1,
}


def _clean_piece(text: str) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    text = re.sub(r"^(?:locations?|office locations?)\s*[:\-]\s*", "", text, flags=re.I).strip()
    text = re.sub(r"\s+Locations?$", "", text, flags=re.I).strip()
    text = re.sub(r"\s+preferred$", "", text, flags=re.I).strip()
    if re.fullmatch(r"(remote|hybrid|onsite|on-site)", text, flags=re.I):
        return ""
    text = re.sub(
        r"^(remote|hybrid|onsite|on-site)\b\s*"
        r"(?:[-–—,:]\s*|\bin\b\s*|\bwithin\b\s*|\bbased\s+in\b\s*|\s+)",
        "",
        text,
        flags=re.I,
    ).strip()
    text = re.sub(r"^(?:in|within)\s+", "", text, flags=re.I).strip()
    text = text.strip(" -–—:")
    return text


def _title_city(text: str) -> str:
    return " ".join(p.capitalize() if not p.isupper() else p for p in text.split())


def _canonical_city_key(text: str) -> str:
    key = re.sub(r"\s+", " ", str(text or "").strip().lower().rstrip("."))
    return CITY_ALIASES.get(key, key)


def _city(text: str) -> str:
    return _title_city(_canonical_city_key(text))


def _country(text: str) -> str:
    return COUNTRY_ALIASES.get(text.lower().strip(), _title_city(text.strip()))


def _prefer_city_country(city: str, country: str) -> str:
    city_key = _canonical_city_key(city)
    expected = CITY_COUNTRIES.get(city_key)
    if expected == "USA" and country == "Canada":
        return "USA"
    return country


def _tag(piece: str) -> str:
    piece = _clean_piece(piece)
    if not piece:
        return ""

    pref = re.match(
        r"^(SG|US|USA|United States|United Kingdom|UK|KR|KOR|South Korea|Korea)\s*[-–]\s*(.+)$",
        piece, flags=re.I)
    if pref:
        country = _country(pref.group(1))
        city = _clean_piece(pref.group(2))
        if country == "Singapore" or city.lower() == "singapore":
            return "Singapore"
        return f"{_city(city)}, {country}"

    parts = [_clean_piece(p) for p in piece.split(",")]
    parts = [p for p in parts if p]
    if len(parts) >= 2:
        low_parts = [p.lower() for p in parts]
        if any(p == "singapore" or p == "sg" for p in low_parts):
            return "Singapore"
        if low_parts[0] in US_STATE_CODES and low_parts[-1] in COUNTRY_ALIASES:
            return _country(parts[-1])
        if (
            low_parts[0] in US_STATE_NAMES
            and low_parts[0] not in CITY_COUNTRIES
            and low_parts[-1] in COUNTRY_ALIASES
        ):
            return _country(parts[-1])
        if len(parts) >= 3 and low_parts[-1] in COUNTRY_ALIASES:
            country = _prefer_city_country(parts[0], _country(parts[-1]))
            return f"{_city(parts[0])}, {country}"
        if low_parts[-1] in US_STATE_CODES or low_parts[-1] in US_STATE_NAMES:
            return f"{_city(parts[0])}, USA"
        if len(parts) >= 3 and (low_parts[1] in US_STATE_CODES or low_parts[1] in US_STATE_NAMES):
            return f"{_city(parts[0])}, USA"
        if low_parts[0] in COUNTRY_ALIASES:
            country = _country(parts[0])
            city = parts[-1]
            if city.lower() in COUNTRY_ALIASES:
                return country
            return f"{_city(city)}, {country}"
        if low_parts[-1] in COUNTRY_ALIASES:
            country = _country(parts[-1])
            city = parts[0]
            if city.lower() in COUNTRY_ALIASES and len(parts) == 2:
                return _country(city)
            country = _prefer_city_country(city, country)
            return f"{_city(city)}, {country}"
        city = parts[0]
        country = _prefer_city_country(city, _country(parts[-1]))
        return f"{_city(city)}, {country}"
    if len(parts) == 1:
        piece = parts[0]

    trailing = re.match(r"^(.+?)\s+(US|USA|United States|SG|KR|KOR)$", piece, flags=re.I)
    if trailing:
        city = _clean_piece(trailing.group(1)).rstrip(",").strip()
        country = _country(trailing.group(2))
        if country == "Singapore" or city.lower() == "singapore":
            return "Singapore"
        return f"{_city(city)}, {country}"

    low = _canonical_city_key(piece)
    piece = low
    if low in COUNTRY_ALIASES:
        return _country(piece)
    if low in CITY_COUNTRIES:
        country = CITY_COUNTRIES[low]
        if country == "Singapore":
            return "Singapore"
        return f"{_city(piece)}, {country}"
    if low in US_STATE_NAMES:
        return "USA"
    return _city(piece)


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
