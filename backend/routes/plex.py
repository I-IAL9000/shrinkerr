"""Plex auth endpoints — PIN-based OAuth ("Plex Connect")."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.database import connect_db
from backend.plex_auth import (
    create_pin,
    check_pin,
    get_resources,
    get_user,
    probe_connection,
)


router = APIRouter(prefix="/api/plex")


class CheckPinRequest(BaseModel):
    pin_id: int


class ResourcesRequest(BaseModel):
    token: str


class SaveConnectionRequest(BaseModel):
    token: str
    server_url: str
    server_name: str = ""
    server_client_id: str = ""


@router.post("/auth/start")
async def auth_start():
    """Create a new auth PIN and return the popup URL.

    The frontend opens `auth_url` in a window, then polls `/auth/check` with
    `pin_id` every ~2 seconds until a token appears.
    """
    try:
        return await create_pin()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"plex.tv unreachable: {exc}")


@router.post("/auth/check")
async def auth_check(req: CheckPinRequest):
    """Poll a pending PIN once. Returns {token, expired, user?}.

    When a token is found, we also include the Plex user record so the UI
    can immediately show "Connected as …" without a second round-trip.
    """
    result = await check_pin(req.pin_id)
    if result["token"]:
        user = await get_user(result["token"])
        result["user"] = user
    return result


@router.post("/auth/resources")
async def auth_resources(req: ResourcesRequest):
    """List the user's Plex servers.

    Returned list is ranked so servers the backend can actually reach appear
    first. This matters because Plex users typically have multiple
    "connections" per server (local LAN, remote WAN, relay) — picking the
    wrong one means slow/broken refreshes.
    """
    try:
        servers = await get_resources(req.token)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"plex.tv unreachable: {exc}")

    # Probe each connection; tag with `reachable` so UI can preselect best.
    # Keep it bounded — first ~6 connections across all servers is plenty.
    probed = 0
    for server in servers:
        for conn in server.get("connections", []):
            if probed >= 6:
                conn["reachable"] = None  # untested
                continue
            conn["reachable"] = await probe_connection(conn["uri"], req.token, timeout=3.0)
            probed += 1
        # Best connection for this server: local+reachable > reachable > local > first
        conns = server["connections"]
        if conns:
            best = next((c for c in conns if c.get("reachable") and c.get("local")), None) \
                or next((c for c in conns if c.get("reachable")), None) \
                or next((c for c in conns if c.get("local")), None) \
                or conns[0]
            server["recommended_uri"] = best.get("uri", "")

    return {"servers": servers}


@router.post("/auth/save")
async def auth_save(req: SaveConnectionRequest):
    """Persist the resolved token + server URL into the settings table.

    This is the last step of the Connect flow — after the user has picked
    which server to use, the frontend calls here instead of re-using the
    generic /api/settings endpoint (which would also wipe other Plex
    settings if the payload was incomplete).
    """
    if not req.token or not req.server_url:
        raise HTTPException(status_code=400, detail="token and server_url required")

    db = await connect_db()
    try:
        await db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            ("plex_token", req.token),
        )
        await db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            ("plex_url", req.server_url.rstrip("/")),
        )
        if req.server_name:
            await db.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                ("plex_server_name", req.server_name),
            )
        if req.server_client_id:
            await db.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                ("plex_server_client_id", req.server_client_id),
            )
        await db.commit()
    finally:
        await db.close()

    return {"success": True}


@router.post("/auth/disconnect")
async def auth_disconnect():
    """Clear stored Plex credentials. Leaves plex_url alone so the user can
    reconnect easily; wipes the token only.
    """
    db = await connect_db()
    try:
        await db.execute("DELETE FROM settings WHERE key = 'plex_token'")
        await db.execute("DELETE FROM settings WHERE key = 'plex_server_name'")
        await db.execute("DELETE FROM settings WHERE key = 'plex_server_client_id'")
        await db.commit()
    finally:
        await db.close()

    return {"success": True}


@router.get("/auth/status")
async def auth_status():
    """Return the current connection state: whether we have a token, who the
    user is, and which server is active. Used by the Settings page on load.
    """
    db = await connect_db()
    try:
        async with db.execute(
            "SELECT key, value FROM settings WHERE key IN "
            "('plex_token', 'plex_url', 'plex_server_name', 'plex_server_client_id')"
        ) as cur:
            rows = {r["key"]: r["value"] for r in await cur.fetchall()}
    finally:
        await db.close()

    token = rows.get("plex_token", "")
    connected = bool(token and rows.get("plex_url"))

    user = None
    if token:
        user = await get_user(token)

    return {
        "connected": connected,
        "server_url": rows.get("plex_url", ""),
        "server_name": rows.get("plex_server_name", ""),
        "user": user,
    }
