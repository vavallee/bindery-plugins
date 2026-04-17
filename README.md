# bindery-plugins

Plugins that extend third-party tools with Bindery-specific integrations.
Sibling repo to [`bindery`](https://github.com/vavallee/bindery); kept
separate so its Python toolchain and release cadence don't weigh on
Bindery's Go codebase.

## Plugins

| Name             | Target       | Path                        | Status  |
|------------------|--------------|-----------------------------|---------|
| Bindery Bridge   | Calibre 6+   | `plugins/calibre-bridge/`   | v0.1.0  |

## Quick start

Install Bindery Bridge manually:

1. Grab the latest `calibre-bridge-vX.Y.Z.zip` from
   [Releases](https://github.com/vavallee/bindery-plugins/releases).
2. Calibre -> Preferences -> Plugins -> Load plugin from file -> pick the
   `.zip`.
3. Restart Calibre, configure port/API key in Preferences -> Plugins ->
   User plugins -> Bindery Bridge -> Customize.
4. Point Bindery at it: Settings -> Calibre -> mode `plugin`, URL
   `http://<calibre-host>:<port>`.

For Kubernetes users: see
[`docs/installation.md`](docs/installation.md) for the init-container Helm
chart under `charts/calibre-plugin-installer/`.

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
