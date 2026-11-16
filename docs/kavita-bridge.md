# Kavita Bridge: setting up Bindery

This is the Bindery side of [Kavita Bridge](../plugins/kavita-bridge/README.md),
with what Bindery sends and how its notifier behaves. Everything here was read
from vavallee/bindery at commit 074a2bdc: `internal/notifier/notifier.go`,
`internal/importer/scanner.go` and `scanner_import.go`,
`web/src/pages/settings/NotificationsTab.tsx`, `web/src/i18n/locales/en.json` and
`docs/API.md` (Webhook payload). If you only need Kavita to rescan after an
import, read the plugin README's section on the plain webhook first: it needs no
bridge.

## Add the notification

In Bindery open **Settings**, then **Notifications** (the heading reads **Webhook
Notifications**), and press **+ Add Notification**.

| Field in Bindery | Value |
|---|---|
| Name | `Kavita bridge` |
| Method (drop down) | `POST` (`PUT` also works) |
| Webhook URL | `http://kavita-bridge:8103/v1/events` |
| Custom headers (JSON, optional) | `{"Authorization": "Bearer YOUR_BRIDGE_TOKEN"}` |
| Trigger on | **On Import** ticked, everything else off |

`YOUR_BRIDGE_TOKEN` is the value of `KAVITA_BRIDGE_API_KEY` on the bridge. The
header must be a JSON object whose values are all strings; Bindery shows
"Headers must be valid JSON object with string values, or empty." otherwise.
Bindery applies each header to the request after its own `Content-Type` and
`User-Agent`.

The **On Import** toggle sends the `bookImported` event ("a finished download was
added to your library"). The bridge acts on `bookImported` only and answers
every other event `200` without doing anything, so a notification that also
receives other events is harmless but pointless.

The test button sends an event named `test`. The bridge answers it `200` and
scans nothing, so a green test proves the URL and the token, not Kavita. For
Kavita, call `GET /v1/health` with the token:

```bash
curl -H "Authorization: Bearer $BRIDGE_TOKEN" http://kavita-bridge:8103/v1/health
```

## Network rules Bindery enforces

- A webhook to a private address (RFC1918) is refused unless Bindery runs with
  `BINDERY_NOTIFICATIONS_ALLOW_PRIVATE=1`. Loopback, link local and cloud
  metadata addresses stay blocked even then, so `http://127.0.0.1:8103` never
  works. Give the bridge a container name, a LAN address or a host name.
- Bindery re-checks every redirect hop and refuses redirects into those ranges.
- `BINDERY_OUTBOUND_PROXY`, when set, applies to webhooks too.

## What Bindery sends

A `POST` with `Content-Type: application/json` and a JSON object. Bindery's
`normalizeEventPayload` adds `eventType` to every event, moves the original
`title` to `item`, and rewrites `title` to what happened and `message` to the
subject. For a `bookImported` event that is:

```json
{
  "eventType": "bookImported",
  "title": "Book Imported",
  "message": "The Way of Kings (ebook)",
  "body": "The Way of Kings (ebook)",
  "item": "The Way of Kings",
  "format": "ebook",
  "mediaFormat": "ebook"
}
```

The bridge keys on `eventType`. The fields that can appear on `bookImported`:

| Field | When | Notes |
|---|---|---|
| `eventType` | always | `bookImported` |
| `title`, `message`, `body`, `item` | always | Display text. `item` is the book title. |
| `format`, `mediaFormat` | always | `ebook` or `audiobook`. |
| `path` | **audiobooks only** | The folder the audiobook was moved to. **An ebook event has no `path`**: the importer passes an empty path and the field is left out. |
| `skippedFiles` | a shared folder merge that skipped files | List of file names. |
| `contentCheck` | the content check found something | An object, see `docs/API.md` in Bindery. |

Because an ebook event has no `path`, the bridge's path based library choice
cannot work for ebooks. Use `KAVITA_BRIDGE_UNMAPPED=libraries` with your Kavita
ebook library ids, or `all`. If Bindery adds a path to the ebook event later, set
`KAVITA_BRIDGE_SCAN_MODE=path` and a `KAVITA_BRIDGE_PATH_MAP` that translates
Bindery's folder to the one Kavita sees.

An upgrade (a better file replacing one you have) is sent as `upgrade`, not
`bookImported`, so it does not trigger a scan.

## Timeout and retries

Bindery sends each notification once. The HTTP client has a 10 second timeout,
there is no retry, and a failure or a non 2xx answer is only logged
(`notifier: failed to send notification`). The import calls the notifier
directly, so a slow receiver slows the import by up to 10 seconds per
notification. Both facts shape the bridge:

- It answers `202` as soon as it has queued the event, before it contacts Kavita.
- Kavita errors never turn into a `5xx` for Bindery. A `5xx` would only produce a
  log line in Bindery and could never be retried.
- A `401` (wrong token) or `415`, `413` and `400` are the bridge refusing a
  request, and show up in Bindery's log with the status.

If a scan is lost because Kavita was down, nothing resends it. Kavita's own
scheduled scan, or the next import, picks the book up.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Bindery logs `webhook returned HTTP 401` | The bearer token in Custom headers differs from `KAVITA_BRIDGE_API_KEY`. |
| Bindery logs `url not allowed` | The address is private and `BINDERY_NOTIFICATIONS_ALLOW_PRIVATE` is not `1`, or it is loopback. |
| The bridge logs `bookImported received` but Kavita does not rescan | Check `GET /v1/health` for `keyValid` and `canScan`; check `unmapped` (the default `skip` scans nothing when the event has no path). |
| Kavita rescans late | A scan was already running; Kavita defers a new request by 3 hours. Raise `KAVITA_BRIDGE_DEBOUNCE_SECONDS`. |
| The bridge exits at start with `refusing to start` | A non loopback bind needs `KAVITA_BRIDGE_API_KEY` of 16 or more characters. |
