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


async def _title(db_path, file_path: str) -> None:
    """The title's metadata, as the Poster grid caches it (by its title_key)."""
    from backend.scan_filters import title_key
    folder = title_key(file_path)
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT INTO poster_cache (folder_path, title, year, rating, genres, country, network, status, credits, "
            "source) VALUES (?, 'Show', '2010', 8.1, 'Drama, Action & Adventure', 'United Kingdom, Ireland', 'BBC One', "
            "'Ended', ?, 'tmdb')",
            (folder, json.dumps({"actor": ["Ann Lee", "Bo Ek"], "director": ["Jo Park"], "studio": ["Acme"]})))
        await db.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("ctype, op, value, hit", [
    ("title_actor", "is", "ann lee", True), ("title_actor", "is", "Cy Moe", False),
    ("title_actor", "contains", "Ek", True), ("title_actor", "does_not_contain", "Ek", False),
    ("title_director", "is", "Jo Park", True), ("title_studio", "is_not", "Acme", False),
    ("title_country", "is", "Ireland", True), ("title_network", "is", "BBC One", True),
    ("title_genre", "is", "action", True), ("title_genre", "is", "Comedy", False),
    ("title_show_status", "is", "ended", True), ("title_year", "greater_than", "2005", True),
    ("title_year", "is", "2011", False), ("title_rating", "less_than", "8", False),
])
async def test_title_conditions(test_db, ctype, op, value, hit):
    """Rules match the Scanner's Title lists (v0.10.0), as Advanced Search
    does: year, rating, genre, country, network, show status, credits."""
    await _title(test_db, FILE)
    await _rule(test_db, ctype, value, op=op)
    result = (await resolve_rules_for_batch([FILE]))[FILE]
    assert (result is not None) is hit, (ctype, op, value)


def test_advanced_search_has_the_title_lists():
    """Advanced Search's conditions for what the filter menus' Title lists
    hold: country, actor, director, studio, Plex label."""
    from backend.scan_filters import compile_condition, title_key
    row = {"file_path": "/m/Show [tvdb-5]/Season 1/e1.mkv"}
    key = title_key(row["file_path"])
    assert key == "/m/Show [tvdb-5]"
    ctx = {"title_meta": {key: ("2010", 8.1, "Drama", None, None, "United Kingdom, Ireland")},
           "title_credits": {key: {"actor": ["Ann Lee"], "director": ["Jo Park"], "studio": ["Acme"]}},
           "server_labels": {"/m/Show [tvdb-5]/": {"Keep 4K"}}, "media_roots": frozenset({"/m"})}

    def matches(prop, op, value) -> bool:
        return compile_condition(0, {"property": prop, "op": op, "value": value}).py(dict(row), ctx)
    assert matches("country", "eq", "ireland") and not matches("country", "eq", "France")
    assert matches("actor", "contains", "ann") and matches("director", "eq", "Jo Park")
    assert matches("studio", "ne", "Other") and not matches("studio", "ne", "acme")
    assert matches("plex_label", "eq", "keep 4k") and matches("actor", "in", ["Nobody", "Ann Lee"])
