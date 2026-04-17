# bindery-plugins

Plugins that extend third-party tools with Bindery-specific integrations.
Sibling repo to [`bindery`](https://github.com/vavallee/bindery); kept separate so its Python toolchain and release cadence don't weigh on Bindery's Go codebase.

## Plugins

| Name | Target | Path | Status |
|---|---|---|---|
| [Bindery Bridge](plugins/calibre-bridge/README.md) | Calibre 6+ | `plugins/calibre-bridge/` | v0.1.0 |

## What is Bindery Bridge?

Bindery can mirror every imported book into your Calibre library automatically. The classic path uses `calibredb add`, which works fine when both apps share a filesystem **and** the same container. In a Kubernetes homelab — where Calibre runs in its own pod — `calibredb` isn't in the Bindery pod's `$PATH`.

Bindery Bridge solves this without bundling Calibre into Bindery's distroless image. The plugin runs a small HTTP server **inside the Calibre GUI process** on a configurable port. Bindery posts the file path; Calibre adds the book using its own internal database API. Both pods share an NFS volume, so only the path travels over the network — no file upload, no binary sharing.

```
Bindery pod                           Calibre pod
──────────────────────────────────    ─────────────────────────────────
importScanner.pushToCalibre()
  → PluginClient.Add("/nfs/book.epub")
    → POST http://calibre:8099/v1/books ──→ BinderyBridgeAction (HTTP server)
                                              → db.new_api.add_books()
                                              ← {"id": 1234}
  ← calibre_id=1234 persisted on book row
```

The NFS volume `/media/BOOKS` is mounted into both pods; Bindery writes the file there during import, then tells the plugin where it landed.

## Quick start

### 1 — Install the plugin

**Desktop / non-Kubernetes**

1. Download `calibre-bridge-vX.Y.Z.zip` from [Releases](https://github.com/vavallee/bindery-plugins/releases).
2. Calibre → **Preferences → Plugins → Load plugin from file** → pick the `.zip`.
3. Restart Calibre.

**Kubernetes** — use the [init-container Helm chart](docs/installation.md#tier-2--kubernetes-init-container).

### 2 — Configure the plugin

Calibre → **Preferences → Plugins → User plugins → Bindery Bridge → Customize**

| Setting | Default | Notes |
|---|---|---|
| Listen port | `8099` | Any unused port. Must match what you enter in Bindery. |
| Bind host | `127.0.0.1` | Set to `0.0.0.0` when Bindery runs in a separate pod/host. |
| API key | *(empty)* | Set to any secret string; paste the same value into Bindery. |

Restart Calibre after saving.

### 3 — Configure Bindery

Settings → Calibre → **Mode: Calibre Bridge plugin**

| Field | Example |
|---|---|
| Plugin URL | `http://calibre.default.svc.cluster.local:8099` |
| API key | *(same value you set in the plugin)* |

Click **Test** — Bindery calls `GET /v1/health` and shows the plugin and Calibre version if everything is reachable.

## Development

Requires Python 3.10+ with `ruff`, `mypy`, and `pytest`.

```bash
# run tests (no Calibre install needed — calibre imports are stubbed)
pytest plugins/calibre-bridge/tests

# lint
ruff check plugins/ && ruff format --check plugins/

# build a .zip ready for manual install
python scripts/build_plugin.py plugins/calibre-bridge
# → dist/calibre-bridge-v0.1.0.zip
```

See [`docs/`](docs/) for the HTTP protocol contract and full installation guide.

## License

MIT. See [`LICENSE`](LICENSE).
