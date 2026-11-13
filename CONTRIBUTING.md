# Contributing

## Repo layout

```
bindery-plugins/
├── plugins/
│   └── calibre-bridge/  # Calibre plugin (plugin/, tests/, conftest.py)
├── scripts/
│   └── scaffold_plugin.py
└── charts/
    └── calibre-plugin-installer/
```

## Scaffold a new plugin

```
python scripts/scaffold_plugin.py my-plugin --port 8102
```

This creates `plugins/my-plugin/` with:
- `__init__.py` — Calibre plugin entry point (`InterfaceActionBase` subclass)
- `conftest.py` — stubs calibre/Qt at collection time (required)
- `plugin/action.py` — `InterfaceAction` with server lifecycle
- `plugin/server.py`: `PluginServer`, the threaded HTTP server wrapper
- `plugin/handlers.py`: HTTP handler with a timing safe bearer check
- `plugin/config.py`: Qt config widget with a `commit()` method
- `tests/conftest.py`: the `calibre_stubs` fixture
- `tests/test_handlers.py` — starter test

## Plugin anatomy

**Every module a plugin imports must live inside that plugin's own
directory.** `scripts/build_plugin.py` zips `plugins/<name>/**` plus the
repository's `LICENSE` and `COPYRIGHT`, and nothing else, so an import from
anywhere else in this repo resolves when you run pytest and then fails when
Calibre loads the released zip. A shared
`pluginbase/` package existed until 0.6.0 for exactly this purpose and was
removed once it became clear it could never have shipped: nothing imported it,
it was excluded from coverage, and it carried a second copy of the bearer
check that still used `==` after the real one was fixed. One implementation,
inside the plugin, is the rule.

If two plugins ever do need to share code, the honest options are to vendor it
into each plugin directory or to teach `build_plugin.py` to copy a shared
package into the zip the way it already copies the licence files. Pick one
deliberately rather than relying on the repo root being importable.
`tests/test_build_plugin.py` and `tests/test_scaffold_plugin.py` assert this
invariant, so a regression fails CI rather than a user's Calibre.

## Tests

### conftest setup

Every plugin directory needs a root-level `conftest.py` that stubs calibre and
Qt before pytest collects the package, and a `tests/conftest.py` with the
fixtures the tests share. `scaffold_plugin.py` generates both.

### Running tests

```bash
# Everything
pytest

# One plugin, with coverage
pytest plugins/calibre-bridge/tests --cov=plugins/calibre-bridge/plugin

# The release tooling under scripts/
pytest tests

# With bandit security scan
bandit -r plugins/calibre-bridge/plugin
```

The interpreter matters. The test matrix is Python 3.10, 3.11 and 3.14,
because that is what Calibre 6, Calibre 7 and 8, and Calibre 9 embed. No
Calibre release has ever embedded 3.12 or 3.13, so passing on those proves
nothing about the interpreter the plugin will actually run on.

### Coverage floor

Aim for ≥ 80% line coverage across `plugin/`. CI will report coverage;
PRs dropping below 80% should justify the gap in the description.

Include tests for:
- Auth (valid key, empty key = allow-all, wrong key = 401)
- `GET /v1/health` returns version string
- DB-not-ready path (503)
- Path traversal inputs (if `add_book`-style logic exists)
- Config widget init, commit, and generate-key button

### Real Calibre checks

The tests above replace Calibre and Qt with fakes, so they prove the plugin
agrees with our idea of Calibre, not with Calibre. Two bugs shipped that way
with a green build: the plugin failed to start because `genesis` read an
attribute the main window does not have yet (#26), and delivered books stayed
out of the library list until Ctrl+R because the view was never told about
them (#30).

The `Calibre integration` workflow (`.github/workflows/calibre-integration.yml`)
runs the built plugin zip inside a real Calibre, installed with Calibre's own
Linux installer. It runs on every pull request and every push to `main`
against the last 6.x, 7.x and 8.x releases and a pinned 9.x (the versions are
listed in the workflow). A weekly run and a manual run add whatever Calibre
released most recently; that leg reports a failure without failing the run.

The scripts live in `plugins/calibre-bridge/tests/integration/`, which
`build_plugin.py` keeps out of the release zip. There are two checks:

- **Model check** (`model_check.py`, run inside `calibre-debug`). A real
  library, the real `BooksModel` the library list is drawn from, and the
  plugin imported from the installed zip. It delivers a generated EPUB through
  `add_book_detailed` on a worker thread, lets the plugin's own refresh run,
  and asserts that the model grew by one row, that a search through the view
  finds the book, that the format is attached, that a second delivery and an
  ISBN match are duplicates, and that `addFormat` repaints the row. Only the
  main window is a stand in; the script's docstring says exactly which parts.
- **Boot check** (in `run.py`). Starts the whole Calibre GUI with the plugin
  configured on a loopback port, waits for `GET /v1/health`, posts a book and
  expects a `201`, posts it again and expects a `409`, shuts Calibre down with
  `calibre -s`, and fails if Calibre printed a traceback from the plugin.

To run them yourself you need a Calibre install and nothing else. No display
is needed, and your own Calibre settings and library are not touched: every
Calibre process gets a temporary configuration directory. Close your own
Calibre before the boot check. Calibre allows one running copy per user, so
the check refuses to start while it finds one; the model check does not mind.

```bash
# Both checks, against the Calibre on your PATH
python3 plugins/calibre-bridge/tests/integration/run.py

# One check, against a specific install
python3 plugins/calibre-bridge/tests/integration/run.py model --calibre-dir /opt/calibre

# A specific Calibre version, unpacked next to your own without replacing it
wget -nv -O- https://download.calibre-ebook.com/linux-installer.sh \
  | sh /dev/stdin install_dir=~/calibre-9.18.0 isolated=y version=9.18.0
python3 plugins/calibre-bridge/tests/integration/run.py --calibre-dir ~/calibre-9.18.0/calibre
```

`--plugin-dir` points the checks at another copy of the plugin source, for
example a worktree of an older tag, to see whether they would have caught a
bug. The workflow's manual run has a `plugin_ref` input that does the same.

When a change touches how books reach the library or the list, or what
`genesis` does, add the expectation to these scripts as well as to the unit
tests. When a Calibre version in the matrix fails, it is a finding about the
plugin until shown otherwise: fix it, or mark that leg as expected to fail
with a comment that links the issue.

## Type hints

All production code must pass mypy under the project's settings in
`pyproject.toml`. Run:

```bash
# Plugin with hyphen in directory name requires --explicit-package-bases
mypy --explicit-package-bases plugins/calibre-bridge/plugin
mypy --ignore-missing-imports scripts/
```

Add type annotations to all public function signatures. The calibre/Qt
stubs are excluded via `[[tool.mypy.overrides]]` in `pyproject.toml`.

## Code style

```bash
ruff check plugins/ scripts/   # lint
ruff format plugins/ scripts/  # format
```

Line length: 100. Python 3.10+.

## Release tags

Tags follow the convention `v-<plugin-name>-X.Y.Z`:

```
v-calibre-bridge-0.6.3
v-my-plugin-1.0.0
```

The CI `release` job fires on any tag matching `v*`, builds calibre-bridge,
and takes the release notes from the matching `### [X.Y.Z]` section of
`CHANGELOG.md`. The zip inside the release is still named
`calibre-bridge-vX.Y.Z.zip`; only the tag carries the `v-` prefix. A future
plugin will need its own `release` job gated on its own `v-<plugin>-` tags.

Bump `version = (X, Y, Z)` in the plugin's `__init__.py` and
`PLUGIN_VERSION` in `plugin/handlers.py` to match the tag.

## Helm chart

The `charts/calibre-plugin-installer` chart downloads and installs a plugin
zip at pod startup. It builds the download URL from `pluginVersion` through
`releaseUrlTemplate`, so after a release is published bump `pluginVersion` in
`values.yaml` (and the pin in `argocd/application.yaml`) to the new version.

The init container uses curl with retry flags — do not replace with bare
`curl <url>`. See `templates/patch.yaml` for the required flags.
