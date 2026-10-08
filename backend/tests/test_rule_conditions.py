"""v0.10.0 (SC-09): rule conditions that silently never matched, so "skip"
rules didn't protect the files they named.

- Sonarr/Radarr tag conditions were accepted but never evaluated.
- Watched and Jellyfin/Emby tag conditions read a cache that was only loaded
  when some rule used a Plex label / collection / genre / library.
- Creating a rule refused seven condition types the rule editor offers.
- After every full scan the Plex sync emptied the whole metadata cache, even
  without Plex configured, wiping Jellyfin/Emby data.
"""
import json

import aiosqlite
import pytest

from backend.api_errors import ApiError
from backend.rule_resolver import resolve_rules_for_batch

FILE = "/media/TV/Show (2010)/Season 01/Show - S01E01.mkv"


async def _rule(db_path, ctype, value, op="is", action="skip"):
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT INTO encoding_rules (name, match_type, match_value, match_conditions, priority, action, enabled, "
            "created_at) VALUES ('r', ?, ?, ?, 0, ?, 1, '2026-10-08T00:00:00')",
            (ctype, value, json.dumps({"match_mode": "any", "conditions": [
                {"type": ctype, "operator": op, "value": value}]}), action))
        await db.commit()


async def _cache(db_path, folder, mtype, mvalue):
    async with aiosqlite.connect(db_path) as db:
        await db.execute("INSERT INTO plex_metadata_cache (folder_path, metadata_type, metadata_value, synced_at) "
                         "VALUES (?, ?, ?, '2026-10-08T00:00:00')", (folder, mtype, mvalue))
        await db.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("ctype, value", [("plex_watched", "true"), ("emby_watched", "true"),
                                          ("jellyfin_tag", "keep-original"), ("emby_tag", "keep-original")])
async def test_a_cache_rule_matches_on_its_own(test_db, ctype, value):
    await _rule(test_db, ctype, value)
    tag_type = ctype if ctype.endswith("_tag") else "watch_status"
    await _cache(test_db, "/media/TV/Show (2010)/", tag_type, "watched" if tag_type == "watch_status" else value)
    assert (await resolve_rules_for_batch([FILE]))[FILE] is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("op, tags, matches", [("is", {"keep-original"}, True), ("is", set(), False),
                                               ("is_not", {"keep-original"}, False), ("is_not", set(), True)])
async def test_arr_tag_rules_match(test_db, monkeypatch, op, tags, matches):
    import backend.arr as arr

    async def tags_for(paths):
        return {p: set(tags) for p in paths}

    monkeypatch.setattr(arr, "arr_tags_for_paths", tags_for)
    await _rule(test_db, "arr_tag", "Keep-Original", op)
    assert ((await resolve_rules_for_batch([FILE]))[FILE] is not None) is matches


@pytest.mark.asyncio
async def test_arr_tags_follow_the_path_mapping(monkeypatch):
    import backend.arr as arr

    async def settings():
        return {"sonarr_url": "http://sonarr", "sonarr_api_key": "k", "sonarr_path_mapping": "/media/TV=/tv"}

    async def labels(client, service, url, key):
        return {1: "keep-original", 2: "anime"}

    async def items(client, service, url, key):
        return [{"path": "/tv/Show (2010)", "tags": [1]}, {"path": "/tv/Other", "tags": [2]}]

    monkeypatch.setattr(arr, "_get_arr_settings", settings)
    monkeypatch.setattr(arr, "_arr_tag_labels", labels)
    monkeypatch.setattr(arr, "_arr_items", items)
    got = await arr.arr_tags_for_paths([FILE, "/media/Movies/X (2001)/X.mkv"])
    assert got == {FILE: {"keep-original"}, "/media/Movies/X (2001)/X.mkv": set()}


@pytest.mark.asyncio
async def test_every_condition_the_editor_offers_can_be_saved(test_db):
    pytest.importorskip("apscheduler")
    from backend.routes import rules as route
    for ctype, op, value in [("file_size", "greater_than", "10"), ("date_added", "less_than", "7d"),
                             ("title", "contains", "Pilot"), ("nzbget_category", "is", "tv"),
                             ("jellyfin_tag", "is", "x"), ("emby_tag", "is", "x"), ("emby_watched", "is", "true"),
                             ("arr_tag", "is", "x")]:
        await route.create_rule(route.RuleCreate(
            name=ctype, match_conditions=[route.MatchCondition(type=ctype, operator=op, value=value)]))
    with pytest.raises(ApiError) as exc:
        await route.create_rule(route.RuleCreate(
            name="bad", match_conditions=[route.MatchCondition(type="bogus", value="x")]))
    assert exc.value.code == "rules.invalidMatchType"


@pytest.mark.asyncio
async def test_plex_sync_without_plex_keeps_the_cache(test_db):
    from backend.plex import sync_plex_metadata_cache
    await _cache(test_db, "/media/TV/Show (2010)/", "jellyfin_tag", "keep-original")
    await _cache(test_db, "/media/TV/Show (2010)/", "watch_status", "watched")
    await sync_plex_metadata_cache()
    async with aiosqlite.connect(test_db) as db:
        async with db.execute("SELECT COUNT(*) FROM plex_metadata_cache") as cur:
            assert (await cur.fetchone())[0] == 2
