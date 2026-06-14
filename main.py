#!/usr/bin/env python3
"""Plane webhook listener — receives Plane webhook POSTs and forwards
formatted notifications to the #plane Matrix room."""

import hashlib
import hmac
import os
import time
from collections import defaultdict

import httpx
import structlog
import uvicorn
from fastapi import FastAPI, Header, HTTPException, Request, Response

log = structlog.get_logger()

# --- Config ---
PLANE_WEBHOOK_SECRET = os.environ.get("PLANE_WEBHOOK_SECRET", "")
MATRIX_HOMESERVER = os.environ.get("MATRIX_HOMESERVER", "http://127.0.0.1:8008")
MATRIX_ACCESS_TOKEN = os.environ.get("MATRIX_ACCESS_TOKEN", "")
MATRIX_ROOM_ID = os.environ.get("MATRIX_ROOM_ID", "")
PLANE_API_BASE = os.environ.get("PLANE_API_BASE", "http://127.0.0.1:3007/api/v1")
PLANE_API_TOKEN = os.environ.get("PLANE_API_TOKEN", "")
PLANE_WORKSPACE = os.environ.get("PLANE_WORKSPACE", "forge")
PLANE_BASE_URL = os.environ.get("PLANE_BASE_URL", "https://plane.helmforge.me")
PORT = int(os.environ.get("PORT", "3006"))

app = FastAPI(title="plane-webhook-listener", version="0.1.0")

# --- State tracking ---
# work_item_id -> last known state name
state_cache: dict[str, str] = {}

# work_item_id -> timestamp of last notification (dedup window)
last_notified: dict[str, float] = defaultdict(float)
DEDUP_WINDOW = 5.0  # seconds

# project UUID -> identifier string
project_cache: dict[str, str] = {}

# state UUID -> state name (across all projects)
state_name_cache: dict[str, str] = {}


def load_project_cache():
    """Load project UUID -> identifier mapping from Plane API."""
    if not PLANE_API_TOKEN:
        log.warning("no_plane_api_token", msg="Cannot load project cache")
        return
    try:
        resp = httpx.get(
            f"{PLANE_API_BASE}/workspaces/{PLANE_WORKSPACE}/projects/",
            headers={"X-API-Key": PLANE_API_TOKEN},
            params={"per_page": 100},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        projects = data.get("results", data) if isinstance(data, dict) else data
        for p in projects:
            project_cache[p["id"]] = p["identifier"]
            # Load states for this project
            try:
                sr = httpx.get(
                    f"{PLANE_API_BASE}/workspaces/{PLANE_WORKSPACE}/projects/{p['id']}/states/",
                    headers={"X-API-Key": PLANE_API_TOKEN},
                    timeout=15,
                )
                sr.raise_for_status()
                sdata = sr.json()
                states = sdata.get("results", sdata) if isinstance(sdata, dict) else sdata
                for s in states:
                    state_name_cache[s["id"]] = s["name"]
                time.sleep(0.2)  # pace to avoid rate limit
            except Exception:
                pass  # states will show as "?" if cache misses
        log.info("project_cache_loaded", projects=len(project_cache), states=len(state_name_cache))
    except Exception as e:
        log.error("project_cache_failed", error=str(e))


def verify_signature(body: bytes, signature: str) -> bool:
    """Verify Plane webhook HMAC-SHA256 signature."""
    if not PLANE_WEBHOOK_SECRET:
        return True  # no secret configured, skip verification
    expected = hmac.new(
        PLANE_WEBHOOK_SECRET.encode(), body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def send_matrix_message(message: str):
    """Post a message to the #plane Matrix room."""
    if not MATRIX_ACCESS_TOKEN or not MATRIX_ROOM_ID:
        log.warning("matrix_not_configured", msg=message)
        return
    txn_id = f"plane-wh-{int(time.time() * 1000)}"
    try:
        resp = httpx.put(
            f"{MATRIX_HOMESERVER}/_matrix/client/v3/rooms/{MATRIX_ROOM_ID}/send/m.room.message/{txn_id}",
            headers={"Authorization": f"Bearer {MATRIX_ACCESS_TOKEN}"},
            json={"msgtype": "m.text", "body": message},
            timeout=10,
        )
        resp.raise_for_status()
        log.info("matrix_sent", room=MATRIX_ROOM_ID)
    except Exception as e:
        log.error("matrix_send_failed", error=str(e))


def get_identifier(project_id: str) -> str:
    """Resolve project UUID to identifier string."""
    return project_cache.get(project_id, "UNKNOWN")


def format_state_change(identifier: str, seq: int, name: str, old_state: str, new_state: str) -> str:
    """Format a state change notification."""
    url = f"{PLANE_BASE_URL}/forge/browse/{identifier}-{seq}"
    done_marker = "" if new_state != "Done" else " \u2713"
    return f"[PLANE] {identifier}-{seq}: {name}\nState: {old_state} \u2192 {new_state}{done_marker}\n{url}"


def format_created(identifier: str, seq: int, name: str, priority: str, state: str, creator: str) -> str:
    """Format a ticket creation notification."""
    url = f"{PLANE_BASE_URL}/forge/browse/{identifier}-{seq}"
    return f"[PLANE] {identifier}-{seq}: {name}\nPriority: {priority} | State: {state} | Created by: {creator}\n{url}"


@app.on_event("startup")
async def startup():
    load_project_cache()
    log.info("started", port=PORT, projects=len(project_cache))


@app.get("/health")
async def health():
    return {"status": "ok", "projects_cached": len(project_cache)}


@app.post("/webhook/plane")
async def webhook(request: Request, x_plane_signature: str = Header(default="")):
    body = await request.body()

    # Verify signature
    if PLANE_WEBHOOK_SECRET and not verify_signature(body, x_plane_signature):
        log.warning("invalid_signature")
        raise HTTPException(status_code=401, detail="Invalid signature")

    payload = await request.json()

    event = payload.get("event")
    action = payload.get("action")
    data = payload.get("data", {})

    log.info("webhook_received", plane_event=event, action=action, keys=list(payload.keys()),
             data_keys=list(data.keys()) if isinstance(data, dict) else "not_dict")

    # Only process issue events
    if event != "issue":
        return Response(status_code=200)

    work_item_id = data.get("id", "")
    project_id = data.get("project", data.get("project_id", ""))
    seq = data.get("sequence_id", 0)
    name = data.get("name", "Untitled")
    priority = data.get("priority", "none")
    identifier = get_identifier(project_id)

    # Dedup: skip if we notified about this item in the last N seconds
    now = time.time()
    if now - last_notified.get(work_item_id, 0) < DEDUP_WINDOW:
        log.debug("dedup_skip", work_item_id=work_item_id)
        return Response(status_code=200)

    # Extract state — can be a UUID string or a dict with "id"/"name"
    raw_state = data.get("state", "")
    if isinstance(raw_state, dict):
        state_id = raw_state.get("id", "")
        state_name_direct = raw_state.get("name", "")
    else:
        state_id = raw_state
        state_name_direct = ""

    def resolve_state(sid: str) -> str:
        if state_name_direct and sid == state_id:
            return state_name_direct
        return state_name_cache.get(sid, "?")

    # Plane uses "created"/"updated"/"deleted"
    if action == "created":
        state_name = resolve_state(state_id) if state_id else "Backlog"
        creator = data.get("created_by", "unknown")
        if isinstance(creator, dict):
            creator = creator.get("display_name", creator.get("email", "unknown"))

        msg = format_created(identifier, seq, name, priority, state_name, creator)
        send_matrix_message(msg)
        state_cache[work_item_id] = state_id
        last_notified[work_item_id] = now
        log.info("issue_created", identifier=f"{identifier}-{seq}", name=name)

    elif action == "updated":
        old_state_id = state_cache.get(work_item_id, "")

        if state_id and state_id != old_state_id:
            old_name = resolve_state(old_state_id) if old_state_id else "?"
            new_name = resolve_state(state_id)
            msg = format_state_change(identifier, seq, name, old_name, new_name)
            send_matrix_message(msg)
            state_cache[work_item_id] = state_id
            last_notified[work_item_id] = now
            log.info("state_changed", identifier=f"{identifier}-{seq}",
                     old=old_name, new=new_name)
        elif not old_state_id:
            state_cache[work_item_id] = state_id

    return Response(status_code=200)


if __name__ == "__main__":
    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ]
    )
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
