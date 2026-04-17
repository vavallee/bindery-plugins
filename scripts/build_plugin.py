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

EXCLUDE_DIRS = {'__pycache__', 'tests', '.pytest_cache', '.mypy_cache'}
EXCLUDE_SUFFIXES = {'.pyc', '.pyo'}


def read_version(init_path: pathlib.Path) -> str:
    tree = ast.parse(init_path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for stmt in node.body:
                if isinstance(stmt, ast.Assign):
                    for target in stmt.targets:
                        if isinstance(target, ast.Name) and target.id == 'version':
                            value = ast.literal_eval(stmt.value)
                            return '.'.join(str(p) for p in value)
    raise RuntimeError('could not find version tuple in %s' % init_path)


def plugin_zip_name(plugin_dir: pathlib.Path, version: str) -> str:
    slug = plugin_dir.name.replace('_', '-')
    return f'{slug}-v{version}.zip'


def build(plugin_dir: pathlib.Path, output_dir: pathlib.Path) -> pathlib.Path:
    init_file = plugin_dir / '__init__.py'
    if not init_file.is_file():
        raise SystemExit(f'missing {init_file}')
    version = read_version(init_file)
    output_dir.mkdir(parents=True, exist_ok=True)
    out_zip = output_dir / plugin_zip_name(plugin_dir, version)

    with zipfile.ZipFile(out_zip, 'w', compression=zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(plugin_dir.rglob('*')):
            if path.is_dir():
                continue
            rel = path.relative_to(plugin_dir)
            if any(part in EXCLUDE_DIRS for part in rel.parts):
                continue
            if path.suffix in EXCLUDE_SUFFIXES:
                continue
            zf.write(path, rel.as_posix())

    digest = hashlib.sha256(out_zip.read_bytes()).hexdigest()
    (output_dir / (out_zip.name + '.sha256')).write_text(f'{digest}  {out_zip.name}\n')
    print(f'built {out_zip} ({digest})')
    return out_zip


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('plugin_dir', type=pathlib.Path)
    parser.add_argument('--output-dir', type=pathlib.Path, default=pathlib.Path('dist'))
    args = parser.parse_args()
    build(args.plugin_dir, args.output_dir)
    return 0


if __name__ == '__main__':
    sys.exit(main())
