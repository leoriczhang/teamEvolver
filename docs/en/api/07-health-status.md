# Health and Status API

## 1. API Implementation Overview

Health and status interfaces are used to monitor the running status of the teamEvolver service, check component connectivity, and manually trigger evolution cycles. These interfaces do not require authentication (designed for internal network deployment; access should be controlled at the network boundary).

Code implementation: `teamEvolver/proxy/routes.py` (`/health`, `/healthz`, `/status`, `/trigger`, `/storage/status`)

## 2. Interface and Parameter Specification

---

### GET /health

Health check endpoint, returns service alive status.

**Authentication:** None

**Response:**

| Field | Type | Description |
|-------|------|-------------|
| `status` | string | Fixed as `"ok"` |

---

### GET /healthz

Kubernetes-style liveness probe, returns simple ok status.

**Authentication:** None

**Response:**

| Field | Type | Description |
|-------|------|-------------|
| `ok` | boolean | Fixed as `true` |

---

### GET /status

Service status dashboard, returns runtime metrics such as queue depth and registered skill count.

**Authentication:** None (also used by the console)

**Query Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `refresh` | boolean | No | Whether to force refresh cache, default false |

**Response Fields:**

| Field | Type | Description |
|-------|------|-------------|
| `running` | boolean | Whether evolution loop is running (note: current implementation always returns false; service availability is determined by other fields) |
| `pending_sessions` | integer | Number of Sessions waiting in queue for processing |
| `registered_skills` | integer | Number of registered team Skills |
| `skills` | object | Skill details map, keyed by skill name |
| `skills.<name>.skill_id` | string | Skill ID |
| `skills.<name>.version` | integer | Current version number |

**Caching:** Response cached for 5 seconds; `refresh=true` forces a refresh.

---

### POST /trigger

Manually trigger an evolution cycle. The background still automatically scans the queue at configured intervals (default 600 seconds).

**Authentication:** None (should be restricted by firewall)

**Method:** POST

**Response:** Returned by the embedded evolution application, typically 202 Accepted indicating triggered.

**Note:** The `/trigger` path is handled by the embedded evolution middleware, responded to by the evolution engine in `teamEvolver/evolve/runtime/orchestrator.py`. If the evolution engine is not ready or not configured, it may return an error.

Related code: `teamEvolver/proxy/routes.py:824` (`_is_embedded_evolve_path`), `teamEvolver/proxy/server.py:332` (`_dispatch_embedded_evolve_request`)

---

### POST /trigger-dreamcycle

Trigger DreamCycle memory maintenance tasks.

**Authentication:** None (internal interface)

**Response:**

| Field | Type | Description |
|-------|------|-------------|
| `status` | string | `started` or `not_configured` |

Returns 503 status code when DreamCycle is not configured.

---

### GET /storage/status

Check shared storage (OpenViking) configuration and connectivity.

**Authentication:** None

**Response Fields:**

| Field | Type | Description |
|-------|------|-------------|
| `backend` | string | Storage backend type (`viking` or `none`) |
| `deployment` | string | Deployment mode (`cloud` or `local`) |
| `endpoint` | string | OpenViking endpoint address |
| `namespace` | string | Namespace |
| `api_key_present` | boolean | Whether API Key is configured |
| `reachable` | boolean | Whether storage is reachable |
| `reason` | string | Unreachability reason (when reachable=false) |

Code: `teamEvolver/proxy/routes.py:1520` (`_storage_status`)

---

### GET /langfuse/status

Check Langfuse observability integration status.

**Authentication:** None

**Response Fields:**

| Field | Type | Description |
|-------|------|-------------|
| `enabled` | boolean | Whether Langfuse is enabled |
| `tracing` | object | Tracing status |
| `host` | string | Langfuse host address |
| `public_key_present` | boolean | Whether Public Key is configured |
| `secret_key_present` | boolean | Whether Secret Key is configured |
| `reachable` | boolean | Whether reachable |
| `total_sessions` | integer | Total Session count |
| `reason` | string | Unreachability reason |

## 3. Usage Examples

### Health Check

```bash
curl -fsS "http://localhost:52010/health"
```

Response:

```json
{"status": "ok"}
```

### View Service Status

```bash
curl -fsS "http://localhost:52010/status"
```

Example response:

```json
{
  "running": false,
  "pending_sessions": 3,
  "registered_skills": 5,
  "skills": {
    "database-debugging": {"skill_id": "database-debugging", "version": 3},
    "code-review": {"skill_id": "code-review", "version": 1}
  }
}
```

### Manually Trigger Evolution

```bash
curl -fsS -X POST "http://localhost:52010/trigger"
```

### Check Storage Status

```bash
curl -fsS "http://localhost:52010/storage/status"
```

Example response (healthy):

```json
{
  "backend": "viking",
  "deployment": "local",
  "endpoint": "http://localhost:52011",
  "namespace": "resources",
  "api_key_present": false,
  "reachable": true
}
```

### Check Liveness Probe

```bash
curl -fsS "http://localhost:52010/healthz"
```

Response:

```json
{"ok": true}
```

## 4. Response Contract and Error Handling

### Error Codes

| HTTP Status | Scenario |
|------------|---------|
| 503 | DreamCycle not configured (`/trigger-dreamcycle`) |
| 503 | Upstream model base URL not configured (model proxy endpoints, does not affect health checks) |
| 503 | Session storage not configured (at `/ingest_session`, does not affect health checks) |

### Deployment Recommendations

1. **Load Balancer Health Checks:** Use `/healthz` as the L7 health check path, `/health` as the readiness probe.
2. **Network Isolation:** `/trigger` and `/trigger-dreamcycle` have no authentication; production environments should restrict access to internal networks via firewalls.
3. **Monitoring Metrics:** Periodically scrape the `pending_sessions` value from `/status`; continuous growth indicates insufficient evolution processing capacity or queue blockage.
4. **Storage Alerts:** Trigger an alert when `/storage/status` returns `reachable: false`, indicating shared storage is unavailable and Skill sync and Memory functionality will be degraded.
