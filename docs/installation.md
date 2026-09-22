# Installation

The Bindery Bridge plugin ships three installation tiers. Pick the one that
matches your deployment.

**How Calibre finds a plugin.** All three tiers end in the same place: an entry
in Calibre's plugin registry. `calibre.customize.ui.initialize_plugins` builds
the list of loaded plugins from `config['plugins']`, a name to zip path
dictionary stored in `<config dir>/customize.py.json`, plus the builtin and
system plugin sets. It never scans the plugins directory. Copying a zip into
`<config dir>/plugins` therefore installs nothing; something has to write the
registry entry, and `calibre-customize -a` is the supported way to do it.

## Tier 1A: manual .zip (desktop Calibre)

1. Download the latest `calibre-bridge-vX.Y.Z.zip` from
   [GitHub Releases](https://github.com/vavallee/bindery-plugins/releases).
2. Verify it against the published checksum:
   ```bash
   curl -sSLO https://github.com/vavallee/bindery-plugins/releases/download/v-calibre-bridge-X.Y.Z/calibre-bridge-vX.Y.Z.zip
   curl -sSLO https://github.com/vavallee/bindery-plugins/releases/download/v-calibre-bridge-X.Y.Z/calibre-bridge-vX.Y.Z.zip.sha256
   sha256sum -c calibre-bridge-vX.Y.Z.zip.sha256
   ```
3. In Calibre: **Preferences, Plugins, Load plugin from file** and select the
   `.zip`.
4. Restart Calibre.
5. Open **Preferences, Plugins, User plugins, Bindery Bridge, Customize** and
   set the listen port, bind host, and API key.

This path requires zero infrastructure and matches Calibre's native plugin UX.
The GUI file picker must be able to navigate to the zip file, which works on
bare metal and macOS installs where the browser runs on the same host.

## Tier 1B: `kubectl exec` (containerised or PVC Calibre)

When Calibre runs in a container (for example `linuxserver/calibre`), the GUI
file picker inside KasmVNC can only browse paths that exist **inside the
container**, so you cannot navigate to a zip sitting on your laptop or NAS.
Install the plugin by uploading it directly into the container with
`kubectl exec`:

```bash
# 1. Download the zip and its checksum into the container
kubectl exec -n <namespace> deployment/<calibre-deployment> -- sh -c '
  cd /tmp &&
  wget -q -O calibre-bridge.zip https://github.com/vavallee/bindery-plugins/releases/download/v-calibre-bridge-X.Y.Z/calibre-bridge-vX.Y.Z.zip &&
  wget -q -O calibre-bridge.zip.sha256 https://github.com/vavallee/bindery-plugins/releases/download/v-calibre-bridge-X.Y.Z/calibre-bridge-vX.Y.Z.zip.sha256 &&
  sed "s/  .*/  calibre-bridge.zip/" calibre-bridge.zip.sha256 | sha256sum -c -'

# 2. Install via calibre-customize (ships with linuxserver/calibre)
kubectl exec -n <namespace> deployment/<calibre-deployment> -- \
  calibre-customize -a /tmp/calibre-bridge.zip

# 3. Restart the pod so genesis() runs and the HTTP server starts
kubectl rollout restart deployment/<calibre-deployment> -n <namespace>
```

The `sed` in step 1 rewrites the filename in the checksum file, because the
published sidecar names the versioned zip and we saved it under a shorter name.

After the pod comes back up:

1. Open the Calibre web GUI (port 8080) and navigate to **Preferences,
   Plugins, User plugins, Bindery Bridge, Customize**.
2. Set **Listen port** (`8099`), **Bind host** (`0.0.0.0`), and **API key**.
3. Save. The dialog restarts the HTTP server in place, no pod restart needed.

### Verify the plugin is registered

```bash
kubectl exec -n <namespace> deployment/<calibre-deployment> -- \
  calibre-customize -l | grep "Bindery Bridge"
```

If that prints nothing, Calibre has no registry entry for the plugin and it
will not load, whatever is sitting in the plugins directory.

### Verify the plugin is listening

```bash
kubectl exec -n <namespace> deployment/<calibre-deployment> -- \
  ss -tlnp | grep 8099
```

Expected output: `LISTEN 0 5 0.0.0.0:8099`

### Expose the port in Kubernetes

Add port 8099 to the Calibre `Service` so Bindery can reach it:

```yaml
# calibre Service spec.ports, add alongside webgui (8080) and content (8081)
- name: plugin
  port: 8099
  targetPort: 8099
  protocol: TCP
```

Port 8099 has no transport security and the plugin binds `0.0.0.0` by default,
so set an API key and restrict who can reach the port. The Tier 2 chart can
render a NetworkPolicy that does the second part.

### Updating via kubectl exec

Repeat steps 1 to 3 above with the new version. `calibre-customize -a`
replaces the registered zip in place.

## Tier 2: Kubernetes init containers (ArgoCD managed)

For the homelab pattern used by Bindery, a small Helm chart at
`charts/calibre-plugin-installer` adds a strategic merge patch to an existing
Calibre `Deployment`. The patch injects two init containers that run before
Calibre starts:

1. `bindery-bridge-fetch` downloads the release zip and its `.sha256` into a
   scratch `emptyDir` and runs `sha256sum -c`. It uses a digest pinned
   `curlimages/curl` image and is the only container here that needs network
   access.
2. `bindery-bridge-register` runs `calibre-customize -a` against the verified
   zip, which writes the registry entry in `customize.py.json` and copies the
   zip into Calibre's plugins directory. It then runs `calibre-customize -l`
   and fails the pod if the plugin is not in the registry, so a broken install
   shows up as a pod that will not start rather than as a Calibre that quietly
   has no bridge.

Because step 2 needs the `calibre-customize` CLI, it runs the Calibre image.
Set `calibreImage` to the image your Calibre `Deployment` already runs.

### One time setup

1. Copy `argocd/application.yaml` into your homelab GitOps repo.
2. Adjust `spec.source.repoURL`, `spec.destination`, and the values to point at
   your environment. The important keys are:
   - `pluginVersion`: the plugin release to install, for example `"0.5.0"`
   - `pluginName`: the plugin's registered name, `"Bindery Bridge"`. It has to
     match the `name` attribute in `plugins/calibre-bridge/__init__.py`,
     because that is the key Calibre stores in its registry.
   - `calibreDeploymentName`: the Calibre `Deployment` name to patch
   - `calibreNamespace`: its namespace
   - `calibreHome`: where the config volume is mounted, typically `/config`
   - `calibreConfigDir`: Calibre's config directory, typically
     `/config/.config/calibre`
   - `calibreImage`: the image providing `calibre-customize`
   - `volumeName`: the existing volume that maps to `calibreHome`
   - `runAsUser`: the uid that owns the config volume, matching the Calibre
     container's PUID
3. `kubectl apply -f application.yaml`, or let ArgoCD pick it up through your
   ApplicationSet.

### Upgrades

Bump `pluginVersion` in the chart's `values.yaml` or in the ApplicationSet
override, merge, and let ArgoCD sync. The init containers re-download and
re-register the matching `.zip` on the next Calibre pod restart.

### Restricting who can reach the bridge

`networkPolicy` is opt in and off by default. When you enable it:

```yaml
networkPolicy:
  enabled: true
  ingress:
    enabled: true
    bridgePort: 8099
    from:
      - namespaceSelector:
          matchLabels:
            kubernetes.io/metadata.name: bindery
        podSelector:
          matchLabels:
            app.kubernetes.io/name: bindery
    openPorts: [8080, 8081]
```

Two things to know. `ingress.from` is required: an ingress rule with no peers
admits every pod in the cluster to the bridge port, so the chart refuses to
render one and tells you why. And a policy with `Ingress` in `policyTypes`
denies every port it does not name, which is what `openPorts` is for. Leave the
Calibre web GUI and content server in that list or you will lock yourself out
of the GUI.

`networkPolicy.egress` restricts the fetch container's download and is
unchanged from earlier chart versions, other than moving under `egress`:
`networkPolicy.egressCIDRs` is now `networkPolicy.egress.cidrs`.

### Migrating from chart 0.1.x

Chart 0.1.x dropped the release zip into `pluginDestDir` and stopped there,
which registered nothing, so Calibre never loaded the plugin. Chart 0.2.0
removes that value. Rendering fails with an explanatory message if it is still
set, rather than silently ignoring it.

| 0.1.x | 0.2.0 |
|---|---|
| `pluginDestDir: /config/.config/calibre/plugins` | removed, use `calibreConfigDir: /config/.config/calibre` plus `calibreHome: /config` |
| `image.tag: 8.5.0` | `image.digest`, pinned, with `image.tag` kept for readability |
| `networkPolicy.egressCIDRs` | `networkPolicy.egress.cidrs` |
| no ingress control | `networkPolicy.ingress` |
| n/a | `calibreImage`, the image providing `calibre-customize` |
| n/a | `pluginName`, `runAsUser`, `verifyChecksum` |

If you ran 0.1.x, the plugin is not installed no matter what the pod logs said.
Upgrade the chart and restart the Calibre pod, then confirm with
`calibre-customize -l` as in Tier 1B.

## Tier 3: Calibre "Get new plugins" index (future)

Calibre ships an in GUI plugin browser sourced from the MobileRead forum.
Submission requires manual review by upstream and is planned once Tier 1 and
Tier 2 have seen production use in the homelab. No user action required today.
When this ships, Bindery Bridge will appear in **Preferences, Plugins, Get new
plugins**.

Note that submission may carry licensing requirements. See the licensing note
in [`../README.md`](../README.md).
