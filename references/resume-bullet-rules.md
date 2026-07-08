# Resume Bullet Rules

Every experience bullet in a generated or tailored resume must satisfy all three rules. The validator `scripts/validate_resume_bullets.py` enforces rules 1 and 3 mechanically; rule 2 requires judgment plus the evidence map.

## Rule 1 — Strong action verb first

Each bullet starts with a strong past-tense (or present-tense for current role) action verb. No weak openers.

Weak openers (reject): `Responsible for`, `Worked on`, `Helped`, `Helped with`, `Assisted`, `Participated in`, `Involved in`, `Tasked with`, `Was part of`, `Contributed to` (unless contribution is then made specific), gerunds like `Working on`, and bullets starting with a noun.

Strong verb examples: Led, Built, Shipped, Designed, Reduced, Increased, Launched, Migrated, Automated, Negotiated, Drove, Owned, Architected, Cut, Grew, Delivered, Scaled, Refactored, Consolidated, Established.

## Rule 2 — Evidence-backed

Every factual claim (metric, scope, technology, outcome) traces to an entry in `profile/evidence-map.md` or to explicit user input in the conversation. If evidence is missing, ask the user — never estimate a metric or infer an achievement. Bullets with unverifiable claims must be flagged in the validation report, not silently included.

## Rule 3 — Valid reason for inclusion

Each bullet exists for a documented strategic reason tied to the target job group. Valid reason codes:

| Code | Meaning |
|---|---|
| `req_match` | Directly addresses a must-have or nice-to-have requirement in the target JDs |
| `impact` | Demonstrates quantified impact relevant to the role's scope |
| `scope` | Establishes seniority/scope (team size, budget, system scale) |
| `domain` | Shows domain familiarity the target group values |
| `differentiator` | Rare capability that distinguishes the candidate |
| `narrative` | Needed for career-story coherence (use sparingly, max 2 per resume) |

Record reasons in the validation report (`resumes/<target-group>/resume-validation.md`) as a table: bullet text → verb check → evidence ref → reason code. A bullet with no defensible reason gets cut, no matter how impressive it sounds.

## Format constraints

- Professional English always — baseline bullets may arrive as rough notes in any language; the generated bullet never does.
- Target length: **20–27 words and 200–250 characters** per bullet; hard cap 250 characters (validator-enforced; word count outside the band is a warning, not a failure). Exact-metric bullets may run shorter when padding would dilute precision.
- Quantify where evidence allows; don't force fake precision.
- No first-person pronouns, no periods-optional inconsistency (pick one style per resume).
