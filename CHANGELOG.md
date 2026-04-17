# Changelog

All notable changes are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this repo
follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html) on a
per-plugin basis (tag format `v-<plugin>-X.Y.Z`).

## calibre-bridge

### [0.2.0] - 2026-04-17

- Replace `PyQt5.Qt` imports with calibre's `qt.core` compatibility shim for
  forward compatibility with Qt6.
- Qt imports in `config.py` are now at module scope (acceptable — the module
  is only ever loaded lazily in GUI context via `actual_plugin` indirection).
- `genesis()` imports `load_config` and `BridgeServer` lazily to avoid any
  import-time side effects before the GUI is ready.
- Added `_restart_server()`: applying new settings in the config dialog now
  restarts the HTTP server in-place without requiring a Calibre restart.
- Changed default `bind_host` from `127.0.0.1` to `0.0.0.0` so the server
  is reachable from other pods/containers out of the box.
- Simplified `_get_db()` — removed broken `_db_ready` flag that was set
  `False` and immediately reset `True` in a no-op `finally` block.

### [0.1.0] - 2026-04-17

Initial release.

- HTTP server (`ThreadingHTTPServer`) starts in `genesis()` and stops in
  `shutting_down()`, bound to configurable host/port (default
  `127.0.0.1:8099`).
- `GET /v1/health` returns plugin version, Calibre version, and active
  library path.
- `POST /v1/books` accepts `{"path": "..."}` and adds the book via
  `db.new_api.add_books` with `add_duplicates=False`. Returns `201` on add,
  `409` on duplicate, `401` without a valid bearer token, `503` during a
  library swap.
- Configuration dialog (Preferences -> Plugins -> Bindery Bridge ->
  Customize) stores `port`, `bind_host`, `api_key` via `JSONConfig`.
