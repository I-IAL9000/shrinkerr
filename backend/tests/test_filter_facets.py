"""List facets (v0.10.0, filter overhaul phase 3): a value of a list is a
filter token "<facet>:<value>" that parse_filter turns into a filter, so the
tree, lists, Add to Queue, saved views and policies apply it like any other.
Within a list, values widen; across lists, they narrow."""
import json
from urllib.parse import quote

import aiosqlite
import pytest
import pytest_asyncio

from backend.scan_filters import (FACETS, PY_COLUMNS, channel_band, facet_token, lang_aliases, lang_key,
                                  parse_filter, rating_band)


def test_language_keys_fold_codes():
    assert [lang_key(c) for c in ("fra", "fr", "FRE", " fre ", "deu", "en", "zho")] == \
        ["fre", "fre", "fre", "fre", "ger", "eng", "chi"]
    assert [lang_key(c) for c in ("", None, "und", "UND")] == ["und"] * 4
    assert {"fre", "fra", "fr"} <= set(lang_aliases("fre"))
    assert set(lang_aliases("und")) == {"", "und"}
    # The list (lang_aliases, in SQL) and the index (lang_key) agree: "yue"
    # keys as itself, so a Chinese list doesn't also match Cantonese tracks.
    assert all(lang_key(a) == "chi" for a in lang_aliases("chi"))


def test_channel_and_rating_bands():
    assert [channel_band(n) for n in (1, 2, 3, 6, 7, 8, 12)] == ["1", "2", "6", "6", "6", "8", "8"]
    assert [channel_band(n) for n in (0, None, "x")] == [None, None, None]
    assert [rating_band(r) for r in (8.4, 8.0, 7.2, 6.0, 5.5, 3.1, None, 0)] == \
        ["8", "8", "7", "6", "5", "lt5", "none", "none"]


def test_tokens_quote_their_value():
    assert facet_token("network", "Paramount+, HD") == "network:" + quote("Paramount+, HD", safe="")
    assert set(FACETS) == {"alang", "slang", "acodec", "achan", "genre", "year", "rating", "country",
                           "network", "showstatus", "library", "folder"}


def tr(lang, codec="aac", channels=2):
    return {"stream_index": 1, "language": lang, "codec": codec, "channels": channels}


ROWS = {
    # path: (audio tracks, subtitle tracks)
    "/m/Films/Amelie (2001) [tt0211915]/Amelie.mkv": ([tr("fra", "ac3", 6)], [tr("eng")]),
    "/m/Films/Heat (1995) [tt0113277]/Heat.mkv": ([tr("eng", "truehd", 8), tr("fre", "ac3", 6)], []),
    "/m/Films/Mute (2018) [tt1464335]/Mute.mkv": ([tr("", "aac", 1)], [tr("ger")]),
    "/m/TV/The Wire (2002) [tvdb-79126]/Season 1/The Wire S01E01.mkv": ([tr("en", "eac3", 2)], []),
    "/m/Other, Stuff/Home Video.mkv": ([], []),
}
POSTERS = [
    # folder, year, rating, genres, country, network, status
    ("/m/Films/Amelie (2001) [tt0211915]", "2001", 8.3, "Comedy, Romance", "France", None, None),
    ("/m/Films/Heat (1995) [tt0113277]", "1995", 8.3, "Crime, Drama", "United States", None, None),
    ("/m/Films/Mute (2018) [tt1464335]", "2018", 5.4, "Science Fiction", "United Kingdom, Germany", None, None),
    ("/m/TV/The Wire (2002) [tvdb-79126]", "2002", 9.3, "Crime, Drama", "United States", "HBO", "Ended"),
]


@pytest_asyncio.fixture
async def library(test_db):
    async with aiosqlite.connect(test_db) as db:
        for path, (audio, subs) in ROWS.items():
            await db.execute(
                "INSERT INTO scan_results (file_path, file_size, scan_timestamp, audio_tracks_json, "
                "subtitle_tracks_json) VALUES (?, 1, '2026-10-12', ?, ?)", (path, json.dumps(audio), json.dumps(subs)))
        await db.executemany(
            "INSERT INTO poster_cache (folder_path, year, rating, genres, country, network, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)", POSTERS)
        await db.execute("INSERT INTO media_dirs (path, label) VALUES ('/m/Films', 'Films'), ('/m/TV', 'TV'), "
                         "('/m', 'Everything')")
        await db.commit()
        async with db.execute("SELECT id, path FROM media_dirs") as cur:
            dirs = {path: str(i) for i, path in await cur.fetchall()}
    return test_db, dirs


async def matching(db_path, text):
    """The files a filter lists, as the tree endpoint finds them."""
    from backend.routes.scan import _build_enrichment_context
    expr = parse_filter(text)
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        ctx = await _build_enrichment_context(db, titles=expr.needs_titles) if expr.needs_ctx else None
        async with db.execute(
            f"SELECT {PY_COLUMNS}{expr.select_sql} FROM scan_results WHERE removed_from_list = 0{expr.where_sql}",
            [*expr.select_params, *expr.where_params]) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
    return sorted(r["file_path"].rsplit("/", 1)[-1] for r in rows if not expr.needs_ctx or expr.post_filter(r, ctx))


@pytest.mark.asyncio
async def test_track_lists(library):
    db, _ = library
    assert await matching(db, "alang:fre") == ["Amelie.mkv", "Heat.mkv"]          # "fra" and "fre"
    assert await matching(db, "alang:eng") == ["Heat.mkv", "The Wire S01E01.mkv"]  # "eng" and "en"
    assert await matching(db, "alang:und") == ["Mute.mkv"]
    assert await matching(db, "slang:ger") == ["Mute.mkv"]
    assert await matching(db, "acodec:truehd") == ["Heat.mkv"]
    assert await matching(db, "achan:8") == ["Heat.mkv"]                           # its widest track
    assert await matching(db, "achan:6") == ["Amelie.mkv"]
    assert await matching(db, "achan:1,achan:2") == ["Mute.mkv", "The Wire S01E01.mkv"]  # a list widens
    assert await matching(db, "alang:fre,acodec:truehd") == ["Heat.mkv"]                 # lists narrow
    assert await matching(db, "alang:fre,!acodec:truehd") == ["Amelie.mkv"]


@pytest.mark.asyncio
async def test_title_lists(library):
    db, _ = library
    assert await matching(db, "genre:drama") == ["Heat.mkv", "The Wire S01E01.mkv"]
    assert await matching(db, "year:1990s") == ["Heat.mkv"]
    assert await matching(db, "year:2001,year:2018") == ["Amelie.mkv", "Mute.mkv"]
    assert await matching(db, "rating:8") == ["Amelie.mkv", "Heat.mkv", "The Wire S01E01.mkv"]
    assert await matching(db, "rating:none") == ["Home Video.mkv"]
    assert await matching(db, facet_token("country", "United Kingdom")) == ["Mute.mkv"]
    assert await matching(db, "network:HBO") == ["The Wire S01E01.mkv"]
    assert await matching(db, "showstatus:ended") == ["The Wire S01E01.mkv"]


@pytest.mark.asyncio
async def test_library_and_folder(library):
    db, dirs = library
    assert await matching(db, f"library:{dirs['/m/TV']}") == ["The Wire S01E01.mkv"]
    assert await matching(db, f"library:{dirs['/m']}") == ["Home Video.mkv"]  # the most specific folder wins
    assert await matching(db, facet_token("folder", "/m/Other, Stuff")) == ["Home Video.mkv"]  # a comma in it
    assert await matching(db, facet_token("folder", "/m/Films/Heat (1995) [tt0113277]")) == ["Heat.mkv"]


@pytest.mark.asyncio
async def test_unknown_lists_and_empty_values_are_ignored(library):
    db, _ = library
    assert len(await matching(db, "nosuchlist:x")) == len(ROWS)
    assert len(await matching(db, "alang:")) == len(ROWS)
