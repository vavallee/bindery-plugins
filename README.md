# bindery-plugins

Plugins that extend third-party tools with Bindery-specific integrations.
Sibling repo to [`bindery`](https://github.com/vavallee/bindery); kept
separate so its Python toolchain and release cadence don't weigh on
Bindery's Go codebase.

## Plugins

| Name             | Target       | Path                        | Status  |
|------------------|--------------|-----------------------------|---------|
| Bindery Bridge   | Calibre 6+   | `plugins/calibre-bridge/`   | v0.3.0  |

## Quick start

### Desktop Calibre

1. Grab the latest `calibre-bridge-vX.Y.Z.zip` from
   [Releases](https://github.com/vavallee/bindery-plugins/releases).
2. Calibre → **Preferences → Plugins → Load plugin from file** → select the
   `.zip`.
3. Restart Calibre, then open **Preferences → Plugins → User plugins →
   Bindery Bridge → Customize** and set the listen port, bind host, and API
   key.
4. Point Bindery at it: **Settings → Calibre → mode `plugin`**, URL
   `http://<calibre-host>:<port>`.

### Kubernetes / containerised Calibre (PVC install)

When Calibre runs in a container (e.g. `linuxserver/calibre`), the GUI's file
picker can only see paths inside the container — you can't browse to a zip on
your laptop. Install via `kubectl exec` instead:

```bash
# 1. Download the zip into the container
kubectl exec -n <namespace> deployment/<calibre> -- \
  wget -q -O /tmp/calibre-bridge.zip \
  https://github.com/vavallee/bindery-plugins/releases/download/v-calibre-bridge-X.Y.Z/calibre-bridge-vX.Y.Z.zip

# 2. Install (calibre-customize ships with linuxserver/calibre)
kubectl exec -n <namespace> deployment/<calibre> -- \
  calibre-customize -a /tmp/calibre-bridge.zip

# 3. Restart the pod so Calibre picks up the new plugin
kubectl rollout restart deployment/<calibre> -n <namespace>
```

After restart, the plugin HTTP server starts automatically. Configure the
API key and port via **Preferences → Plugins → User plugins → Bindery Bridge
→ Customize** (using the Calibre web GUI at port 8080).

For a GitOps/ArgoCD approach using a Helm init-container, see
[`docs/installation.md`](docs/installation.md).

## Development

- Python 3.10+ with `ruff` and `pytest` installed.
- Run plugin unit tests:
  ```
  pytest plugins/calibre-bridge/tests
  ```
- Build a `.zip`:
  ```
  python scripts/build_plugin.py plugins/calibre-bridge
  ```

See [`docs/`](docs/) for the HTTP protocol contract and installation tiers.

## License

MIT. See [`LICENSE`](LICENSE).
