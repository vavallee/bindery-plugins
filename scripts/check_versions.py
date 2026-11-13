#!/usr/bin/env python3
"""Check that every place naming a version agrees with the others.

CI runs this on every push and pull request, so a release PR or a chart PR
that leaves something behind fails before it is merged, not at tag time.

The plugin version is written once, in the plugin's `__init__.py`. It has to
equal the newest `### [X.Y.Z]` section of CHANGELOG.md, because the release
PR changes both together, and `### [Unreleased]` has to stay the first section
so the next entry has somewhere to go.

The Helm chart and the ArgoCD example name the release they install. They
legitimately trail the plugin between a release and the chart PR that follows
it, so they are checked against each other and required to name a release the
changelog knows about, not compared with `__init__.py`.

Usage:
    python scripts/check_versions.py
"""

from __future__ import annotations

import argparse
import pathlib
import sys

import bump_chart
import release_guard
from build_plugin import read_version

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
PLUGIN_DIR = "plugins/calibre-bridge"


def problems(repo_root: pathlib.Path = REPO_ROOT, plugin_dir: str = PLUGIN_DIR) -> list[str]:
    """Everything that disagrees, as one message each. Empty means consistent."""
    found: list[str] = []
    plugin = pathlib.PurePosixPath(plugin_dir).name
    declared = read_version(repo_root / plugin_dir / "__init__.py")
    changelog = (repo_root / "CHANGELOG.md").read_text(encoding="utf-8")

    latest = None
    try:
        sections = release_guard.plugin_sections(changelog, plugin)
        if not sections or sections[0].label != release_guard.UNRELEASED:
            found.append(
                f"CHANGELOG: the first section under '## {plugin}' must be "
                f"'### [{release_guard.UNRELEASED}]'"
            )
        latest = release_guard.latest_release(changelog, plugin)
    except release_guard.GuardError as exc:
        found.append(str(exc))

    if latest is not None and declared != latest:
        found.append(
            f"{plugin_dir}/__init__.py declares {declared} but the newest CHANGELOG "
            f"section is [{latest}]: a release PR changes both"
        )

    try:
        pins = bump_chart.read_pins(repo_root)
    except bump_chart.ChartError as exc:
        found.append(str(exc))
        return found

    if len(set(pins.values())) > 1:
        listing = ", ".join(f"{name} {version}" for name, version in pins.items())
        found.append(
            f"the chart and ArgoCD pins disagree ({listing}): "
            "run python scripts/bump_chart.py X.Y.Z"
        )
    if latest is not None:
        for name, version in pins.items():
            if release_guard.version_key(version) > release_guard.version_key(latest):
                found.append(
                    f"{name} is {version}, ahead of the newest CHANGELOG section [{latest}]"
                )
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--repo-root", type=pathlib.Path, default=REPO_ROOT)
    args = parser.parse_args(argv)

    found = problems(args.repo_root)
    for problem in found:
        print(f"check_versions: {problem}", file=sys.stderr)
    if found:
        return 1
    print("check_versions: versions agree")
    return 0


if __name__ == "__main__":
    sys.exit(main())
