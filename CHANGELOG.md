# Changelog

All notable changes are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this repo
follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html) on a
per-plugin basis (tag format `v-<plugin>-X.Y.Z`).

## calibre-bridge

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
