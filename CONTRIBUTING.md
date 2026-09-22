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

Tags follow the convention `<plugin-name>-vX.Y.Z`:

```
calibre-bridge-v0.4.0
my-plugin-v1.0.0
```

The CI `release` job fires on any tag matching `v*`. A `calibre-bridge-v*`
tag builds and releases calibre-bridge; future plugins can add their own
`release` jobs gated on `<plugin>-v*` tags.

Bump `version = (X, Y, Z)` in the plugin's `__init__.py` and
`PLUGIN_VERSION` in `plugin/handlers.py` to match the tag.

## Helm chart

The `charts/calibre-plugin-installer` chart downloads and installs a plugin
zip at pod startup. Update `values.yaml` `pluginUrl` when cutting a release.

The init container uses curl with retry flags — do not replace with bare
`curl <url>`. See `templates/patch.yaml` for the required flags.
