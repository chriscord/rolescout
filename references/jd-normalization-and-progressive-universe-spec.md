# JD Normalization and Progressive Universe Specification

Status: implemented
Date: 2026-07-11

## Invariants

- A saved project preference revision is immutable input. Model workers cannot edit it.
- Workers return typed proposals only. One revision-aware coordinator validates and
  atomically writes derived universe state.
- Raw JD text is retained. Deterministic code extracts and validates requirement candidates;
  the scoring model performs semantic candidate-to-requirement judgment.
- Search persistence is URL-keyed upsert. A changed preference or universe does not delete
  historical jobs; source lifecycle fields represent removal or reopening.

## JD requirement contract

The canonical cache is `<project>/targets/requirements/<job_id>.json`. It is reusable only
when both the raw-JD SHA-256 and the normalizer schema version match.

Each atom includes a stable ID, source quote, category, obligation, importance,
substitutability, confidence, and structured years when present. Supported priority classes
are:

- obligation: `minimum_required`, `required`, `preferred`, `responsibility`, informational
- importance: `eligibility`, `central`, `supporting`, `bonus`
- category: experience, degree, license, language, location, work authorization,
  travel/schedule, management, or skill/domain

The deterministic normalizer is high recall across arbitrary headings and paragraph order.
Responsibilities do not consume the scoring requirement budget. Eligibility constraints
such as travel, location, licensing, and work authorization remain requirements even when an
employer places them under Responsibilities. Preferred items never become minimums.
Coverage validation ensures explicit years, credentials, languages, and authorization
signals survive normalization; EEO boilerplate and incidental location words are excluded.
Every explicit minimum/required atom is retained. Preferred atoms remain in a separate lane;
payload size is controlled by requirement-aware batching rather than truncating a JD.

Scoring receives atoms rather than a front-truncated JD summary. For every injected scoring
requirement, the model must return coverage (`met|partial|unmet|unknown`), confidence,
direct/adjacent months, evidence IDs, and a concise reason. Exact ID coverage is validated.
The model interprets semantic function equivalence; deterministic post-processing enforces:

- unmet eligibility or central minimum: `role_fit <= 2`, `likelihood <= 2`, priority low
- unknown eligibility or central minimum: `role_fit <= 3`, `likelihood <= 3`
- supporting or preferred gaps: no hard cap

Company brand, compensation, and growth cannot offset a central minimum gap.

## Capability ledger

Profile intake writes `profiles/<person>/capability-ledger.json` beside the candidate profile
and evidence map. A dedicated cached builder also backfills or refreshes only this artifact
when existing canonical profile evidence changes. Each experience episode records function,
`direct|adjacent|exposure`,
dates, evidence IDs, and scope. The scoring contract forbids combining adjacent exposure
with direct functional tenure or double-counting overlapping months. Legacy profiles trigger
this bounded build before scoring rather than rewriting the whole profile.

Scoring batches are limited by both job count and total required atoms. Runner-derived
criteria are not requested from the model. An invalid row is repaired alone with its exact
expected criterion, policy, and requirement IDs. Each current rating carries a dependency
fingerprint over the JD, candidate ledger, policies, config, and scoring contract. Only stale
or new rows are evaluated. Valid current rows commit atomically; unresolved rows keep their
prior database score and are recorded in `strategy/score-freshness.json`.

## Progressive universe

`opportunity-plan` is an internal model workflow, not a public command. The Universe Manager
owns `seed_ready`, `expanding`, `ready`, `partial`, and stale-revision behavior.

On create/update, the server atomically saves preferences and immediately materializes exact
named employers. Descriptors remain pending and are never searched as literal companies.
The request returns without waiting.

A bounded worker pool creates one proposal per exact seed or descriptor. Each proposal
preserves the input and preference revision and contains an inferred archetype, relationship
typed employer suggestions, location/market rationale, confidence, exclusions, and an
omissions review. No peer company names are hardcoded. The single coordinator rejects stale
revisions, preserves all explicit seeds, canonicalizes/deduplicates names, and atomically
merges accepted proposals.

Search can run while expansion is active. If the user searched before expansion completed,
the coordinator queues a deterministic refresh over the latest complete universe. Search
never invokes Score; scoring is an explicit user command over already captured roles.
Re-enumeration intentionally keeps source-plan, coverage, and lifecycle artifacts coherent;
database writes remain non-destructive upserts.

UI behavior:

- no public `opportunity-plan` button
- Search and Score are separate controls; Search never invokes a model score call
- preference save waits only for the local atomic commit, then closes
- the project badge and preference copy distinguish immediately searchable seeds from
  background employer expansion
- background refresh reserves the same atomic run slot as user commands

## Acceptance evidence

- Meta-style responsibilities before Minimum Qualifications retain both 12+ year Product
  requirements and BA/BS without coverage issues.
- Long company introductions and EEO citizenship language do not become requirements.
- Eligibility constraints under Responsibilities remain scoring requirements.
- Unmet central minimums cap fit and likelihood deterministically.
- Explicit seeds are available before expansion; stale proposals cannot merge.
- One coordinator deduplicates proposals without losing explicit seeds.
- Release tests cover deterministic search, score finalization, privacy, skill packaging,
  normalization, enforcement, and progressive-universe state.
