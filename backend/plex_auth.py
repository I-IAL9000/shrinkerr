"""Plex PIN-based OAuth flow (a.k.a. "Plex Connect").

Used instead of asking the user to manually paste an X-Plex-Token. Flow:

    1. POST https://plex.tv/api/v2/pins       → {id, code, authToken=null}
    2. Open https://app.plex.tv/auth#!?clientID=…&code=…  in a popup.
       User signs in, approves the request.
    3. Poll GET https://plex.tv/api/v2/pins/{id}  until authToken is set.
    4. Token is saved in the same settings.plex_token column we already use.

We also expose helpers to list the user's servers (so the Settings UI can
auto-populate plex_url) and fetch user info (email / display name for a
"Connected as …" badge).

The client identifier must be stable across requests (Plex expects it).
It's generated once and persisted in the settings table.
"""
from __future__ import annotations

import asyncio
import uuid
import xml.etree.ElementTree as ET
from typing import Optional

import httpx

from backend.database import connect_db


PLEX_TV_BASE = "https://plex.tv"
PRODUCT_NAME = "Shrinkerr"
DEVICE_NAME = "Shrinkerr"
PLATFORM = "Web"

# Pins expire after 15 minutes per Plex docs; we stop polling after 10.
POLL_TIMEOUT_SECONDS = 600
POLL_INTERVAL_SECONDS = 2


async def _get_or_create_client_id() -> str:
    """Return the stable X-Plex-Client-Identifier for this Shrinkerr install.

    Plex uses this to attribute devices in "Authorized Devices." We generate
    a random UUID once and persist it — regenerating it on every call would
    create a new "device" in the user's Plex account on every auth attempt.
    """
    db = await connect_db()
    try:
        async with db.execute(
            "SELECT value FROM settings WHERE key = 'plex_client_id'"
        ) as cur:
            row = await cur.fetchone()
            if row and row["value"]:
                return row["value"]

        cid = str(uuid.uuid4())
        await db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES ('plex_client_id', ?)",
            (cid,),
        )
        await db.commit()
        return cid
    finally:
        await db.close()


def _plex_headers(client_id: str, token: Optional[str] = None) -> dict[str, str]:
    headers = {
        "X-Plex-Client-Identifier": client_id,
        "X-Plex-Product": PRODUCT_NAME,
        "X-Plex-Device": DEVICE_NAME,
        "X-Plex-Device-Name": DEVICE_NAME,
        "X-Plex-Platform": PLATFORM,
        "X-Plex-Version": "1.0",
        "Accept": "application/json",
    }
    if token:
        headers["X-Plex-Token"] = token
    return headers


async def create_pin() -> dict:
    """Create a new auth PIN.

    Returns: {
        "pin_id": int,
        "code": str,           # 4-char code shown on plex.tv/link (we don't show it)
        "client_id": str,
        "auth_url": str,       # URL to open in a popup for user sign-in
    }
    """
    client_id = await _get_or_create_client_id()
    headers = _plex_headers(client_id)

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{PLEX_TV_BASE}/api/v2/pins",
            headers=headers,
            data={"strong": "true"},
        )
        resp.raise_for_status()
        data = resp.json()

    pin_id = data["id"]
    code = data["code"]

    # Auth URL opens Plex's sign-in page with our PIN pre-filled.
    # forwardUrl is intentionally omitted; once the user signs in, the popup
    # just shows "Your device is now authorized" and the backend poll picks
    # up the token. No redirect back to our app is needed.
    auth_url = (
        f"https://app.plex.tv/auth#!?clientID={client_id}"
        f"&code={code}"
        f"&context%5Bdevice%5D%5Bproduct%5D={PRODUCT_NAME}"
        f"&context%5Bdevice%5D%5BdeviceName%5D={DEVICE_NAME}"
        f"&context%5Bdevice%5D%5Bplatform%5D={PLATFORM}"
    )

    return {
        "pin_id": pin_id,
        "code": code,
        "client_id": client_id,
        "auth_url": auth_url,
    }


async def check_pin(pin_id: int) -> dict:
    """Poll a PIN once. Returns {"token": str|None, "expired": bool}.

    Callers should poll every ~2 seconds until token is set or expired=True.

    Plex's PIN lifecycle:
      * POST /pins                  → 200, returns {id, code, expiresAt, authToken: null}
      * GET /pins/{id} (pre-auth)   → 200, authToken still null, expiresAt present
      * GET /pins/{id} (post-auth)  → 200, authToken populated
      * GET /pins/{id} (after TTL)  → 404 (we treat this as expired)

    Important: `expiresAt` is the expiry *timestamp*, not a flag — it's
    populated on every successful response, so we must compare against
    the current time (or just rely on Plex's 404 as the authoritative
    signal). Earlier code that naively treated a non-null expiresAt as
    "expired" fired on the very first poll, before the user even had a
    chance to sign in.
    """
    import datetime as _dt

    client_id = await _get_or_create_client_id()
    headers = _plex_headers(client_id)

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(
            f"{PLEX_TV_BASE}/api/v2/pins/{pin_id}",
            headers=headers,
        )
        if resp.status_code == 404:
            return {"token": None, "expired": True}
        resp.raise_for_status()
        data = resp.json()

    token = data.get("authToken") or None

    # Defensive: if the PIN's expiresAt has actually passed, treat as expired
    # even if Plex hasn't 404'd yet. Otherwise keep polling.
    expired = False
    expires_at = data.get("expiresAt")
    if not token and expires_at:
        try:
            # Plex returns ISO 8601 like "2024-04-17T15:23:45Z"
            exp_dt = _dt.datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
            now = _dt.datetime.now(_dt.timezone.utc)
            if now >= exp_dt:
                expired = True
        except Exception:
            # If we can't parse it, fall back to trusting the 404 branch.
            pass

    return {"token": token, "expired": expired}


async def get_user(token: str) -> dict | None:
    """Fetch the Plex user record for a given auth token.

    Returns a dict with keys {email, username, title, thumb}, or None on failure.
    """
    client_id = await _get_or_create_client_id()
    headers = _plex_headers(client_id, token=token)

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{PLEX_TV_BASE}/api/v2/user",
                headers=headers,
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception:
        return None

    return {
        "email": data.get("email", ""),
        "username": data.get("username", ""),
        "title": data.get("title", ""),
        "thumb": data.get("thumb", ""),
    }


async def get_resources(token: str) -> list[dict]:
    """List Plex Media Servers owned by / shared with the authenticated user.

    Returns a list of dicts, one per server:
        {
            "name": str,
            "client_identifier": str,
            "owned": bool,
            "product_version": str,
            "connections": [
                {"uri": str, "local": bool, "relay": bool, "protocol": str}, ...
            ],
        }

    The Settings UI presents these so the user can pick the right URL
    (typically the "local" one for Docker-on-same-host setups).
    """
    client_id = await _get_or_create_client_id()
    headers = _plex_headers(client_id, token=token)

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{PLEX_TV_BASE}/api/v2/resources",
            headers=headers,
            params={"includeHttps": "1", "includeRelay": "1"},
        )
        resp.raise_for_status()
        resources = resp.json()

    servers: list[dict] = []
    for r in resources:
        # Only interested in media servers; skip Plex clients / mobile devices.
        provides = r.get("provides", "")
        if "server" not in provides:
            continue

        connections = []
        for c in r.get("connections", []) or []:
            connections.append({
                "uri": c.get("uri", ""),
                "address": c.get("address", ""),
                "port": c.get("port", 0),
                "local": bool(c.get("local")),
                "relay": bool(c.get("relay")),
                "protocol": c.get("protocol", ""),
            })

        servers.append({
            "name": r.get("name", ""),
            "client_identifier": r.get("clientIdentifier", ""),
            "owned": bool(r.get("owned")),
            "product_version": r.get("productVersion", ""),
            "platform": r.get("platform", ""),
            "connections": connections,
        })

    # Sort: owned servers first, then by name.
    servers.sort(key=lambda s: (not s["owned"], s["name"].lower()))
    return servers


async def probe_connection(uri: str, token: str, timeout: float = 5.0) -> bool:
    """Check whether a candidate server URI is actually reachable from the
    backend. Helpful for auto-picking the best "connection" from resources().
    """
    client_id = await _get_or_create_client_id()
    headers = _plex_headers(client_id, token=token)
    try:
        async with httpx.AsyncClient(timeout=timeout, verify=False) as client:
            resp = await client.get(f"{uri.rstrip('/')}/identity", headers=headers)
            return resp.status_code == 200
    except Exception:
        return False
