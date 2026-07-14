<div align="center">

# ☕ RoleNavi

**A local-first AI tool for job-search research and application preparation.**

<img src="assets/demo.gif" alt="RoleNavi demo" width="920">

[![version](https://img.shields.io/badge/version-0.1.0-blueviolet)]()
[![python](https://img.shields.io/badge/python-3.10%2B-blue)]()
[![local-first](https://img.shields.io/badge/local--first-career%20data-success)]()
[![license](https://img.shields.io/badge/license-MIT-green)]()

English | [한국어](README.ko.md) | [日本語](README.ja.md) | [繁體中文](README.zh-Hant.md)

</div>

---

## Overview

RoleNavi helps people preparing for a career move save time on job-search research and application preparation.

RoleNavi does not run a hosted backend or collect career data on a RoleNavi server. Source files, stores, and generated materials stay on your device. A live synthesis run still sends a minimized workflow packet to the selected model provider through a CLI you authenticate, with Codex as the default. Codex synthesis starts in a disposable staging directory with read-only sandboxing, shell/unified-exec/apps/web search disabled, no transcript history, and an allowlisted process environment. RoleNavi shows a provider notice before the first live run and reports the data classes used by each workflow. Contacts, application state, compensation history, work authorization, LinkedIn URLs, and unrelated private notes are excluded from model prompts by default; target compensation is a model-allowed search preference.

The enforceable boundaries and residual risks are documented in
[`references/privacy-threat-model.md`](references/privacy-threat-model.md).

Enter target locations, example companies, and target level. RoleNavi then researches relevant openings, organizes and summarizes them, scores fit, and helps you decide which positions are worth preparing for first.

## How To Install

RoleNavi is designed for an active ChatGPT/Codex subscription. Its default live
workflows use the locally authenticated Codex CLI, so connect the subscription
after installation with `npm install -g @openai/codex` and `codex login`.

### macOS

```bash
curl -fsSL https://raw.githubusercontent.com/chriscord/rolenavi/main/tools/install-macos.sh | bash
cd rolenavi
./start
```

### Linux

```bash
curl -fsSL https://raw.githubusercontent.com/chriscord/rolenavi/main/tools/install-linux.sh | bash
cd rolenavi
./start
```

### Windows PowerShell

```powershell
irm https://raw.githubusercontent.com/chriscord/rolenavi/main/tools/install-windows.ps1 | iex
cd rolenavi
.\start.cmd
```

Each installer creates `./rolenavi` in the directory where you run it. The
launcher manages the internal Python environment and opens the
local web UI, so users never need to activate `.venv`. Set
`ROLENAVI_INSTALL_DIR` before running the installer to choose another location.
Rerunning the same command safely updates a clean RoleNavi checkout and resumes
installation; it will not overwrite an unrelated directory or tracked changes.

The Unix commands below assume you are inside `rolenavi` and use `./start`.
On Windows, use `.\start.cmd` instead.

### Optional browser tooling for LinkedIn analysis

For automated LinkedIn profile analysis, install either
[Chrome DevTools MCP](https://github.com/ChromeDevTools/chrome-devtools-mcp) or
[Playwright](https://playwright.dev/docs/intro#installing-playwright). They are
recommended, not required, and enable browser-based capture of current profile
content when available.

Optional render QA for resume DOCX files:

RoleNavi can generate resume DOCX files without these tools. Install them only
if you want visual render checks for one-page layout verification. Without them,
RoleNavi records render QA as blocked and still runs structural DOCX checks.

```bash
# Python package used by the render checker
python -m pip install -e ".[render]"
# Or include the spreadsheet extra too:
python -m pip install -e ".[xlsx,render]"

# macOS
brew install libreoffice poppler

# Ubuntu/Debian
sudo apt update
sudo apt install libreoffice poppler-utils
```

```powershell
# Windows
python -m pip install -e ".[xlsx,render]"
winget install TheDocumentFoundation.LibreOffice
winget install oschwartz10612.Poppler
```

The optional render stack is: `pdf2image` (Python), LibreOffice/`soffice`, and
Poppler/`pdftoppm`.

If Python is too old:

```bash
# macOS with Homebrew
brew install python@3.12

# Ubuntu/Debian
sudo apt update
sudo apt install python3.12 python3.12-venv
```

```powershell
# Windows
winget install Python.Python.3.12
```

Optional external CLI connection (developer-only; RoleNavi cannot verify an arbitrary CLI's sandbox):

```bash
./start run search \
  --provider cli \
  --llm-name glm \
  --llm-cmd 'your-agent run --model {model} --effort {effort}'
```

The prompt is sent through stdin. `{root}` and `{project}` resolve to the disposable staging directory; `{model}` and `{effort}` come from the model profile file. A `{prompt}` argv placeholder is rejected by default because process listings can expose packet content.

Set `ROLENAVI_ENABLE_UNSANDBOXED_CLI=1` only after reviewing the external
provider's filesystem and tool isolation. The process starts in a disposable
staging directory with an allowlisted environment.

> [!WARNING]
> RoleNavi is tested with the Codex subscription connection. Other AI-agent
> integrations use the experimental external-CLI adapter and have not been
> tested or supported.

## Verify Installation

```bash
./start --version
```

Expected output:

```text
rolenavi 0.1.0
```

Then run:

```bash
./start doctor
```

## How To Use

```bash
cd rolenavi
./start
```

`./start` runs the installed equivalent of `rolenavi web` without activating
`.venv`. The browser opens automatically at `http://127.0.0.1:8787`. The
interface is loopback-only and is not hosted.

For default live AI workflows, first connect your ChatGPT/Codex subscription with `codex login`. RoleNavi invokes that local Codex CLI; it does not require an API key.

The workflow deliberately keeps people in control: **deterministic job search → agentic evaluation → choose focused positions → preparation → manual application**.

1. **Profile — create this first.** Add your name, LinkedIn URL, and resume. Supported resume formats: **PDF, DOCX, Markdown (`.md`), `.txt`, or HTML**. Add supporting materials when useful. Saving the profile or uploading a resume starts `profile-intake` in the background: deterministic extraction builds a bounded source packet, then typed model output is materialized as `candidate-profile.md` and `evidence-map.md`. A LinkedIn URL is a local pointer only; current LinkedIn evidence must come from the supported import/capture path.

   > [!NOTE]
   > **Standing instructions stay local by default.** Put model-shareable search preferences in the structured project target fields. Free-form profile instructions can contain private facts and are therefore not injected into live prompts by default.

2. **Projects.** Create or select a project. Treat one project as one job-search and preparation session. Set the session preferences freely: example companies, target role, level, target location, compensation range, exclusions, and any other constraints that should guide the search.

3. **Plan → capture → evaluate → finalize.** `opportunity-plan` is an optional bounded model phase that writes a validated company universe. `search` is deterministic capture from that universe (or declared seeds in seed-only mode), including URL/JD normalization and persistence. `score` sends compact captured-job batches for semantic evaluation, then deterministic finalization applies weights and writes scores. This separates deterministic job search from agentic evaluation: agents evaluate captured evidence, but do not collect postings or decide whether to submit applications. Star positions to register them as **focused** positions.

   > [!IMPORTANT]
   > Prep commands require at least one focused position. This is intentional: they prepare strategy, resume, LinkedIn, and interview materials for positions you have chosen to pursue.

4. **Prep.** After selecting at least one focused position, choose `prep`, then click **Run** to run all four preparation workflows together. You can also run them one by one. Results appear under the matching Prep tabs:

   - **Strategy** (`prep-strategy`) — Groups relevant positions, builds the overall application strategy, explains priorities, and identifies strengths, weaknesses, and the preparation path for the focused set.
   - **Resume** (`prep-resume`) — Generates targeted resume drafts by job group.
   - **LinkedIn** (`prep-linkedin`) — Reviews the current LinkedIn profile and shows recommended changes as current → to-be updates.
   - **Interview** (`prep-interview`) — Analyzes the resume and target-position JDs to prepare likely questions and answer plans, a resume-based story bank, recent company/position news, and an industry/company glossary.

5. **Apply.** Choose `apply`, then click **Run**. RoleNavi creates tracker rows in the Applications tab for focused positions and generates application instructions for each position. For safety, it does **not** auto-apply. After you submit an application yourself, update the tracker status manually; the Jobs list reflects that status automatically.

> [!NOTE]
> Keep the terminal open while the web interface is running. The web interface is a local companion to the terminal process.

### CLI

Every CLI command uses the active profile and project unless you pass `--project <code>`.

```bash
./start init --person you --focus ai-product --locations "San Francisco"
./start run profile-intake --person you
./start run opportunity-plan
./start run search
./start run score
./start run prep
./start run prep-strategy
./start run prep-resume
./start run prep-linkedin
./start run prep-interview
./start run story-bank
./start run apply
./start export --public
./start privacy audit
```

| Command | Expected outcome |
|---|---|
| `./start init --person you --focus ai-product --locations "San Francisco"` | Creates or activates a profile/project pair. Use `--companies`, `--role`, `--level`, `--comp-range`, and `--negatives` to set project preferences from the command line. |
| `./start run profile-intake --person you` | Builds or refreshes `profiles/<person>/candidate-profile.md` and `profiles/<person>/evidence-map.md` from resume/materials and accepted LinkedIn current-source content. |
| `./start run opportunity-plan` | Optionally creates a bounded, typed company universe from model-allowed target preferences. |
| `./start run search` | Runs deterministic provider-first discovery, captures direct posting URLs/JD snapshots, writes the raw Jobs store, and builds the UI-visible Jobs view. It does not score by default. |
| `./start run score` | Rates every current UI-visible Jobs row through runner-built compact batches, then the runner recomputes weighted scores and writes `fit_score`/`priority` back to the Jobs view. |
| `./start run prep` | Runs strategy, resume, LinkedIn, and interview preparation for focused positions. |
| `./start run prep-strategy` | Produces the grouped application strategy and priority plan only. |
| `./start run prep-resume` | Produces targeted resume drafts for the focused job groups. |
| `./start run prep-linkedin` | Produces LinkedIn current → to-be recommendations. |
| `./start run prep-interview` | Produces interview packs and the story bank for focused positions. |
| `./start run story-bank` | Rebuilds the shared resume-derived story bank independently. |
| `./start run apply` | Creates application instructions and tracker rows for focused positions; no automatic submission. |
| `./start export --public` / `--private` | Creates an explicit sensitivity-separated export and revision manifest. |
| `./start privacy audit` | Reports local runtime/telemetry footprint without printing private contents. |
| `./start clean --runtime` | Prints a dry-run retention manifest; add `--apply` to delete it. |
| `./start delete-person --person <slug>` | Prints a dry-run profile/project deletion manifest; add `--apply` to delete. |

Switch projects with `./start init --activate <code>`, or run one command against a specific project with `--project <code>`.

## Model Settings

RoleNavi does not inherit your Codex CLI default model or reasoning effort. For Codex runs, RoleNavi passes explicit settings per workflow.

Default profiles:

| Workflow | Model | Reasoning effort |
|---|---|---|
| `opportunity-plan` | `gpt-5.5` | `medium` |
| `score` | `gpt-5.5` | `medium` |
| `profile-intake` | `gpt-5.5` | `high` |
| `prep-strategy` | `gpt-5.5` | `xhigh` |
| `prep`, `prep-resume`, `prep-linkedin`, `prep-interview`, `story-bank` | `gpt-5.5` | `high` |
| `apply` | `gpt-5.5` | `medium` |

`search` is deterministic by default, so it does not invoke a model unless optional auto-scoring or the legacy search path is explicitly enabled.

The editable file is created at `~/.rolenavi/model-profiles.json` when `./start doctor` or a live Codex run checks model settings. Edit that file directly, or point RoleNavi at another JSON file:

```bash
ROLENAVI_MODEL_PROFILES=/path/to/model-profiles.json ./start run search
```

For `--provider cli`, the same file drives the `{model}` and `{effort}` placeholders through its `external_cli` section.

One-run override:

```bash
ROLENAVI_CODEX_MODEL=gpt-5.5 ROLENAVI_CODEX_EFFORT=high ./start run prep-resume
```

## Key Features

| Feature | Description |
|---|---|
| Deterministic job search | Multi-source discovery with canonical URLs, deduplication, job-description snapshots, and a research log. |
| Agentic evaluation and deterministic finalization | Compact captured-job batches receive semantic evaluation; explicit weights, gates, and separate human overrides determine the final 0–100 priority. |
| Preparation materials | Target-group strategy, one-page DOCX resume variants, LinkedIn review, and positioning notes. |
| Interview preparation | Per-position packs: likely questions, answer plans, resume-based story bank, company/position news, glossary, and interview-specific preparation notes. |
| Application instructions | Local-only application steps per position: checked links, required materials, visible questions, sensitive-field guidance, and tracker rows. |
| Evidence-backed materials | Resume, LinkedIn, and interview claims trace back to the local evidence map. |
| Human-controlled workflow | Search → choose focused positions → prepare → submit manually. RoleNavi never submits an application for you. |
| Local tracker | User-managed pipeline with status, next action, due date, and notes. |
| Sensitivity-separated SQLite stores | Public job-posting facts live in `data/public-opportunities.db`; private application and pipeline state lives in `private/pipeline.db`. Exports are explicit and remain separated. |
| Local data model | Profiles, projects, generated files, SQLite stores, and telemetry remain on your device. Use the privacy audit and dry-run cleanup/deletion commands to inspect retention. |
| Defensive local operations | Atomic writes, path validation, size limits, and a deny-by-default privacy registry protect local artifacts and model packets. |
| CLI flexibility | Codex by default, plus a developer-only generic adapter for other authenticated local agent CLIs. |

## Languages

GitHub does not provide a built-in README language switcher. This repository uses explicit links:

- [한국어](README.ko.md)
- [日本語](README.ja.md)
- [繁體中文](README.zh-Hant.md)

---

This is a non-profit project. It is not affiliated with recruiting firms, job-search agencies, employers, or job boards.

License: MIT.
