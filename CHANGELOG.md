# Changelog

All notable changes are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this repo
follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html) on a
per-plugin basis (tag format `v-<plugin>-X.Y.Z`).

## calibre-bridge

### [0.5.0] - Unreleased

#### Security

- **Optional ingest-root restriction** — the path guard previously only
  blocked `..`, so absolute paths (e.g. `/etc/passwd`) and symlink escapes
  were ingested. A new `ingest_root` config key (default empty) restricts
  adds to files whose resolved real path lives inside it; symlink escapes are
  caught via `resolve()`. Empty preserves the historical no-restriction
  behaviour for backward compatibility. Violations return `400`.
- **Request body size cap** — `POST /v1/books` read the body using an
  unbounded `Content-Length`, allowing a remote OOM. A `max_body_bytes`
  config key (default 64 MiB) now rejects oversized requests with `413`
  before the body is read.
- **Fail closed without an api_key** — the server now refuses to start when
  bound to a non-loopback host (anything other than `127.0.0.1` / `localhost`
  / `::1`) with an empty `api_key`, instead of silently exposing the
  unauthenticated add endpoint. Loopback binds and any bind with an api_key
  set are unaffected.

#### Fixed

- A malformed (non-numeric) `Content-Length` header now returns `400` instead
  of raising an uncaught `ValueError` that severed the request connection.
- Extensionless paths are rejected with `400` instead of passing an empty
  format key to Calibre's `add_books`.
- An unparseable optional `seriesIndex` is now ignored rather than aborting
  the entire add, mirroring the existing `rating` handling.

#### Added

- `POST /v1/books` now accepts optional Bindery metadata and applies it to
  the Calibre metadata object before adding the book.
- `GET /v1/health` now advertises `capabilities: ["book_metadata"]` so
  Bindery can safely distinguish metadata-capable plugin versions from
  older path-only releases.

#### Fixed

- **GUI did not refresh after a sync** — newly added books only appeared
  after a manual Ctrl+R. The refresh hook imported `PyQt5.Qt`, which fails on
  Qt6 Calibre (6+), so the scheduled callback never ran; and even when it did,
  it called `resort()`, which only re-orders already-loaded rows. Now imports
  `qt.core` (with a `PyQt5` fallback) and schedules
  `library_view.model().books_added()` + `tags_view.recount()` on the GUI
  thread so inserts show up immediately. Duplicate (409) responses skip the
  refresh.

### [0.4.0] - 2026-05-13

#### Fixed

- **Qt thread crash on book add** — `add_books()` was called with `run_hooks=True`, causing Calibre's hook system to update Qt GUI widgets from the HTTP server's background thread. The handler thread aborted without sending a response; callers saw an empty TCP reply / EOF. Fixed by passing `run_hooks=False` and scheduling a `QTimer.singleShot(0, ...)` on the GUI thread so the library view still refreshes automatically after each add.
- **Duplicate path returned Metadata tuple instead of id** — `add_books()` returns `(ids, dups)` where `dups` is a list of `(mi, format_map)` input tuples, not book ids. The old code did `list(dups)[0]`, returning a `Metadata` object; the handler's `int()` coercion then raised `TypeError` outside the try/except, again producing an empty TCP reply. Fixed by using `db.new_api.find_identical_books(mi)` to recover the existing library id. If no match is found, returns `id=0` with `duplicate=True` rather than crashing.
- Added `_coerce_book_id()` defensive guard in the handler so any future regression in the adder yields a clean `id=0` rather than an EOF.

### [0.3.1] - 2026-04-21

- Test suite expanded from 5 to 20 tests — adds 12 handler edge-case tests and 3 `BridgeServer` lifecycle tests. All HTTP error paths are now covered (401, 404, 400 variants, 409 duplicate, 503 library not ready, empty-key bypass).
- Adds `SECURITY.md` with supported-version table and responsible disclosure policy.
- Pinned all GitHub Actions in `ci.yml`, `scorecard.yml`, and `security.yml` to commit SHAs (OpenSSF Scorecard `Pinned-Dependencies` compliance).
- Fixed pre-existing ruff lint issues (`UP031`, `SIM105`, `I001`) and Bandit findings (`B110`, `B104`).
- Fixed pre-existing `mypy` and `helm lint` failures in CI.
- Corrects `PLUGIN_VERSION` in `handlers.py` (was still `"0.2.0"`, now matches `__init__.py` version tuple).

### [0.3.0] - 2026-04-17

- Added **Show/Hide** toggle button to API-key field in the config dialog — reveals or masks the key on demand; label updates to reflect current state.
- Added **Generate** button — fills the API-key field with `os.urandom(32).hex()` and auto-reveals it so the user can inspect/copy before saving.

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
