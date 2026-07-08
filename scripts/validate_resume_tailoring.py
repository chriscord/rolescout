#!/usr/bin/env python3
"""Validate whether a tailored resume is actually target-specific.

This complements validate_resume_bullets.py. It checks transformation quality:
baseline-copy risk, target requirement mapping, source job linkage, and optional
cross-variant overlap.
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
from pathlib import Path


SKIP_SECTIONS = ("skill", "education", "certification", "language", "interest")
MUST_PRIORITIES = {"must", "required", "high", "critical"}
SELECTED_REWRITE_TYPES = {"selected", "copy", "near_copy", "unchanged"}


def normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def split_ids(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    return [p.strip() for p in re.split(r"[,;]", str(value)) if p.strip()]


def extract_bullets(text: str) -> list[str]:
    bullets: list[str] = []
    section = ""
    skip = False
    current: str | None = None

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        h = re.match(r"^#{1,6}\s+(.*)", line)
        if h:
            if current:
                bullets.append(current)
                current = None
            section = h.group(1).lower()
            skip = any(s in section for s in SKIP_SECTIONS)
            continue
        m = re.match(r"^\s*(?:[-*]|[▪•])\s+(.*)", raw)
        if m and not skip:
            if current:
                bullets.append(current)
            current = m.group(1).strip()
            continue
        if current and not h and raw[:1].isspace():
            current += " " + line

    if current:
        bullets.append(current)
    return bullets


def load_text(path: str | None) -> str:
    return Path(path).read_text(encoding="utf-8") if path else ""


def load_json(path: str | None):
    if not path:
        return None
    return json.loads(Path(path).read_text(encoding="utf-8"))


def find_reason(bullet: str, reasons: list[dict]) -> dict | None:
    for reason in reasons:
        prefix = str(reason.get("bullet_prefix", "")).strip()
        if prefix and bullet.startswith(prefix):
            return reason
    bullet_norm = normalize(bullet)
    for reason in reasons:
        prefix = normalize(str(reason.get("bullet_prefix", "")))
        if prefix and bullet_norm.startswith(prefix):
            return reason
    return None


def similarity_to_baseline(bullets: list[str], baseline_bullets: list[str]) -> list[float]:
    if not baseline_bullets:
        return []
    baseline_norm = [normalize(b) for b in baseline_bullets]
    ratios: list[float] = []
    for bullet in bullets:
        b_norm = normalize(bullet)
        ratios.append(max(difflib.SequenceMatcher(None, b_norm, base).ratio()
                          for base in baseline_norm))
    return ratios


def target_requirements(target_brief) -> list[dict]:
    if not target_brief:
        return []
    reqs = target_brief.get("requirements", [])
    if not isinstance(reqs, list):
        return []
    must = [r for r in reqs if str(r.get("priority", "")).lower() in MUST_PRIORITIES]
    return must or reqs


def validate(args) -> tuple[list[str], dict]:
    resume_text = load_text(args.resume_md)
    bullets = extract_bullets(resume_text)
    baseline_bullets = extract_bullets(load_text(args.baseline)) if args.baseline else []
    target_brief = load_json(args.target_brief)
    reasons = load_json(args.reasons) or []
    errors: list[str] = []

    if not bullets:
        errors.append("no experience bullets found")
        return errors, {}
    if args.reasons and not isinstance(reasons, list):
        errors.append("reasons file must be a JSON list")
        reasons = []

    reason_by_bullet: list[tuple[str, dict | None]] = []
    for i, bullet in enumerate(bullets):
        reason = find_reason(bullet, reasons)
        reason_by_bullet.append((bullet, reason))
        if args.reasons and reason is None:
            errors.append(f"bullet {i}: no reasons.json entry")
            continue
        if not reason:
            continue
        req_ids = split_ids(reason.get("requirement_ids") or reason.get("requirement_id"))
        source_ids = split_ids(reason.get("source_job_ids") or reason.get("source_job_id"))
        evidence_ids = split_ids(reason.get("evidence_ids") or reason.get("evidence"))
        rewrite_type = str(reason.get("rewrite_type", "")).strip().lower()
        if target_brief and reason.get("reason") == "req_match" and not req_ids:
            errors.append(f"bullet {i}: req_match missing requirement_ids")
        if target_brief and reason.get("reason") == "req_match" and not source_ids:
            errors.append(f"bullet {i}: req_match missing source_job_ids")
        if args.reasons and not evidence_ids:
            errors.append(f"bullet {i}: missing evidence reference")
        if args.require_rewrite_type and not rewrite_type:
            errors.append(f"bullet {i}: missing rewrite_type")

    ratios = similarity_to_baseline(bullets, baseline_bullets)
    if ratios:
        avg = sum(ratios) / len(ratios)
        near_count = sum(1 for r in ratios if r >= args.near_copy_threshold)
        near_ratio = near_count / len(ratios)
        if avg > args.max_avg_similarity:
            errors.append(
                f"too similar to baseline: avg_similarity={avg:.2f} "
                f"(max {args.max_avg_similarity:.2f})"
            )
        if near_ratio > args.max_near_copy_ratio:
            errors.append(
                f"too similar to baseline: near_copy_ratio={near_ratio:.2f} "
                f"({near_count}/{len(ratios)}, max {args.max_near_copy_ratio:.2f})"
            )

    selected_count = 0
    for bullet, reason in reason_by_bullet:
        if not reason:
            continue
        rewrite_type = str(reason.get("rewrite_type", "")).strip().lower()
        if rewrite_type in SELECTED_REWRITE_TYPES:
            selected_count += 1
    if reason_by_bullet:
        selected_ratio = selected_count / len(reason_by_bullet)
        if selected_ratio > args.max_selected_ratio:
            errors.append(
                f"rewrite_type 'selected' overused: {selected_count}/{len(reason_by_bullet)} "
                f"(max ratio {args.max_selected_ratio:.2f})"
            )

    reqs = target_requirements(target_brief)
    if reqs:
        req_ids = {str(r.get("id", "")).strip() for r in reqs if str(r.get("id", "")).strip()}
        covered: set[str] = set()
        for _, reason in reason_by_bullet:
            if reason:
                covered.update(split_ids(reason.get("requirement_ids") or
                                         reason.get("requirement_id")))
        if req_ids:
            coverage = len(req_ids & covered) / len(req_ids)
            if coverage < args.min_requirement_coverage:
                missing = sorted(req_ids - covered)
                errors.append(
                    f"requirement coverage too low: {coverage:.2f} "
                    f"(min {args.min_requirement_coverage:.2f}); missing {missing}"
                )

    other_overlap = []
    for other in args.other_resume:
        other_bullets = extract_bullets(load_text(other))
        other_ratios = similarity_to_baseline(bullets, other_bullets)
        if other_ratios:
            overlap_count = sum(1 for r in other_ratios if r >= args.cross_overlap_threshold)
            overlap_ratio = overlap_count / len(other_ratios)
            other_overlap.append((other, overlap_ratio, overlap_count, len(other_ratios)))
            if overlap_ratio > args.max_cross_overlap:
                errors.append(
                    f"cross-variant overlap too high with {other}: {overlap_ratio:.2f} "
                    f"({overlap_count}/{len(other_ratios)}, max {args.max_cross_overlap:.2f})"
                )

    metrics = {
        "bullets": len(bullets),
        "avg_similarity": round(sum(ratios) / len(ratios), 4) if ratios else None,
        "near_copy_count": sum(1 for r in ratios if r >= args.near_copy_threshold) if ratios else None,
        "selected_count": selected_count,
        "other_overlap": other_overlap,
    }
    return errors, metrics


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("resume_md")
    ap.add_argument("--baseline")
    ap.add_argument("--target-brief")
    ap.add_argument("--reasons")
    ap.add_argument("--other-resume", action="append", default=[])
    ap.add_argument("--max-avg-similarity", type=float, default=0.74)
    ap.add_argument("--near-copy-threshold", type=float, default=0.82)
    ap.add_argument("--max-near-copy-ratio", type=float, default=0.35)
    ap.add_argument("--max-selected-ratio", type=float, default=0.25)
    ap.add_argument("--min-requirement-coverage", type=float, default=0.8)
    ap.add_argument("--cross-overlap-threshold", type=float, default=0.82)
    ap.add_argument("--max-cross-overlap", type=float, default=0.55)
    ap.add_argument("--require-rewrite-type", action="store_true", default=True)
    args = ap.parse_args()

    errors, metrics = validate(args)
    if errors:
        print(f"FAIL: {len(errors)} tailoring issue(s)")
        for err in errors:
            print(f"  - {err}")
        if metrics:
            print(f"metrics: {json.dumps(metrics, sort_keys=True)}")
        return 1
    print(f"PASS: {metrics.get('bullets', 0)} bullet(s) target-tailored")
    if metrics:
        print(f"metrics: {json.dumps(metrics, sort_keys=True)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
