# Bindery Bridge HTTP Protocol (v1)

The Bindery Bridge Calibre plugin exposes a versioned HTTP API. Bindery uses it to add books to the running Calibre library without shelling out to `calibredb`. All endpoints live under `/v1/`.

## Design notes

- **Path-based, not upload-based.** The request body carries a filesystem path, not file bytes. Both Bindery and Calibre must see the file at that path (e.g. a shared NFS volume). This keeps the protocol lightweight and avoids re-transferring potentially large files over the cluster network.
- **Single-library.** The server always targets Calibre's currently active library. If the user switches libraries in the GUI, `library_changed()` briefly returns `503` while the swap completes.
- **Thread-safe.** `db.new_api` is Calibre's multi-reader/single-writer API; concurrent `POST /v1/books` calls are serialised by Calibre's own lock.

---

## Versioning

- **`/v1/` is the stable contract.** Backwards-compatible changes (new optional fields, new endpoints, new non-breaking status codes) do **not** bump the version prefix.
- **Breaking changes** introduce `/v2/`. Both prefixes are served for at least one plugin minor-version cycle. The deprecated prefix returns `Deprecation: true` and `Sunset: <version>` response headers until removal at the next major version.
- Bindery sends `User-Agent: bindery/<version> plugin-api/v1`. The server logs it; a missing or malformed value is not rejected.

---

## Authentication

All `POST` endpoints require a bearer token:

```
Authorization: Bearer <api_key>
```

The `api_key` is set in the plugin's config dialog. If left blank, unauthenticated requests are accepted — only safe when bound to loopback (`127.0.0.1`).

`GET /v1/health` is **always unauthenticated** so monitoring and Bindery's Test button can probe readiness without credentials.

A request with a missing or wrong token gets `401 Unauthorized`.

---

## Endpoints

### `GET /v1/health`

Liveness probe. Returns plugin version, Calibre version, and active library path.

**No authentication required.**

**Response — 200 OK**

```json
{
  "plugin_version": "0.1.0",
  "calibre_version": "9.7.0",
  "library": "/media/BOOKS"
}
```

| Field | Type | Notes |
|---|---|---|
| `plugin_version` | string | Semver of the installed plugin. |
| `calibre_version` | string | Calibre version string (from `numeric_version()`). |
| `library` | string | Absolute path of the active Calibre library. Empty string while the library is initialising or mid-swap. |

---

### `POST /v1/books`

Add a book to the active Calibre library by filesystem path.

**Request headers**

```
Authorization: Bearer <api_key>
Content-Type: application/json
```

**Request body**

```json
{
  "path": "/media/BOOKS/Author Name/Book Title/book.epub"
}
```

| Field | Type | Required | Notes |
|---|---|---|---|
| `path` | string | yes | Absolute path on the **Calibre process** filesystem. Must be readable by the Calibre process. The format (EPUB, MOBI, PDF, etc.) is detected from the file extension. |

**Responses**

| Status | Meaning | Body |
|---|---|---|
| `201 Created` | Book was added successfully. | `{"id": 1234, "duplicate": false}` |
| `409 Conflict` | Book already exists (`add_duplicates=false` matched an existing row). `id` is the existing book's Calibre id. Bindery records this id and does not treat it as an error. | `{"id": 1234, "duplicate": true}` |
| `400 Bad Request` | Body is missing, not valid JSON, `path` is absent, or the file cannot be opened. | `{"error": "<message>"}` |
| `401 Unauthorized` | Bearer token missing or incorrect. | `{"error": "unauthorized"}` |
| `503 Service Unavailable` | Library is mid-swap (`library_changed()` in progress). **Retry with exponential backoff** — the swap typically completes in under a second. | `{"error": "library not ready"}` |
| `500 Internal Server Error` | Unexpected failure inside the plugin. Check Calibre's Job log. | `{"error": "<message>"}` |

**Example — successful add**

```
POST /v1/books HTTP/1.1
Host: calibre.default.svc.cluster.local:8099
Authorization: Bearer s3cr3t
Content-Type: application/json

{"path": "/media/BOOKS/Ursula K. Le Guin/The Left Hand of Darkness/book.epub"}

HTTP/1.1 201 Created
Content-Type: application/json

{"id": 42, "duplicate": false}
```

---

## Error envelope

All non-2xx responses include:

```json
{"error": "<human-readable message>"}
```

Messages are safe to surface in logs and UI. Do **not** pattern-match them for control flow — use the HTTP status code.

---

## Retry behaviour

Clients should retry `503` responses (library swap). Bindery retries once after a 2-second delay. Retry budget: 1 retry, then surface the error to the user.

Other status codes are not retried automatically. `409` is handled as a successful duplicate (the `calibre_id` is persisted from the response).

---

## Backwards-compatibility rules

For plugin authors and Bindery maintainers:

| Change | Breaking? |
|---|---|
| New optional request field | No |
| New response field | No — clients must ignore unknown fields |
| New endpoint | No |
| New status code (client already treats unknown 4xx/5xx as errors) | No |
| Renaming or removing a request/response field | **Yes** → bump to `/v2/` |
| Changing the meaning of an existing status code | **Yes** → bump to `/v2/` |
| Removing an endpoint | **Yes** → bump to `/v2/` |
