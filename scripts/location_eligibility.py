#!/usr/bin/env python3
"""Classify job locations against project target locations.

This helper is deliberately geographic, not employer-specific. It prevents the
agent from treating a nearby metro location (for example Palo Alto vs San
Francisco/San Mateo) as a hard exclusion when the project target did not specify
exact-city strictness.
"""

from __future__ import annotations

import argparse
import json
import re
import sys


try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except AttributeError:
    pass


METRO_AREAS = {
    "Bay Area": {
        "san francisco", "san mateo", "palo alto", "mountain view",
        "menlo park", "redwood city", "san jose", "sunnyvale",
        "cupertino", "santa clara", "fremont", "oakland", "berkeley",
        "south san francisco", "san bruno", "burlingame", "foster city",
    },
    "Los Angeles metro": {
        "los angeles", "santa monica", "culver city", "burbank",
        "glendale", "pasadena", "long beach", "el segundo", "irvine",
    },
}


def _city(value: str) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"\b(united states|usa|u\.s\.a\.|us|california|ca)\b", "", text)
    text = re.sub(r"[^a-z\s-]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip(" ,-")
    return text


def _metros_for(city: str) -> set[str]:
    return {name for name, cities in METRO_AREAS.items() if city in cities}


def evaluate_location(location: str, targets: list[str],
                      strictness: str = "metro_review") -> dict:
    """Return eligible/excluded/review for a location against target cities.

    strictness:
      - exact_city: only exact city matches are eligible.
      - metro: same metro is eligible.
      - metro_review: same metro but non-exact city is review.
    """
    loc_city = _city(location)
    target_cities = [_city(t) for t in targets if str(t or "").strip()]
    if not loc_city or not target_cities:
        return {"status": "review", "reason": "location or target locations missing"}
    if loc_city in target_cities:
        return {"status": "eligible", "reason": f"{location} matches a target city"}
    if strictness == "exact_city":
        return {"status": "excluded", "reason": f"{location} is outside exact target cities"}

    loc_metros = _metros_for(loc_city)
    target_metros = set()
    for target in target_cities:
        target_metros.update(_metros_for(target))
    shared = sorted(loc_metros & target_metros)
    if shared:
        status = "eligible" if strictness == "metro" else "review"
        return {
            "status": status,
            "reason": f"{location} is outside exact target cities but inside {', '.join(shared)}",
            "metro": shared[0],
        }
    return {"status": "excluded", "reason": f"{location} is outside target metro areas"}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Evaluate job location eligibility.")
    ap.add_argument("location")
    ap.add_argument("--target", action="append", default=[],
                    help="target location; repeatable")
    ap.add_argument("--strictness", default="metro_review",
                    choices=["exact_city", "metro", "metro_review"])
    args = ap.parse_args(argv)
    print(json.dumps(evaluate_location(args.location, args.target, args.strictness),
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
