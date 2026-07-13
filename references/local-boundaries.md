# Local output and provider-processing boundaries

Public RoleNavi produces local research, prioritization, preparation, and tracker
artifacts. It does not execute recruiting actions outside the user's machine.

## Not supported in the public runtime

- Submitting job applications.
- Sending messages or emails.
- Saving LinkedIn edits.
- Uploading files to external sites.
- Scheduling interviews or calendar events.
- Creating or modifying external accounts.
- Accepting terms on behalf of the user.
- Sending contacts, application state, compensation history, work-authorization
  data, LinkedIn profile URLs, or unrelated private notes to a model by default.

If a task appears to require one of these actions, produce local instructions,
draft text, or tracker notes only.

## Live model processing

RoleNavi stores source files and generated artifacts locally. A live synthesis
run still sends a minimized workflow packet to the selected model provider.
Before the first live run, RoleNavi displays that provider disclosure and it
prints the workflow's data classes on every run.

Target compensation (`comp_range`) is a search preference and may be sent for
relevant search/strategy decisions. Resume text, captured LinkedIn content, and
candidate evidence are sent only to workflows that require those sources.
LinkedIn URLs, contacts, pipeline state, compensation history, work authorization,
and unrelated private notes are excluded by default by the prompt gateway.

## Local writes

- The runner, not a model process, reads and writes approved files under
  `profiles/` and `projects/`.
- Updating the local SQLite store through `scripts/upsert_rows.py`.
- Producing strategy, resume, LinkedIn-review, and tracker artifacts.
- Summarizing public job-posting facts gathered during research.

All material claims must still be truthful and evidence-backed.
