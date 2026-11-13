#!/usr/bin/env python3
"""Point the Helm chart and the ArgoCD example at a published plugin release.

This is the whole of the chart PR that follows a release. It sets, in one go:

- `appVersion` in `charts/calibre-plugin-installer/Chart.yaml`
- `pluginVersion` in `charts/calibre-plugin-installer/values.yaml`
- `pluginVersion` and `targetRevision` in `argocd/application.yaml`

and bumps the chart's own patch `version` when the chart itself changed.

Usage:
    python scripts/bump_chart.py X.Y.Z
"""

from __future__ import annotations

import argparse
import pathlib
import re
import sys
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
    Pin(
        "ArgoCD targetRevision",
        ARGOCD_APP,
        re.compile(rf"^([ \t]+targetRevision:[ \t]*v-calibre-bridge-)({_V})([ \t]*)$", re.M),
    ),
)

_CHART_VERSION = re.compile(r"^(version:[ \t]*)(\d+)\.(\d+)\.(\d+)([ \t]*)$", re.M)


class ChartError(Exception):
    """A chart file does not have the shape this script edits."""


def _only_match(pattern: re.Pattern[str], text: str, what: str) -> re.Match[str]:
    matches = list(pattern.finditer(text))
    if len(matches) != 1:
        raise ChartError(f"expected exactly one {what}, found {len(matches)}")
    return matches[0]


def read_pins(repo_root: pathlib.Path = REPO_ROOT) -> dict[str, str]:
    """The version each pin currently names, keyed by pin name."""
    found = {}
    for pin in PINS:
        text = (repo_root / pin.path).read_text(encoding="utf-8")
        found[pin.name] = _only_match(pin.pattern, text, f"{pin.name} in {pin.path}").group(2)
    return found


def bump(version: str, repo_root: pathlib.Path = REPO_ROOT) -> list[str]:
    """Set every pin to `version`. Returns the files that changed."""
    if re.fullmatch(_V, version) is None:
        raise ChartError(f"{version!r} is not a plugin version: expected X.Y.Z")

    texts: dict[str, str] = {}
    for pin in PINS:
        text = texts.get(pin.path)
        if text is None:
            text = (repo_root / pin.path).read_text(encoding="utf-8")
        match = _only_match(pin.pattern, text, f"{pin.name} in {pin.path}")
        texts[pin.path] = text[: match.start(2)] + version + text[match.end(2) :]

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
    parser.add_argument("--repo-root", type=pathlib.Path, default=REPO_ROOT)
    args = parser.parse_args(argv)

    try:
        changed = bump(args.version, args.repo_root)
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
