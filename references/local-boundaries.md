# Local-only boundaries

Public RoleScout produces local research, prioritization, preparation, and tracker
artifacts. It does not execute recruiting actions outside the user's machine.

## Not supported in the public runtime

- Submitting job applications.
- Sending messages or emails.
- Saving LinkedIn edits.
- Uploading files to external sites.
- Scheduling interviews or calendar events.
- Creating or modifying external accounts.
- Accepting terms on behalf of the user.
- Sharing resume, LinkedIn, compensation, visa, contact, or application-status
  data with external systems.

If a task appears to require one of these actions, produce local instructions,
draft text, or tracker notes only.

## Safe local work

- Reading and writing files under `profiles/` and `projects/`.
- Updating the local SQLite store through `scripts/upsert_rows.py`.
- Producing strategy, resume, LinkedIn-review, and tracker artifacts.
- Summarizing public job-posting facts gathered during research.

All material claims must still be truthful and evidence-backed.
