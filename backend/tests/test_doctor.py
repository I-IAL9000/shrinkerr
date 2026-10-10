"""Doctor (v0.10.0): what Shrinkerr depends on, checked in one place, and a
diagnostics bundle for bug reports — without the secrets."""
import asyncio
import io
import json
import os
import zipfile
from datetime import datetime, timedelta, timezone

import aiosqlite
import pytest
import pytest_asyncio

import backend.doctor as doctor


@pytest_asyncio.fixture
async def db(test_db, monkeypatch):
    monkeypatch.setattr(doctor, "DB_PATH", test_db)
    conn = await aiosqlite.connect(test_db)
    yield conn
    await conn.close()


async def _set(conn, **settings):
    for k, v in settings.items():
        await conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (k, v))
    await conn.commit()


async def _media(conn, *paths):
    for p in paths:
        await conn.execute("INSERT INTO media_dirs (path, enabled) VALUES (?, 1)", (str(p),))
    await conn.commit()


@pytest.mark.asyncio
async def test_media_folders(db, tmp_path, monkeypatch):
    assert await doctor.check_media_folders(db, {}) == [doctor._check("media_folder", "warn", None, problem="none")]
    good, gone = tmp_path / "Movies", tmp_path / "Gone"
    good.mkdir()
    await _media(db, good, gone)
    by_target = {c["target"]: c for c in await doctor.check_media_folders(db, {"disk_space_threshold_gb": "0"})}
    assert by_target[str(good)]["status"] == "ok" and by_target[str(good)]["data"]["free"] > 0
    assert (by_target[str(gone)]["status"], by_target[str(gone)]["data"]["problem"]) == ("error", "missing")
    # Less free than the disk-space warning setting.
    low = {c["target"]: c for c in await doctor.check_media_folders(db, {"disk_space_threshold_gb": "1000000"})}
    assert (low[str(good)]["status"], low[str(good)]["data"]["problem"]) == ("warn", "low_space")


@pytest.mark.asyncio
@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root can write anywhere")
async def test_a_read_only_folder(db, tmp_path):
    ro = tmp_path / "RO"
    ro.mkdir()
    ro.chmod(0o555)
    try:
        await _media(db, ro)
        [check] = await doctor.check_media_folders(db, {"disk_space_threshold_gb": "0"})
        assert (check["status"], check["data"]["problem"]) == ("error", "readonly")
    finally:
        ro.chmod(0o755)


@pytest.mark.asyncio
async def test_a_stalled_share_doesnt_hang_the_page(db, tmp_path, monkeypatch):
    import time
    monkeypatch.setattr(doctor, "_FOLDER_TIMEOUT", 0.2)
    monkeypatch.setattr(doctor, "_folder_state", lambda p: time.sleep(1) or ("ok", {}))
    await _media(db, tmp_path)
    [check] = await doctor.check_media_folders(db, {})
    assert (check["status"], check["data"]["problem"]) == ("error", "timeout")


@pytest.mark.asyncio
async def test_data_folder_and_database(db, monkeypatch):
    [data] = await doctor.check_data_folder()
    assert data["status"] == "ok" and not [f for f in os.listdir(os.path.dirname(doctor.DB_PATH)) if f.startswith(".doctor")]
    [database] = await doctor.check_database()
    assert database["status"] == "ok" and database["data"]["size"] > 0
    monkeypatch.setattr(doctor, "_quick_check", lambda p: "*** in database main ***\nPage 3: btree")
    [bad] = await doctor.check_database()
    assert (bad["status"], bad["data"]["detail"]) == ("error", "*** in database main ***")


@pytest.mark.asyncio
async def test_ffmpeg_and_the_default_encoder(monkeypatch):
    import backend.encoder_caps as caps_mod
    import backend.test_encode as test_encode
    from backend.encoder_caps import EncoderCaps
    monkeypatch.setattr(doctor.shutil, "which", lambda tool: None)

    async def vmaf():
        return False
    monkeypatch.setattr(test_encode, "check_vmaf_available", vmaf)
    monkeypatch.setattr(caps_mod, "detect_encoders", lambda force=False: EncoderCaps(nvenc=False, qsv=False, vaapi=True))
    checks = {c["id"]: c for c in await doctor.check_ffmpeg({"default_encoder": "nvenc"})}
    assert checks["ffmpeg"]["status"] == checks["ffprobe"]["status"] == "error"
    assert checks["vmaf"]["status"] == "info"
    assert (checks["encoder"]["status"], checks["encoder"]["data"]["available"]) == ("warn", ["libx265", "vaapi"])
    ok = {c["id"]: c for c in await doctor.check_ffmpeg({"default_encoder": "cpu"})}
    assert ok["encoder"]["status"] == "ok"


@pytest.mark.asyncio
async def test_connections(monkeypatch):
    import backend.routes.settings as settings_route

    async def fake(req):
        return {"success": True, "version": "4.0"} if req.service == "plex" else {"success": False, "error": "HTTP 401"}
    monkeypatch.setattr(settings_route, "test_api_key", fake)
    monkeypatch.delenv("SHRINKERR_TMDB_API_KEY", raising=False)
    checks = await doctor.check_connections({"plex_url": "http://plex", "sonarr_url": "http://sonarr", "radarr_url": ""})
    assert [(c["target"], c["status"]) for c in checks] == [("plex", "ok"), ("sonarr", "error"), ("tmdb", "warn")]
    assert checks[1]["data"]["detail"] == "HTTP 401" and checks[2]["data"]["problem"] == "no_key"


@pytest.mark.asyncio
async def test_workers_and_password(db):
    now = datetime.now(timezone.utc)
    await db.executemany(
        "INSERT INTO worker_nodes (id, name, registered_at, last_heartbeat) VALUES (?, ?, ?, ?)",
        [("a", "gpu-box", now.isoformat(), now.isoformat()),
         ("b", "old-pc", now.isoformat(), (now - timedelta(days=3)).isoformat())])
    await db.commit()
    nodes = await doctor.check_nodes(db)
    assert [(c["target"], c["status"]) for c in nodes] == [("gpu-box", "ok"), ("old-pc", "warn")]
    assert doctor.check_auth({"auth_enabled": "true", "auth_password_hash": "x"})[0]["status"] == "ok"
    assert doctor.check_auth({"auth_enabled": "false"})[0]["status"] == "info"


@pytest.mark.asyncio
async def test_the_diagnostics_bundle_has_no_secrets(db, monkeypatch):
    from backend.logstream import LogEntry, log_buffer

    async def checks():
        return {"checked_at": "now", "version": "0.10.0", "checks": [{"id": "x", "detail": "token=leak123456"}]}
    monkeypatch.setattr(doctor, "run_checks", checks)
    monkeypatch.setenv("SHRINKERR_TMDB_API_KEY", "bundledtmdbkey42")
    await _set(db, api_key="supersecretkey123", plex_token="plextoken999", smtp_user="me@example.com",
               default_encoder="nvenc")
    await db.execute(
        "INSERT INTO jobs (file_path, job_type, status, created_at, error_log, ffmpeg_command) "
        "VALUES ('/m/a.mkv', 'convert', 'failed', '2026-10-10', 'GET /x?X-Plex-Token=plextoken999 failed', "
        "'ffmpeg -i /m/a.mkv')")
    await db.commit()
    log_buffer.append(LogEntry("2026-10-10T00:00:00", "info", "PLEX", "url?X-Plex-Token=othertoken777&x=1"))
    log_buffer.append(LogEntry("2026-10-10T00:00:01", "info", "API", "key supersecretkey123 used"))
    log_buffer.append(LogEntry("2026-10-10T00:00:02", "info", "TMDB", "lookup with bundledtmdbkey42 failed"))

    name, data = await doctor.diagnostics_bundle()
    assert name.startswith("shrinkerr-diagnostics-0.10.0-") and name.endswith(".zip")
    z = zipfile.ZipFile(io.BytesIO(data))
    assert set(z.namelist()) == {"README.txt", "doctor.json", "settings.json", "system.json", "library.json",
                                 "failed_jobs.json", "log.txt"}
    everything = "".join(z.read(n).decode() for n in z.namelist())
    for secret in ("supersecretkey123", "plextoken999", "othertoken777", "bundledtmdbkey42", "leak123456",
                   "me@example.com"):
        assert secret not in everything, secret
    settings = json.loads(z.read("settings.json"))
    assert settings["api_key"] == "***" and settings["default_encoder"] == "nvenc"
    assert json.loads(z.read("system.json"))["environment"]["SHRINKERR_TMDB_API_KEY"] == "***"
    failed = json.loads(z.read("failed_jobs.json"))
    assert failed[0]["file_path"] == "/m/a.mkv" and "X-Plex-Token=***" in failed[0]["error_log"]
    assert json.loads(z.read("library.json"))["jobs"] == {"failed": 1}
    assert "X-Plex-Token=***" in z.read("log.txt").decode()


@pytest.mark.asyncio
async def test_run_checks_puts_them_together(db, monkeypatch):
    async def none(*a):
        return []
    for name in ("check_media_folders", "check_connections", "check_nodes", "check_ffmpeg"):
        monkeypatch.setattr(doctor, name, none)
    report = await doctor.run_checks()
    assert [c["id"] for c in report["checks"]] == ["data_folder", "database", "auth"]
    assert report["version"]
