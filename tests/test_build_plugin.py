"""Tests for scripts/build_plugin.py.

This script produces the artefact every install tier consumes, so a silent
break here ships a broken zip.
"""

from __future__ import annotations

import hashlib
import pathlib
import zipfile

import pytest

PLUGIN_INIT = """\
from calibre.customize import InterfaceActionBase


class DemoBridge(InterfaceActionBase):
    name = "Demo Bridge"
    version = (1, 2, 3)
    minimum_calibre_version = (6, 0, 0)
"""


def make_repo_root(root: pathlib.Path, *, copyright_file: bool = True) -> pathlib.Path:
    """A stand in for the repository root, holding the licence files."""
    repo = root / "repo"
    repo.mkdir(exist_ok=True)
    (repo / "LICENSE").write_text("GNU GENERAL PUBLIC LICENSE\nVersion 3, 29 June 2007\n")
    if copyright_file:
        (repo / "COPYRIGHT").write_text("Copyright (C) 2026 vavallee\n")
    return repo


def make_plugin_tree(root: pathlib.Path) -> pathlib.Path:
    plugin_dir = root / "demo-bridge"
    (plugin_dir / "plugin").mkdir(parents=True)
    (plugin_dir / "tests").mkdir()
    (plugin_dir / "plugin" / "__pycache__").mkdir()

    (plugin_dir / "__init__.py").write_text(PLUGIN_INIT)
    (plugin_dir / "plugin" / "__init__.py").write_text("")
    (plugin_dir / "plugin" / "handlers.py").write_text("HANDLERS = 1\n")
    (plugin_dir / "plugin-import-name-demo_bridge.txt").write_text("")
    # Everything below must be excluded from the zip.
    (plugin_dir / "tests" / "test_handlers.py").write_text("def test_x(): pass\n")
    (plugin_dir / "plugin" / "__pycache__" / "handlers.cpython-312.pyc").write_bytes(b"\x00")
    (plugin_dir / "plugin" / "stale.pyc").write_bytes(b"\x00")
    return plugin_dir


def test_read_version_reads_the_class_attribute(build_plugin, tmp_path):
    init_file = tmp_path / "__init__.py"
    init_file.write_text(PLUGIN_INIT)
    assert build_plugin.read_version(init_file) == "1.2.3"


def test_read_version_raises_when_absent(build_plugin, tmp_path):
    init_file = tmp_path / "__init__.py"
    init_file.write_text("class Foo:\n    name = 'Foo'\n")
    with pytest.raises(RuntimeError):
        build_plugin.read_version(init_file)


def test_zip_name_matches_the_release_url_convention(build_plugin, tmp_path):
    # The Helm chart and docs/installation.md both build URLs from this shape.
    assert (
        build_plugin.plugin_zip_name(tmp_path / "calibre-bridge", "0.5.0")
        == "calibre-bridge-v0.5.0.zip"
    )


def test_underscores_in_the_directory_name_become_hyphens(build_plugin, tmp_path):
    assert (
        build_plugin.plugin_zip_name(tmp_path / "calibre_bridge", "0.5.0")
        == "calibre-bridge-v0.5.0.zip"
    )


def test_build_produces_the_expected_members(build_plugin, tmp_path):
    plugin_dir = make_plugin_tree(tmp_path)
    out_zip = build_plugin.build(plugin_dir, tmp_path / "dist")

    assert out_zip.name == "demo-bridge-v1.2.3.zip"
    with zipfile.ZipFile(out_zip) as zf:
        names = zf.namelist()

    assert "__init__.py" in names
    assert "plugin/handlers.py" in names
    assert "plugin-import-name-demo_bridge.txt" in names
    assert not [n for n in names if n.startswith("tests/")]
    assert not [n for n in names if "__pycache__" in n]
    assert not [n for n in names if n.endswith(".pyc")]


def test_build_writes_paths_calibre_can_load(build_plugin, tmp_path):
    # Calibre's zip loader reads members by posix path, and the top level must
    # hold __init__.py or add_plugin rejects the zip.
    plugin_dir = make_plugin_tree(tmp_path)
    out_zip = build_plugin.build(plugin_dir, tmp_path / "dist")

    with zipfile.ZipFile(out_zip) as zf:
        names = zf.namelist()

    assert not [n for n in names if "\\" in n]
    assert not [n for n in names if n.startswith("/") or n.startswith("..")]
    assert "__init__.py" in names


def test_build_writes_a_checksum_sidecar_that_matches(build_plugin, tmp_path):
    # The Helm init container runs `sha256sum -c` against this file, so both the
    # digest and the two-space filename format have to be right.
    plugin_dir = make_plugin_tree(tmp_path)
    out_zip = build_plugin.build(plugin_dir, tmp_path / "dist")

    sidecar = out_zip.parent / (out_zip.name + ".sha256")
    assert sidecar.is_file()

    expected = hashlib.sha256(out_zip.read_bytes()).hexdigest()
    assert sidecar.read_text() == f"{expected}  {out_zip.name}\n"


def test_build_creates_the_output_directory(build_plugin, tmp_path):
    plugin_dir = make_plugin_tree(tmp_path)
    out_dir = tmp_path / "nested" / "dist"
    out_zip = build_plugin.build(plugin_dir, out_dir)
    assert out_zip.parent == out_dir


def test_build_rejects_a_directory_without_an_init(build_plugin, tmp_path):
    empty = tmp_path / "not-a-plugin"
    empty.mkdir()
    with pytest.raises(SystemExit):
        build_plugin.build(empty, tmp_path / "dist")


def test_build_is_reproducible_in_content(build_plugin, tmp_path):
    plugin_dir = make_plugin_tree(tmp_path)
    first = build_plugin.build(plugin_dir, tmp_path / "a")
    second = build_plugin.build(plugin_dir, tmp_path / "b")
    with zipfile.ZipFile(first) as zf1, zipfile.ZipFile(second) as zf2:
        assert zf1.namelist() == zf2.namelist()
        assert [zf1.read(n) for n in zf1.namelist()] == [zf2.read(n) for n in zf2.namelist()]


def test_main_accepts_the_arguments_ci_passes(build_plugin, tmp_path, monkeypatch):
    plugin_dir = make_plugin_tree(tmp_path)
    monkeypatch.setattr(
        "sys.argv",
        ["build_plugin.py", str(plugin_dir), "--output-dir", str(tmp_path / "dist")],
    )
    assert build_plugin.main() == 0
    assert (tmp_path / "dist" / "demo-bridge-v1.2.3.zip").is_file()


def test_the_real_plugin_builds(build_plugin, tmp_path):
    repo_root = pathlib.Path(__file__).resolve().parent.parent
    plugin_dir = repo_root / "plugins" / "calibre-bridge"
    if not plugin_dir.is_dir():
        pytest.skip("calibre-bridge plugin not present")

    out_zip = build_plugin.build(plugin_dir, tmp_path / "dist")
    with zipfile.ZipFile(out_zip) as zf:
        names = zf.namelist()
    assert "__init__.py" in names
    assert "plugin/handlers.py" in names
    assert not [n for n in names if n.startswith("tests/")]


def test_licence_files_travel_in_the_zip(build_plugin, tmp_path):
    # GPL-3.0 section 4: the licence has to ship with the artefact, and the zip
    # is what every install tier consumes.
    plugin_dir = make_plugin_tree(tmp_path)
    repo_root = make_repo_root(tmp_path)

    out_zip = build_plugin.build(plugin_dir, tmp_path / "dist", repo_root)
    with zipfile.ZipFile(out_zip) as zf:
        names = zf.namelist()
        licence = zf.read("LICENSE").decode()

    assert "LICENSE" in names
    assert "COPYRIGHT" in names
    assert "GNU GENERAL PUBLIC LICENSE" in licence


def test_a_build_without_a_licence_is_refused(build_plugin, tmp_path):
    plugin_dir = make_plugin_tree(tmp_path)
    repo_root = tmp_path / "bare"
    repo_root.mkdir()

    with pytest.raises(SystemExit):
        build_plugin.build(plugin_dir, tmp_path / "dist", repo_root)


def test_copyright_is_optional(build_plugin, tmp_path):
    plugin_dir = make_plugin_tree(tmp_path)
    repo_root = make_repo_root(tmp_path, copyright_file=False)

    out_zip = build_plugin.build(plugin_dir, tmp_path / "dist", repo_root)
    with zipfile.ZipFile(out_zip) as zf:
        names = zf.namelist()

    assert "LICENSE" in names
    assert "COPYRIGHT" not in names


def test_a_plugin_owned_licence_is_not_overwritten(build_plugin, tmp_path):
    plugin_dir = make_plugin_tree(tmp_path)
    (plugin_dir / "LICENSE").write_text("plugin specific terms\n")
    repo_root = make_repo_root(tmp_path)

    out_zip = build_plugin.build(plugin_dir, tmp_path / "dist", repo_root)
    with zipfile.ZipFile(out_zip) as zf:
        assert zf.namelist().count("LICENSE") == 1
        assert zf.read("LICENSE").decode() == "plugin specific terms\n"


def test_licence_files_sit_at_the_zip_root(build_plugin, tmp_path):
    # calibre loads the plugin from the zip root, so the licence has to be
    # beside __init__.py where anyone unpacking the artefact will see it.
    plugin_dir = make_plugin_tree(tmp_path)
    repo_root = make_repo_root(tmp_path)

    out_zip = build_plugin.build(plugin_dir, tmp_path / "dist", repo_root)
    with zipfile.ZipFile(out_zip) as zf:
        names = zf.namelist()

    assert "__init__.py" in names
    assert not [n for n in names if n.endswith("/LICENSE") or n.endswith("/COPYRIGHT")]


def test_the_checksum_still_covers_the_whole_zip(build_plugin, tmp_path):
    # The sidecar is computed after the zip is closed, so adding members has to
    # keep it correct without any change to the Helm chart's verify step.
    plugin_dir = make_plugin_tree(tmp_path)
    repo_root = make_repo_root(tmp_path)

    out_zip = build_plugin.build(plugin_dir, tmp_path / "dist", repo_root)
    sidecar = out_zip.parent / (out_zip.name + ".sha256")

    expected = hashlib.sha256(out_zip.read_bytes()).hexdigest()
    assert sidecar.read_text() == f"{expected}  {out_zip.name}\n"


def test_the_real_zip_carries_the_repository_licence(build_plugin, tmp_path):
    repo_root = pathlib.Path(__file__).resolve().parent.parent
    plugin_dir = repo_root / "plugins" / "calibre-bridge"
    if not plugin_dir.is_dir():
        pytest.skip("calibre-bridge plugin not present")

    out_zip = build_plugin.build(plugin_dir, tmp_path / "dist", repo_root)
    with zipfile.ZipFile(out_zip) as zf:
        names = zf.namelist()

    assert "LICENSE" in names
    # COPYRIGHT arrives with the relicense branch. Assert it whenever the file
    # is there, so this starts covering it the moment that branch lands.
    if (repo_root / "COPYRIGHT").is_file():
        assert "COPYRIGHT" in names
