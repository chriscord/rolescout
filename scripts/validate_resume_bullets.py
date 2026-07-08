#!/usr/bin/env python3
"""Lint resume bullets for strong action verbs and valid inclusion reasons.

Usage:
  python3 scripts/validate_resume_bullets.py resume.md [--reasons validation.json]

Extracts bullets ('- ' or '* ' lines) from the markdown resume, skipping the
skills/education sections. Checks each bullet against references/resume-bullet-rules.md:
  1. starts with a strong action verb (not a weak opener / noun / gerund)
  2. length <= 250 chars (hard); 20-27 words is the target band (warning only)
  3. if --reasons given: every bullet has a valid reason code and evidence ref

reasons JSON: [{"bullet_prefix": "first ~30 chars", "reason": "req_match", "evidence": "EV-003"}, ...]
Exit 0 if all pass, 1 otherwise.
"""
import argparse
import json
import re
import sys
from pathlib import Path

WEAK_OPENERS = [
    "responsible for", "worked on", "helped with", "helped ", "assisted",
    "participated in", "involved in", "tasked with", "was part of",
    "worked with", "worked as", "duties included",
]
VALID_REASONS = {"req_match", "impact", "scope", "domain", "differentiator", "narrative"}
SKIP_SECTIONS = ("skill", "education", "certification", "language", "interest")


def extract_bullets(md: str):
    bullets, section, skip = [], "", False
    for line in md.splitlines():
        h = re.match(r"^#{1,6}\s+(.*)", line)
        if h:
            section = h.group(1).lower()
            skip = any(s in section for s in SKIP_SECTIONS)
            continue
        m = re.match(r"^\s*[-*]\s+(.*)", line)
        if m and not skip:
            bullets.append(m.group(1).strip())
    return bullets


def check_verb(bullet: str):
    text = re.sub(r"^\*\*|^__", "", bullet).strip()
    low = text.lower()
    for w in WEAK_OPENERS:
        if low.startswith(w):
            return f"weak opener '{text[:len(w)].strip()}'"
    first = re.split(r"[\s,:]", text, 1)[0]
    if not first:
        return "empty bullet"
    if first.lower().endswith("ing"):
        return f"gerund opener '{first}' — use past/present tense verb"
    if not first[0].isupper():
        return f"'{first}' not capitalized — should start with an action verb"
    # Heuristic: strong openers are verbs; flag obvious noun-phrase starts.
    if first.lower() in {"the", "a", "an", "my", "our", "this", "over", "more", "various"}:
        return f"starts with '{first}', not an action verb"
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("resume_md")
    ap.add_argument("--reasons", help="JSON file mapping bullets to reason codes + evidence refs")
    args = ap.parse_args()

    md = Path(args.resume_md).read_text(encoding="utf-8")
    bullets = extract_bullets(md)
    if not bullets:
        print("FAIL: no experience bullets found in resume")
        return 1

    reasons = []
    if args.reasons:
        with open(args.reasons, encoding="utf-8") as f:
            reasons = json.load(f)

    errors, warnings = [], []
    for i, b in enumerate(bullets):
        verb_err = check_verb(b)
        if verb_err:
            errors.append(f"bullet {i}: {verb_err}: \"{b[:60]}...\"" if len(b) > 60
                          else f"bullet {i}: {verb_err}: \"{b}\"")
        if len(b) > 250:
            errors.append(f"bullet {i}: {len(b)} chars (max 250): \"{b[:60]}...\"")
        n_words = len(b.split())
        if not 20 <= n_words <= 27:
            warnings.append(f"bullet {i}: {n_words} words (target 20-27): \"{b[:60]}...\"")
        if args.reasons:
            match = next((r for r in reasons if b.startswith(r.get("bullet_prefix", "\x00"))), None)
            if match is None:
                errors.append(f"bullet {i}: no reason entry: \"{b[:60]}\"")
            else:
                if match.get("reason") not in VALID_REASONS:
                    errors.append(f"bullet {i}: invalid reason '{match.get('reason')}' "
                                  f"(valid: {sorted(VALID_REASONS)})")
                if not str(match.get("evidence", "")).strip():
                    errors.append(f"bullet {i}: missing evidence reference")

    if args.reasons:
        narrative_count = sum(1 for r in reasons if r.get("reason") == "narrative")
        if narrative_count > 2:
            errors.append(f"{narrative_count} 'narrative' bullets (max 2)")

    for w in warnings:
        print(f"  WARN: {w}")
    if errors:
        print(f"FAIL: {len(errors)} issue(s) across {len(bullets)} bullet(s)")
        for e in errors:
            print(f"  - {e}")
        return 1
    print(f"PASS: {len(bullets)} bullet(s) valid"
          + (f" ({len(warnings)} length warning(s))" if warnings else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
