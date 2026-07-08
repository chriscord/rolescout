<div align="center">

# ☕ RoleScout

**A local-first AI tool for job-search research and application preparation.**

[![version](https://img.shields.io/badge/version-0.1.0-blueviolet)]()
[![python](https://img.shields.io/badge/python-3.10%2B-blue)]()
[![local-first](https://img.shields.io/badge/local--first-career%20data-success)]()
[![license](https://img.shields.io/badge/license-MIT-green)]()

English | [한국어](README.ko.md) | [日本語](README.ja.md) | [繁體中文](README.zh-Hant.md)

</div>

---

## Overview

RoleScout helps people preparing for a career move save time on job-search research and application preparation.

RoleScout does not run a hosted backend or collect your career data on a RoleScout server. Your resume, target companies, project store, generated materials, and tracker data are kept on your local device. Live model runs are executed through a local CLI that you authenticate yourself, with Codex as the default.

Enter target locations, example companies, and target level. RoleScout then researches relevant openings, organizes and summarizes them, scores fit, and helps you decide which positions are worth preparing for first.

## How To Install

Requirements:

- Git
- Python 3.10 or newer
- Node.js/npm for installing the Codex CLI
- A ChatGPT/Codex account for live model runs

Connect the default model CLI:

```bash
npm install -g @openai/codex
codex login
```

macOS or Linux:

```bash
git clone https://github.com/chriscord/rolescout
cd rolescout
./tools/setup.sh
source .venv/bin/activate
```

Windows PowerShell:

```powershell
git clone https://github.com/chriscord/rolescout
cd rolescout
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip setuptools
python -m pip install -e ".[xlsx]"
```

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

Optional local CLI connection:

```bash
rolescout run search \
  --provider cli \
  --llm-name glm \
  --llm-cmd 'your-agent run --model {model} --effort {effort}'
```

`{prompt}` is replaced with the run prompt. If omitted, the prompt is sent through stdin. `{root}`, `{project}`, `{model}`, and `{effort}` are also available. `{model}` and `{effort}` come from the model profile file; adjust the template for the CLI you use.

## Verify Installation

```bash
rolescout --version
```

Expected output:

```text
rolescout 0.1.0
```

Then run:

```bash
rolescout doctor
```

## How To Use

```bash
rolescout web
```

The browser opens automatically at `http://127.0.0.1:8787`. The interface is loopback-only and is not hosted.

1. **Profile — create this first.** Add your name, LinkedIn URL, and resume. Supported resume formats: **PDF, DOCX, Markdown (`.md`), or `.txt`** (also `.doc` / `.html`). Add supporting materials when useful. Research, scoring, and preparation all build on this profile.

> **Standing instructions (optional, recommended).** Free-text guidance in your Profile that RoleScout injects into every run: your priorities, constraints, and what to emphasize. Approval boundaries always win over it; RoleScout never submits or sends anything on your behalf. Use it for preferences that are not obvious from the resume, such as gaps to address directly, experiences to emphasize, or relocation constraints.

2. **Projects.** Create or select a project. Treat one project as one job-search and preparation session. Set the session preferences freely: example companies, target role, level, target location, compensation range, exclusions, and any other constraints that should guide the search.

3. **Search.** Use the chat session panel on the right to choose `search`, then click **Run**. This can take time because RoleScout checks many ATS and company-career sources. When the run finishes, the Jobs tab contains the researched job list. The first search run also scores fit against the current resume/profile. Star positions on the left side of the job list to register them as **focused** positions.

> **Important.** Prep commands require at least one focused position. This is intentional: they prepare strategy, resume, LinkedIn, and interview materials for positions you have chosen to pursue.

4. **Prep.** After selecting at least one focused position, choose `prep`, then click **Run** to run `prep-strategy`, `prep-resume`, `prep-linkedin`, and `prep-interview` together. You can also run those commands one by one. Results appear under the Prep tab: **Strategy**, **Resume**, **LinkedIn**, and **Interview**.

**Strategy.** Groups relevant positions, builds the overall application strategy, and explains priorities. It identifies strengths, weaknesses, and the preparation path for the focused set.

**Resume.** Generates targeted resume drafts by job group.

**LinkedIn.** Reviews the current LinkedIn profile and shows recommended changes as current → to-be updates.

**Interview.** Analyzes the resume and target-position JDs to prepare likely questions and answer plans, a resume-based story bank, recent company/position news, and an industry/company glossary.

5. **Apply.** Choose `apply`, then click **Run**. RoleScout creates tracker rows in the Applications tab for focused positions and generates application instructions for each position. For safety, it does **not** auto-apply. After you submit an application yourself, update the tracker status manually; the Jobs list reflects that status automatically.

### Note

Keep the terminal open while the web interface is running. The web interface is a local companion to the terminal process.

### CLI

Every CLI command uses the active profile and project unless you pass `--project <code>`.

```bash
rolescout init --person you --focus ai-product --locations "San Francisco"
rolescout run search
rolescout run score
rolescout run prep
rolescout run prep-strategy
rolescout run prep-resume
rolescout run prep-linkedin
rolescout run prep-interview
rolescout run apply
```

| Command | Expected outcome |
|---|---|
| `rolescout init --person you --focus ai-product --locations "San Francisco"` | Creates or activates a profile/project pair. Use `--companies`, `--role`, `--level`, `--comp-range`, and `--negatives` to set project preferences from the command line. |
| `rolescout run search` | Builds the opportunity thesis, searches relevant sources, writes the Jobs list, and runs scoring once after the first search. |
| `rolescout run score` | Recomputes fit and priority for the current Jobs list using the active project preferences and scoring model. |
| `rolescout run prep` | Runs strategy, resume, LinkedIn, and interview preparation for focused positions. |
| `rolescout run prep-strategy` | Produces the grouped application strategy and priority plan only. |
| `rolescout run prep-resume` | Produces targeted resume drafts for the focused job groups. |
| `rolescout run prep-linkedin` | Produces LinkedIn current → to-be recommendations. |
| `rolescout run prep-interview` | Produces interview packs and the story bank for focused positions. |
| `rolescout run apply` | Creates application instructions and tracker rows for focused positions; no automatic submission. |

Switch projects with `rolescout init --activate <code>`, or run one command against a specific project with `--project <code>`.

## Model Settings

RoleScout does not inherit your Codex CLI default model or reasoning effort. For Codex runs, RoleScout passes explicit settings per workflow.

Default profiles:

| Workflow | Model | Reasoning effort |
|---|---|---|
| `search` | `gpt-5.5` | `medium` |
| `score` | `gpt-5.5` | `medium` |
| `prep-strategy` | `gpt-5.5` | `xhigh` |
| `prep`, `prep-resume`, `prep-linkedin`, `prep-interview` | `gpt-5.5` | `high` |
| `apply` | `gpt-5.5` | `medium` |

The editable file is created at `~/.rolescout/model-profiles.json` when `rolescout doctor` or a live Codex run checks model settings. Edit that file directly, or point RoleScout at another JSON file:

```bash
ROLESCOUT_MODEL_PROFILES=/path/to/model-profiles.json rolescout run search
```

For `--provider cli`, the same file drives the `{model}` and `{effort}` placeholders through its `external_cli` section.

One-run override:

```bash
ROLESCOUT_CODEX_MODEL=gpt-5.5 ROLESCOUT_CODEX_EFFORT=high rolescout run prep-resume
```

## Key Features

| Feature | Description |
|---|---|
| Job research | Multi-source discovery with canonical URLs, deduplication, job-description snapshots, and a research log. |
| Fit scoring | Weighted 0-100 prioritization with explicit criteria and separate human overrides. |
| Preparation materials | Target-group strategy, one-page DOCX resume variants, LinkedIn review, and positioning notes. |
| Interview preparation | Per-position packs: likely questions, answer plans, resume-based story bank, company/position news, glossary, and interview-specific preparation notes. |
| Application instructions | Local-only application steps per position: checked links, required materials, visible questions, sensitive-field guidance, and tracker rows. |
| Evidence discipline | Resume and profile recommendations must trace back to the local evidence map. |
| Local tracker | User-managed pipeline with status, next action, due date, and notes. |
| Local data model | Profiles, projects, generated files, SQLite stores, and telemetry remain on your device. |
| CLI flexibility | Codex by default, plus a generic adapter for other authenticated local agent CLIs. |

## Languages

GitHub does not provide a built-in README language switcher. This repository uses explicit links:

- [한국어](README.ko.md)
- [日本語](README.ja.md)
- [繁體中文](README.zh-Hant.md)

---

This is a non-profit project. It is not affiliated with recruiting firms, job-search agencies, employers, or job boards.

License: MIT.
