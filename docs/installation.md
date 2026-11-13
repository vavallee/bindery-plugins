# Installation

The Bindery Bridge plugin ships three installation tiers, plus pull mode for
a desktop Calibre Bindery cannot reach (Tier 1C). Pick the one that
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
   Releases after 0.8.3 also carry signed build provenance. With the GitHub
   CLI, `gh attestation verify calibre-bridge-vX.Y.Z.zip --repo vavallee/bindery-plugins`
   confirms the zip was built by this repo's release workflow.
3. In Calibre: **Preferences, Plugins, Load plugin from file** and select the
   `.zip`.
4. Restart Calibre.
5. Open **Preferences, Plugins, User plugins, Bindery Bridge, Customize** and
   set the listen port, bind host, and API key.

Press **Generate** for the API key. A fresh install has no key and a bind host
of `0.0.0.0`, and the bridge does not listen like that: with no key on a
network bind it refuses to start and serves a health only page that names the
reason, so nothing can add books until you set a key. A key shorter than 16
characters is refused on a network bind in the same way, including a short key
that an earlier version accepted: after upgrading, such an install serves the
health only page until you press **Generate** and update the key in Bindery.
Bind host `127.0.0.1`
may run with no key, which is for local use only (the bridge then also refuses
requests that carry an `Origin` header or a non loopback `Host`), and setting a
key is recommended even there.

Failed keys are throttled per client address: five wrong or malformed bearer
tokens within a minute block that address for a minute, and during the block
every request from it gets `429` with a `Retry-After` header, even with the
correct key. Behind NAT or on a Docker bridge many clients share one address,
so any of them can lock the others out for a minute, and a Bindery holding a
stale key locks itself out, because its health checks count too. What you see
is `429` with `Retry-After` in the Calibre bridge log and in Bindery's
Calibre status. The fix is to correct the key (Bindery Settings, Calibre, and
the plugin's Customize window); the block then lifts on its own.

This path requires zero infrastructure and matches Calibre's native plugin UX.
The GUI file picker must be able to navigate to the zip file, which works on
bare metal and macOS installs where the browser runs on the same host.

## Tier 1C: desktop Calibre with pull mode

For a Calibre on a desktop or laptop that Bindery cannot reach: no shared
drive, no path remap, no inbound firewall rule and no fixed address. The
plugin connects out to Bindery and fetches each book itself. Needs plugin
0.8.0 or later and a Bindery with the pull routes.

1. Install the zip as in Tier 1A and restart Calibre.
2. Open the library the books should go to. Pull delivers only to the library
   that is open when it is turned on, and pauses while any other one is open.
3. Open **Preferences, Plugins, User plugins, Bindery Bridge, Customize** (or
   the Bindery Bridge toolbar button):
   - **API key**: press **Generate**, or paste the key Bindery already has.
   - **Pull mode**: tick **Fetch books from Bindery (pull mode)**.
   - **Bindery URL**: the address you open Bindery at, including any URL base,
     for example `https://bindery.example.net`.
   - **CA file**: only for a Bindery behind a private CA or a self signed
     certificate. Point it at that certificate (PEM). There is no option to
     skip certificate checks.
4. In Bindery: **Settings, Calibre**, mode `plugin`, **Transport** `Pull`,
   and the same API key.
5. Reopen the plugin settings. **Pull status** should read "Connected to
   Bindery; nothing waiting", or report how many books the last pass
   delivered and how many are still waiting. The line updates every few
   seconds while the window is open. Messages you may see instead:
   - "Bindery is set to push": step 4 is not done yet.
   - "Bindery rejected the API key": the two keys differ. The plugin waits an
     hour before trying again, or saving the settings retries at once.
   - "Paused: a different library is open": switch back, or turn pull off and
     on again in the library that should receive books.
   - "Bindery URL is plain http": the key and the books travel unencrypted.
     Fine on a trusted LAN, use https otherwise.

The push server keeps listening as before; with pull mode Bindery simply does
not call it. A book Calibre already has is acknowledged as already there, so
switching between push and pull never makes a second copy.

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
   - `pluginVersion`: the plugin release to install, written `"X.Y.Z"`
     without the tag prefix. The file ships pinned to a published release.
   - `spec.source.targetRevision`: the release tag the chart is read from,
     `v-calibre-bridge-X.Y.Z`. Keep it on the same release as `pluginVersion`.
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
override (and `targetRevision` beside it), merge, and let ArgoCD sync. The
init containers re-download and re-register the matching `.zip` on the next
Calibre pod restart.

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

## Troubleshooting

Start with the plugin settings (**Preferences, Plugins, User plugins, Bindery
Bridge, Customize**, or the Bindery Bridge toolbar button). The two status
lines update every few seconds while the window is open:

- **Status** says whether the push server is listening, and why not if it is
  not.
- **Pull status** shows the result of the last pass, how many deliveries are
  still waiting in Bindery, the last error with the time it happened, and the
  time of the last successful check. The error goes away after the next pass
  that reaches Bindery and finishes cleanly.

### Warnings without any setup

Start Calibre from a terminal with `calibre-debug -g`. The plugin prints its
warnings and errors there. That includes every step of the book list refresh
that failed, on a line starting with `Calibre GUI refresh:`, which is the
place to look when a delivered book only shows up after Ctrl+R. Warnings keep
appearing there while the debug log is on. That stderr copy is not redacted,
only the log file is, so do not paste raw `calibre-debug -g` output into a
public issue without checking it for your API key first.

### The debug log

Warnings are not the whole story. To record everything the plugin does:

1. Open the plugin settings, tick **Write debug log** and save. It takes
   effect at once, with no restart.
2. Do the thing that goes wrong.
3. Open the file. **Log file** in the settings window shows the full path and
   can be selected and copied, and **Open log folder** opens the folder. The
   file is `bindery_bridge.log` in Calibre's configuration folder:
   `%APPDATA%\calibre` on Windows, `~/Library/Preferences/calibre` on macOS,
   `~/.config/calibre` on Linux, and usually `/config/.config/calibre` in a
   `linuxserver/calibre` container.
4. Untick **Write debug log** and save when you are done.

The log holds at most 1 MB, plus the two files before it (`.log.1` and
`.log.2`), so it never grows past about 3 MB. If it cannot be opened, the
**Status** line says so and gives the reason.

For a bug report, paste:

- the lines of `bindery_bridge.log` from around the time of the problem,
- the **Status** and **Pull status** lines,
- the plugin version, the Calibre version and the operating system.

The API key is never written to the log. File paths, which usually include
the author and the title, and your Bindery address are, so read what you paste
before you post it.

## Tier 3: Calibre "Get new plugins" index (future)

Calibre ships an in GUI plugin browser sourced from the MobileRead forum.
Submission requires manual review by upstream and is planned once Tier 1 and
Tier 2 have seen production use in the homelab. No user action required today.
When this ships, Bindery Bridge will appear in **Preferences, Plugins, Get new
plugins**.

Note that submission may carry licensing requirements. See the licensing note
in [`../README.md`](../README.md).
