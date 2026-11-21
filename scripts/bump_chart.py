#!/usr/bin/env python3
"""Point the Helm chart and the ArgoCD example at a published plugin release.

This is the whole of the chart PR that follows a release. It sets, in one go:

- `appVersion` in `charts/calibre-plugin-installer/Chart.yaml`
- `pluginVersion` in `charts/calibre-plugin-installer/values.yaml`
- `pluginVersion`, `pluginSha256` and `targetRevision` in `argocd/application.yaml`
- `pluginSha256` in `values.yaml`, only when that file carries one (the
  chart default is empty, and the chart refuses to render with it empty)

and bumps the chart's own patch `version` when the chart itself changed.

`targetRevision` is either the tag, `v-calibre-bridge-X.Y.Z`, or the commit
the tag points at, `<40 hex>  # v-calibre-bridge-X.Y.Z`. The form already in
the file is kept. For the commit form the SHA comes from `--commit`, else from
the local tag, else from `git ls-remote`. The hash comes from `--sha256`, which
takes 64 hex digits, the path of a release `.sha256` file, or an https URL of
one. A hash is required whenever the version changes; the script never writes
an empty one and writes nothing at all when an input is missing.

Usage:
    python scripts/bump_chart.py X.Y.Z --sha256 <hex|file|url> [--commit <sha>]
"""

from __future__ import annotations

import argparse
import pathlib
import re
import subprocess
import sys
import urllib.request
from typing import NamedTuple

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

CHART_YAML = "charts/calibre-plugin-installer/Chart.yaml"
VALUES_YAML = "charts/calibre-plugin-installer/values.yaml"
ARGOCD_APP = "argocd/application.yaml"

_V = r"\d+\.\d+\.\d+"


class Pin(NamedTuple):
    """One place that names the plugin version the chart installs."""

    name: str
    path: str
    # Three groups: what precedes the version, the version, what follows it.
    pattern: re.Pattern[str]


# The files are edited as text rather than parsed, so comments and layout
# survive and the tooling keeps its zero dependency footprint. Each pattern
# has to match exactly once, which is what catches a file that changed shape.
PINS = (
    Pin(
        "Chart.yaml appVersion",
        CHART_YAML,
        re.compile(rf'^(appVersion:[ \t]*"?)({_V})("?[ \t]*)$', re.M),
    ),
    Pin(
        "values.yaml pluginVersion",
        VALUES_YAML,
        re.compile(rf'^(pluginVersion:[ \t]*"?)({_V})("?[ \t]*)$', re.M),
    ),
    # The generator element. The `"{{pluginVersion}}"` template line further
    # down is not numeric, so it does not match.
    Pin(
        "ArgoCD pluginVersion",
        ARGOCD_APP,
        re.compile(rf'^([ \t]+pluginVersion:[ \t]*"?)({_V})("?[ \t]*)$', re.M),
    ),
)

REPO_URL = "https://github.com/vavallee/bindery-plugins.git"
TAG_PREFIX = "v-calibre-bridge-"
_HEX40 = r"[0-9a-f]{40}"
_HEX64 = r"[0-9a-f]{64}"
_TARGET_NAME = "ArgoCD targetRevision"

# `targetRevision` has two accepted forms. Group 2 is the version in both.
TARGET_TAG = re.compile(rf"^([ \t]+targetRevision:[ \t]*{TAG_PREFIX})({_V})([ \t]*)$", re.M)
TARGET_SHA = re.compile(
    rf"^([ \t]+targetRevision:[ \t]*)({_HEX40})([ \t]+#[ \t]*{TAG_PREFIX})({_V})([ \t]*)$", re.M
)
# The generator element. The `"{{pluginSha256}}"` template line is not hex.
ARGOCD_HASH = re.compile(rf'^([ \t]+pluginSha256:[ \t]*")({_HEX64})("[ \t]*)$', re.M)
VALUES_HASH = re.compile(rf'^(pluginSha256:[ \t]*")({_HEX64})("[ \t]*)$', re.M)

_CHART_VERSION = re.compile(r"^(version:[ \t]*)(\d+)\.(\d+)\.(\d+)([ \t]*)$", re.M)


class ChartError(Exception):
    """A chart file does not have the shape this script edits."""


def _only_match(pattern: re.Pattern[str], text: str, what: str) -> re.Match[str]:
    matches = list(pattern.finditer(text))
    if len(matches) != 1:
        raise ChartError(f"expected exactly one {what}, found {len(matches)}")
    return matches[0]


def _target(text: str) -> tuple[re.Match[str], bool]:
    """The one targetRevision line, and whether it is the commit SHA form."""
    matches = [(m, False) for m in TARGET_TAG.finditer(text)] + [
        (m, True) for m in TARGET_SHA.finditer(text)
    ]
    if len(matches) != 1:
        raise ChartError(
            f"expected exactly one {_TARGET_NAME} in {ARGOCD_APP}, found {len(matches)}"
        )
    return matches[0]


def target_version(text: str) -> str:
    match, is_sha = _target(text)
    return match.group(4 if is_sha else 2)


def read_pins(repo_root: pathlib.Path = REPO_ROOT) -> dict[str, str]:
    """The version each pin currently names, keyed by pin name."""
    found = {}
    for pin in PINS:
        text = (repo_root / pin.path).read_text(encoding="utf-8")
        found[pin.name] = _only_match(pin.pattern, text, f"{pin.name} in {pin.path}").group(2)
    argocd = (repo_root / ARGOCD_APP).read_text(encoding="utf-8")
    found[_TARGET_NAME] = target_version(argocd)
    return found


def read_hashes(repo_root: pathlib.Path = REPO_ROOT) -> dict[str, str]:
    """What the ArgoCD example pins besides the version: its SHA-256 and, in the
    commit form, the commit. Raises when the hash line is missing or not hex."""
    argocd = (repo_root / ARGOCD_APP).read_text(encoding="utf-8")
    found = {
        "ArgoCD pluginSha256": _only_match(
            ARGOCD_HASH, argocd, f"64 hex digit pluginSha256 in {ARGOCD_APP}"
        ).group(2)
    }
    match, is_sha = _target(argocd)
    if is_sha:
        found["ArgoCD targetRevision commit"] = match.group(2)
    return found


def parse_sha256(value: str) -> str:
    """64 hex digits from a literal, a `.sha256` file path or an https URL."""
    value = value.strip()
    if re.fullmatch(_HEX64, value, re.I):
        return value.lower()
    if value.startswith("https://"):
        try:
            with urllib.request.urlopen(value, timeout=30) as response:  # noqa: S310
                content = response.read(4096).decode("utf-8", "replace")
        except OSError as exc:
            raise ChartError(f"could not fetch {value}: {exc}") from exc
    elif value.startswith(("http://", "ftp:")):
        raise ChartError("a hash URL must be https")
    else:
        try:
            content = pathlib.Path(value).read_text(encoding="utf-8")
        except OSError as exc:
            raise ChartError(
                f"--sha256 is neither 64 hex digits nor a readable file: {value!r}"
            ) from exc
    words = content.split()
    if not words or not re.fullmatch(_HEX64, words[0], re.I):
        raise ChartError(f"no 64 hex digit SHA-256 at the start of {value!r}")
    return words[0].lower()


def _git(args: list[str], cwd: pathlib.Path) -> str | None:
    try:
        done = subprocess.run(  # noqa: S603
            ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=60, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


def resolve_commit(
    version: str, repo_root: pathlib.Path = REPO_ROOT, *, remote: bool = True
) -> str | None:
    """The commit `v-calibre-bridge-<version>` points at, or None.

    Tries the local tag, then `git ls-remote`. For an annotated tag the peeled
    `^{}` line is the commit; a lightweight tag has only the plain line.
    """
    tag = f"{TAG_PREFIX}{version}"
    local = _git(["rev-parse", "--verify", "-q", f"refs/tags/{tag}^{{commit}}"], repo_root)
    if local and re.fullmatch(_HEX40, local.strip()):
        return local.strip()
    if not remote:
        return None
    listing = _git(["ls-remote", REPO_URL, f"refs/tags/{tag}", f"refs/tags/{tag}^{{}}"], repo_root)
    commit = None
    for line in (listing or "").splitlines():
        sha, _, ref = line.partition("\t")
        if not re.fullmatch(_HEX40, sha):
            continue
        if ref == f"refs/tags/{tag}^{{}}":
            return sha
        if ref == f"refs/tags/{tag}":
            commit = sha
    return commit


def bump(
    version: str,
    repo_root: pathlib.Path = REPO_ROOT,
    *,
    sha256: str | None = None,
    commit: str | None = None,
) -> list[str]:
    """Set every pin to `version`. Returns the files that changed.

    `sha256` is required when the version changes, so a bump can never leave
    the previous release's hash behind. Nothing is written unless every input
    resolved.
    """
    if re.fullmatch(_V, version) is None:
        raise ChartError(f"{version!r} is not a plugin version: expected X.Y.Z")
    if sha256 is not None:
        sha256 = parse_sha256(sha256)
    if commit is not None:
        commit = commit.strip().lower()
        if re.fullmatch(_HEX40, commit) is None:
            raise ChartError(f"--commit must be a 40 hex digit commit SHA, got {commit!r}")

    texts: dict[str, str] = {}
    for pin in PINS:
        text = texts.get(pin.path)
        if text is None:
            text = (repo_root / pin.path).read_text(encoding="utf-8")
        match = _only_match(pin.pattern, text, f"{pin.name} in {pin.path}")
        texts[pin.path] = text[: match.start(2)] + version + text[match.end(2) :]

    argocd = texts[ARGOCD_APP]
    already = set(read_pins(repo_root).values()) == {version}
    target, is_sha = _target(argocd)
    hash_match = _only_match(ARGOCD_HASH, argocd, f"64 hex digit pluginSha256 in {ARGOCD_APP}")

    if sha256 is None and not already:
        raise ChartError(
            f"{version} needs its SHA-256: pass --sha256 with 64 hex digits or the "
            "release's .sha256 file. Refusing to leave the previous release's hash in place."
        )
    if is_sha and commit is None and not already:
        commit = resolve_commit(version, repo_root)
        if commit is None:
            raise ChartError(
                f"{ARGOCD_APP} pins a commit, and {TAG_PREFIX}{version} is neither a local "
                "tag nor visible on the remote: pass --commit"
            )

    # Edit from the end of the file backwards so earlier offsets stay valid.
    edits: list[tuple[int, int, str]] = [(hash_match.start(2), hash_match.end(2), sha256 or "")]
    if is_sha:
        edits.append((target.start(4), target.end(4), version))
        if commit is not None:
            edits.append((target.start(2), target.end(2), commit))
    else:
        edits.append((target.start(2), target.end(2), version))
    for start, end, value in sorted(edits, reverse=True):
        if value:
            argocd = argocd[:start] + value + argocd[end:]
    texts[ARGOCD_APP] = argocd

    values_match = VALUES_HASH.search(texts[VALUES_YAML])
    if values_match and sha256 is not None:
        text = texts[VALUES_YAML]
        texts[VALUES_YAML] = text[: values_match.start(2)] + sha256 + text[values_match.end(2) :]

    changed = [
        path
        for path, text in texts.items()
        if text != (repo_root / path).read_text(encoding="utf-8")
    ]
    if not changed:
        return []

    # A chart whose content changed gets a new chart version. The ArgoCD
    # example is not part of the chart, so a change to it alone does not.
    if CHART_YAML in changed or VALUES_YAML in changed:
        chart = texts[CHART_YAML]
        match = _only_match(_CHART_VERSION, chart, f"chart version in {CHART_YAML}")
        patch = int(match.group(4)) + 1
        texts[CHART_YAML] = (
            chart[: match.start(2)]
            + f"{match.group(2)}.{match.group(3)}.{patch}"
            + chart[match.end(4) :]
        )
        if CHART_YAML not in changed:
            changed.append(CHART_YAML)

    for path in changed:
        (repo_root / path).write_text(texts[path], encoding="utf-8")
    return sorted(changed)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("version", help="the published plugin release, X.Y.Z")
    parser.add_argument(
        "--sha256", help="the zip's SHA-256: 64 hex digits, a .sha256 file path or an https URL"
    )
    parser.add_argument("--commit", help="the 40 hex digit commit the tag points at")
    parser.add_argument("--repo-root", type=pathlib.Path, default=REPO_ROOT)
    args = parser.parse_args(argv)

    try:
        changed = bump(args.version, args.repo_root, sha256=args.sha256, commit=args.commit)
    except ChartError as exc:
        print(f"bump_chart: {exc}", file=sys.stderr)
        return 1

    if not changed:
        print(f"bump_chart: already at {args.version}, nothing to do")
    for path in changed:
        print(f"bump_chart: updated {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
