"""Safety review for ``scripts/update-workstation.cmd``.

The updater is a Windows batch file that updates an existing clone in place.
Batch cannot be executed in this environment (no ``cmd.exe``), so the tests here
work in two ways:

* **static** -- parse the script and assert that the only Git commands it can
  run are ``rev-parse``, ``status``, ``fetch`` and ``merge --ff-only``; that no
  destructive verb appears anywhere it would be executed; that it refuses
  before changing anything on a missing Git, a wrong branch, a detached HEAD, or
  a dirty tracked tree; and that it never touches the local settings file;
* **semantic** -- replay the exact command sequence the script runs against real
  throwaway repositories, and assert the properties the script relies on: a
  fast-forward preserves untracked and ignored user files, and a dirty tree or a
  diverged remote aborts without changing HEAD or the working tree.

The semantic half uses ``git`` and ``subprocess`` in the *test* suite only; the
first-party safety rule that forbids shelling out from ``axiom_forex/`` is
untouched.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "update-workstation.cmd"
START_SCRIPT = REPO_ROOT / "scripts" / "start-workstation.cmd"
EXPECTED_BRANCH = "arena/f67c2859-axiom-forex"

#: Git subcommands the updater is allowed to execute.
ALLOWED_GIT_SUBCOMMANDS = {"rev-parse", "status", "fetch", "merge"}

#: Verbs that must never be executed by the updater.
FORBIDDEN_EXECUTED = (
    "reset",
    "clean",
    "checkout",
    "stash",
    "rebase",
    "pull",
    "push",
    "clone",
    "submodule",
    "rm ",
    "del ",
    "rd ",
    "rmdir",
    "move ",
    "copy ",
    "xcopy",
    "robocopy",
    "ren ",
    "rename",
    "format",
    "takeown",
    "icacls",
)


def script_text() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def executed_lines(text: str | None = None) -> list[str]:
    """Lines the batch interpreter would actually run as external commands.

    Includes commands hidden inside ``for /f ... in ('command')`` substitutions,
    because that is how the script reads the branch and the working-tree status.
    """

    commands: list[str] = []
    for raw in (text or script_text()).splitlines():
        line = raw.strip()
        if not line or line.lower().startswith(("rem", "::")):
            continue
        lowered = line.lower()
        if lowered.startswith(
            (
                "echo",
                "set ",
                "setlocal",
                "endlocal",
                "for ",
                "if ",
                "pause",
                "exit",
                "cd ",
                "pushd",
                "popd",
                "@echo",
                ")",
                "(",
            )
        ):
            if lowered.startswith("for "):
                commands.extend(_substitutions(line))
            continue
        commands.append(line)
    return commands


def _substitutions(line: str) -> list[str]:
    """Commands inside ``for /f ... in ('...')``."""

    found: list[str] = []
    for match in re.finditer(r"in \('([^']*)'\)", line):
        command = match.group(1)
        command = command.replace("2^>nul", "").replace("2>nul", "").strip()
        if command:
            found.append(command)
    return found


def script_lines() -> list[str]:
    return [line.strip() for line in script_text().splitlines()]


def first_line_index(predicate) -> int:
    for index, line in enumerate(script_lines()):
        if predicate(line.lower()):
            return index
    raise AssertionError("line not found")


def git_commands(text: str | None = None) -> list[str]:
    return [line for line in executed_lines(text) if line.lower().startswith("git ")]


def git_subcommand(line: str) -> str:
    parts = line.split()
    return parts[1] if len(parts) > 1 else ""


# ---------------------------------------------------------------------------
# static review
# ---------------------------------------------------------------------------


def test_the_updater_exists_and_documents_the_branch_it_tracks():
    assert SCRIPT.exists()
    text = script_text()
    assert EXPECTED_BRANCH in text
    assert "close the running workstation" in text.lower()


def test_only_rev_parse_status_fetch_and_merge_are_executed():
    subcommands = {git_subcommand(line) for line in git_commands()}
    assert subcommands, "the updater should call git"
    assert subcommands <= ALLOWED_GIT_SUBCOMMANDS, subcommands - ALLOWED_GIT_SUBCOMMANDS


def test_the_update_is_fast_forward_only():
    merges = [line for line in git_commands() if git_subcommand(line) == "merge"]
    assert merges, "the updater must fast-forward"
    for line in merges:
        assert "--ff-only" in line, line
        assert " --force" not in line and " -f" not in line, line


def test_fetch_never_prunes_or_forces():
    fetches = [line for line in git_commands() if git_subcommand(line) == "fetch"]
    assert fetches
    for line in fetches:
        assert "--prune" not in line, line
        assert "--force" not in line and " -f" not in line, line


def test_no_destructive_command_is_ever_executed():
    for line in executed_lines():
        lowered = f" {line.lower()} "
        for verb in FORBIDDEN_EXECUTED:
            assert f" {verb}" not in lowered, f"destructive command reachable: {line}"
        for verb in ("reset", "clean", "stash", "checkout", "clone"):
            assert verb not in lowered, f"destructive verb reachable: {line}"


def test_the_dirty_check_ignores_untracked_files_but_sees_tracked_ones():
    status_lines = [line for line in git_commands() if git_subcommand(line) == "status"]
    assert status_lines
    assert any("--porcelain" in line for line in status_lines)
    assert any("--untracked-files=no" in line for line in status_lines)


def test_it_refuses_before_touching_anything():
    text = script_text().lower()
    # Preconditions, in the order the script checks them.
    assert "where git" in text
    assert "rev-parse --is-inside-work-tree" in text
    assert "rev-parse --abbrev-ref head" in text
    assert 'if /i "%current_branch%"=="head" goto :detached' in text
    assert "goto :wrong_branch" in text
    assert "goto :dirty" in text
    # Each refusal exits non-zero and says nothing was changed.
    for label in (
        ":no_git",
        ":no_repo",
        ":detached",
        ":wrong_branch",
        ":dirty",
        ":fetch_failed",
        ":no_remote_branch",
        ":not_fast_forward",
        ":no_venv",
        ":pip_failed",
    ):
        assert label in text, label
    assert text.count("nothing was changed") >= 7
    assert text.count("exit /b 1") >= 9
    assert "exit /b 0" in text


def test_refusals_happen_before_the_fetch():
    fetch_at = first_line_index(lambda line: line.startswith("git fetch"))
    for guard in ("goto :no_git", "goto :no_repo", "goto :wrong_branch", "goto :dirty"):
        guard_at = first_line_index(lambda line, g=guard: g in line)
        assert guard_at < fetch_at, guard


def test_the_local_settings_file_is_never_written_moved_or_deleted():
    for line in executed_lines():
        assert "mt5_settings" not in line.lower(), line
    text = script_text()
    # It is named only to promise that it is preserved.
    assert "mt5_settings.local.json" in text
    assert "untouched" in text or "left as they" in text


def test_it_refreshes_the_existing_venv_without_recreating_it():
    text = script_text()
    lines = executed_lines()
    venv_calls = [line for line in lines if "%VENV_PY%" in line]
    assert venv_calls, "the updater should refresh .venv"
    for line in venv_calls:
        assert "-m pip install -e ." in line, line
        assert "--no-deps" in line, line
    # The interpreter is the one already in .venv; it is never created or removed.
    assert 'VENV_PY=%CD%\\.venv\\Scripts\\python.exe' in text
    executed = "\n".join(line.lower() for line in lines)
    for forbidden in (
        "python -m venv",
        "pip install --upgrade",
        "pip install metatrader5",
        "pip uninstall",
        "rmdir",
        "rd .venv",
    ):
        assert forbidden not in executed, forbidden
    assert 'if not exist "%venv_py%"' in text.lower()


def test_it_asks_the_user_to_close_the_workstation_first():
    text = script_text().lower()
    assert "close the running workstation" in text
    assert "ctrl+c" in text
    # And the reminder to relaunch afterwards.
    assert "start-workstation.cmd" in text


def test_it_never_starts_the_workstation_itself():
    for line in executed_lines():
        assert "workstation.launch" not in line.lower(), line
        assert "start-workstation.cmd" not in line.lower() or "echo" in line.lower()


def test_the_guidance_it_prints_is_itself_non_destructive():
    """"git switch" and "git clone" appear only inside echo text, never executed."""

    text = script_text().lower()
    for guidance in ("git switch", "git clone --branch", "git remote -v"):
        assert guidance in text, guidance
        for line in executed_lines():
            assert guidance not in line.lower(), line


def test_the_companion_start_script_is_still_observe_only():
    start = START_SCRIPT.read_text(encoding="utf-8").lower()
    assert "workstation.launch" in start
    assert "--simulate" in start
    assert "never asks for a password" in start


# ---------------------------------------------------------------------------
# semantic review: replay the script's git sequence on throwaway repositories
# ---------------------------------------------------------------------------


def git(repo: Path, *args: str, expect: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        env={
            "GIT_AUTHOR_NAME": "test",
            "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "test",
            "GIT_COMMITTER_EMAIL": "test@example.invalid",
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": str(repo.parent),
            "GIT_CONFIG_NOSYSTEM": "1",
        },
    )
    if expect and result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {result.stderr}")
    return result


@pytest.fixture()
def updatable_clone(tmp_path: Path):
    """`origin` with a feature branch, plus a clone carrying user files."""

    origin = tmp_path / "origin.git"
    origin.mkdir()
    git(origin, "init", "--quiet", "--bare")

    seed = tmp_path / "seed"
    seed.mkdir()
    git(seed, "init", "--quiet", "-b", "main")
    (seed / "tracked.txt").write_text("v1\n", encoding="utf-8")
    (seed / "docs.example").write_text("readme\n", encoding="utf-8")
    git(seed, "add", "tracked.txt", "docs.example")
    git(seed, "commit", "--quiet", "-m", "first")
    git(seed, "push", "--quiet", str(origin), "HEAD:refs/heads/" + EXPECTED_BRANCH)

    clone = tmp_path / "clone"
    git(tmp_path, "clone", "--quiet", "--branch", EXPECTED_BRANCH, str(origin), str(clone))

    # User-owned state that the update must preserve.
    (clone / "mt5_settings.local.json").write_text('{"server": "Broker Name - Live"}\n', encoding="utf-8")
    (clone / ".venv" / "Scripts").mkdir(parents=True)
    (clone / ".venv" / "Scripts" / "python.exe").write_text("stub\n", encoding="utf-8")
    (clone / "notes.md").write_text("my notes\n", encoding="utf-8")
    git(clone, "config", "user.name", "test")
    git(clone, "config", "user.email", "test@example.invalid")

    return {"origin": origin, "seed": seed, "clone": clone}


def push_new_commit(seed: Path, origin: Path, message: str = "second") -> None:
    (seed / "tracked.txt").write_text((seed / "tracked.txt").read_text() + "v2\n", encoding="utf-8")
    git(seed, "commit", "--quiet", "-am", message)
    git(seed, "push", "--quiet", str(origin), "HEAD:refs/heads/" + EXPECTED_BRANCH)


def replay_fast_forward(clone: Path, origin: Path) -> subprocess.CompletedProcess:
    """The exact sequence in the script: fetch, then merge --ff-only."""

    fetch = subprocess.run(
        ["git", "fetch", "origin", EXPECTED_BRANCH],
        cwd=clone,
        capture_output=True,
        text=True,
    )
    assert fetch.returncode == 0, fetch.stderr
    return subprocess.run(
        ["git", "merge", "--ff-only", f"origin/{EXPECTED_BRANCH}"],
        cwd=clone,
        capture_output=True,
        text=True,
    )


def head(repo: Path) -> str:
    return git(repo, "rev-parse", "HEAD").stdout.strip()


def test_a_clean_clone_fast_forwards_and_keeps_every_user_file(updatable_clone):
    clone, origin, seed = updatable_clone["clone"], updatable_clone["origin"], updatable_clone["seed"]
    before = head(clone)

    # The script's dirty check sees nothing: only untracked/ignored files exist.
    status = git(clone, "status", "--porcelain", "--untracked-files=no")
    assert status.stdout.strip() == ""

    push_new_commit(seed, origin)
    result = replay_fast_forward(clone, origin)

    assert result.returncode == 0, result.stderr
    assert head(clone) != before
    assert (clone / "tracked.txt").read_text() == "v1\nv2\n"
    # User state untouched.
    assert (clone / "mt5_settings.local.json").read_text() == '{"server": "Broker Name - Live"}\n'
    assert (clone / ".venv" / "Scripts" / "python.exe").read_text() == "stub\n"
    assert (clone / "notes.md").read_text() == "my notes\n"


def test_a_dirty_tracked_file_stops_the_update_and_changes_nothing(updatable_clone):
    clone, origin, seed = updatable_clone["clone"], updatable_clone["origin"], updatable_clone["seed"]
    (clone / "tracked.txt").write_text("my local edit\n", encoding="utf-8")

    # The guard the script uses before fetching.
    status = git(clone, "status", "--porcelain", "--untracked-files=no")
    assert status.stdout.strip(), "a tracked edit must be visible to the guard"

    push_new_commit(seed, origin)
    before = head(clone)
    result = replay_fast_forward(clone, origin)

    assert result.returncode != 0
    assert head(clone) == before
    assert (clone / "tracked.txt").read_text() == "my local edit\n"


def test_a_diverged_remote_never_rewrites_the_branch(updatable_clone):
    clone, origin, seed = updatable_clone["clone"], updatable_clone["origin"], updatable_clone["seed"]
    first = head(clone)

    # The clone moves forward legitimately...
    push_new_commit(seed, origin)
    assert replay_fast_forward(clone, origin).returncode == 0
    before = head(clone)
    assert before != first
    assert (clone / "tracked.txt").read_text() == "v1\nv2\n"

    # ...then the remote history is rewritten without this commit.
    git(seed, "reset", "--quiet", "--hard", first)
    (seed / "tracked.txt").write_text("rewritten\n", encoding="utf-8")
    git(seed, "commit", "--quiet", "-am", "rewritten")
    git(seed, "push", "--quiet", "--force", str(origin), "HEAD:refs/heads/" + EXPECTED_BRANCH)

    result = replay_fast_forward(clone, origin)

    assert result.returncode != 0
    assert "fast-forward" in (result.stderr + result.stdout).lower()
    assert head(clone) == before
    assert (clone / "tracked.txt").read_text() == "v1\nv2\n"


def test_the_branch_guard_reads_the_branch_the_script_compares(updatable_clone):
    clone = updatable_clone["clone"]
    branch = git(clone, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    assert branch == EXPECTED_BRANCH

    git(clone, "switch", "--quiet", "-c", "some-other-branch")
    other = git(clone, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    assert other != EXPECTED_BRANCH  # the script refuses here
