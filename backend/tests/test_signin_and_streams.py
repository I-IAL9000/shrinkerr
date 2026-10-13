"""A signed-in session renews while it's used (it ended 30 days after
signing in, mid-use: every request got 401 and the page showed an empty
library); the worker asks the media servers for active streams at most
every STREAM_CHECK_EVERY seconds (it asked Plex twice a second while its
slots were full). v0.10.0."""
import hashlib
import hmac
import time

import pytest


def _token(secret: str, username: str, at: float) -> str:
    ts = str(int(at))
    return f"{username}:{ts}:" + hmac.new(secret.encode(), f"{username}:{ts}".encode(), hashlib.sha256).hexdigest()


@pytest.mark.asyncio
async def test_a_session_in_use_is_renewed(test_db, monkeypatch):
    import backend.main as main
    from fastapi.responses import JSONResponse
    from starlette.requests import Request
    settings = {"auth_enabled": True, "api_key": "", "auth_username": "hal", "auth_password_hash": "x",
                "session_secret": "s3cret", "auth_method": "forms", "auth_required": "enabled"}
    monkeypatch.setattr(main, "_get_auth_settings_sync", lambda: settings)

    async def call(cookie):
        scope = {"type": "http", "method": "GET", "path": "/api/scan/status", "headers": [
            (b"cookie", f"shrinkerr_session={cookie}".encode())], "client": ("203.0.113.9", 1234),
                 "scheme": "http", "server": ("shrinkerr", 6680), "query_string": b""}

        async def ok(_request):
            return JSONResponse({"ok": True})
        return await main.api_key_auth(Request(scope), ok)

    fresh = await call(_token("s3cret", "hal", time.time() - 3600))
    assert fresh.status_code == 200 and "set-cookie" not in fresh.headers  # an hour old: as it is
    old = await call(_token("s3cret", "hal", time.time() - 20 * 86400))
    assert old.status_code == 200
    renewed = old.headers["set-cookie"].split(";")[0].split("=", 1)[1]
    assert main._validate_session(renewed, settings) and int(renewed.split(":")[1]) > time.time() - 60
    expired = await call(_token("s3cret", "hal", time.time() - 31 * 86400))
    assert expired.status_code == 401


@pytest.mark.asyncio
async def test_streams_are_asked_for_at_most_every_few_seconds(test_db, monkeypatch):
    import backend.queue as queue_mod
    from backend.queue import QueueWorker
    w = QueueWorker(test_db)
    asked, now = [], [1000.0]
    monkeypatch.setattr(queue_mod.time, "monotonic", lambda: now[0])

    async def plex():
        asked.append("plex")
        return True

    async def nope():
        return False
    monkeypatch.setattr(w, "_should_pause_for_plex", plex)
    monkeypatch.setattr(w, "_should_pause_for_jellyfin", nope)
    monkeypatch.setattr(w, "_should_pause_for_emby", nope)
    assert await w._stream_pause_server() == "Plex"
    now[0] += 0.5
    assert await w._stream_pause_server() == "Plex" and asked == ["plex"]  # the loop's next pass: no new request
    now[0] += queue_mod.STREAM_CHECK_EVERY
    assert await w._stream_pause_server() == "Plex" and asked == ["plex", "plex"]
