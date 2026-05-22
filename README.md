# Podmortem — Pod Restart Root Cause Logger

Watches for Kubernetes pod restarts and captures the **reason**, **last logs**, and **events** at the moment of restart. Stores a searchable history in SQLite that persists beyond the 1-hour event TTL in Kubernetes.

![Architecture](Designer.png?v=2)

## Features

- Real-time pod restart detection via Kubernetes Watch API
- Captures previous container logs (the crashed container's output)
- Records associated pod events at time of restart
- SQLite-backed searchable history
- Rich CLI for querying restart history
- Runs in-cluster or locally with kubeconfig

## Project Structure

```
podmortem/
├── src/podmortem/
│   ├── __init__.py
│   ├── watcher.py        # Core Kubernetes watch loop
│   ├── storage.py        # SQLite storage layer
│   └── cli.py            # CLI (click + rich)
├── charts/podmortem/     # Helm chart
│   ├── Chart.yaml
│   ├── values.yaml
│   └── templates/
│       ├── deployment.yaml
│       ├── serviceaccount.yaml
│       ├── clusterrole.yaml
│       ├── clusterrolebinding.yaml
│       ├── pvc.yaml
│       └── NOTES.txt
├── Dockerfile
├── .dockerignore
├── pyproject.toml
└── requirements.txt
```

## Build

```bash
cd podmortem
python3 -m venv .venv
source .venv/bin/activate
pip3 install -e .
```

## Deploy to Kubernetes

### Option A: Helm (Recommended)

```bash
# Install with defaults (watches all namespaces)
helm install podmortem charts/podmortem -n podmortem --create-namespace

# Install with custom values
helm install podmortem charts/podmortem -n podmortem --create-namespace \
  --set image.tag=v0.1.1 \
  --set watchNamespace=production \
  --set persistence.size=5Gi

# Upgrade an existing release
helm upgrade podmortem charts/podmortem -n podmortem

# Uninstall
helm uninstall podmortem -n podmortem
```

#### Helm Values

| Parameter | Default | Description |
|-----------|---------|-------------|
| `image.repository` | `devopsart1/podmortem` | Container image repository |
| `image.tag` | `latest` | Image tag |
| `image.pullPolicy` | `Always` | Image pull policy |
| `watchNamespace` | `""` (all) | Namespace to watch (empty = all) |
| `verbose` | `true` | Enable debug logging |
| `persistence.enabled` | `true` | Enable PVC for SQLite data |
| `persistence.size` | `1Gi` | PVC storage size |
| `persistence.storageClass` | `""` | StorageClass (empty = default) |
| `persistence.existingClaim` | `""` | Use an existing PVC |
| `resources.requests.cpu` | `50m` | CPU request |
| `resources.requests.memory` | `64Mi` | Memory request |
| `resources.limits.cpu` | `100m` | CPU limit |
| `resources.limits.memory` | `128Mi` | Memory limit |
| `rbac.create` | `true` | Create ClusterRole/Binding |
| `serviceAccount.create` | `true` | Create ServiceAccount |

## Usage

### Query from Kubernetes (no local install needed)

```bash
# Get the pod name
POD=$(kubectl get pod -n podmortem -l app.kubernetes.io/name=podmortem -o jsonpath='{.items[0].metadata.name}')

# Query history
kubectl exec -n podmortem $POD -- podmortem history

# Filter by namespace/pod
kubectl exec -n podmortem $POD -- podmortem history -n production -p my-app

# Full details of a specific restart
kubectl exec -n podmortem $POD -- podmortem detail 1

# Purge old records
kubectl exec -n podmortem $POD -- podmortem purge --before "2026-05-01T00:00:00" -y
```

### Watch Mode (run in-cluster or locally)

```bash
# Watch all namespaces
podmortem watch -v

# Watch a specific namespace
podmortem watch -n production -v

# Custom DB path
podmortem watch --db-path ./local-restarts.db
```

### Query Restart History

```bash
# Show last 20 restarts
podmortem history

# All restarts with a higher limit
podmortem history -l 100

# Filter by namespace
podmortem history -n production

# Filter by pod name (substring match)
podmortem history -p my-app

# Show restarts since a specific time
podmortem history -s "2026-05-20T00:00:00"

# Include the crashed container's logs in output
podmortem history -p my-app --show-logs

# All restarts with logs
podmortem history -l 100 --show-logs
```

### Filter by Specific Deployment

```bash
# Filter by deployment/pod name (substring match)
podmortem history -n production -p payment-service

# With logs included
podmortem history -n production -p payment-service --show-logs

# See full details of a specific restart
podmortem detail 1

# More examples
podmortem history -p order-api
podmortem history -p nginx --since "2026-05-21T00:00:00"
podmortem history -n staging -p worker -l 50
```

> **Note:** The `-p` flag does substring matching, so `-p api` would match `payment-api`, `order-api`, `gateway-api`, etc.

### View Full Details of a Specific Restart

```bash
podmortem detail 1
```

This shows the complete record: reason, exit code, last container logs, and all events captured at the moment of the crash.

## CLI Reference

| Command | Description |
|---------|-------------|
| `podmortem watch` | Start watching for pod restarts |
| `podmortem history` | Query restart history with filters |
| `podmortem detail <id>` | Show full details for a restart record |
| `podmortem purge` | Delete restart history records |

### Watch Options

```
--namespace, -n   Namespace to watch (default: all namespaces)
--db-path         Path to SQLite database file
--verbose, -v     Enable debug logging
```

### History Options

```
--namespace, -n   Filter by namespace
--pod, -p         Filter by pod name (substring match)
--since, -s       Show restarts since ISO timestamp
--limit, -l       Max results (default: 20)
--show-logs       Include last container logs in output
```

### Detail Options

```
--db-path         Path to SQLite database file
```

### Purge Options

```
--id              Delete a specific record by ID
--namespace, -n   Delete all records in a namespace
--pod, -p         Delete records matching pod name (substring)
--before, -b      Delete records before ISO timestamp
--all             Delete ALL records
--yes, -y         Skip confirmation prompt
--db-path         Path to SQLite database file
```

#### Purge Examples

```bash
# Delete a single record
podmortem purge --id 5

# Delete all records in a namespace
podmortem purge -n staging -y

# Delete records for a specific pod
podmortem purge -p clares-engine

# Delete records older than a date
podmortem purge --before "2026-05-01T00:00:00" -y

# Wipe everything
podmortem purge --all -y
```

## How It Works

1. On startup, seeds current restart counts for all running pods
2. Watches the Kubernetes API for pod ADDED/MODIFIED events
3. When a container's `restartCount` increases:
   - Fetches previous container logs (`kubectl logs --previous`)
   - Fetches related pod events
   - Stores everything in SQLite with timestamp
4. Data persists indefinitely (unlike `kubectl get events` which expires after ~1 hour)

### Why This Is Useful

- `kubectl get events` data is lost after ~1 hour
- Pod logs from crashed containers are only available until the next restart
- Podmortem captures both **at the moment of crash** and stores them permanently

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `PODMORTEM_DB_PATH` | `/data/podmortem.db` | Path to SQLite database |
