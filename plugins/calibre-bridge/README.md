# Bindery Bridge — Calibre plugin

Exposes a versioned HTTP API inside the Calibre GUI process so [Bindery](https://github.com/vavallee/bindery) can add imported books to your Calibre library without `calibredb` being installed alongside Bindery.

Requires Calibre 6.0+ and Python 3.10+ (bundled with Calibre).

## How it works

When installed, the plugin starts a `ThreadingHTTPServer` in a daemon thread from Calibre's `genesis()` lifecycle hook. It stays alive as long as the Calibre GUI is open. Bindery posts the absolute path of an imported file; the plugin calls `db.new_api.add_books()` — the same API `calibredb` uses — and returns the new Calibre book id.

Both Bindery and Calibre must be able to reach the same file at the posted path. In a typical homelab setup this means both pods mount the same NFS volume.

## Installation

See the [installation guide](../../docs/installation.md) for all three tiers:

- **Manual .zip** via Calibre's plugin dialog (always works, no infrastructure needed)
- **Kubernetes init-container** via the `charts/calibre-plugin-installer` Helm chart
- **Calibre "Get new plugins"** index (planned, not yet submitted)

## Configuration

Open **Preferences → Plugins → User plugins → Bindery Bridge → Customize**.

| Setting | Default | Description |
|---|---|---|
| Listen port | `8099` | TCP port the HTTP server binds to. Change if `8099` conflicts with another service. |
| Bind host | `127.0.0.1` | Interface to bind. Use `0.0.0.0` when Bindery runs in a different container or host; use `127.0.0.1` when both apps run on the same machine. |
| API key | *(empty)* | Shared secret for bearer authentication. Leave empty to disable auth (only safe on loopback). Use any random string of 20+ characters — e.g. output of `openssl rand -hex 20`. |

After saving, **restart Calibre** so the HTTP server binds with the new settings.

## Bindery-side settings

In Bindery: **Settings → Calibre → Mode: Calibre Bridge plugin**

| Field | Example value | Notes |
|---|---|---|
| Plugin URL | `http://calibre.default.svc.cluster.local:8099` | Base URL of the plugin server. No trailing slash. Use the Kubernetes service DNS name, or `http://localhost:8099` for single-host setups. |
| API key | *(matches plugin config)* | Must match exactly what is set in the plugin. |

Use the **Test** button to verify connectivity. A successful test returns the plugin version and Calibre version.

## Networking (Kubernetes)

The plugin binds on `0.0.0.0:<port>` inside the Calibre pod. Expose it via a `ClusterIP` service on the same port:

```yaml
apiVersion: v1
kind: Service
metadata:
  name: calibre
  namespace: default
spec:
  selector:
    app: calibre          # adjust to your pod labels
  ports:
    - name: bindery-bridge
      port: 8099
      targetPort: 8099
```

Bindery then reaches the plugin at `http://calibre.default.svc.cluster.local:8099`.

The Calibre pod does **not** need to reach Bindery — communication is one-way.

## Troubleshooting

### Test button returns "connection refused"
- Check that Calibre is running and the plugin loaded: in Calibre's Job status bar you should see no error on startup.
- Confirm the bind host is `0.0.0.0` (not `127.0.0.1`) when Bindery is in a separate pod.
- Check the port matches on both sides.
- In Kubernetes: verify the `Service` exists and `kubectl get endpoints calibre` shows a ready address.

### Test button returns "authentication failed"
- The API key in Bindery's settings doesn't match the plugin config. Copy-paste to avoid typos; the field is case-sensitive.
- If you left the plugin's API key blank, clear Bindery's API key field too.

### Books aren't appearing in Calibre after import
- The plugin returns a `calibre_id`; check Bindery's book detail page to confirm the id was persisted. If it's blank, the import call failed — check Bindery's logs for `pushToCalibre` errors.
- Confirm the file path Bindery passes is accessible from the **Calibre pod's** filesystem (same NFS mount point).
- `409 Conflict` means the book already existed in Calibre — this is normal for a re-import and Bindery will still record the existing `calibre_id`.

### Plugin doesn't appear in Calibre preferences
- Verify the `.zip` was loaded from **Preferences → Plugins → Load plugin from file**, not extracted manually.
- Calibre must be restarted after installing a new plugin.
- Check that the `.zip` contains `plugin-import-name-bindery_bridge.txt` at the root — this marker tells Calibre it is a multi-file plugin.

### Calibre upgraded and the plugin stopped working
- The plugin declares `minimum_calibre_version = (6, 0, 0)`. Calibre major releases occasionally change plugin APIs.
- Check [Releases](https://github.com/vavallee/bindery-plugins/releases) for a compatibility update, or open an issue.

## HTTP API

See [`docs/protocol.md`](../../docs/protocol.md) for the full versioned API contract.

Quick reference:

| Method | Path | Auth | Purpose |
|---|---|---|---|
| `GET` | `/v1/health` | none | Liveness probe; returns plugin + Calibre versions |
| `POST` | `/v1/books` | Bearer | Add a book by filesystem path |

## Changelog

See [`CHANGELOG.md`](../../CHANGELOG.md).
