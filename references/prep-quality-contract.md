# Prep Quality Contract

Version: `rolescout-prep-quality-v1`

This document is the shared contract for `prep-strategy`, `prep-resume`,
`prep-linkedin`, story-bank generation, and `prep-interview`. Skill instructions,
model prompts, runner packets, validators, and web rendering must agree with it.

## Pipeline invariants

1. Prep is scoped only to focused jobs and the canonical person profile, evidence
   map, decision policy, current resume, and current LinkedIn capture.
2. The model performs semantic judgment and writing. Deterministic code owns input
   extraction, schema checks, path scope, persistence, and publish eligibility.
3. Artifact state is explicit: `generated -> repairing (when needed) -> validated -> published`. Generated
   output is retained under `runtime/runs/<run-id>/staging/<agent>/`; it is not
   current until its workflow validator passes and per-group atomic publish completes.
   Failed generation must not replace the last valid artifact shown in the web UI.
   A repairable intermediate failure is shown as in progress, not counted as a final
   "generated but not published" warning. Only the newest exhausted attempt is a final warning.
4. Independent groups publish independently. A failed resume group does not roll
   back successful groups or suppress LinkedIn. A failed story-bank refresh may use
   the last valid bank for interview. The overall `prep` result is `partial` when
   some independent phases/groups publish and others fail.
5. Internal evidence IDs may appear in audit artifacts, mappings, and interview
   references. They must not appear in user-facing resume copy.
6. Public web research is permitted for strategy and required for interview
   company research. It is read-only, must not authenticate, and must never transmit
   candidate-private packet data to a site.

## Strategy

`strategy/prep-strategy.md` must contain:

- an executive summary that synthesizes the candidate's strengths, material gaps,
  the recommended application play, and sequencing;
- evidence-backed strengths and weaknesses against the focused set;
- ranked focused positions and coherent job groups;
- group-specific positioning, resume emphasis, LinkedIn direction, next action,
  and confidence;
- portfolio strategy; and
- same-company multi-position advice supported by current public web research and
  cited URLs when a company has multiple focused roles.

The executive summary has no required paragraph count, sentence count, or exact
length. Validation checks presence and substance, not prose geometry.

The same run must refresh `strategy/target-priorities.md`, every active
`targets/job-groups/<slug>.md`, and the focused rows' `job_group` values. The web UI
must only combine artifacts from the current, validated group set.

Every focused assignment has a typed `pursue`, `conditional`, or `parked` disposition plus an
evidence-backed reason. Aggregate prep consumes this contract: resume and LinkedIn generation run
for pursue/conditional groups, interview generation runs for pursue roles, and parked groups do not
incur LLM calls unless the user explicitly runs a standalone workflow or overrides the decision.

Grouping uses the smallest credible set that can share a positioning and resume.
Score-time groups are hints rather than immutable inputs; a near-one-role-per-group
result is rejected as pathological fragmentation. Singleton groups require a real
positioning incompatibility, not merely a different job title.

## Resume

The runner extracts the latest user-provided resume once per run to
`resumes/baseline-extracted.md`; group agents consume it but cannot overwrite it.
Each pursue/conditional group must produce a target brief, baseline score, tailored draft,
reasons map, validation report, and a one-page DOCX after validation.

The runner packet provides the exact `rolescout-resume-group-artifacts-v1`
machine-readable contract. `target-brief.json` uses schema
`rolescout-resume-target-brief-v1`, one top-level `requirements` array, and only
`must`/`preferred` priorities. `target-brief.json` and `reasons.json` are typed JSON
artifact values, not JSON encoded inside a text field. A structural or content gate
failure receives up to two bounded repairs with exact validator feedback and the
previous generated set. A repair may return a path-keyed subset; the runner merges
that patch over the previous complete set before revalidation. Every attempt has a
collision-safe staging path. If all repairs fail, generated files remain in run
staging while other groups continue.

Repair feedback is cumulative across attempts. A later repair must retain the prior bullet/word
budget, rewrite-ratio, and coverage constraints while fixing the newest blocker; it may not trade
one previously fixed gate for another failure.

The tailored draft preserves the baseline resume's professional document shape and
section order unless a deliberate, documented improvement is needed. Validation-only
material such as evidence gaps, target labels, requirement IDs, and EV IDs belongs in
the target brief or validation report, never in the resume itself. The DOCX is rendered
only from a validator-clean draft.

Blocking validator errors are reported before non-blocking warnings and are passed to repair in
full. A one-page draft has at most 16 experience bullets and 360 experience-bullet words; length
guidance is 16–27 words per bullet with a hard 250-character cap. Non-blocking length warnings do
not trigger repair by themselves.

DOCX font sizes use WordprocessingML half-point units. The final gate opens the
document in an available pagination engine and requires exactly one page; dependency
presence alone is not a render pass. The web UI reports only the newest attempt for
each logical group, so a successful repair supersedes its earlier failure warning.

## LinkedIn source capture

A fresh browser capture is complete only after the profile surface has been scrolled
and expanded enough to capture:

- headline/top card;
- experience;
- skills; and
- education.

About may legitimately be absent and is recorded as absent rather than fabricated.
If the required profile sections cannot be captured before timeout, the runner keeps
the previous valid capture and reports a capture failure instead of publishing a
top-card-only snapshot.

## LinkedIn review

The scored sections are exactly:

1. Headline
2. About
3. Experience entries
4. Skills
5. Education

Experience has weight three; the other scored sections have weight one. Featured,
Activity, licenses, and certifications are not required score sections and must not
receive score rows. They may be mentioned as optional recommendations when relevant.

The review needs a score table, an `Overall score:` line that states Experience x3,
three highest-leverage fixes, and a `###` proposal section for each scored section.
Every proposal section contains a truthful fenced `Current` block and a fenced
`Proposed` block containing only the exact copy-ready LinkedIn content. Advisory
actions, rationale, and caveats belong in `Add`, `Change`, or `Guidance` blocks
outside `Proposed`. The web UI renders only `Current` and `Proposed` inside its
LinkedIn mockups; advisory blocks are rendered separately and must never become a
role card, skill tag, or education entry. `Part 1` and `Part 2` wrapper headings are
neither required nor forbidden. The web UI parses the semantic score/proposal
structure, not those wrapper labels.

## Story bank

The canonical JSON is an object with schema-compatible metadata and a non-empty
`entries` array. Every entry contains `id`, `title`, `source`, `situation`, `task`,
`action`, `result`, `best_for`, and `ev_refs`. Stable IDs use `ST-01`, `ST-02`, and
so on. The runner may normalize a legacy top-level list into the canonical object,
but it must reject semantically incomplete entries.

## Interview

Interview generation starts only after the story bank validates. Per-role packs use
the exact role JD, submitted or group resume, weaknesses from scoring and strategy,
and the canonical story bank. Public web research is mandatory for reported interview
questions, current company/product context, glossary, and recent news; every external
claim or reported question is cited.

The final pack contains, in order: Self Introduction, Job Requirements, Adversarial
Questions, The Whys, Behavioral Questions, Glossary, News, Questions to Ask, and
Sources. Missing stage output is a generation failure; the runner must not publish
placeholder sections as a completed pack.

## Acceptance gates

- Skill packages build and validate.
- Prompt contract tests assert the rich workflow contracts and web-research policy.
- LinkedIn capture tests reject top-card-only payloads.
- LinkedIn validator/UI tests pass without Part headings, reject Featured/Activity
  score rows, and require all five scored sections.
- Story-bank tests cover canonical objects, legacy-list normalization, stable IDs,
  and incomplete-entry rejection.
- Resume tests confirm current-source extraction, group write isolation, and no
  public EV/gap leakage.
- Prep orchestration tests confirm invalid artifacts are not promoted and interview
  assembly fails rather than shipping placeholders; valid sibling groups publish,
  failed output remains staged, and the aggregate run reports partial.
- The repository release check passes.
