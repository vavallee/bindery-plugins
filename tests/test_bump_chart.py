"""Tests for scripts/bump_chart.py.

The chart PR after a release used to be four hand edits in three files, and
`appVersion` was simply forgotten for six releases. One command sets them all,
so these tests pin down that it reaches every one and touches nothing else.
"""

from __future__ import annotations

import pathlib
import shutil

import pytest

OLD_HASH = "a" * 64
NEW_HASH = "c" * 64
NEW_COMMIT = "d" * 40
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
    changed = bump_chart.bump("1.3.0", release_tree, sha256=NEW_HASH)

    assert changed == sorted(FILES)
    assert set(bump_chart.read_pins(release_tree).values()) == {"1.3.0"}
    assert sorted(changed_lines(before, snapshot(release_tree))) == sorted(
        [
            "version: 0.2.8",
            'appVersion: "1.3.0"',
            'pluginVersion: "1.3.0"',
            '            pluginVersion: "1.3.0"',
            f'            pluginSha256: "{NEW_HASH}"',
            "        targetRevision: v-calibre-bridge-1.3.0",
        ]
    )


def test_bump_leaves_the_helm_template_line_alone(bump_chart, release_tree):
    bump_chart.bump("1.3.0", release_tree, sha256=NEW_HASH)
    argocd = (release_tree / "argocd" / "application.yaml").read_text()
    assert 'pluginVersion: "{{pluginVersion}}"' in argocd


def test_bump_is_idempotent(bump_chart, release_tree):
    bump_chart.bump("1.3.0", release_tree, sha256=NEW_HASH)
    after_first = snapshot(release_tree)

    assert bump_chart.bump("1.3.0", release_tree, sha256=NEW_HASH) == []
    assert snapshot(release_tree) == after_first


def test_bump_repairs_pins_that_disagree(bump_chart, release_tree):
    values = release_tree / "charts" / "calibre-plugin-installer" / "values.yaml"
    values.write_text(values.read_text().replace("1.2.3", "1.2.2"))

    changed = bump_chart.bump("1.2.3", release_tree, sha256=OLD_HASH)

    assert changed == sorted(FILES[:2])
    assert set(bump_chart.read_pins(release_tree).values()) == {"1.2.3"}


def test_an_argocd_only_repair_leaves_the_chart_version_alone(bump_chart, release_tree):
    argocd = release_tree / "argocd" / "application.yaml"
    argocd.write_text(
        argocd.read_text().replace("v-calibre-bridge-1.2.3", "v-calibre-bridge-1.2.2")
    )

    assert bump_chart.bump("1.2.3", release_tree, sha256=NEW_HASH) == ["argocd/application.yaml"]
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
        bump_chart.bump("1.3.0", release_tree, sha256=NEW_HASH)
    with pytest.raises(bump_chart.ChartError, match="ArgoCD targetRevision"):
        bump_chart.read_pins(release_tree)
    assert snapshot(release_tree) == before


def test_main_reports_what_it_changed(bump_chart, release_tree, capsys):
    assert bump_chart.main(["1.3.0", "--sha256", NEW_HASH, "--repo-root", str(release_tree)]) == 0
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

    bump_chart.bump("99.0.0", tmp_path, sha256=NEW_HASH, commit=NEW_COMMIT)

    assert set(bump_chart.read_pins(tmp_path).values()) == {"99.0.0"}
    assert bump_chart.read_hashes(tmp_path) == {
        "ArgoCD pluginSha256": NEW_HASH,
        "ArgoCD targetRevision commit": NEW_COMMIT,
    }
    # Four pins, the hash, the commit line and the chart's own version, and not
    # one other line. values.yaml ships an empty pluginSha256 and keeps it.
    assert len(changed_lines(before, snapshot(tmp_path))) == 6
    assert 'pluginSha256: ""' in (tmp_path / FILES[1]).read_text()


# The commit SHA form: `targetRevision: <40 hex>  # v-calibre-bridge-X.Y.Z`.


def test_read_pins_reads_the_version_from_the_sha_comment(bump_chart, sha_tree):
    assert bump_chart.read_pins(sha_tree)["ArgoCD targetRevision"] == "1.2.3"
    assert bump_chart.read_hashes(sha_tree) == {
        "ArgoCD pluginSha256": OLD_HASH,
        "ArgoCD targetRevision commit": "b" * 40,
    }


def test_a_bump_run_end_to_end_on_the_sha_form(bump_chart, sha_tree):
    before = snapshot(sha_tree)
    changed = bump_chart.bump("1.2.4", sha_tree, sha256=NEW_HASH, commit=NEW_COMMIT)

    assert changed == sorted(FILES)
    assert set(bump_chart.read_pins(sha_tree).values()) == {"1.2.4"}
    assert sorted(changed_lines(before, snapshot(sha_tree))) == sorted(
        [
            "version: 0.2.8",
            'appVersion: "1.2.4"',
            'pluginVersion: "1.2.4"',
            '            pluginVersion: "1.2.4"',
            f'            pluginSha256: "{NEW_HASH}"',
            f"        targetRevision: {NEW_COMMIT}  # v-calibre-bridge-1.2.4",
        ]
    )
    argocd = (sha_tree / FILES[2]).read_text()
    assert 'pluginSha256: "{{pluginSha256}}"' in argocd


def test_a_missing_hash_fails_and_writes_nothing(bump_chart, sha_tree, capsys):
    before = snapshot(sha_tree)
    with pytest.raises(bump_chart.ChartError, match="needs its SHA-256"):
        bump_chart.bump("1.2.4", sha_tree, commit=NEW_COMMIT)
    assert snapshot(sha_tree) == before
    assert bump_chart.main(["1.2.4", "--commit", NEW_COMMIT, "--repo-root", str(sha_tree)]) == 1
    assert "needs its SHA-256" in capsys.readouterr().err
    assert snapshot(sha_tree) == before


def test_a_missing_commit_that_cannot_be_resolved_fails_and_writes_nothing(
    bump_chart, sha_tree, monkeypatch
):
    monkeypatch.setattr(bump_chart, "resolve_commit", lambda *a, **k: None)
    before = snapshot(sha_tree)
    with pytest.raises(bump_chart.ChartError, match="pass --commit"):
        bump_chart.bump("1.2.4", sha_tree, sha256=NEW_HASH)
    assert snapshot(sha_tree) == before


@pytest.mark.parametrize("bad", ["", "abc", "g" * 64, "a" * 63, "a" * 65])
def test_a_malformed_hash_is_refused(bump_chart, sha_tree, bad, tmp_path):
    before = snapshot(sha_tree)
    with pytest.raises(bump_chart.ChartError):
        bump_chart.bump("1.2.4", sha_tree, sha256=bad, commit=NEW_COMMIT)
    assert snapshot(sha_tree) == before


def test_a_malformed_commit_is_refused(bump_chart, sha_tree):
    with pytest.raises(bump_chart.ChartError, match="40 hex"):
        bump_chart.bump("1.2.4", sha_tree, sha256=NEW_HASH, commit="abc123")


def test_the_hash_can_come_from_a_sha256_file(bump_chart, sha_tree, tmp_path):
    sidecar = tmp_path / "calibre-bridge-v1.2.4.zip.sha256"
    sidecar.write_text(f"{NEW_HASH.upper()}  calibre-bridge-v1.2.4.zip\n")
    bump_chart.bump("1.2.4", sha_tree, sha256=str(sidecar), commit=NEW_COMMIT)
    assert bump_chart.read_hashes(sha_tree)["ArgoCD pluginSha256"] == NEW_HASH

    junk = tmp_path / "junk.sha256"
    junk.write_text("not a hash  file.zip\n")
    with pytest.raises(bump_chart.ChartError, match="no 64 hex"):
        bump_chart.parse_sha256(str(junk))
    with pytest.raises(bump_chart.ChartError, match="https"):
        bump_chart.parse_sha256("http://example.com/x.sha256")


def test_sha_form_bump_is_idempotent_and_needs_no_inputs_when_already_there(bump_chart, sha_tree):
    bump_chart.bump("1.2.4", sha_tree, sha256=NEW_HASH, commit=NEW_COMMIT)
    after_first = snapshot(sha_tree)

    assert bump_chart.bump("1.2.4", sha_tree, sha256=NEW_HASH, commit=NEW_COMMIT) == []
    assert bump_chart.bump("1.2.4", sha_tree) == []
    assert snapshot(sha_tree) == after_first


def test_values_yaml_hash_moves_when_the_file_carries_one(bump_chart, sha_tree):
    values = sha_tree / FILES[1]
    values.write_text(values.read_text() + f'pluginSha256: "{OLD_HASH}"\n')
    bump_chart.bump("1.2.4", sha_tree, sha256=NEW_HASH, commit=NEW_COMMIT)
    assert f'pluginSha256: "{NEW_HASH}"' in values.read_text()


def test_the_commit_is_resolved_from_the_local_tag(bump_chart, sha_tree, tmp_path):
    import subprocess

    repo = tmp_path / "git"
    repo.mkdir()
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "PATH": __import__("os").environ["PATH"], "HOME": str(tmp_path)}  # fmt: skip
    for cmd in (
        ["init", "-q"],
        ["commit", "-q", "--allow-empty", "-m", "x"],
        ["tag", "-a", "-m", "annotated", "v-calibre-bridge-1.2.4"],
    ):
        subprocess.run(["git", *cmd], cwd=repo, env=env, check=True)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, env=env, check=True, capture_output=True, text=True
    ).stdout.strip()

    assert bump_chart.resolve_commit("1.2.4", repo) == head
    assert bump_chart.resolve_commit("9.9.9", repo, remote=False) is None


def test_the_hash_url_opener_refuses_file_and_ftp_urls(bump_chart):
    opener = bump_chart._https_only_opener()
    for url in ("file:///etc/hostname", "ftp://example.invalid/x.sha256"):
        with pytest.raises(OSError):
            opener.open(url, timeout=1)
