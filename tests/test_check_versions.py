"""Tests for scripts/check_versions.py.

Releases 0.8.2 and 0.8.3 each edited the version by hand in nine files, and the
chart's `appVersion` said 0.2.0 throughout. The check is what keeps the places
that are left in step, so each way they can drift apart has a test.
"""

from __future__ import annotations

import pathlib

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


def edit(path: pathlib.Path, old: str, new: str) -> None:
    text = path.read_text()
    assert old in text, f"{old!r} not in {path}"
    path.write_text(text.replace(old, new))


def test_this_repository_is_consistent(check_versions):
    assert check_versions.problems(REPO_ROOT) == []


def test_a_consistent_tree_passes(check_versions, release_tree):
    assert check_versions.problems(release_tree) == []


def test_a_version_bump_without_a_changelog_section_fails(check_versions, release_tree):
    edit(release_tree / "plugins" / "calibre-bridge" / "__init__.py", "(1, 2, 3)", "(1, 3, 0)")
    (problem,) = check_versions.problems(release_tree)
    assert "declares 1.3.0 but the newest CHANGELOG section is [1.2.3]" in problem


def test_a_changelog_section_without_a_version_bump_fails(check_versions, release_tree):
    edit(release_tree / "CHANGELOG.md", "### [Unreleased]", "### [Unreleased]\n\n### [1.3.0]")
    (problem,) = check_versions.problems(release_tree)
    assert "declares 1.2.3 but the newest CHANGELOG section is [1.3.0]" in problem


def test_the_chart_may_trail_the_plugin(check_versions, tmp_path, make_release_tree):
    # The state of main between a release and the chart PR that follows it.
    tree = make_release_tree(tmp_path / "repo", plugin="1.2.3", chart="1.2.2")
    assert check_versions.problems(tree) == []


def test_the_chart_may_not_run_ahead_of_the_changelog(check_versions, tmp_path, make_release_tree):
    tree = make_release_tree(tmp_path / "repo", plugin="1.2.3", chart="1.2.4")
    found = check_versions.problems(tree)
    assert len(found) == 4
    assert all("ahead of the newest CHANGELOG section [1.2.3]" in p for p in found)


def test_pins_that_disagree_fail(check_versions, release_tree):
    edit(
        release_tree / "charts" / "calibre-plugin-installer" / "Chart.yaml",
        'appVersion: "1.2.3"',
        'appVersion: "0.2.0"',
    )
    (problem,) = check_versions.problems(release_tree)
    assert "disagree" in problem
    assert "Chart.yaml appVersion 0.2.0" in problem
    assert "bump_chart.py" in problem


def test_an_argocd_revision_that_is_not_the_release_tag_fails(check_versions, release_tree):
    edit(
        release_tree / "argocd" / "application.yaml",
        "targetRevision: v-calibre-bridge-1.2.3",
        "targetRevision: development",
    )
    (problem,) = check_versions.problems(release_tree)
    assert "ArgoCD targetRevision" in problem


def test_argocd_revision_and_plugin_version_move_together(check_versions, release_tree):
    edit(
        release_tree / "argocd" / "application.yaml",
        "targetRevision: v-calibre-bridge-1.2.3",
        "targetRevision: v-calibre-bridge-1.2.2",
    )
    (problem,) = check_versions.problems(release_tree)
    assert "disagree" in problem


def test_a_missing_unreleased_section_fails(check_versions, release_tree):
    edit(release_tree / "CHANGELOG.md", "### [Unreleased]\n\n", "")
    (problem,) = check_versions.problems(release_tree)
    assert "'### [Unreleased]'" in problem


def test_unreleased_has_to_come_first(check_versions, release_tree):
    changelog = release_tree / "CHANGELOG.md"
    edit(changelog, "### [Unreleased]\n\n", "")
    changelog.write_text(changelog.read_text() + "\n### [Unreleased]\n")
    (problem,) = check_versions.problems(release_tree)
    assert "first section" in problem


def test_main_exit_codes(check_versions, release_tree, capsys):
    assert check_versions.main(["--repo-root", str(release_tree)]) == 0
    assert "versions agree" in capsys.readouterr().out

    edit(release_tree / "plugins" / "calibre-bridge" / "__init__.py", "(1, 2, 3)", "(2, 0, 0)")
    assert check_versions.main(["--repo-root", str(release_tree)]) == 1
    assert "declares 2.0.0" in capsys.readouterr().err
