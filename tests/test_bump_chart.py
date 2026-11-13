"""Tests for scripts/bump_chart.py.

The chart PR after a release used to be four hand edits in three files, and
`appVersion` was simply forgotten for six releases. One command sets them all,
so these tests pin down that it reaches every one and touches nothing else.
"""

from __future__ import annotations

import pathlib
import shutil

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

FILES = (
    "charts/calibre-plugin-installer/Chart.yaml",
    "charts/calibre-plugin-installer/values.yaml",
    "argocd/application.yaml",
)


def snapshot(root: pathlib.Path) -> dict[str, list[str]]:
    return {name: (root / name).read_text().splitlines() for name in FILES}


def changed_lines(before: dict[str, list[str]], after: dict[str, list[str]]) -> list[str]:
    changed = []
    for name in FILES:
        assert len(before[name]) == len(after[name]), f"{name} gained or lost lines"
        changed += [new for old, new in zip(before[name], after[name], strict=True) if old != new]
    return changed


def test_read_pins_finds_all_four(bump_chart, release_tree):
    assert bump_chart.read_pins(release_tree) == {
        "Chart.yaml appVersion": "1.2.3",
        "values.yaml pluginVersion": "1.2.3",
        "ArgoCD pluginVersion": "1.2.3",
        "ArgoCD targetRevision": "1.2.3",
    }


def test_bump_moves_every_pin_and_the_chart_version(bump_chart, release_tree):
    before = snapshot(release_tree)
    changed = bump_chart.bump("1.3.0", release_tree)

    assert changed == sorted(FILES)
    assert set(bump_chart.read_pins(release_tree).values()) == {"1.3.0"}
    assert sorted(changed_lines(before, snapshot(release_tree))) == sorted(
        [
            "version: 0.2.8",
            'appVersion: "1.3.0"',
            'pluginVersion: "1.3.0"',
            '            pluginVersion: "1.3.0"',
            "        targetRevision: v-calibre-bridge-1.3.0",
        ]
    )


def test_bump_leaves_the_helm_template_line_alone(bump_chart, release_tree):
    bump_chart.bump("1.3.0", release_tree)
    argocd = (release_tree / "argocd" / "application.yaml").read_text()
    assert 'pluginVersion: "{{pluginVersion}}"' in argocd


def test_bump_is_idempotent(bump_chart, release_tree):
    bump_chart.bump("1.3.0", release_tree)
    after_first = snapshot(release_tree)

    assert bump_chart.bump("1.3.0", release_tree) == []
    assert snapshot(release_tree) == after_first


def test_bump_repairs_pins_that_disagree(bump_chart, release_tree):
    values = release_tree / "charts" / "calibre-plugin-installer" / "values.yaml"
    values.write_text(values.read_text().replace("1.2.3", "1.2.2"))

    changed = bump_chart.bump("1.2.3", release_tree)

    assert changed == sorted(FILES[:2])
    assert set(bump_chart.read_pins(release_tree).values()) == {"1.2.3"}


def test_an_argocd_only_repair_leaves_the_chart_version_alone(bump_chart, release_tree):
    argocd = release_tree / "argocd" / "application.yaml"
    argocd.write_text(
        argocd.read_text().replace("v-calibre-bridge-1.2.3", "v-calibre-bridge-1.2.2")
    )

    assert bump_chart.bump("1.2.3", release_tree) == ["argocd/application.yaml"]
    assert "version: 0.2.7" in (release_tree / FILES[0]).read_text()


@pytest.mark.parametrize("version", ["1.3", "v1.3.0", "1.3.0-rc1", "1.3.0.1", "latest", ""])
def test_bump_refuses_anything_but_a_plain_version(bump_chart, release_tree, version):
    before = snapshot(release_tree)
    with pytest.raises(bump_chart.ChartError, match="not a plugin version"):
        bump_chart.bump(version, release_tree)
    assert snapshot(release_tree) == before


def test_a_file_that_changed_shape_is_an_error_and_nothing_is_written(bump_chart, release_tree):
    # What argocd/application.yaml looked like before it was pinned to a tag.
    argocd = release_tree / "argocd" / "application.yaml"
    argocd.write_text(
        argocd.read_text().replace("targetRevision: v-calibre-bridge-1.2.3", "targetRevision: dev")
    )
    before = snapshot(release_tree)

    with pytest.raises(bump_chart.ChartError, match="ArgoCD targetRevision"):
        bump_chart.bump("1.3.0", release_tree)
    with pytest.raises(bump_chart.ChartError, match="ArgoCD targetRevision"):
        bump_chart.read_pins(release_tree)
    assert snapshot(release_tree) == before


def test_main_reports_what_it_changed(bump_chart, release_tree, capsys):
    assert bump_chart.main(["1.3.0", "--repo-root", str(release_tree)]) == 0
    out = capsys.readouterr().out
    for name in FILES:
        assert f"updated {name}" in out

    assert bump_chart.main(["1.3.0", "--repo-root", str(release_tree)]) == 0
    assert "nothing to do" in capsys.readouterr().out


def test_main_fails_on_a_bad_version(bump_chart, release_tree, capsys):
    assert bump_chart.main(["one.two.three", "--repo-root", str(release_tree)]) == 1
    assert "not a plugin version" in capsys.readouterr().err


def test_the_real_files_have_the_shape_the_script_edits(bump_chart, tmp_path):
    """Run against a copy of this repository's own chart and ArgoCD files."""
    for name in FILES:
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(REPO_ROOT / name, tmp_path / name)
    before = snapshot(tmp_path)

    bump_chart.bump("99.0.0", tmp_path)

    assert set(bump_chart.read_pins(tmp_path).values()) == {"99.0.0"}
    # Four pins and the chart's own version, and not one other line.
    assert len(changed_lines(before, snapshot(tmp_path))) == 5
