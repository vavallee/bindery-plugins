# Installation

The Bindery Bridge plugin ships in three installation tiers. Pick the one that matches your deployment.

---

## Tier 1 — Manual .zip (always works)

No infrastructure required. Matches Calibre's native plugin UX.

1. Download the latest `calibre-bridge-vX.Y.Z.zip` from [GitHub Releases](https://github.com/vavallee/bindery-plugins/releases).
2. In Calibre: **Preferences → Plugins → Load plugin from file** → select the `.zip`.
3. Restart Calibre.
4. **Configure the plugin**: Preferences → Plugins → User plugins → Bindery Bridge → Customize.
   - Set **Bind host** to `0.0.0.0` if Bindery runs on a different host or in a container.
   - Set a strong **API key** (e.g. `openssl rand -hex 20`).
   - Leave **Listen port** at `8099` unless it conflicts.
5. Restart Calibre again so the HTTP server picks up your settings.
6. In Bindery: Settings → Calibre → Mode: **Calibre Bridge plugin**. Enter the URL (`http://<calibre-host>:8099`) and API key. Click **Test**.

---

## Tier 2 — Kubernetes init-container

For homelab setups where Calibre runs as a Kubernetes `Deployment`. The `charts/calibre-plugin-installer` Helm chart injects a strategic-merge patch onto the Calibre deployment that downloads and installs the plugin before the main container starts. No manual steps after the initial wiring.

### Prerequisites

- A running Calibre `Deployment` with a persistent `config` volume mounted at `/config` (the directory that holds `.config/calibre/`).
- ArgoCD (or `helm install`) managing the Calibre namespace.
- Network access from the init container to `github.com` at deploy time (to download the `.zip`).

### Step 1 — Add a ClusterIP service for the plugin port

The plugin HTTP server needs to be reachable from the Bindery pod. Create a service if you don't already have one:

```yaml
# calibre-bridge-service.yaml
apiVersion: v1
kind: Service
metadata:
  name: calibre
  namespace: default          # adjust to your namespace
spec:
  selector:
    app: calibre              # adjust to your pod labels
  ports:
    - name: bindery-bridge
      port: 8099
      targetPort: 8099
```

```bash
kubectl apply -f calibre-bridge-service.yaml
```

Bindery will reach the plugin at `http://calibre.default.svc.cluster.local:8099`.

### Step 2 — Deploy the Helm chart

Copy `argocd/application.yaml` from this repo into your homelab GitOps repository and adjust the values:

```yaml
# argocd/application.yaml (key values to customise)
spec:
  source:
    repoURL: https://github.com/vavallee/bindery-plugins
    targetRevision: development
    helm:
      values: |
        pluginVersion: "0.1.0"              # plugin release to install
        calibreDeploymentName: calibre      # name of your Calibre Deployment
        calibreNamespace: default           # its namespace
        pluginDestDir: /config/.config/calibre/plugins
        volumeName: config                  # existing volume that maps to /config
```

Commit and push. ArgoCD will apply the patch on its next sync cycle.

What the chart does:

- Adds an `initContainer` named `bindery-bridge-installer` to the Calibre `Deployment` using `curlimages/curl`.
- The init container downloads `calibre-bridge-v<pluginVersion>.zip` from GitHub Releases into `pluginDestDir`.
- Calibre loads plugins from that directory on startup.
- Runs as non-root with a read-only root filesystem (`readOnlyRootFilesystem: true`, `allowPrivilegeEscalation: false`, all capabilities dropped).

### Step 3 — Configure the plugin

On the **first** deploy (or after the Calibre pod restarts), configure the plugin:

1. `kubectl exec -it <calibre-pod> -- bash` (or use your Calibre pod's shell)
2. Open the Calibre GUI (if you access it via VNC/noVNC): Preferences → Plugins → User plugins → Bindery Bridge → Customize.
3. Set **Bind host** to `0.0.0.0`, choose a port (`8099`), and set an **API key**.
4. Restart Calibre (restart the pod: `kubectl rollout restart deployment/calibre`).

Alternatively, write the config directly to the JSON file before first launch:

```bash
mkdir -p /config/.config/calibre/
cat > /config/.config/calibre/plugins/bindery_bridge.json <<'EOF'
{
  "port": 8099,
  "bind_host": "0.0.0.0",
  "api_key": "your-secret-key-here"
}
EOF
```

### Step 4 — Configure Bindery

Settings → Calibre → Mode: **Calibre Bridge plugin**

| Field | Value |
|---|---|
| Plugin URL | `http://calibre.default.svc.cluster.local:8099` |
| API key | *(same value set above)* |

Click **Test** to confirm. A green tick with the plugin version means it's working.

### Upgrades

To upgrade the plugin:

1. Bump `pluginVersion` in your `values.yaml` (or the ApplicationSet override).
2. Commit and push.
3. ArgoCD syncs and restarts the Calibre pod.
4. The new `.zip` is downloaded by the init container before Calibre starts.

This is the same flow Bindery uses for image tag bumps — one value change, one merge, ArgoCD handles the rest.

---

## Tier 3 — Calibre "Get new plugins" index (planned)

Calibre ships an in-GUI plugin browser sourced from the MobileRead forum. Submission requires manual review by the Calibre maintainer. This tier is planned after Tier 1 and Tier 2 have production use. When available, Bindery Bridge will appear under **Preferences → Plugins → Get new plugins** and can be installed and updated from within Calibre itself.

No action required today. Tier 1 and Tier 2 cover all current users.
