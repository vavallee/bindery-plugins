# bindery-plugins

Plugins that extend third party tools with Bindery specific integrations.
Sibling repo to [`bindery`](https://github.com/vavallee/bindery), kept separate
so its Python toolchain and release cadence do not weigh on Bindery's Go
codebase.

## Plugins

| Name             | Target       | Path                        | Status  |
|------------------|--------------|-----------------------------|---------|
| Bindery Bridge   | Calibre 6+   | `plugins/calibre-bridge/`   | v0.6.0  |

## What this repo builds

`scripts/build_plugin.py` packages a directory under `plugins/` into the zip
Calibre loads. It writes two files into `dist/`:

- `calibre-bridge-vX.Y.Z.zip`, the plugin itself, plus `LICENSE` and
  `COPYRIGHT` copied into the zip root so the licence travels with the
  artefact. It contains only this repo's files: no Calibre code and no third
  party code is redistributed in it.
- `calibre-bridge-vX.Y.Z.zip.sha256`, a `sha256sum -c` compatible sidecar.

The plugin has no runtime dependencies. It uses the Python standard library
plus what Calibre provides in process. Everything in
`requirements-dev.txt` is CI and development tooling and is not shipped.

## How a release is produced

1. A `v*` tag on `main` triggers `.github/workflows/ci.yml`.
2. `test`, `test release tooling` and `lint` gate the build. The plugin test
   matrix is Python 3.10, 3.11 and 3.14, which is what Calibre 6, Calibre 7 and
   8, and Calibre 9 embed respectively, read from `bypy/sources.json` in the
   Calibre tree.
3. `build` runs `scripts/build_plugin.py` and verifies the checksum sidecar
   against the zip it just produced.
4. `release` extracts the matching `CHANGELOG.md` section and publishes the zip
   and the `.sha256` to GitHub Releases.

## How to verify a release

```bash
curl -sSLO https://github.com/vavallee/bindery-plugins/releases/download/v-calibre-bridge-X.Y.Z/calibre-bridge-vX.Y.Z.zip
curl -sSLO https://github.com/vavallee/bindery-plugins/releases/download/v-calibre-bridge-X.Y.Z/calibre-bridge-vX.Y.Z.zip.sha256
sha256sum -c calibre-bridge-vX.Y.Z.zip.sha256
```

The Helm installer in `charts/calibre-plugin-installer` runs exactly this check
before installing, and refuses to proceed if it fails.

## Quick start

### Desktop Calibre

1. Grab the latest `calibre-bridge-vX.Y.Z.zip` from
   [Releases](https://github.com/vavallee/bindery-plugins/releases) and verify
   it as above.
2. Calibre: **Preferences, Plugins, Load plugin from file**, select the `.zip`.
3. Restart Calibre, then open **Preferences, Plugins, User plugins, Bindery
   Bridge, Customize** and set the listen port, bind host, and API key.
4. Point Bindery at it: **Settings, Calibre, mode `plugin`**, URL
   `http://<calibre-host>:<port>`.

### Kubernetes or containerised Calibre

When Calibre runs in a container (for example `linuxserver/calibre`), the GUI's
file picker can only see paths inside the container, so you cannot browse to a
zip on your laptop. Install with `calibre-customize` instead:

```bash
# 1. Download the zip into the container
kubectl exec -n <namespace> deployment/<calibre> -- \
  wget -q -O /tmp/calibre-bridge.zip \
  https://github.com/vavallee/bindery-plugins/releases/download/v-calibre-bridge-X.Y.Z/calibre-bridge-vX.Y.Z.zip

# 2. Register it (calibre-customize ships with linuxserver/calibre)
kubectl exec -n <namespace> deployment/<calibre> -- \
  calibre-customize -a /tmp/calibre-bridge.zip

# 3. Restart the pod so Calibre picks up the new plugin
kubectl rollout restart deployment/<calibre> -n <namespace>
```

Step 2 is not optional and copying the zip into Calibre's plugins directory is
not a substitute for it. Calibre builds its plugin list from the registry in
`customize.py.json` and never scans that directory.

After restart, the plugin HTTP server starts automatically. Configure the API
key and port via **Preferences, Plugins, User plugins, Bindery Bridge,
Customize** using the Calibre web GUI at port 8080.

For a GitOps and ArgoCD approach using a Helm init container, see
[`docs/installation.md`](docs/installation.md).

## Development

- Python 3.10 or newer. Install the pinned tooling with
  `pip install -r requirements-dev.txt`.
- Run every test:
  ```
  pytest
  ```
  `plugins/calibre-bridge/tests` covers the plugin, `tests/` covers the release
  tooling under `scripts/`.
- Lint:
  ```
  ruff check plugins/ scripts/ tests/
  ruff format --check plugins/ scripts/ tests/
  ```
- Build a `.zip`:
  ```
  python scripts/build_plugin.py plugins/calibre-bridge
  ```
- Render the Helm chart:
  ```
  helm template ci charts/calibre-plugin-installer
  ```

Everything a plugin imports must live inside its own `plugins/<name>/`
directory: `build_plugin.py` zips that directory and nothing else, so an
import from elsewhere in the repo passes pytest here and then fails to load
inside Calibre. See [`CONTRIBUTING.md`](CONTRIBUTING.md).

See [`docs/`](docs/) for the HTTP protocol contract and installation tiers.

## Licensing and attribution

This repo is licensed **GPL-3.0 or later**. The full licence text is in
[`LICENSE`](LICENSE) and the copyright line, the standard notice and the
reasoning behind the choice are in [`COPYRIGHT`](COPYRIGHT). Both files are
copied into every release zip.

The reason is that the plugin is loaded into and runs inside Calibre, and it
uses Qt through Calibre's bundled copy:

- **Calibre** is copyright Kovid Goyal and contributors and is licensed
  **GPL-3.0**. <https://github.com/kovidgoyal/calibre>
- **Qt**, which Calibre bundles and which this plugin's configuration widget
  uses through `qt.core`, is licensed **LGPL-3.0 or GPL-3.0**.
  <https://www.qt.io/licensing>

Neither is redistributed in the release zip. The combination happens on your
machine when Calibre loads the plugin, and that combined work is subject to
Calibre's terms, which is why this repo carries the licence that combination
requires rather than one that would advertise permissions it cannot convey.

This project is **not affiliated with, endorsed by, or a product of the Calibre
project**. "Calibre" is used descriptively, to say what the plugin integrates
with.

Bindery itself is unaffected and stays MIT. It never links Calibre: it runs
`calibredb` as a separate process or speaks HTTP to this plugin, and both are
arm's length interfaces. The two repos are separate for exactly this reason.
