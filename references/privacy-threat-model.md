# Privacy Threat Model

RoleNavi is local-storage software with optional remote model processing. “Local”
describes storage and runner-owned output, not where a selected provider processes
an approved prompt packet.

## Protected assets

- resume and captured LinkedIn content;
- candidate profile/evidence and derived career artifacts;
- contacts, application state, compensation history, work authorization, and notes;
- provider credentials and unrelated process environment values;
- other people’s profiles and other search projects.

## Trust boundaries and controls

1. The privacy registry is deny-by-default per workflow. The prompt gateway removes
   unknown fields, local-only keys, contacts, LinkedIn URLs, phone/email values, and
   workflow-irrelevant source classes; it bounds packet sizes and fingerprints the
   final prompt.
2. Codex runs in a disposable neutral directory with read-only sandboxing and shell,
   unified exec, apps, web search, multi-agent, and history persistence disabled. The
   process receives an environment allowlist. Arbitrary external CLIs are developer-only.
3. Model output is untrusted typed data. The runner enforces schema, count/size limits,
   path traversal checks, workflow artifact allowlists, and store allowlists, then writes
   atomically. Direct provider write/action events are rejected.
4. Public opportunities and private pipeline state use different SQLite files and
   different explicit exports. Global telemetry is metrics-only; legacy raw telemetry
   is purged by schema migration.
5. Loopback web requests require an unguessable startup token, loopback Host and trusted
   Origin/Referer, security headers, and request-size limits.

## Residual risks

- Approved resume/profile text is processed under the selected provider’s account terms.
- A compromised local OS or provider binary can bypass application-level controls.
- Captured source documents may contain sensitive facts in unstructured prose; the
  gateway uses deterministic redaction but users should review source material.
- Legacy combined databases remain on disk after non-destructive migration until the
  user removes them after verifying the split stores.
- Encryption at rest is not provided; OS disk encryption and account isolation remain
  the user’s responsibility.

Use `rolenavi privacy audit`, `rolenavi clean --runtime`, and the dry-run-first
`rolenavi delete-person` command to inspect and enforce lifecycle boundaries.
