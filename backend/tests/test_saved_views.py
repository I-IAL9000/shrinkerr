"""Saved views (v0.10.0): named Scanner filters on the server, for the
Scanner, the rules' "Saved view" condition and the auto-queue's limit."""
import json

import aiosqlite
import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, patch

from backend.api_errors import ApiError

FILES = {
    "/m/Movies/Dune/Dune.2160p.mkv": dict(video_codec="hevc", video_height=2160, needs_conversion=0),
    "/m/Movies/Heat/Heat.1080p.mkv": dict(video_codec="h264", video_height=1080, needs_conversion=1),
    "/m/TV/Show/S01E01.720p.mkv": dict(video_codec="h264", video_height=720, needs_conversion=1),
}


@pytest_asyncio.fixture
async def env(test_db, monkeypatch):
    import sys
    import backend.config
    import backend.scanner as scanner
    from backend.queue import JobQueue, QueueWorker
    from backend.routes.jobs import init_job_routes
    for name, module in list(sys.modules.items()):
        if name.startswith("backend.") and isinstance(getattr(module, "DB_PATH", None), str):
            monkeypatch.setattr(module, "DB_PATH", test_db)
    monkeypatch.setattr(scanner, "settings", backend.config.settings)
    worker = QueueWorker(test_db)
    monkeypatch.setattr(worker, "start", lambda *a, **kw: None)
    init_job_routes(worker, JobQueue(test_db))
    async with aiosqlite.connect(test_db) as db:
        for path, cols in FILES.items():
            await db.execute(
                "INSERT INTO scan_results (file_path, file_size, duration, video_codec, video_height, "
                "needs_conversion, audio_tracks_json, scan_timestamp) VALUES (?, 4000000000, 3600, ?, ?, ?, '[]', '2026-10-10')",
                (path, cols["video_codec"], cols["video_height"], cols["needs_conversion"]))
        await db.commit()
    return test_db


async def _save(name, filter):
    from backend.routes.views import SaveViewRequest, save_view
    return await save_view(SaveViewRequest(name=name, filter=filter))


@pytest.mark.asyncio
async def test_saving_listing_and_replacing(env):
    from backend.routes.views import list_views
    hd = await _save("HD H.264", "x264,res_1080p")
    await _save("4K", "res_4k")
    assert [(v["name"], v["filter"]) for v in await list_views()] == [("4K", "res_4k"), ("HD H.264", "x264,res_1080p")]
    again = await _save("HD H.264", "x264,res_720p")  # same name: replaced, same id
    assert again["id"] == hd["id"] and again["filter"] == "x264,res_720p"
    for name, filter in (("", "x264"), ("Nothing", "all"), ("Nothing", "")):
        with pytest.raises(ApiError):
            await _save(name, filter)


@pytest.mark.asyncio
async def test_which_files_a_view_lists(env):
    from backend.routes.views import paths_in_view
    view = await _save("Needs converting, HD", "needs_conversion,res_1080p,res_720p")
    assert await paths_in_view(view["id"], list(FILES)) == {"/m/Movies/Heat/Heat.1080p.mkv", "/m/TV/Show/S01E01.720p.mkv"}
    assert await paths_in_view(view["id"], ["/m/Movies/Dune/Dune.2160p.mkv"]) == set()
    assert await paths_in_view(view["id"], ["/m/Movies/Heat/Heat.1080p.mkv"]) == {"/m/Movies/Heat/Heat.1080p.mkv"}
    assert await paths_in_view(999, list(FILES)) == set()  # gone: nothing


async def _rule(db_path, name, conditions, mode="any", priority=1, **cols):
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT INTO encoding_rules (name, match_conditions, enabled, priority, action, nvenc_cq, created_at) "
            "VALUES (?, ?, 1, ?, 'encode', ?, '2026-10-10')",
            (name, json.dumps({"match_mode": mode, "conditions": conditions}), priority, cols.get("nvenc_cq")))
        await db.commit()


@pytest.mark.asyncio
async def test_a_rule_can_match_a_view(env):
    from backend.rule_resolver import resolve_rules_for_batch
    view = await _save("4K", "res_4k")
    await _rule(env, "4K gets CQ 28", [{"type": "saved_view", "operator": "is", "value": str(view["id"])}], nvenc_cq=28)
    await _rule(env, "Not 4K gets CQ 22", [{"type": "saved_view", "operator": "is_not", "value": str(view["id"])}],
                priority=2, nvenc_cq=22)
    rules = await resolve_rules_for_batch(list(FILES))
    assert {p: r["nvenc_cq"] for p, r in rules.items()} == {
        "/m/Movies/Dune/Dune.2160p.mkv": 28, "/m/Movies/Heat/Heat.1080p.mkv": 22, "/m/TV/Show/S01E01.720p.mkv": 22}


@pytest.mark.asyncio
async def test_the_rules_api_accepts_the_condition(env):
    from backend.rule_resolver import CONDITION_TYPES
    assert "saved_view" in CONDITION_TYPES


@pytest.mark.asyncio
async def test_auto_queue_can_be_limited_to_a_view(env):
    from backend.watcher import FileWatcher
    view = await _save("TV", "type_tv")
    async with aiosqlite.connect(env) as db:
        await db.execute("INSERT INTO media_dirs (path, label) VALUES ('/m/TV', 'TV Shows')")
        for key, value in (("auto_queue_new", "true"), ("auto_queue_view", str(view["id"]))):
            await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
        await db.commit()

    class Scanned:
        def __init__(self, path):
            self.file_path = path
    with patch("backend.rule_resolver.resolve_rules_for_batch",
               new=AsyncMock(side_effect=lambda paths, extra_context=None: {p: None for p in paths})):
        assert await FileWatcher(env)._auto_queue_new_files([Scanned(p) for p in FILES]) == 1
    async with aiosqlite.connect(env) as db:
        async with db.execute("SELECT file_path FROM jobs") as cur:
            assert [r[0] for r in await cur.fetchall()] == ["/m/TV/Show/S01E01.720p.mkv"]


@pytest.mark.asyncio
async def test_a_view_in_use_cant_be_deleted(env):
    from backend.routes.views import delete_view, list_views
    used_by_queue = await _save("TV", "type_tv")
    used_by_rule = await _save("4K", "res_4k")
    spare = await _save("Spare", "x265")
    async with aiosqlite.connect(env) as db:
        await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('auto_queue_view', ?)", (str(used_by_queue["id"]),))
        await db.commit()
    await _rule(env, "4K rule", [{"type": "saved_view", "operator": "is", "value": str(used_by_rule["id"])}])
    with pytest.raises(ApiError) as e:
        await delete_view(used_by_queue["id"])
    assert e.value.code == "views.inUseByAutoQueue"
    with pytest.raises(ApiError) as e:
        await delete_view(used_by_rule["id"])
    assert e.value.code == "views.inUseByRules" and e.value.params == {"rules": "4K rule"}
    await delete_view(spare["id"])
    assert [v["name"] for v in await list_views()] == ["4K", "TV"]
    with pytest.raises(ApiError):
        await delete_view(spare["id"])


@pytest.mark.asyncio
async def test_the_auto_queue_setting_needs_a_real_view(env, monkeypatch):
    import backend.routes.settings as settings_route
    from backend.models import SettingsUpdate
    monkeypatch.setattr(settings_route, "DB_PATH", env)
    with pytest.raises(ApiError):
        await settings_route.update_encoding_settings(SettingsUpdate(auto_queue_view="42"))
    view = await _save("TV", "type_tv")
    await settings_route.update_encoding_settings(SettingsUpdate(auto_queue_view=str(view["id"])))
    assert (await settings_route.get_encoding_settings())["auto_queue_view"] == str(view["id"])
    await settings_route.update_encoding_settings(SettingsUpdate(auto_queue_view=""))  # every new file
    assert (await settings_route.get_encoding_settings())["auto_queue_view"] == ""
