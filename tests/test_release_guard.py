"""Tests for scripts/release_guard.py.

The guard is what stands between a pushed tag and a public GitHub Release, and
the only other way to exercise it is to push a tag. Everything it decides is
tested here instead: tag parsing, the version match, the changelog section and
the commit being on main, the last one against a real throwaway git repository.
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

TAG = "v-calibre-bridge-1.2.3"

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.invalid",
    # Keep the developer's own git settings (signing, hooks, templates) out.
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
}


def git(repo: pathlib.Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, **_GIT_ENV},
    )
    return result.stdout.strip()


def commit(repo: pathlib.Path, message: str) -> str:
    git(repo, "add", "-A")
    git(repo, "commit", "--allow-empty", "-m", message)
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path, make_release_tree):
    """A git repository shaped like this one, with main pushed to `origin`.

    main:    release ── later
                │
    feature:    └── stray

    `origin/main` points at `later`, so `release` and `later` are on main and
    `stray` is not.
    """
    root = make_release_tree(tmp_path / "repo")
    git(root, "init", "--quiet", "--initial-branch=main")
    release = commit(root, "release 1.2.3")
    git(root, "checkout", "--quiet", "-b", "feature")
    (root / "stray.txt").write_text("not on main\n")
    stray = commit(root, "stray work")
    git(root, "checkout", "--quiet", "main")
    later = commit(root, "later work on main")
    git(root, "update-ref", "refs/remotes/origin/main", later)
    return {"root": root, "release": release, "stray": stray, "later": later}


def run_check(release_guard, repo, tag=TAG, commit_ref=None):
    root = repo["root"]
    return release_guard.check(
        tag,
        root / "plugins" / "calibre-bridge",
        root / "CHANGELOG.md",
        root,
        commit_ref or repo["release"],
    )


# ── tag parsing ───────────────────────────────────────────────────────────────


def test_a_release_tag_yields_its_version(release_guard):
    assert release_guard.parse_tag("v-calibre-bridge-0.8.3", "calibre-bridge") == "0.8.3"
    assert release_guard.parse_tag("v-calibre-bridge-10.20.30", "calibre-bridge") == "10.20.30"


@pytest.mark.parametrize(
    "tag",
    [
        "v1.2.3",  # the old `v*` trigger let this through
        "v-calibre-bridge-1.2",
        "v-calibre-bridge-1.2.3.4",
        "v-calibre-bridge-1.2.3-rc1",
        "v-calibre-bridge-1.2.x",
        "v-calibre-bridge-01.2.3",
        "v-calibre-bridge-1.2.3\n",
        " v-calibre-bridge-1.2.3",
        "calibre-bridge-1.2.3",
        "v-calibre-bridge-",
        "v-other-plugin-1.2.3",
        "vv-calibre-bridge-1.2.3",
        "",
    ],
)
def test_anything_else_is_not_a_release_tag(release_guard, tag):
    with pytest.raises(release_guard.GuardError):
        release_guard.parse_tag(tag, "calibre-bridge")


def test_the_plugin_name_in_the_tag_is_literal(release_guard):
    # `.` in a plugin name must not act as a wildcard.
    with pytest.raises(release_guard.GuardError):
        release_guard.parse_tag("v-calibreXbridge-1.2.3", "calibre.bridge")


# ── changelog ─────────────────────────────────────────────────────────────────

CHANGELOG = """\
# Changelog

## calibre-bridge

### [Unreleased]

#### Fixed

- **Not out yet.** This must never be published as release notes.

### [1.2.3] - 2026-02-03

#### Fixed

- **The third fix.** It fixes the third thing.

### [1.2.2] - 2026-02-02

- The second fix.

### [1.2.1] - 2026-02-01

#### Fixed

### [1.2.0] - 2026-01-31

## other-plugin

### [9.9.9] - 2026-03-01

- Belongs to another plugin.

### [1.2.2] - 2026-01-01

- Same number, other plugin.
"""


def test_notes_are_the_section_for_that_version(release_guard):
    notes = release_guard.release_notes(CHANGELOG, "calibre-bridge", "1.2.3")
    assert notes == (
        "### [1.2.3] - 2026-02-03\n\n#### Fixed\n\n- **The third fix.** It fixes the third thing.\n"
    )


def test_notes_stop_at_the_next_plugin(release_guard):
    notes = release_guard.release_notes(CHANGELOG, "other-plugin", "9.9.9")
    assert notes == "### [9.9.9] - 2026-03-01\n\n- Belongs to another plugin.\n"


def test_notes_come_from_the_right_plugin(release_guard):
    notes = release_guard.release_notes(CHANGELOG, "calibre-bridge", "1.2.2")
    assert "The second fix." in notes
    assert "other plugin" not in notes


def test_a_missing_section_is_an_error_not_a_generic_body(release_guard):
    with pytest.raises(release_guard.GuardError, match=r"no '### \[4\.5\.6\]' section"):
        release_guard.release_notes(CHANGELOG, "calibre-bridge", "4.5.6")


@pytest.mark.parametrize("version", ["1.2.1", "1.2.0"])
def test_a_section_without_notes_is_an_error(release_guard, version):
    # 1.2.1 holds only a subheading, 1.2.0 holds nothing at all.
    with pytest.raises(release_guard.GuardError, match="is empty"):
        release_guard.release_notes(CHANGELOG, "calibre-bridge", version)


def test_unreleased_is_never_release_notes(release_guard):
    with pytest.raises(release_guard.GuardError):
        release_guard.release_notes(CHANGELOG, "calibre-bridge", "Unreleased")
    assert "Not out yet" not in release_guard.release_notes(CHANGELOG, "calibre-bridge", "1.2.3")


def test_unreleased_is_not_a_released_version(release_guard):
    assert release_guard.released_versions(CHANGELOG, "calibre-bridge") == [
        "1.2.3",
        "1.2.2",
        "1.2.1",
        "1.2.0",
    ]
    assert release_guard.latest_release(CHANGELOG, "calibre-bridge") == "1.2.3"


def test_the_latest_release_is_the_highest_not_the_first(release_guard):
    text = "## calibre-bridge\n\n### [0.9.0]\n\n- a\n\n### [0.10.0]\n\n- b\n"
    assert release_guard.latest_release(text, "calibre-bridge") == "0.10.0"


def test_a_changelog_without_the_plugin_is_an_error(release_guard):
    with pytest.raises(release_guard.GuardError, match="no '## kobo-bridge' part"):
        release_guard.release_notes(CHANGELOG, "kobo-bridge", "1.2.3")


# ── the whole check, against a real git repository ────────────────────────────


def test_a_release_commit_on_main_passes(release_guard, repo):
    notes = run_check(release_guard, repo)
    assert notes.startswith("### [1.2.3] - 2026-02-03")
    assert "The third fix." in notes


def test_the_tip_of_main_passes(release_guard, repo):
    assert run_check(release_guard, repo, commit_ref=repo["later"])


def test_a_tag_that_disagrees_with_the_plugin_version_fails(release_guard, repo):
    with pytest.raises(release_guard.GuardError, match="says 1.2.2 but .* declares 1.2.3"):
        run_check(release_guard, repo, tag="v-calibre-bridge-1.2.2")


def test_a_commit_that_is_not_on_main_fails(release_guard, repo):
    with pytest.raises(release_guard.GuardError, match="is not on origin/main"):
        run_check(release_guard, repo, commit_ref=repo["stray"])


def test_a_version_with_no_changelog_section_fails(release_guard, repo):
    root = repo["root"]
    init_file = root / "plugins" / "calibre-bridge" / "__init__.py"
    init_file.write_text(init_file.read_text().replace("(1, 2, 3)", "(1, 3, 0)"))
    with pytest.raises(release_guard.GuardError, match=r"no '### \[1\.3\.0\]' section"):
        run_check(release_guard, repo, tag="v-calibre-bridge-1.3.0")


def test_every_failure_is_reported_at_once(release_guard, repo):
    with pytest.raises(release_guard.GuardError) as excinfo:
        run_check(release_guard, repo, tag="v-calibre-bridge-7.7.7", commit_ref=repo["stray"])
    message = str(excinfo.value)
    assert "declares 1.2.3" in message
    assert "is not on origin/main" in message
    assert "no '### [7.7.7]' section" in message


def test_a_bad_tag_fails_before_anything_else_is_read(release_guard, tmp_path):
    with pytest.raises(release_guard.GuardError, match="not a calibre-bridge release tag"):
        release_guard.check(
            "v1.2.3", tmp_path / "calibre-bridge", tmp_path / "CHANGELOG.md", tmp_path
        )


def test_a_missing_main_ref_fails_closed(release_guard, repo):
    git(repo["root"], "update-ref", "-d", "refs/remotes/origin/main")
    with pytest.raises(release_guard.GuardError, match="could not compare"):
        run_check(release_guard, repo)


def test_a_ref_that_looks_like_an_option_is_refused(release_guard, repo):
    with pytest.raises(release_guard.GuardError, match="refusing"):
        release_guard.is_ancestor(repo["root"], "--help", "origin/main")


# ── command line, as the release job calls it ─────────────────────────────────


def guard_argv(repo, tag=TAG, commit_ref=None, notes=None):
    root = repo["root"]
    argv = [
        "--tag",
        tag,
        "--plugin-dir",
        str(root / "plugins" / "calibre-bridge"),
        "--changelog",
        str(root / "CHANGELOG.md"),
        "--repo",
        str(root),
        "--commit",
        commit_ref or repo["release"],
    ]
    if notes is not None:
        argv += ["--notes-out", str(notes)]
    return argv


def test_main_writes_the_notes_and_exits_zero(release_guard, repo, tmp_path):
    notes = tmp_path / "release_notes.md"
    assert release_guard.main(guard_argv(repo, notes=notes)) == 0
    assert notes.read_text().startswith("### [1.2.3] - 2026-02-03")


def test_main_exits_nonzero_and_writes_no_notes(release_guard, repo, tmp_path, capsys):
    notes = tmp_path / "release_notes.md"
    argv = guard_argv(repo, commit_ref=repo["stray"], notes=notes)
    assert release_guard.main(argv) == 1
    assert not notes.exists()
    assert "is not on origin/main" in capsys.readouterr().err


def test_main_annotates_the_run_on_github(release_guard, repo, monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    assert release_guard.main(guard_argv(repo, tag="v9.9.9")) == 1
    assert "::error title=Release guard::" in capsys.readouterr().out


def test_the_script_runs_standalone(repo, tmp_path):
    # The release job runs it as a file, from the repository root.
    notes = tmp_path / "release_notes.md"
    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "release_guard.py"),
            *guard_argv(repo, notes=notes),
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    assert notes.is_file()


# ── this repository ───────────────────────────────────────────────────────────


def test_the_current_release_has_notes(release_guard, build_plugin):
    plugin_dir = REPO_ROOT / "plugins" / "calibre-bridge"
    version = build_plugin.read_version(plugin_dir / "__init__.py")
    changelog = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    notes = release_guard.release_notes(changelog, "calibre-bridge", version)
    assert notes.startswith(f"### [{version}]")


def test_the_release_job_is_guarded():
    """The workflow half of the guard, which no unit test can run.

    Asserted on the text so that loosening the trigger, or publishing before
    the guard has run, fails here instead of on the next tag.
    """
    workflow = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    release_job = workflow[workflow.index("\n  release:\n") :]

    assert 'tags: ["v-calibre-bridge-[0-9]+.[0-9]+.[0-9]+"]' in workflow
    assert "if: startsWith(github.ref, 'refs/tags/v-calibre-bridge-')" in release_job
    assert release_job.index("scripts/release_guard.py") < release_job.index("gh release create")
    assert release_job.index("scripts/release_guard.py") < release_job.index("build_plugin.py")
    # Full history, or the guard cannot see whether the commit is on main.
    assert "fetch-depth: 0" in release_job
    # Nothing from the event is interpolated into a shell script.
    for line in release_job.splitlines():
        if "${{" in line:
            assert line.strip() == "GH_TOKEN: ${{ github.token }}", line
