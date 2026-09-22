#!/usr/bin/env python3
"""Build a Calibre plugin .zip from a plugin subdirectory.

Usage:
    python scripts/build_plugin.py plugins/calibre-bridge [--output-dir dist]
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import pathlib
import sys
import zipfile

EXCLUDE_DIRS = {"__pycache__", "tests", ".pytest_cache", ".mypy_cache"}
EXCLUDE_SUFFIXES = {".pyc", ".pyo"}

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

# Files copied from the repository root into the zip root. The zip is the
# artefact every install tier consumes, and GPL-3.0 section 4 requires the
# licence to travel with it, so the build fails rather than ships without one.
# COPYRIGHT carries the copyright line and the notice; it is included when
# present.
LICENCE_FILES = ("LICENSE", "COPYRIGHT")
REQUIRED_LICENCE_FILES = frozenset({"LICENSE"})


def read_version(init_path: pathlib.Path) -> str:
    tree = ast.parse(init_path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for stmt in node.body:
                if isinstance(stmt, ast.Assign):
                    for target in stmt.targets:
                        if isinstance(target, ast.Name) and target.id == "version":
                            value = ast.literal_eval(stmt.value)
                            return ".".join(str(p) for p in value)
    raise RuntimeError(f"could not find version tuple in {init_path}")


def plugin_zip_name(plugin_dir: pathlib.Path, version: str) -> str:
    slug = plugin_dir.name.replace("_", "-")
    return f"{slug}-v{version}.zip"


def licence_files(repo_root: pathlib.Path) -> list[pathlib.Path]:
    """Licence files to ship inside the zip, in declaration order."""
    found = []
    for name in LICENCE_FILES:
        path = repo_root / name
        if path.is_file():
            found.append(path)
        elif name in REQUIRED_LICENCE_FILES:
            raise SystemExit(f"missing {path}: the plugin zip has to carry the licence")
    return found


def build(
    plugin_dir: pathlib.Path,
    output_dir: pathlib.Path,
    repo_root: pathlib.Path = REPO_ROOT,
) -> pathlib.Path:
    init_file = plugin_dir / "__init__.py"
    if not init_file.is_file():
        raise SystemExit(f"missing {init_file}")
    version = read_version(init_file)
    licences = licence_files(repo_root)
    output_dir.mkdir(parents=True, exist_ok=True)
    out_zip = output_dir / plugin_zip_name(plugin_dir, version)

    with zipfile.ZipFile(out_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        members = set()
        for path in sorted(plugin_dir.rglob("*")):
            if path.is_dir():
                continue
            rel = path.relative_to(plugin_dir)
            if any(part in EXCLUDE_DIRS for part in rel.parts):
                continue
            if path.suffix in EXCLUDE_SUFFIXES:
                continue
            zf.write(path, rel.as_posix())
            members.add(rel.as_posix())
        for path in licences:
            # A licence the plugin ships itself wins over the repository one.
            if path.name in members:
                continue
            zf.write(path, path.name)

    digest = hashlib.sha256(out_zip.read_bytes()).hexdigest()
    (output_dir / (out_zip.name + ".sha256")).write_text(f"{digest}  {out_zip.name}\n")
    print(f"built {out_zip} ({digest})")
    return out_zip


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plugin_dir", type=pathlib.Path)
    parser.add_argument("--output-dir", type=pathlib.Path, default=pathlib.Path("dist"))
    parser.add_argument(
        "--repo-root",
        type=pathlib.Path,
        default=REPO_ROOT,
        help="Directory holding LICENSE and COPYRIGHT (default: this repository)",
    )
    args = parser.parse_args()
    build(args.plugin_dir, args.output_dir, args.repo_root)
    return 0


if __name__ == "__main__":
    sys.exit(main())
