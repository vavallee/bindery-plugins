# Installation

The Bindery Bridge plugin ships three installation tiers. Pick the one that
matches your deployment.

## Tier 1 — Manual .zip (always works)

1. Download the latest `calibre-bridge-vX.Y.Z.zip` from
   [GitHub Releases](https://github.com/vavallee/bindery-plugins/releases).
2. In Calibre: **Preferences -> Plugins -> Load plugin from file** and select
   the `.zip`.
3. Restart Calibre.
4. Open **Preferences -> Plugins -> User plugins -> Bindery Bridge ->
   Customize** and set the listen port, bind host, and API key.
5. Restart Calibre once more so the HTTP server binds with your settings.

This path requires zero infrastructure and matches Calibre's native plugin
UX.

## Tier 2 — Kubernetes init-container (ArgoCD-managed)

For the homelab pattern used by Bindery, a tiny Helm chart at
`charts/calibre-plugin-installer` adds a strategic-merge patch to an existing
Calibre `Deployment`. The patch injects an init container that downloads the
plugin `.zip` from GitHub Releases into Calibre's persistent plugins
directory before the main container starts.

### One-time setup

1. Copy `argocd/application.yaml` into your homelab GitOps repo.
2. Adjust the `spec.source.repoURL`, `spec.destination`, and `values` to
   point at your environment. The important keys are:
   - `pluginVersion` — the plugin release to install, e.g. `"0.1.0"`
   - `calibreDeploymentName` — the Calibre `Deployment` name to patch
   - `calibreNamespace` — its namespace
   - `pluginDestDir` — Calibre's plugins directory inside the container,
     typically `/config/.config/calibre/plugins`
   - `volumeName` — the existing volume mount that maps to `/config`
3. `kubectl apply -f application.yaml` (or let ArgoCD pick it up through
   your ApplicationSet).

### Upgrades

Bump `pluginVersion` in the chart's `values.yaml` (or the ApplicationSet
override), merge, and let ArgoCD sync. The init container re-downloads the
matching `.zip` on the next Calibre pod restart. This is the same flow
Bindery uses for its own image tag bumps.

## Tier 3 — Calibre "Get new plugins" index (future)

Calibre ships an in-GUI plugin browser sourced from the MobileRead forum.
Submission requires manual review by upstream and is planned once Tier 1 and
Tier 2 have seen production use in the homelab. No user action required
today — when this ships, Bindery Bridge will appear in **Preferences ->
Plugins -> Get new plugins**.
