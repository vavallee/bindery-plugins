# Kavita Bridge

Tell [Kavita](https://www.kavitareader.com/) to scan its library when Bindery
finishes importing a book (vavallee/bindery#3901).

This plugin is separate from the Calibre plugin in this repository. It is a
small standalone receiver, Python standard library only, with no Calibre and no
third party packages. Bindery's webhook notifier calls it; it calls Kavita.

Status: unreleased, version 0.1.0. No release tag, zip or published image
exists for it yet. It was written against Kavita's source and has never run
against a live Kavita. [What is confirmed and what is not](#what-is-confirmed-and-what-is-not)
says exactly which parts.

## Do you need this? Try the plain webhook first

Kavita accepts an Auth key straight in an `x-api-key` header on its scan
routes, and Bindery's webhook notifier can send custom headers. So Bindery can
call Kavita directly, with no bridge at all:

1. In Kavita, as an admin, create an Auth key under your user settings. Give it
   an expiry only if you are willing to replace it. The account must be an
   admin, because the scan routes require the admin role.
2. In Bindery open **Settings**, **Notifications**, **+ Add Notification** and
   fill in:
   - **Name**: `Kavita`
   - **Method**: `POST`
   - **Webhook URL**: `http://kavita:5000/api/Library/scan-all`, or, to scan one
     library, `http://kavita:5000/api/Library/scan?libraryId=1`
   - **Custom headers (JSON, optional)**: `{"x-api-key": "YOUR_KAVITA_AUTH_KEY"}`
   - **Trigger on**: tick **On Import** and nothing else
3. Press the test button. Bindery sends an event named `test`, which Kavita
   answers like any other scan request.
4. If Kavita is on your LAN, Bindery refuses private addresses for webhooks
   unless you set `BINDERY_NOTIFICATIONS_ALLOW_PRIVATE=1` (docs/DEPLOYMENT.md in
   the Bindery repo).

That is the whole setup, and for a library you import into a few books at a
time it is enough. Use the bridge only for what it cannot do:

| Plain webhook | Bridge |
|---|---|
| One scan request per imported book. A bulk import is a burst of requests. | One scan per library per burst (the debounce window, default 30 s). |
| Kavita's `scan-all` does not collapse repeats: while a scan runs, every further request is queued as its own job for 3 hours later (`Kavita.Services/TaskScheduler.cs` lines 449 to 461 at v0.9.1.4). A burst can queue many delayed full scans. | The burst is sent as one request. |
| The Kavita key is stored in Bindery's notification settings. | The Kavita key lives only in the bridge. Bindery holds a token for the bridge, not for Kavita. |
| Library is fixed in the URL. | Library chosen by the imported file's path, when the event carries one (see the limit below). |

One limit applies to both setups and to path selection in particular. Bindery's
`bookImported` event for an **ebook** carries no file path: the importer passes
an empty path (`internal/importer/scanner_import.go` line 1324 in
vavallee/bindery at 074a2bdc, `importedPayload` in `scanner.go` line 540 omits
an empty path). Only an **audiobook** event carries `path`, the destination
folder (line 971), and Kavita does not serve audiobooks. So for ebooks the
bridge cannot pick a library from the path today. Use `unmapped=libraries` with
the ids of your ebook libraries, or `unmapped=all`. Path selection starts to
matter if Bindery adds the path to the ebook event.

## What the bridge does

```
Bindery webhook      POST /v1/events + bearer token      kavita-bridge
kavita-bridge        x-api-key header, scan request      Kavita
```

- `bookImported` events are queued and answered `202` at once. After the
  debounce window the bridge scans each affected library once.
- Every other event (`grabbed`, `downloadFailed`, `health`, `upgrade`,
  `bookAnnounced`, `requestCreated`, `test`) gets `200 {"status":"ignored"}` and
  nothing else happens. Bindery's own **Trigger on** toggles decide what is
  sent; the bridge ignores the rest as a second line.
- An `upgrade` event, sent when a better file replaces one you have, is not
  acted on. Kavita notices those changes on its next scheduled scan.

## Run it

Python 3.10 or newer, from a checkout of this repository:

```bash
cd plugins/kavita-bridge
export KAVITA_BRIDGE_KAVITA_URL=http://kavita:5000
export KAVITA_BRIDGE_KAVITA_API_KEY=...        # a Kavita admin Auth key
export KAVITA_BRIDGE_API_KEY=$(openssl rand -hex 32)   # what Bindery will send
python -m kavita_bridge
```

Or as a container. The repository publishes no image, so build one with the
repository root as the context:

```bash
docker build -f plugins/kavita-bridge/Dockerfile -t kavita-bridge .
docker run -d -p 8103:8103 \
  -e KAVITA_BRIDGE_KAVITA_URL=http://kavita:5000 \
  -e KAVITA_BRIDGE_KAVITA_API_KEY=... \
  -e KAVITA_BRIDGE_API_KEY=... \
  kavita-bridge
```

The image binds every interface, so it will not start until
`KAVITA_BRIDGE_API_KEY` is set to 16 or more characters.

### Bindery side

In Bindery open **Settings**, **Notifications**, **+ Add Notification**:

- **Name**: `Kavita bridge`
- **Method**: `POST`
- **Webhook URL**: `http://kavita-bridge:8103/v1/events`
- **Custom headers (JSON, optional)**: `{"Authorization": "Bearer YOUR_BRIDGE_TOKEN"}`
- **Trigger on**: **On Import** only

Bindery treats loopback as forbidden for webhooks, so the bridge cannot run on
the same host and be reached as `127.0.0.1`; give it a container name or LAN
address, and set `BINDERY_NOTIFICATIONS_ALLOW_PRIVATE=1` for a private address.
The full walk through, with the payload Bindery sends, is in
[docs/kavita-bridge.md](../../docs/kavita-bridge.md).

### Configuration

Every setting is an environment variable. A JSON file can hold the same names
in lower case without the prefix (`KAVITA_BRIDGE_CONFIG=/path/config.json`);
environment variables win. A secret can also be read
from a file by adding `_FILE` to its name (`KAVITA_BRIDGE_API_KEY_FILE`), for
Docker and Kubernetes secrets. Keep a config file that holds a key readable by
its owner only.

| Variable | Default | Meaning |
|---|---|---|
| `KAVITA_BRIDGE_KAVITA_URL` | required | Kavita base URL, for example `http://kavita:5000`. Include the base path if Kavita has one. Credentials in the URL are refused. |
| `KAVITA_BRIDGE_KAVITA_API_KEY` | required | A Kavita Auth key belonging to an admin. |
| `KAVITA_BRIDGE_API_KEY` | empty | The token Bindery must send as `Authorization: Bearer`. Compared in constant time. |
| `KAVITA_BRIDGE_BIND_HOST` | `127.0.0.1` | Interface to listen on. |
| `KAVITA_BRIDGE_PORT` | `8103` | Port to listen on. |
| `KAVITA_BRIDGE_DEBOUNCE_SECONDS` | `30` | Window that collapses a burst into one scan. `0` scans at once. |
| `KAVITA_BRIDGE_SCAN_MODE` | `all` | `all` scans every library. `path` scans the library whose folder holds the imported path. |
| `KAVITA_BRIDGE_PATH_MAP` | empty | `BINDERY_PATH=KAVITA_PATH` pairs separated by `;`, for example `/media/books=/data/books`. The longest matching prefix wins; matching is on whole path segments. With no match the path is used as is. |
| `KAVITA_BRIDGE_UNMAPPED` | `skip` | What to do when the event has no path, or no Kavita library holds it: `skip`, `all` (scan everything), or `libraries` (scan the ids below). |
| `KAVITA_BRIDGE_UNMAPPED_LIBRARIES` | empty | Comma separated Kavita library ids for `unmapped=libraries`. |
| `KAVITA_BRIDGE_FORCE` | `false` | Pass `force=true` to Kavita, which ignores its file I/O shortcuts. Slower; rarely wanted. |
| `KAVITA_BRIDGE_AUTH_MODE` | `api-key` | `api-key` sends `x-api-key` on every call. `jwt` first exchanges the key for a token at `/api/Plugin/authenticate`, and signs in again once on a 401. |
| `KAVITA_BRIDGE_KAVITA_TIMEOUT` | `10` | Seconds before any call to Kavita is abandoned. |
| `KAVITA_BRIDGE_LOG_LEVEL` | `INFO` | Python log level. |

### Startup refusals

The rule is the Calibre bridge's (docs/protocol.md, Authentication). With no
`KAVITA_BRIDGE_API_KEY` the bridge accepts unauthenticated requests, which is
for local use only, and it only does that on a loopback bind. A bind that is not
loopback needs a key of at least 16 characters, or the process exits with status
2 and a message that names the cause and never the key. Unlike the Calibre
plugin there is no degraded mode: this is a process, so a refusal is visible in
its log and exit code.

## Behaviour you can rely on

- **Debounce.** The first `bookImported` of a burst starts a timer. Every event
  that arrives before it fires joins the batch. The window is fixed from the
  first event, so a steady trickle cannot postpone the scan for ever. An event
  after the timer starts the next batch. Kavita defers a scan requested while
  another runs (3 hours, `Kavita.Services/TaskScheduler.cs` lines 463 to 484; `ScanLibraries` for scan-all is lines 449 to 461), so a long window
  is kinder than a short one for big imports.
- **Library selection by path.** With `scan_mode=path` the bridge reads
  `path` from the event, rewrites it with `KAVITA_BRIDGE_PATH_MAP`, lists
  Kavita's libraries once per batch and scans each library whose most specific
  folder contains the path. A path containing `..` never matches. Windows style
  paths (`D:\Books`) are compared without regard to case and with either slash.
- **Unmapped paths.** A path no library holds, or an event with no path, follows
  `KAVITA_BRIDGE_UNMAPPED`. The default is to log it and scan nothing.
- **Kavita errors are never Bindery's problem.** The bridge answers `202` before
  it talks to Kavita, so a Kavita outage, a wrong key or a slow reply cannot
  become a `5xx` to Bindery. Failures are logged and the next batch tries again.
  There is no retry inside a batch: Bindery's notifier does not retry either (it
  sends once with a 10 second timeout and logs a failure,
  `internal/notifier/notifier.go` lines 223 and 261 to 286), and it waits for
  the answer while it imports, so the bridge replies at once.
- **Timeouts.** Every call to Kavita has the `KAVITA_BRIDGE_KAVITA_TIMEOUT`
  limit. Redirects from Kavita are errors, because a redirected POST would
  silently become a GET.
- **Secrets stay out of logs.** Neither key, the token or a JWT is logged. The
  JWT sign in puts the key in a query string because Kavita's action reads it
  there; the bridge never prints a URL, and a filter on the log replaces any
  known secret that slips into a message.
- **Requests refused early.** A request with an `Origin` header gets `403`;
  `Content-Type: application/json` is required (`415`); bodies over 256 KiB get
  `413`; a stalled client is dropped after 10 seconds; at most 32 connections
  are served at once. There is no failed attempt throttle like the Calibre
  plugin's, so keep the bridge off the open internet.

### Health

`GET /v1/health` always answers `200` with `{"status":"ok","version":"0.1.0"}`.
A caller that sends the bearer token also gets what the bridge learned about
Kavita:

```json
{"status": "ok", "version": "0.1.0",
 "kavita": {"reachable": true, "keyValid": true, "canScan": true}}
```

`reachable` is whether Kavita answered at all, `keyValid` whether it accepted the
key, `canScan` whether the account is an admin. No URL, key, username or Kavita
version appears. The Kavita check is cached for 15 seconds, and a caller without
the token causes no call to Kavita. The container's `HEALTHCHECK` uses the
token free form, so it reports that the bridge is up, not that Kavita is.

## What is confirmed and what is not

Read from the Kavita source at tag `v0.9.1.4` (2026-09-02, the newest release
when this was written; the same files on `develop` at e844b64f match) and from
the Kavita wiki page [API](https://wiki.kavitareader.com/guides/api/). The tag
points at commit 879b8b73. Every path below is a full repository path at that
tag, and every line number is a line of that file at that tag.

**Confirmed in source**

| Fact | Where |
|---|---|
| Scan one library: `POST /api/Library/scan?libraryId={id}&force={bool}`; ids and `force` are query parameters, no body | `Kavita.Server/Controllers/LibraryController.cs` lines 346 to 353; `[ApiController]` at `Kavita.Server/Controllers/BaseApiController.cs` line 19 binds simple types from the query |
| Scan all: `POST /api/Library/scan-all?force={bool}` | `Kavita.Server/Controllers/LibraryController.cs` lines 376 to 382 |
| Both need the admin role | `[Authorize(Policy = PolicyGroups.AdminPolicy)]`, `Kavita.Server/Controllers/LibraryController.cs` lines 347 and 377; policy is `RequireRole(AdminRole)`, `Kavita.Server/Extensions/IdentityServiceExtensions.cs` line 154 |
| **An Auth key is accepted directly as the `x-api-key` header on the scan routes** (also as `?apiKey=` and as an `apiKey` route value), with no JWT exchange. Read: `ExtractAuthKey` takes the header from any request; the default scheme (`AddAuthentication(DynamicHybrid)`) sends every request that carries a key to the AuthKey handler, which loads the key's user and roles and builds the principal the `[Authorize]` and `AdminPolicy` checks run against. No per route allow list exists in these files. The handler also rejects keys that fail `AuthKeyHelper.IsValidAuthKey` or are unknown or expired | `Kavita.Server/Middleware/AuthKeyAuthenticationHandler.cs` lines 113 to 134; header name `x-api-key` in `Kavita.Common/Constants/Headers.cs` line 14; `Kavita.Server/Extensions/IdentityServiceExtensions.cs` lines 78 and 85 to 90 route any request carrying a key to that handler |
| The key's roles come from its user, so a key of an admin passes the admin policy | `Kavita.Server/Middleware/AuthKeyAuthenticationHandler.cs` lines 88 to 91 |
| Keys can expire; an expired key does not authenticate | `Kavita.Models/Entities/User/AppUserAuthKey.cs` line 26; `Kavita.Database/Repositories/UserRepository.cs` lines 840 to 853 (`HasNotExpired`) |
| The wiki recommends Auth keys in `x-api-key`, and `GET /api/plugin/authkey-expires` reports an expiry | wiki.kavitareader.com/guides/api; `Kavita.Server/Controllers/PluginController.cs` lines 86 to 96 |
| Optional JWT route: `POST /api/Plugin/authenticate?apiKey=...&pluginName=...`, anonymous, returns a user object with `Token` | `Kavita.Server/Controllers/PluginController.cs` lines 33 to 64 (reply built at 56 to 63) |
| A JWT lives 3 days | `Kavita.Services/TokenService.cs` line 56 (`AddDays(3)`) |
| List libraries and their folders: `GET /api/Library/libraries`, items carry `Id`, `Name` and `Folders` | `Kavita.Server/Controllers/LibraryController.cs` line 227; `Kavita.Models/DTOs/LibraryDto.cs` lines 14 to 61 |
| Health probe: `GET /api/Library/list` is admin only and changes nothing. With no `path` it returns the server's root directories (`Directory.GetLogicalDrives()`), so a 200 proves an admin key and nothing more | `Kavita.Server/Controllers/LibraryController.cs` lines 167 to 180 |
| `GET /api/Library/libraries` is open to any signed in user (class level `[Authorize]` only), so a 200 there cannot show admin rights; that is why health does not use it | `Kavita.Server/Controllers/LibraryController.cs` lines 41 and 227 to 232 |
| A scan requested while another runs is rescheduled 3 hours later; a duplicate request for one library is skipped | `Kavita.Services/TaskScheduler.cs` lines 449 to 484 |
| `POST /api/Library/scan-folder` takes a folder path and an admin key in a JSON body, anonymous route | `Kavita.Server/Controllers/LibraryController.cs` lines 458 to 487; `Kavita.Models/DTOs/ScanFolderDto.cs`. Not used, because Bindery's ebook event has no folder path |
| An Auth key is 8 to 32 letters and digits (or an older GUID) | `Kavita.Common/Helpers/AuthKeyHelper.cs` lines 10, 11 and 28 to 32 |

**Not confirmed: no live Kavita was available**

- That the routes answer as the source implies. Nothing here was run against a
  real Kavita, and the tests use a fake server that implements the same reading.
- That `x-api-key` authenticates `scan` and `scan-all` at run time. That is
  established by reading the handler and the scheme selection above, not by
  sending a request. The OpenAPI description in Kavita's `Startup.cs` was not
  used as evidence.
- JSON field casing in `/api/Library/libraries` and the sign in reply
  (`id`, `name`, `folders`, `token`). ASP.NET defaults to camel case and the
  parser assumes it.
- That a JSON body Bindery sends is ignored by `scan` and `scan-all`. The
  source says it should be, since neither action declares a body parameter.
- That a rejected JWT is answered `401` rather than a redirect or `403`, which
  the jwt mode's single re-sign in depends on.
- The status for a non admin key on `scan`: `403` is assumed (ASP.NET's
  default), and health relies on it to report `canScan: false`.
- Older Kavita. The key handling above is from 0.9.1.4. A comment in
  `AuthKeyHelper.cs` line 31 says keys before v0.8.9 were GUIDs, so Auth keys
  were reworked around then; how older releases treat `x-api-key` was not read.
- The container image was built and started once, and the bridge refused and
  served health as described. It was not run against Bindery.

## Development

```bash
pytest plugins/kavita-bridge/tests --cov=plugins/kavita-bridge/kavita_bridge
ruff check plugins/ && ruff format --check plugins/
(cd plugins/kavita-bridge && mypy --ignore-missing-imports --explicit-package-bases kavita_bridge)
bandit -r plugins/kavita-bridge/kavita_bridge
```

All Kavita behaviour sits in one class, `kavita_bridge/kavita.py`
(`KavitaClient`), so a change in Kavita is a change in one file. Tests run a fake
Kavita (`tests/fake_kavita.py`, `http.server` in a thread) and drive the real
receiver over a socket.

Licence: GPL-3.0, as the rest of this repository (see `COPYRIGHT`). Not
affiliated with or endorsed by the Kavita project or its maintainers.
