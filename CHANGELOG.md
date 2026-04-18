# Changelog

All notable changes are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this repo
follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html) on a
per-plugin basis (tag format `v-<plugin>-X.Y.Z`).

## calibre-bridge

### [0.4.0] - 2026-04-18

#### Fixed

- Duplicate books no longer crash `POST /v1/books` with an empty TCP
  reply. `add_book()` now looks up the existing library id via
  `find_identical_books(mi)` instead of returning the raw `(mi, fmt_map)`
  tuple that Calibre's `add_books()` reports for duplicate inputs. The
  handler also defensively coerces the returned id so any future
  regression surfaces as id=0 rather than another EOF. This bug was
  triggered whenever a book already existed in the Calibre library — it
  silently looked like "all books failed" from Bindery's side.

#### Added

- Regression tests for the duplicate path and the
  no-identical-match fallback. The pre-existing test only covered the
  net-new add path, which is why the bug shipped.

### [0.3.0] - 2026-04-18

#### Fixed

- `POST /v1/books` no longer crashes with an empty TCP reply when Calibre's
  hook system tries to update Qt widgets from the background HTTP thread.
  `add_books` is now called with `run_hooks=False` to avoid unsafe
  cross-thread GUI access.
- After a successful add the GUI library view is refreshed via
  `QTimer.singleShot(0, ...)` on the main thread so new books appear without
  requiring a manual Ctrl+R.

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
