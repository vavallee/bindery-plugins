# Contributing

## Repo layout

```
bindery-plugins/
├── pluginbase/          # Shared helpers (http, server, config, testing)
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
- `plugin/handlers.py` — HTTP handler using `pluginbase.http`
- `plugin/config.py` — `BaseConfigWidget` subclass
- `tests/conftest.py` — re-exports `calibre_stubs` fixture
- `tests/test_handlers.py` — starter test

## Plugin anatomy

Use `pluginbase` instead of copy-pasting boilerplate:

| Module | What it provides |
|--------|-----------------|
| `pluginbase.http` | `ok()`, `bad_request()`, `check_bearer()`, etc. |
| `pluginbase.server` | `PluginServer` (start/stop/is_running) |
| `pluginbase.config` | `BaseConfigWidget` — implement `_save_values` / `_load_values` |
| `pluginbase.testing` | `calibre_stubs` fixture, `make_calibre_stub()`, `load_plugin_module()` |

## Tests

### conftest setup

Every plugin directory needs a root-level `conftest.py` that patches
calibre/Qt before pytest collects the package:

```python
from pluginbase.testing import make_calibre_stub, patch_calibre_modules
patch_calibre_modules(make_calibre_stub())
```

The `tests/conftest.py` re-exports the per-test fixture:

```python
from pluginbase.testing import calibre_stubs  # noqa: F401
```

### Running tests

```bash
# One plugin
pytest plugins/calibre-bridge/tests --cov=plugins/calibre-bridge/plugin

# With bandit security scan
bandit -r plugins/calibre-bridge/plugin
```

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
