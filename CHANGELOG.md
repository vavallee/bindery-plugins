# Changelog

All notable changes are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this repo
follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html) on a
per-plugin basis (tag format `v-<plugin>-X.Y.Z`).

## calibre-bridge

### [0.6.0] - 2026-09-22

#### Fixed

- **The duplicate explosion.** `add_books` is called with
  `add_duplicates=True` whenever Bindery supplies its own `bindery`
  identifier, and the only dedupe left was an exact search for that
  identifier, which by definition never matches a library Bindery did not
  fill. The first "Push all to Calibre" against a library populated by
  Calibre Web Automated, by `calibredb`, by hand or by plugin 0.4.0 therefore
  cloned the whole library and reported it as success. There is now a fallback
  ladder: the `bindery` identifier, then `isbn`, `asin`, `google` and
  `hardcover`, then `find_identical_books`. Any hit returns 409 with the
  existing id.

  The original reason for `add_duplicates=True` is intact. Calibre's own check
  behind `add_duplicates=False` is `Cache.has_book`, which matches on title
  alone and would collapse three different poets' "The Complete Poems" into
  one row. `find_identical_books` is a different heuristic that requires a
  superset of the authors as well as a fuzzy title match, which is why it is
  safe as the last rung.

- **The identifier search was malformed whenever it needed quoting.** An exact
  search built for a value containing a space, a parenthesis or a colon was
  emitted as `identifiers:=bindery:"=series:42 copy"`. Calibre's lexer matches
  a bare word with `[^"()\s]+`, which stops at a quote, so that parsed as two
  ANDed terms rather than one and matched nothing. The quote belongs around
  the whole term: `identifiers:"=bindery:=series:42 copy"`. Two related fixes
  in the same helper: a value starting with `=`, `~` or `^` no longer gets a
  backslash that `_matchkind` leaves in the compared literal, and a value of
  `true` or `false` is refused outright because `KeyPairSearch` turns it into
  a test for the key's existence and would adopt an unrelated book.

  The common case, a numeric `bindery` id, was correct before and after.

#### Security

- **Timing safe bearer token comparison.** The check used `!=` on `str`, which
  returns as soon as two bytes differ and leaks the length of the matching
  prefix. Now `hmac.compare_digest`.

- **`pluginbase/` removed.** It had zero imports, was excluded from coverage,
  and carried a second copy of the bearer check that still used `==`. It also
  could never have shipped: the release zip is `plugins/<name>/` plus the
  licence files and nothing else, so a plugin importing it would have failed
  to load inside Calibre. `scaffold_plugin.py` generated exactly such plugins
  and now generates self contained ones. `tests/test_build_plugin.py` and
  `tests/test_scaffold_plugin.py` assert the invariant so it cannot come
  back.

#### Added

- **Machine readable error codes.** Every error response carries a `code`
  beside the existing `error` string: `unauthorized`, `db_unavailable`,
  `invalid_json`, `invalid_metadata`, `path_not_found`, `path_forbidden`,
  `bad_format`, `body_too_large`, `not_found`, `internal`. The `error` string
  is unchanged in meaning because older clients read it. The case that
  motivated this: a wrong container mount and a malformed metadata object were
  both a bare 400, so Bindery logged "metadata payload rejected" and re-sent
  the whole request for what was a filesystem problem.

- **`GET /v1/paths?path=<abs path>`** (authenticated) reports
  `{"path", "exists", "readable", "isDir"}` so the cross container mount
  mismatch can be diagnosed at setup time instead of after a library wide push
  has already failed. It never opens the file and it applies the same
  `ingest_root` restriction as an add. It deliberately does not need a
  database, so it still answers during a library swap.

- **`metadata.coverPath`** is read and applied. It was silently dropped
  before, and the push still returned 201. The path is validated exactly like
  the book path and capped at 16 MiB. A cover problem never fails the add: the
  book is added without it and the response carries `cover_applied: false`.

- **`PATCH /v1/books/{id}`** (authenticated) applies metadata to a book
  already in the library, so a 409 is no longer a dead end. The rule is fill
  only: a field is written when the Calibre row has nothing in it, a field the
  row already carries is left alone, and nothing is ever cleared. Calibre's
  `Unknown` placeholders count as empty, identifiers merge key by key, and
  `coverPath` is ignored.

- **A refused start is now discoverable.** When the server refuses to bind (a
  non loopback host with no `api_key`) it serves a health only endpoint on the
  same port reporting `status: "degraded"` with the reason, and answers adds
  with 401 carrying that reason, which is the status Bindery already renders
  as "check api_key in Settings". Before this the only signal was a five
  second Calibre status bar toast, invisible on a headless or KasmVNC install,
  and Bindery saw nothing but connection refused. The same state is shown as a
  persistent line in the plugin's configuration dialog.

- **`Retry-After` on every 503**, so a client has a concrete backoff hint for
  the library swap window.

- `GET /v1/health` advertises `cover`, `path_probe`, `metadata_update` and
  `error_codes` alongside `book_metadata`.

#### Changed

- `docs/protocol.md` rewritten to describe what the code does. It documented a
  `/v2/` plus `Deprecation` and `Sunset` policy neither side has ever
  implemented, said 409 came from `add_duplicates=false`, never mentioned the
  413 added in 0.5.0, and never mentioned `coverPath`. It now also states the
  client's 30 second timeout and the 503 backoff expectation.

- The calibre-bridge test suite goes from 65 tests to 153, and production
  coverage of `plugin/` from 82% to 96%. The `plugin/__init__.py` lifecycle
  and the `config.py` widget were at 60% and 51% and are now at 100%.
  `scripts/scaffold_plugin.py` had no tests at all and is now at 98%, covered
  from `tests/` alongside the rest of the release tooling.

#### Compatibility

Every addition is additive. A Bindery speaking 0.4.0 or 0.5.0 sees the same
status codes and the same response bodies plus fields it ignores, never sends
`coverPath`, and never calls the two new endpoints. The one behaviour change
an old client can observe is the dedupe ladder turning what used to be a
silent duplicate into a 409 with the existing id, which such clients already
treat as "already in Calibre".

### [0.5.0] - 2026-06-18

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
