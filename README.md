# plane-webhook-listener

FastAPI service that receives [Plane](https://plane.so) webhook POSTs and forwards formatted notifications to a Matrix room.

## What it does

- Listens on `POST /webhook/plane` for Plane issue events
- Verifies HMAC-SHA256 signatures (`X-Plane-Signature` header)
- Resolves project identifiers and state names via the Plane API (cached at startup)
- Posts formatted notifications to a Matrix room for `issue.created` and `issue.updated` (state change only)
- Deduplicates events within a 5-second window

**Notification formats:**

```
# Created
[PLANE] TQMCP-1: Fix dispatcher routing loop
Priority: high | State: Backlog | Created by: ted
https://plane.helmforge.me/forge/browse/TQMCP-1

# State change
[PLANE] TQMCP-1: Fix dispatcher routing loop
State: In Progress → Done ✓
https://plane.helmforge.me/forge/browse/TQMCP-1
```

Non-issue events (cycles, modules, etc.) are silently ignored with a 200 response.

## Configuration

All config via environment variables. Loaded by `start.sh` from:

| Secrets file | Variables |
|---|---|
| `~/.secrets/matrix-forge.env` | `MATRIX_HOMESERVER`, `MATRIX_ACCESS_TOKEN`, `MATRIX_ROOM_ID` |
| `~/.secrets/plane.env` | `PLANE_API_TOKEN` |
| `~/.secrets/plane-webhook.env` | `PLANE_WEBHOOK_SECRET`, `PLANE_API_BASE`, `PLANE_WORKSPACE`, `PLANE_BASE_URL`, `PORT` |

| Variable | Default | Description |
|---|---|---|
| `PLANE_WEBHOOK_SECRET` | _(none)_ | HMAC secret from Plane webhook config. If unset, signature verification is skipped. |
| `MATRIX_HOMESERVER` | `http://127.0.0.1:8008` | Matrix homeserver URL |
| `MATRIX_ACCESS_TOKEN` | _(required)_ | Matrix bot access token |
| `MATRIX_ROOM_ID` | _(required)_ | Room to post notifications to |
| `PLANE_API_BASE` | `http://127.0.0.1:3007/api/v1` | Plane API base URL |
| `PLANE_API_TOKEN` | _(required)_ | Plane API token for project/state cache |
| `PLANE_WORKSPACE` | `forge` | Plane workspace slug |
| `PLANE_BASE_URL` | `https://plane.helmforge.me` | Base URL for issue links |
| `PORT` | `3006` | Listen port |

## Running

Managed by PM2 via `ecosystem.config.cjs`:

```bash
pm2 start ecosystem.config.cjs
pm2 logs plane-webhook-listener
```

Or directly:

```bash
./start.sh
```

## Plane webhook setup

In Plane → Settings → Webhooks:

- **URL:** `https://plane.helmforge.me/webhook/plane` (or wherever SWAG proxies port 3006)
- **Events:** Issues only
- **Secret:** Generate and save to `~/.secrets/plane-webhook.env` as `PLANE_WEBHOOK_SECRET`

## Endpoints

| Method | Path | Description |
|---|---|---|
| `POST` | `/webhook/plane` | Plane webhook receiver |
| `GET` | `/health` | Health check — returns `{"status": "ok", "projects_cached": N}` |

## Tests

```bash
python3 -m pytest tests/ -v
```

16 tests covering signature verification, event filtering, created/updated notification
formatting, state resolution (inline dict and UUID cache), deduplication, and the health
endpoint.

## Dependencies

```
fastapi
uvicorn
httpx
structlog
```
