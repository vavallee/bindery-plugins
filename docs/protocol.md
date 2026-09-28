# Bindery Bridge HTTP Protocol (v1)

The Bindery Bridge Calibre plugin exposes a small HTTP API that Bindery uses to
add and update books in the running Calibre library without shelling out to
`calibredb`. All endpoints are prefixed with `/v1/`.

This document describes what the plugin actually implements at 0.8.0. Anything
the code does not do is not in here.

## Versioning

There is no `/v2/`, and the server sends no `Deprecation` or `Sunset` headers.
Neither side has ever implemented that, so the policy has been removed rather
than left as a promise.

What exists instead is the `capabilities` list in `GET /v1/health`. Every
addition to this protocol is additive (a new endpoint, a new optional request
field, a new optional response field, a new response header), a client that
does not know about an addition keeps working unchanged, and a client that
wants to use one checks for the matching capability first.

Clients SHOULD send a `User-Agent`. Bindery currently sends
`bindery plugin-api/v1`. The server logs it and never rejects on it.

## Authentication

`POST` and `PATCH` endpoints, and `GET /v1/paths`, require a bearer token
matching the plugin's configured `api_key`. A bad or missing token gets `401`.
The comparison is constant time (`hmac.compare_digest`).

If `api_key` is empty the server accepts unauthenticated requests. That is only
reachable on a loopback bind: since 0.5.0 the server refuses to start when the
bind host is not loopback and no `api_key` is set. Since 0.6.0 that refusal
serves a degraded health endpoint instead of closing the port, so the cause is
visible rather than showing up as connection refused. See
[Degraded mode](#degraded-mode).

`GET /v1/health` is always unauthenticated so clients can probe readiness
without provisioning credentials, and it never answers `401`. It reports the
plugin version and the Calibre version to anyone. Since 0.6.3 it reports the
active library path only to a request carrying a valid bearer token; anyone
else gets `library: ""`. Bindery already sends its token on health, so it still
sees the path. With no `api_key` configured (loopback only) every caller counts
as authenticated and gets the path, as before.

## Timeouts and retries

Bindery uses one `http.Client` with a 30 second timeout for every endpoint. A
large book copied into a Calibre library on slow storage can exceed that, so an
add is not guaranteed to have failed just because the client timed out: the
server may still complete it. A subsequent retry is deduplicated by the
`bindery` identifier and comes back as `409`, which is why the ladder below
matters.

On `503` the server sends a `Retry-After` header (seconds). Clients SHOULD back
off exponentially up to about 30 seconds rather than giving up after a single
retry. The window this covers is a Calibre library swap, which is normally over
in well under a second.

## Errors

Every non 2xx response carries both halves of an error:

```json
{"error": "No such file or directory: '/books/x.epub'", "code": "path_not_found"}
```

`error` is human readable and keeps the meaning it had in 0.4.0, because
clients written against older plugins read it. `code` is the machine readable
half and is the only one that should drive control flow. A client that does not
see a `code` is talking to a plugin older than 0.6.0 and should fall back to
branching on the status alone.

| `code` | Status | Meaning |
|---|---|---|
| `unauthorized` | 401 | Bearer token missing or does not match |
| `db_unavailable` | 503 | Library is mid swap, no database right now |
| `invalid_json` | 400 | Body is not JSON, or `Content-Length` is not a number |
| `invalid_metadata` | 400 | A request field is missing or the wrong type |
| `path_not_found` | 400 | There is no file at `path` as the Calibre process sees it |
| `path_unreadable` | 400 | A file is there but the Calibre process cannot open it (permissions, or a directory). Since 0.6.3 |
| `path_forbidden` | 400 | `path` contains `..`, or resolves outside `ingest_root` |
| `bad_format` | 400 | No usable book format could be read from the extension |
| `body_too_large` | 413 | `Content-Length` exceeds `max_body_bytes` (default 64 MiB) |
| `not_found` | 404 | No such endpoint, or no such book id |
| `copy_failed` | 500 | Calibre read the file but failed to copy it into the library. `error` names the path. Since 0.6.3 |
| `internal` | 500 | Unexpected failure |

`path_not_found` versus `invalid_metadata` is the distinction that motivated
the field. Before 0.6.0 both were a bare `400`, so a wrong container mount and a
malformed metadata object looked identical on the wire; Bindery logged
"metadata payload rejected", retried the whole request with a path only body,
and the operator's first diagnostic line blamed metadata for a filesystem
problem. A client that understands `error_codes` should retry the legacy
payload only on `invalid_metadata`.

`path_unreadable` and `copy_failed` came in 0.6.3; before that both conditions
were `500 internal`. A client that does not know a code should treat it by its
status, which is what Bindery's client does, so adding codes is compatible.

## Endpoints

### `GET /v1/health`

Liveness, version and capability probe. Unauthenticated.

**Response, 200 OK**

```json
{
  "plugin_version": "0.8.0",
  "calibre_version": "9.7.0",
  "library": "/media/BOOKS",
  "capabilities": [
    "book_metadata", "cover", "path_probe", "metadata_update", "error_codes", "add_format",
    "pull"
  ]
}
```

All four fields are always present. `library` is `""` while the library is
initializing or being swapped, and `""` for a caller without a valid bearer
token (see [Authentication](#authentication)).

| Capability | What it means |
|---|---|
| `book_metadata` | `POST /v1/books` accepts and applies the `metadata` object |
| `cover` | `metadata.coverPath` is read and applied |
| `path_probe` | `GET /v1/paths` exists |
| `metadata_update` | `PATCH /v1/books/{id}` exists |
| `error_codes` | Every error response carries a `code` |
| `add_format` | `POST /v1/books` accepts `addFormat` and can answer `format_added`. Since 0.7.0 |
| `pull` | The plugin can fetch deliveries from Bindery's `/bridge/v1` routes. See [Pull mode](#pull-mode-bindery-side-routes). Since 0.8.0 |

A client SHOULD probe capabilities once and cache them, but SHOULD expire that
cache: upgrading the plugin under a running Bindery otherwise leaves the client
sending an older shape until it restarts.

### `GET /v1/paths`

Ask whether the Calibre process can see a path, before anything is imported.
Authenticated.

This is the setup time answer to the cross container mount mismatch. Bindery
sends paths, not bytes, and Calibre opens them on its own side, so when the two
containers mount the library at different points every push fails. Probing the
library root through the configured push path remap catches that at
configuration time instead of after a library wide push has already failed.

**Request**

```
GET /v1/paths?path=/media/BOOKS
Authorization: Bearer <api_key>
```

**Response, 200 OK**

```json
{"path": "/media/BOOKS", "exists": true, "readable": true, "isDir": true}
```

The probe never opens the file, so it cannot be used to read anything, and it
applies the same `ingest_root` restriction as `POST /v1/books`, so it cannot be
used to map the filesystem outside it either. A path with `..` in it, or one
that resolves outside `ingest_root`, gets `400` with `path_forbidden`. A
missing `path` parameter gets `400` with `invalid_metadata`.

It deliberately does not require a database, so it still answers during a
library swap. That is exactly when an operator is looking.

### `POST /v1/books`

Add a book already present on a filesystem path visible to the Calibre process.

**Request headers**

- `Authorization: Bearer <api_key>`
- `Content-Type: application/json`

**Request body**

```json
{
  "path": "/media/BOOKS/Author/Title/book.epub",
  "metadata": {
    "title": "Dune",
    "authors": ["Frank Herbert"],
    "authorSort": "Herbert, Frank",
    "description": "Desert planet.",
    "publisher": "Ace",
    "publishedDate": "1965-08-01",
    "genres": ["Science Fiction"],
    "language": "eng",
    "series": "Dune Chronicles",
    "seriesIndex": "1",
    "rating": 4.6,
    "coverPath": "/media/BOOKS/Author/Title/cover.jpg",
    "identifiers": {
      "isbn": "9780441172719",
      "bindery": "42"
    }
  }
}
```

| Field | Type | Required | Notes |
|---|---|---|---|
| `path` | string | yes | Absolute path on the Calibre process filesystem |
| `metadata` | object | no | Applied to the book before it is added |
| `addFormat` | boolean | no | Let this file join the row an earlier push of the same Bindery book made. Requires the `add_format` capability. See [Adding a format](#adding-a-format-to-an-existing-row) |

`metadata` is supported when `capabilities` includes `book_metadata`. Older
plugins ignore unknown request fields, so a client that requires metadata
should probe first rather than infer from a 201.

Supported metadata fields:

| Field | Type | Calibre field |
|---|---|---|
| `title` | string | Title |
| `authors` | string array | Authors |
| `authorSort` | string | Author sort |
| `description` | string | Comments |
| `publisher` | string | Publisher |
| `publishedDate` | string | Published |
| `genres` | string array | Tags |
| `language` | string | Languages |
| `series` | string | Series |
| `seriesIndex` | string or number | Series index |
| `rating` | number | Rating, 0 to 5 stars. Doubled on the way in, because Calibre stores 0 to 10 |
| `coverPath` | string | Cover. Requires the `cover` capability |
| `identifiers` | object of string | Identifiers |

On a create, these overwrite whatever the file itself embeds: Bindery's
metadata is the better source for a book Bindery just imported.

**`coverPath`**

Read off disk and applied to the new row. It is validated exactly like `path`
(no `..`, inside `ingest_root` when one is configured), because it arrives from
the network and the bytes end up readable through Calibre's content server. It
is also capped at 16 MiB.

A cover problem never fails the add. A cover that is missing, unreadable, empty,
oversized or outside the ingest root is logged and reported, and the book is
added without it. When the request carried a `coverPath` the success response
gains a `cover_applied` boolean; when it did not, the field is absent and the
response is byte for byte the 0.5.0 shape.

Note that a client applying a push path remap must remap `coverPath` too. The
plugin opens it on its own side of the container boundary, exactly like `path`.

**Windows share paths**

Since 0.6.1, a `path` or `coverPath` of the form `\\server\share\...` is
accepted on a Windows host and handed to Calibre in the extended form
`\\?\UNC\server\share\...`, with separators normalised first. Calibre's own
long path handling prefixes `\\?\` to any path over 200 characters without
knowing about UNC, which turned a long share path into one Windows rejects
(`[Errno 22] Invalid argument`). Drive letter paths, paths already in an
extended form, and every path on a non Windows host are passed through
unchanged. A client does not need to do anything: send the share path as the
remap produces it.

**A failed add does not leave an empty row**

Calibre's `add_books` writes the book row before it copies the file. Since
0.6.2, when the copy fails the plugin removes the formatless row that add left
for the request's `bindery` identifier, so a failure does not leave an empty
book that the next push would take for a duplicate. A request without a
`bindery` identifier has nothing to find the row by, so this cleanup does not
apply to it. Since 0.6.3 that failure is
reported as `500` with `copy_failed`.

**Responses**

- `201 Created`, book was added.

  ```json
  {"id": 1234, "duplicate": false}
  ```

  or, when a `coverPath` was sent:

  ```json
  {"id": 1234, "duplicate": false, "cover_applied": true}
  ```

  or, when the file went onto a row that was already there (see
  [Adding a format](#adding-a-format-to-an-existing-row)):

  ```json
  {"id": 1234, "duplicate": false, "format_added": true}
  ```

  `format_added` is only ever `true`, and absent on every other response.

- `409 Conflict`, the book is already in the library. `id` is the existing
  book's Calibre id, so the client can record the linkage.

  ```json
  {"id": 1234, "duplicate": true}
  ```

- `400`, `401`, `413`, `500` as described under [Errors](#errors).
- `503` with `Retry-After` while the library is mid swap.

**How a duplicate is decided**

This is not Calibre's own duplicate detection, and the difference matters.

Calibre's `add_books(add_duplicates=False)` goes through `Cache.has_book`,
which is documented as "Return True iff the database contains an entry with the
same title as the passed in Metadata object. The comparison is
case-insensitive." Title only, authors ignored. Three different poets' "The
Complete Poems" collapse into one row, which is a real collision that was hit
in a live library.

So when Bindery supplies its own `bindery` identifier the plugin passes
`add_duplicates=True` and decides for itself, using a ladder of progressively
weaker evidence and stopping at the first hit:

1. An exact search for the `bindery` identifier. Only matches a row Bindery put
   there itself.
2. An exact search for each of `isbn`, `asin`, `google`, `hardcover`, in that
   order. This is the rung that matters for a library populated by Calibre Web
   Automated, by `calibredb`, by hand or by plugin 0.4.0, where no `bindery`
   identifier exists anywhere.
3. `Cache.find_identical_books`, for a library carrying no identifiers at all.
   Calibre documents this one as "Finds books that have a superset of the
   authors in mi and the same title (title is fuzzy matched)", so unlike
   `has_book` it does not confuse two books that merely share a title.

Any hit returns `409` with the existing id, except for the cases in
[Adding a format](#adding-a-format-to-an-existing-row) below, all of which need
a hit on rung 1. Nothing matched means the book is added.

Rungs 2 and 3 were added in 0.6.0. Before that the only rung was the first one,
so the first "Push all to Calibre" against a library Bindery had not populated
cloned the whole library and reported it as success.

A request without a `bindery` identifier is unchanged: it goes to Calibre with
`add_duplicates=False` and Calibre decides.

**Adding a format to an existing row**

A Bindery book can have more than one file, an EPUB and a PDF say, and Bindery
pushes each one separately with the same `bindery` identifier. Before 0.7.0 the
second push hit rung 1 and came back `409`, so the second format never reached
Calibre.

The file is attached to the matched row, and the response is `201` with
`"duplicate": false` and `"format_added": true`, when all of these hold:

- the ladder matched on rung 1, the `bindery` identifier, and
- either the row has no format at all, or the request sent `"addFormat": true`
  and the row does not already have this file's format.

Anything else that matched is `409` as before. In particular:

- The same format a second time is `409`. The existing file is never replaced.
- A match on rung 2 or 3 never gets a file, with or without `addFormat`. That
  row may be one the user built or curated by hand, and the bridge cannot tell
  which files belong on it.

The row with no format is what a failed add left behind before 0.6.2 started
removing them. It is repaired whether or not `addFormat` was sent, as it has
been since 0.6.2, so an older client gets the same repair.

After the file goes on, the row gets the same fill only update as
[`PATCH /v1/books/{id}`](#patch-v1booksid): empty fields are filled from
`metadata`, nothing the row already has is changed. A row that had no format
also gets `coverPath`, since the add that made it never finished, and the
response carries `cover_applied` as for a create. A row that already had a file
keeps its cover and the response has no `cover_applied`. A metadata or cover
problem at this point is logged and does not fail the request, since the file
is already in the library.

In the Calibre window the row is redrawn in place (`BooksModel.refresh_ids`)
rather than announced as a new book, so the new format shows up without a
Ctrl+R.

The file is added with Calibre's `add_format(..., replace=False)`. A failure
while Calibre copies it is `500` with `copy_failed`, and the row is left as it
was. If another push put the same format on the row in between, Calibre
refuses the copy and the response is `409`.

### `PATCH /v1/books/{id}`

Apply metadata to a book already in the library. Authenticated. Requires the
`metadata_update` capability.

This exists so a `409` is not a dead end. A book pushed before Bindery had good
metadata, or pushed by a bulk sync that sent fewer fields than a live import,
can be filled in afterwards.

**Request**

The body is a metadata object with the same field names as `metadata` above.

```
PATCH /v1/books/1234
Authorization: Bearer <api_key>
Content-Type: application/json

{"series": "Dune Chronicles", "seriesIndex": "1", "description": "Desert planet."}
```

**The fill only rule**

A field is written when the Calibre row has nothing in it. A field the row
already carries is left alone, and nothing is ever cleared.

The reason is that the bridge cannot tell a value it wrote itself from one the
user typed in Calibre, and a push happens without the user asking for it.
Overwriting would silently undo hand edits on every sync. Filling blanks closes
the gap this endpoint exists for without ever taking something away.

Details that follow from that rule:

- Calibre's own placeholders count as empty. A title of `Unknown` and an author
  list of `["Unknown"]` are treated as blank, because that is what
  `Cache.create_book_entry` writes when it has nothing better.
- `identifiers` are merged key by key. A key Calibre does not have is added; a
  key it does have keeps its existing value.
- `seriesIndex` is written only when the same request also supplied the
  `series` it belongs to. Calibre defaults the index to `1.0` and never leaves
  it blank, so there is no way to ask whether it was set.
- `coverPath` is ignored here. Replacing artwork on an existing row is a bigger
  decision than filling an empty text field, and Calibre's `set_metadata`
  treats covers specially ("Covers are always changed if a new cover is
  provided"), which does not fit the rule.

**Responses**

- `200 OK`

  ```json
  {"id": 1234, "updated": true, "fields": ["series", "seriesIndex", "description"]}
  ```

  `updated` is always `true` on a 200: it means the patch was accepted and
  reconciled. `fields` names what actually changed and is empty when the row
  already had everything.

- `404 Not Found` with `code: "not_found"` when the id is gone, or when the id
  is not a number.
- `400`, `401`, `413`, `500` as described under [Errors](#errors).
- `503` with `Retry-After` while the library is mid swap.

## Degraded mode

When `BridgeServer.start` refuses (a non loopback bind with no `api_key`) the
plugin no longer leaves the port closed. It binds a health only server on the
same port:

- `GET /v1/health` returns `200` with `capabilities: []`, `library: ""`, plus
  `status: "degraded"` and an `error` naming the reason.
- Any `POST` or `PATCH` returns `401` carrying the same reason, which is the
  status Bindery already renders as "check api_key in Settings".
- Everything else returns `404`.

No library is touched and no capability is advertised, so nothing the refusal
was protecting is exposed. The same state is shown as a persistent line in the
plugin's own configuration dialog.

## Pull mode (Bindery side routes)

Everything above is served by the plugin and called by Bindery. Pull mode
turns that around: the plugin connects out to Bindery, lists what is waiting,
downloads each file into a local temp directory, adds it through the same code
path as `POST /v1/books`, and acknowledges. Calibre then needs no shared drive,
no path remap, no inbound firewall rule and no fixed address. The push server
keeps running unchanged; pull is opt in on both sides (the plugin's "Pull from
Bindery" setting, and Bindery's Settings, Calibre, Transport).

These routes are served by Bindery, not by the plugin. This section is the
contract both sides build against, protocol `1`.

### Requests

`{base}` is the Bindery URL set in the plugin, including any URL base
(`https://bindery.example.net` or `http://192.168.1.20:8787/bindery`). Every
request carries:

| Header | Value |
|---|---|
| `Authorization` | `Bearer <api_key>`, the plugin's own `api_key`, which Bindery stores as `calibre.plugin_api_key`. The same key authenticates both directions |
| `X-Bridge-Version` | The plugin version, e.g. `0.8.0` |
| `X-Bridge-Capabilities` | Comma separated, exactly the list `GET /v1/health` advertises |
| `User-Agent` | `calibre-bridge/<version>` |

| Route | Answer |
|---|---|
| `GET {base}/bridge/v1/hello` | `{"binderyVersion", "protocol": 1, "maxBatch": 20, "transport": "pull" \| "push"}` |
| `GET {base}/bridge/v1/deliveries?limit=N&cursor=C` | `{"deliveries": [...], "nextCursor", "pending"}`, or `409 {"code": "not_in_pull_mode"}` |
| `GET {base}/bridge/v1/deliveries/{id}/file` | The book, `application/octet-stream` with `Content-Length` |
| `GET {base}/bridge/v1/deliveries/{id}/cover` | The cover image, or `404` when there is none |
| `POST {base}/bridge/v1/deliveries/{id}/ack` | `204` |
| `POST {base}/bridge/v1/deliveries/{id}/nack` | `204` |

Each delivery is:

```json
{
  "id": "d-123",
  "bookId": 42,
  "format": "epub",
  "sizeBytes": 734003,
  "action": "add",
  "metadata": { "title": "...", "authors": ["..."], "identifiers": {"bindery": "42"} },
  "hasCover": true
}
```

`action` is `add` or `add_format` (the second file of a book, the same as
`addFormat: true` on a push). `metadata` has the same shape as the `metadata`
object of `POST /v1/books`, except that `coverPath` is ignored: in pull mode
only the plugin sets it, to the cover it downloaded.

The ack body is
`{"calibreId": int, "outcome": "added" | "already" | "format_added", "coverApplied": bool | null, "library": "<library path>"}`.
The nack body is `{"code": "<error code>", "error": "<message>", "retryable": bool}`.

Errors are JSON `{"error", "code"}`: `401 unauthorized`, `404`,
`409 not_in_pull_mode`, and `429 rate_limited` with `Retry-After`.

### What the plugin does

One pass, every `pull_interval_seconds` (60 by default, first pass about ten
seconds after Calibre starts, and at once after the settings are saved or the
library changes):

1. Nothing when pull is off, no Bindery URL is set, or no library is open.
2. When pull was turned on it recorded the open library's id
   (`Cache.library_id`). While any other library is open the pass stops with
   "Paused: a different library is open" and contacts nothing. The same check
   runs before every delivery, so a switch in the middle of a batch leaves the
   rest queued.
3. `hello`. `transport: "push"` sets "Bindery is set to push; switch Settings,
   Calibre, Transport to Pull" and checks again in five minutes.
4. `deliveries`, `limit` at most 20 (or `maxBatch` if smaller), following
   `nextCursor` until it is empty, at most 50 pages per pass.
5. For each delivery, in a fresh `tempfile.mkdtemp(prefix="bindery-pull-")`:
   - `format` must be a lower case token, `^[a-z0-9]{1,10}$` (`kepub.epub`
     is filed as `kepub`). Anything else is nacked `bad_format`, not
     retryable, without downloading. The file is written as `book.<format>`;
     a file name from Bindery, in the delivery or in `Content-Disposition`,
     is never used.
   - The file is streamed to disk and refused past `max_download_bytes`
     (1 GiB), on the `Content-Length` before reading and again while
     reading. Too large is nacked `body_too_large`, not retryable.
   - With `hasCover`, the cover is fetched too (capped at 16 MiB). A cover
     that fails does not fail the book; the ack then says
     `coverApplied: false`.
   - The book goes through the same adder as `POST /v1/books`, with no
     ingest root restriction because the temp directory is the plugin's own.
   - Ack with the outcome: `added`, `format_added`, or `already` when the
     dedupe ladder found the book.
   - An adder failure is nacked with the code the push API would have
     answered: `bad_format` and `path_forbidden` are not retryable,
     `copy_failed`, `path_unreadable` and anything unexpected (`internal`)
     are.
   - The temp directory is removed whatever happened.
6. A download that fails with an HTTP error is skipped and comes back next
   pass. A failed ack is logged; the delivery is listed again, the adder finds
   it by its `bindery` identifier and the retry is acked `already`, so
   delivery is at least once and never makes a second row.

Backoff:

| Situation | Next pass |
|---|---|
| Success, or nothing waiting | `pull_interval_seconds` |
| No answer, or a 5xx | Doubles each time, from twice the interval up to 15 minutes, back to normal after the next success |
| `429` | `Retry-After` seconds (at most an hour) |
| `401` | One hour, status "Bindery rejected the API key" |
| `409 not_in_pull_mode` or `transport: "push"` | Five minutes |
| `404` on `hello` (a Bindery without these routes) | 15 minutes |

### Transport security

- HTTPS is verified against the system trust store (on Windows, Calibre's
  Python reads the Windows store) plus the optional `ca_file` PEM bundle,
  for a private CA or a self signed Bindery. There is no setting that turns
  verification off.
- Plain `http` is accepted, since a LAN Bindery without TLS is common. When
  the host is not loopback the status line says that the key and every book
  cross the network unencrypted.
- Redirects are refused, not followed. urllib forwards the `Authorization`
  header on a redirect, so following one would hand the key to wherever it
  pointed. Set the Bindery URL to the final address.
- Only `http` and `https` URLs are accepted, with no user name, password,
  query or fragment. Every request times out after 30 seconds.

## Compatibility rules for implementers

- Adding a new optional request field is non breaking.
- Adding a new response field is non breaking. Clients MUST ignore unknown
  fields.
- Adding a new response header is non breaking.
- Adding a new endpoint is non breaking, and gets a capability so clients can
  find it.
- Adding a new response status code is non breaking if clients already treat
  unknown statuses as errors of the right class.
- Changing the meaning of an existing status code IS breaking.
- Removing or renaming a field IS breaking.
- Changing what an existing `error` string says for an existing condition is
  breaking for pre 0.6.0 clients, which have nothing else to read. Add a `code`
  and leave the string alone.

### What an older Bindery sees on 0.8.0

| Client | Behaviour |
|---|---|
| Pre 0.4.0 (no metadata) | Unchanged. Sends `{"path": ...}`, gets `{"id", "duplicate"}` |
| 0.4.0 and 0.5.0 | Unchanged responses. Ignores the extra `code` field and `Retry-After`. Never sends `coverPath`, so never sees `cover_applied`. Never calls the two new endpoints |
| Any client | Gains the dedupe ladder, which turns what used to be a silent duplicate into a `409` with the existing id. A client that already treats `409` as "already there" needs no change |
| Any client | Sees `path_unreadable` (400) and `copy_failed` (500) where 0.6.2 sent `internal` (500). A client branching on status alone sees an unreadable path move from 500 to 400 |
| Any client without the token | Gets `library: ""` from health. Bindery sends its token on every request, health included |
| Any client not sending `addFormat` | A second format of the same Bindery book is still `409`, as on 0.6.3. The repair of a formatless row still happens, and its `201` now also carries `format_added: true`, fills empty metadata and applies `coverPath` |
| Any Bindery without `/bridge/v1` | Nothing changes unless pull is turned on in the plugin. If it is, `hello` answers `404` and the plugin says so in its status line and checks again every 15 minutes; the push API keeps working |
