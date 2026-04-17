# Installation

The Bindery Bridge plugin ships three installation tiers. Pick the one that
matches your deployment.

## Tier 1A — Manual .zip (desktop Calibre)

1. Download the latest `calibre-bridge-vX.Y.Z.zip` from
   [GitHub Releases](https://github.com/vavallee/bindery-plugins/releases).
2. In Calibre: **Preferences → Plugins → Load plugin from file** and select
   the `.zip`.
3. Restart Calibre.
4. Open **Preferences → Plugins → User plugins → Bindery Bridge →
   Customize** and set the listen port, bind host, and API key.

This path requires zero infrastructure and matches Calibre's native plugin
UX. The GUI file picker must be able to navigate to the zip file, which works
on bare-metal and macOS installs where the browser runs on the same host.

## Tier 1B — `kubectl exec` (containerised / PVC Calibre)

When Calibre runs in a container (e.g. `linuxserver/calibre`), the GUI file
picker inside KasmVNC can only browse paths that exist **inside the container**
— you cannot navigate to a zip sitting on your laptop or NAS. Install the
plugin by uploading it directly into the container with `kubectl exec`:

```bash
# 1. Download the zip into the container
kubectl exec -n <namespace> deployment/<calibre-deployment> -- \
  wget -q -O /tmp/calibre-bridge.zip \
  https://github.com/vavallee/bindery-plugins/releases/download/v-calibre-bridge-X.Y.Z/calibre-bridge-vX.Y.Z.zip

# 2. Install via calibre-customize (ships with linuxserver/calibre)
kubectl exec -n <namespace> deployment/<calibre-deployment> -- \
  calibre-customize -a /tmp/calibre-bridge.zip

# 3. Restart the pod so genesis() runs and the HTTP server starts
kubectl rollout restart deployment/<calibre-deployment> -n <namespace>
```

After the pod comes back up:

1. Open the Calibre web GUI (port 8080) and navigate to **Preferences →
   Plugins → User plugins → Bindery Bridge → Customize**.
2. Set **Listen port** (`8099`), **Bind host** (`0.0.0.0`), and **API key**.
3. Save — the dialog restarts the HTTP server in-place, no pod restart needed.

### Verify the plugin is listening

```bash
kubectl exec -n <namespace> deployment/<calibre-deployment> -- \
  ss -tlnp | grep 8099
```

Expected output: `LISTEN 0 5 0.0.0.0:8099`

### Expose the port in Kubernetes

Add port 8099 to the Calibre `Service` so Bindery can reach it:

```yaml
# calibre Service spec.ports — add alongside webgui (8080) and content (8081)
- name: plugin
  port: 8099
  targetPort: 8099
  protocol: TCP
```

### Updating via kubectl exec

```bash
kubectl exec -n <namespace> deployment/<calibre-deployment> -- \
  wget -q -O /tmp/calibre-bridge.zip \
  https://github.com/vavallee/bindery-plugins/releases/download/v-calibre-bridge-X.Y.Z/calibre-bridge-vX.Y.Z.zip && \
kubectl exec -n <namespace> deployment/<calibre-deployment> -- \
  calibre-customize -a /tmp/calibre-bridge.zip && \
kubectl rollout restart deployment/<calibre-deployment> -n <namespace>
```

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
   - `pluginVersion` — the plugin release to install, e.g. `"0.2.0"`
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
matching `.zip` on the next Calibre pod restart.

## Tier 3 — Calibre "Get new plugins" index (future)

Calibre ships an in-GUI plugin browser sourced from the MobileRead forum.
Submission requires manual review by upstream and is planned once Tier 1 and
Tier 2 have seen production use in the homelab. No user action required
today — when this ships, Bindery Bridge will appear in **Preferences →
Plugins → Get new plugins**.
