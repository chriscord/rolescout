"""`rolenavi doctor` — environment and install health check (product-spec §8).

Checks: python version, repo integrity, SQLite behavior on this filesystem
(the synced-folder journal issue), LLM CLI availability (mock vs live), skill
package state, active project integrity, and telemetry home.

Exit 0 = healthy (warnings allowed). Exit 1 = a real defect.
"""

from __future__ import annotations

import sqlite3
import sys
import uuid
import zipfile
from pathlib import Path

from . import PRODUCT_NAME, __version__
from .paths import RoleNaviError, active_project_dir, home_dir, repo_root

OK, WARN, FAIL = "ok", "warn", "fail"


def _sqlite_probe(directory: Path) -> tuple[str, str]:
    """Can we write a SQLite DB here? Detects the synced-folder locking issue
    that store_io mitigates with journal_mode=MEMORY."""
    probe = directory / f".doctor-probe-{uuid.uuid4().hex[:8]}.db"
    try:
        con = sqlite3.connect(probe)
        try:
            con.execute("PRAGMA journal_mode = MEMORY")
            con.execute("CREATE TABLE t (x)")
            con.execute("INSERT INTO t VALUES (1)")
            con.commit()
        finally:
            con.close()
        return OK, "SQLite writable (journal_mode=MEMORY mitigation, matches store_io)"
    except sqlite3.Error as e:
        return FAIL, (f"SQLite write failed here: {e} - synced/cloud folders can block "
                      "SQLite locking; move the repo to a plain local folder")
    finally:
        try:
            probe.unlink(missing_ok=True)
        except OSError:
            pass  # some mounts block unlink; a stray probe file is harmless


def run_checks(root: Path | None = None) -> list[tuple[str, str, str]]:
    """Returns [(status, check, detail)]."""
    results: list[tuple[str, str, str]] = []

    # 1. python
    v = sys.version_info
    if v >= (3, 11):
        results.append((OK, "python", f"{v.major}.{v.minor}.{v.micro}"))
    elif v >= (3, 10):
        results.append((WARN, "python", f"{v.major}.{v.minor} works; 3.11+ recommended"))
    else:
        results.append((FAIL, "python", f"{v.major}.{v.minor} < 3.10 - unsupported"))
        return results

    # 2. repo integrity
    try:
        root = root or repo_root()
    except RoleNaviError as e:
        results.append((FAIL, "repo", str(e)))
        return results
    results.append((OK, "repo", str(root)))
    missing = [m for m in ("scripts", "references", ".agents/skills") if not (root / m).is_dir()]
    if missing:
        results.append((FAIL, "repo assets", f"missing {missing}"))
    else:
        n_skills = len(list((root / ".agents" / "skills").glob("*/SKILL.md")))
        status = OK if n_skills >= 9 else WARN
        results.append((status, "skills", f"{n_skills} skill(s) in .agents/skills/"))

    # 3. SQLite / filesystem behavior (repo side, where project stores live)
    data_dir = root / "projects"
    status, detail = _sqlite_probe(data_dir if data_dir.is_dir() else root)
    results.append((status, "sqlite (repo)", detail))

    # 4. LLM backend (codex subscription / external CLI / mock)
    from .llm import provider_choice
    try:
        choice = provider_choice()
    except Exception as e:
        choice = "mock"
        results.append((FAIL, "llm backend", str(e)))
    if choice == "codex":
        from .llm import codex as codex_mod
        status = codex_mod.logged_in()
        if status is False:
            results.append((WARN, "llm backend",
                            "codex CLI found but not signed in - run `codex login` "
                            "(ChatGPT subscription); until then CLI runs use mock mode"))
        else:
            note = "" if status else " (login state unknown - old CLI)"
            results.append((OK, "llm backend",
                            f"codex CLI (ChatGPT subscription){note} - live runs enabled"))
        provider = object.__new__(codex_mod.CodexProvider)
        provider.exe = codex_mod.binary() or "codex"
        command = provider._exec_command("score", cwd=str(home_dir()), profile={
            "model": "contract-check", "effort": "low", "settings_file": "doctor",
        })
        if "--skip-git-repo-check" not in command:
            results.append((FAIL, "llm staging contract",
                            "isolated non-git staging lacks --skip-git-repo-check"))
        else:
            results.append((OK, "llm staging contract",
                            "isolated read-only staging accepts non-git working directories"))
    elif choice == "cli":
        from .llm import external_cli
        exe = external_cli.binary()
        if exe:
            results.append((OK, "llm backend",
                            f"external CLI ({external_cli._cli_name()}) - live runs enabled"))
        else:
            results.append((FAIL, "llm backend",
                            "ROLENAVI_PROVIDER=cli but ROLENAVI_LLM_CMD is missing "
                            "or executable was not found"))
    else:
        results.append((WARN, "llm backend",
                        "no codex CLI and no ROLENAVI_LLM_CMD - runs use mock mode; "
                        "Codex path: `npm i -g @openai/codex && codex login`"))
    try:
        from .llm import model_profiles
        path = model_profiles.model_profile_path()
        search_profile = model_profiles.codex_profile_for("search")
        prep_profile = model_profiles.codex_profile_for("prep")
        strategy_profile = model_profiles.codex_profile_for("prep-strategy")
        results.append((OK, "model profiles",
                        f"{path} (search={search_profile['model']}/"
                        f"{search_profile['effort']}, prep={prep_profile['model']}/"
                        f"{prep_profile['effort']}, prep-strategy="
                        f"{strategy_profile['model']}/{strategy_profile['effort']})"))
    except RoleNaviError as e:
        results.append((FAIL, "model profiles", str(e)))

    # 5. skill packages (plugin channel state)
    dist = root / "dist"
    stale, absent = [], []
    for d in sorted((root / ".agents" / "skills").glob("*/")):
        if not (d / "SKILL.md").exists():
            continue
        pkg = dist / f"{d.name}.skill"
        if not pkg.exists():
            absent.append(d.name)
            continue
        try:
            with zipfile.ZipFile(pkg) as z:
                names = [n for n in z.namelist() if n.endswith("SKILL.md")]
                packaged = z.read(names[0]).decode("utf-8") if names else ""
            source = (d / "SKILL.md").read_text(encoding="utf-8")
            if packaged.strip() != source.strip():
                stale.append(d.name)
        except (OSError, UnicodeDecodeError, zipfile.BadZipFile):
            stale.append(d.name)
    if absent or stale:
        results.append((FAIL, "skill packages",
                        f"absent: {absent or '-'}; stale: {stale or '-'} "
                        "(rebuild from the release source tree)"))
    else:
        results.append((OK, "skill packages", "dist/*.skill present and fresh"))

    # 6. active project integrity
    proj = active_project_dir(root)
    if proj is None:
        results.append((WARN, "project", "no active project - open the UI with `./start`"))
    else:
        results.append((OK, "project", proj.name))
        public_db = proj / "data" / "public-opportunities.db"
        private_db = proj / "private" / "pipeline.db"
        if public_db.exists() and private_db.exists():
            try:
                con = sqlite3.connect(public_db)
                try:
                    tables = {r[0] for r in con.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'")}
                finally:
                    con.close()
                private_con = sqlite3.connect(private_db)
                try:
                    private_tables = {r[0] for r in private_con.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'")}
                finally:
                    private_con.close()
                if "job_list" in tables and "tracker" in private_tables:
                    results.append((OK, "project store", "split public job + private pipeline stores present"))
                else:
                    results.append((FAIL, "project store", "split stores are missing job_list or tracker"))
            except sqlite3.Error as e:
                results.append((FAIL, "project store", f"unreadable: {e}"))
        else:
            results.append((WARN, "project store",
                            "split stores missing - run `python scripts/init_db.py` to migrate/create"))

    # 7. telemetry home
    try:
        h = home_dir()
        status, detail = _sqlite_probe(h)
        results.append((status, "telemetry home", f"{h} - {detail}" if status != OK else str(h)))
    except OSError as e:
        results.append((WARN, "telemetry home", f"cannot create: {e}"))

    return results


def main(args) -> int:
    print(f"{PRODUCT_NAME} doctor (v{__version__})")
    results = run_checks()
    width = max(len(c) for _, c, _ in results)
    fails = 0
    for status, check, detail in results:
        mark = {OK: "OK  ", WARN: "WARN", FAIL: "FAIL"}[status]
        print(f"  {mark}  {check:<{width}}  {detail}")
        fails += status == FAIL
    print(f"\n{'UNHEALTHY: ' + str(fails) + ' failure(s)' if fails else 'healthy'}"
          f" ({sum(1 for s, _, _ in results if s == WARN)} warning(s))")
    return 1 if fails else 0
