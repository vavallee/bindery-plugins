# Bindery Bridge HTTP Protocol (v1)

The Bindery Bridge Calibre plugin exposes a small, versioned HTTP API that
Bindery uses to add books to the running Calibre library without shelling out
to `calibredb`. All endpoints are prefixed with `/v1/`.

## Versioning policy

- **Backwards-compatible changes** (new optional fields on requests or
  responses, new endpoints, new status codes that clients are expected to
  handle gracefully) do **not** bump the version prefix.
- **Breaking changes** (renaming/removing fields, changing types, removing
  endpoints) introduce a new `/v2/` prefix. Both prefixes are served for at
  least one plugin minor-version cycle. The deprecated prefix returns the
  `Deprecation: true` response header and a `Sunset` header naming the removal
  version, then is dropped at the next plugin major version.
- Clients SHOULD send a `User-Agent` of the form:
  `bindery/<semver> plugin-api/v1`. The server uses it for diagnostic logging
  only; missing or malformed values are not rejected.

## Authentication

`POST` endpoints require a bearer token matching the plugin's configured
`api_key`. If `api_key` is empty, the server accepts unauthenticated requests
(not recommended outside a loopback bind). Requests with a bad or missing
token get `401 Unauthorized`.

`GET /v1/health` is always unauthenticated so clients can probe readiness
without provisioning credentials.

## Endpoints

### `GET /v1/health`

Liveness + version probe.

**Response — 200 OK**
```json
{
  "plugin_version": "0.5.0",
  "calibre_version": "9.7.0",
  "library": "/media/BOOKS",
  "capabilities": ["book_metadata"]
}
```

All fields are always present. `library` is `""` while the library is still
initializing or being swapped. `capabilities` is a list of optional protocol
features supported by the plugin. `book_metadata` means `POST /v1/books`
accepts and applies the optional `metadata` object described below.

### `POST /v1/books`

Add a book already present on a filesystem path visible to the Calibre process
into the active library.

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
    "identifiers": {
      "isbn": "9780441172719",
      "bindery": "42"
    }
  }
}
```

| Field      | Type   | Required | Notes                                           |
|------------|--------|----------|-------------------------------------------------|
| `path`     | string | yes      | Absolute path on the Calibre process filesystem |
| `metadata` | object | no       | Bindery metadata to apply before adding the book |

`metadata` is supported when `GET /v1/health` includes
`"book_metadata"` in `capabilities`. Older plugins ignore unknown request
fields, so clients that require metadata SHOULD probe capabilities before
sending this field.

Supported metadata fields:

| Field           | Type            | Calibre field         |
|-----------------|-----------------|-----------------------|
| `title`         | string          | Title                 |
| `authors`       | string array    | Authors               |
| `authorSort`    | string          | Author sort           |
| `description`   | string          | Comments              |
| `publisher`     | string          | Publisher             |
| `publishedDate` | string          | Published             |
| `genres`        | string array    | Tags                  |
| `language`      | string          | Languages             |
| `series`        | string          | Series                |
| `seriesIndex`   | string/number   | Series Index          |
| `rating`        | number          | Rating, 0-5 stars     |
| `identifiers`   | object<string>  | Identifiers           |

**Responses**

- `201 Created` — book was added.
  ```json
  {"id": 1234, "duplicate": false}
  ```
- `409 Conflict` — the book already exists in the library
  (`add_duplicates=false` matched an existing row). `id` is the existing
  book's Calibre id.
  ```json
  {"id": 1234, "duplicate": true}
  ```
- `400 Bad Request` — body is missing or malformed, or the referenced file
  cannot be opened.
- `401 Unauthorized` — bearer token missing or does not match.
- `503 Service Unavailable` — the library is mid-swap
  (`library_changed()` in progress). Clients SHOULD retry with exponential
  backoff up to ~30s.
- `500 Internal Server Error` — unexpected failure. Response body:
  `{"error": "<message>"}`.

## Error envelope

Non-2xx responses carry `{"error": "<human-readable message>"}`. Messages are
safe to surface in logs; do not pattern-match them for control flow — use the
HTTP status code.

## Backwards-compatibility rules for implementers

- Adding a new optional request field is non-breaking.
- Adding a new response field is non-breaking; clients MUST ignore unknown
  fields.
- Adding a new response status code is non-breaking *if* clients were already
  treating unknown statuses as errors of the appropriate class (4xx / 5xx).
- Changing the meaning of an existing status code **is** breaking.
- Removing or renaming a field **is** breaking.
