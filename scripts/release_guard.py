#!/usr/bin/env python3
"""Decide whether a pushed tag may become a GitHub Release.

The CI `release` job runs this before it builds or publishes anything. It
fails unless all of these hold:

- the tag is `v-<plugin>-X.Y.Z`, three numeric parts and nothing else;
- X.Y.Z is the version in the plugin's `__init__.py`;
- the tagged commit is an ancestor of main;
- CHANGELOG.md has a `### [X.Y.Z]` section for the plugin, with notes in it.

On success it writes that section to `--notes-out` as the release body.

Usage:
    python scripts/release_guard.py --tag v-calibre-bridge-X.Y.Z \
        [--plugin-dir plugins/calibre-bridge] [--notes-out release_notes.md]
"""

from __future__ import annotations

import argparse
import os
import pathlib
import re
import subprocess  # nosec B404 - git is called with a fixed argument list
import sys
from typing import NamedTuple

from build_plugin import read_version

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

VERSION_PATTERN = r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)"
_VERSION = re.compile(VERSION_PATTERN)
_SECTION_HEADING = re.compile(r"^### \[(?P<label>[^\]\n]+)\][^\n]*$", re.M)

UNRELEASED = "Unreleased"


class GuardError(Exception):
    """A reason the tag must not be released."""


class Section(NamedTuple):
    label: str  # "0.8.3" or "Unreleased"
    heading: str  # the whole "### [0.8.3] - 2026-11-12" line
    body: str


def version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def is_version(text: str) -> bool:
    return _VERSION.fullmatch(text) is not None


def parse_tag(tag: str, plugin: str) -> str:
    """Return X.Y.Z from `v-<plugin>-X.Y.Z`, or raise."""
    match = re.fullmatch(rf"v-{re.escape(plugin)}-({VERSION_PATTERN})", tag)
    if match is None:
        raise GuardError(
            f"tag {tag!r} is not a {plugin} release tag: expected v-{plugin}-X.Y.Z "
            "with three numeric parts"
        )
    return match.group(1)


def plugin_sections(changelog: str, plugin: str) -> list[Section]:
    """The `### [...]` sections under the plugin's `## <plugin>` heading."""
    start = re.search(rf"^## {re.escape(plugin)}[ \t]*$", changelog, re.M)
    if start is None:
        raise GuardError(f"CHANGELOG has no '## {plugin}' part")
    part = changelog[start.end() :]
    following = re.search(r"^## ", part, re.M)
    if following is not None:
        part = part[: following.start()]

    headings = list(_SECTION_HEADING.finditer(part))
    sections = []
    for index, heading in enumerate(headings):
        end = headings[index + 1].start() if index + 1 < len(headings) else len(part)
        sections.append(
            Section(heading.group("label").strip(), heading.group(0), part[heading.end() : end])
        )
    return sections


def released_versions(changelog: str, plugin: str) -> list[str]:
    """Every released version in the changelog. `[Unreleased]` is not one."""
    return [s.label for s in plugin_sections(changelog, plugin) if is_version(s.label)]


def latest_release(changelog: str, plugin: str) -> str:
    versions = released_versions(changelog, plugin)
    if not versions:
        raise GuardError(f"CHANGELOG has no released '### [X.Y.Z]' section under '## {plugin}'")
    return max(versions, key=version_key)


def has_notes(body: str) -> bool:
    """True when a section holds something besides blank lines and headings."""
    return any(line.strip() and not line.lstrip().startswith("#") for line in body.splitlines())


def release_notes(changelog: str, plugin: str, version: str) -> str:
    """The `### [X.Y.Z]` section, heading included, as the release body.

    Only a numeric version selects a section, so `[Unreleased]` can never be
    published as release notes, and a missing or empty section is an error
    rather than a generic body.
    """
    if not is_version(version):
        raise GuardError(f"{version!r} is not a released version")
    for section in plugin_sections(changelog, plugin):
        if section.label != version:
            continue
        if not has_notes(section.body):
            raise GuardError(f"CHANGELOG section '### [{version}]' under '## {plugin}' is empty")
        return (section.heading + section.body).strip() + "\n"
    raise GuardError(f"CHANGELOG has no '### [{version}]' section under '## {plugin}'")


def is_ancestor(repo: pathlib.Path, commit: str, main_ref: str) -> bool:
    """True when `commit` is `main_ref` or one of its ancestors."""
    for ref in (commit, main_ref):
        if ref.startswith("-"):
            raise GuardError(f"refusing to pass {ref!r} to git")
    result = subprocess.run(  # nosec B603 B607 - fixed argv, refs checked above
        ["git", "-C", str(repo), "merge-base", "--is-ancestor", commit, main_ref],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    raise GuardError(
        f"could not compare {commit} with {main_ref}: "
        f"{result.stderr.strip() or 'git exited ' + str(result.returncode)}"
    )


def check(
    tag: str,
    plugin_dir: pathlib.Path,
    changelog_path: pathlib.Path,
    repo: pathlib.Path,
    commit: str = "HEAD",
    main_ref: str = "origin/main",
) -> str:
    """Run every check and return the release notes. Raises GuardError."""
    plugin = plugin_dir.name
    # Nothing else can be judged without a version, so a bad tag stops here.
    version = parse_tag(tag, plugin)

    problems = []
    notes = ""

    declared = read_version(plugin_dir / "__init__.py")
    if declared != version:
        problems.append(
            f"tag {tag} says {version} but {plugin_dir / '__init__.py'} declares {declared}"
        )

    try:
        if not is_ancestor(repo, commit, main_ref):
            problems.append(
                f"{commit} is not on {main_ref}: tag a commit that main contains "
                "(a shallow clone gives the same answer, fetch full history first)"
            )
    except GuardError as exc:
        problems.append(str(exc))

    try:
        notes = release_notes(changelog_path.read_text(encoding="utf-8"), plugin, version)
    except GuardError as exc:
        problems.append(str(exc))

    if problems:
        raise GuardError("\n".join(problems))
    return notes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--tag", required=True, help="the pushed tag, e.g. v-calibre-bridge-1.2.3")
    parser.add_argument(
        "--plugin-dir", type=pathlib.Path, default=REPO_ROOT / "plugins" / "calibre-bridge"
    )
    parser.add_argument("--changelog", type=pathlib.Path, default=REPO_ROOT / "CHANGELOG.md")
    parser.add_argument("--repo", type=pathlib.Path, default=REPO_ROOT, help="git work tree")
    parser.add_argument("--commit", default="HEAD", help="the tagged commit (default: HEAD)")
    parser.add_argument("--main-ref", default="origin/main")
    parser.add_argument("--notes-out", type=pathlib.Path, help="write the release notes here")
    args = parser.parse_args(argv)

    try:
        notes = check(
            args.tag, args.plugin_dir, args.changelog, args.repo, args.commit, args.main_ref
        )
    except GuardError as exc:
        for problem in str(exc).splitlines():
            print(f"release guard: {problem}", file=sys.stderr)
            if os.environ.get("GITHUB_ACTIONS") == "true":
                print(f"::error title=Release guard::{problem}")
        return 1

    if args.notes_out is not None:
        args.notes_out.write_text(notes, encoding="utf-8")
    print(f"release guard: {args.tag} may be released")
    return 0


if __name__ == "__main__":
    sys.exit(main())
