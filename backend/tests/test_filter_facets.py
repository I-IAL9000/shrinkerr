"""List facets (v0.10.0, filter overhaul phase 3): a value of a list is a
filter token "<facet>:<value>" that parse_filter turns into a filter, so the
tree, lists, Add to Queue, saved views and policies apply it like any other.
Within a list, values widen; across lists, they narrow."""
import json
from urllib.parse import quote

import aiosqlite
import pytest
import pytest_asyncio

from backend.scan_filters import (FACETS, FILTERS, PY_COLUMNS, channel_band, count_all, encode_advanced,
                                  facet_counts, facet_filter, facet_token, lang_aliases, lang_key, parse_filter,
                                  rating_band)


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
    # Exactly as the browser's encodeURIComponent, so both build the same token.
    assert facet_token("folder", "/m/Films/Heat (1995) [tt0113277]") == \
        "folder:%2Fm%2FFilms%2FHeat%20(1995)%20%5Btt0113277%5D"
    assert facet_token("network", "!~*()'-_. é") == "network:!~*()'-_.%20%C3%A9"


def test_hand_written_values_read_as_the_index_keeps_them():
    def ids(text):
        expr = parse_filter(text)
        return [f.id for g in expr.groups for f in g] + ["!" + f.id for f in expr.excluded]
    assert ids("alang:FRA") == ["alang:fre"]
    assert ids("slang:%20EN%20") == ["slang:eng"]
    assert ids("acodec:TrueHD") == ["acodec:truehd"]
    assert ids("achan:6,achan:12,achan:5,achan:7.1,achan:") == ["achan:6"]  # its bands only
    assert ids("acodec:%20,alang:") == []
    assert ids("genre:Drama") == ["genre:Drama"]  # Python lists: as given
    # One filter per value, however it was written.
    assert ids("alang:fre,alang:%66re,alang:FRA,!alang:fre,!alang:fra") == ["alang:fre", "!alang:fre"]


def test_track_lists_hint_the_raw_json():
    """A LIKE on the raw JSON spares the per-track check, when it can't miss."""
    assert "LIKE '%fre%'" in facet_filter("alang", "fre").sql
    assert "LIKE '%truehd%'" in facet_filter("acodec", "truehd").sql
    assert "LIKE" not in facet_filter("alang", "und").sql             # "" has no text to look for
    assert "LIKE" not in facet_filter("acodec", "Äc3").sql            # JSON may write it Ä
    assert "LIKE" not in facet_filter("acodec", "it's").sql           # never into the SQL text
    assert "LIKE" not in facet_filter("acodec", "aac\n").sql


def test_python_lists_have_no_empty_values():
    # folder -> (year, rating, genres, network, status, country); a media server's genre can be "".
    ctx = {"title_meta": {"/m/x": ("", None, "Drama, ,", " ", "", " , ")}, "server_genres": {"/m/": {"", "drama"}},
           "media_dir_ids": []}
    for key, facet in FACETS.items():
        if facet.values:
            assert all(facet.values({"file_path": "/m/x/a.mkv"}, ctx)), key
    assert set(FACETS["genre"].values({"file_path": "/m/x/a.mkv"}, ctx)) == {"drama"}


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


@pytest.mark.asyncio
async def test_sql_counts_take_list_values(library):
    """The SQL engine's counts (/api/scan/filter-counts while the filter index
    builds) with list values in the filter: their ids aren't SQL names."""
    from backend.routes.scan import _build_enrichment_context
    db_path, _ = library
    adv_any = encode_advanced([{"property": "audio_codec", "op": "eq", "value": "ac3"},
                               {"property": "genre", "op": "eq", "value": "comedy"}], "any")  # SQL or Python
    for text, n in (("alang:fre", 2), ("achan:6", 1), ("!acodec:ac3", 3),
                    (facet_token("folder", "/m/Other, Stuff"), 1), ("!genre:drama", 3),
                    (f"alang:fre,!genre:comedy,{adv_any}", 1)):
        async with aiosqlite.connect(db_path) as db:
            db.row_factory = aiosqlite.Row
            ctx = await _build_enrichment_context(db, titles=True)
            counts = await facet_counts(db, ctx, parse_filter(text))
        assert counts["matching"] == len(await matching(db_path, text)) == n, text


def _stored(tracks):
    return tracks if tracks is None or isinstance(tracks, str) else json.dumps(tracks)


AWKWARD = {
    # name: (audio tracks, subtitle tracks): lists are stored as JSON, text as is
    "tab.mkv": ([{"language": "\tfre", "codec": "AAC", "channels": "6"}], [{"language": " fre "}]),
    "spaces.mkv": ([{"language": " fre ", "codec": "aac\n", "channels": 6.0}], [{"language": "FRA"}]),
    "upper.mkv": ([{"language": "FRA", "codec": "ÄC3", "channels": None}], [{"language": 0}]),
    "zero.mkv": ([{"language": 0, "codec": " ac3 ", "channels": 2}], [{"language": None}]),
    "null.mkv": ([{"language": None, "codec": None}], [{}]),
    "missing.mkv": ([{"codec": "dts"}], [{"language": "ÉNG"}]),
    "accent.mkv": ([{"language": "ÉNG", "channels": 8}, {"language": "en", "channels": 1}], [{"language": "\tfre"}]),
    "list.mkv": ('["eng"]', '["eng"]'),
    "object.mkv": ('{"language": "eng", "channels": 8}', '{"language": "eng"}'),
    "broken.mkv": ("not json", None),
    "scalar.mkv": ('"eng"', '[["x"], 3, null]'),
}


@pytest_asyncio.fixture
async def awkward(test_db):
    async with aiosqlite.connect(test_db) as db:
        for name, (audio, subs) in AWKWARD.items():
            await db.execute(
                "INSERT INTO scan_results (file_path, file_size, scan_timestamp, audio_tracks_json, "
                "subtitle_tracks_json) VALUES (?, 1, '2026-10-12', ?, ?)", (f"/m/x/{name}", _stored(audio), _stored(subs)))
        await db.commit()
    return test_db


@pytest.mark.asyncio
@pytest.mark.parametrize("key, more", [("alang", ["und", "eng", "fre", "chi"]), ("slang", ["und", "eng", "fre"]),
                                       ("acodec", ["aac", "ac3", "truehd"]), ("achan", ["1", "2", "6", "8"])])
async def test_index_values_and_tokens_agree(awkward, key, more):
    """Every value the index would keep for a row lists exactly the rows
    that have it, however oddly the tracks are written."""
    facet = FACETS[key]
    async with aiosqlite.connect(awkward) as db:
        async with db.execute("SELECT id, file_path FROM scan_results") as cur:
            names = {i: fp.rsplit("/", 1)[-1] for i, fp in await cur.fetchall()}
        async with db.execute(facet.pairs_sql.format(where="removed_from_list = 0")) as cur:
            pairs = await cur.fetchall()
    kept: dict[str, set] = {}
    for row_id, raw in pairs:
        value = facet.normalize(raw)
        if value is not None:
            kept.setdefault(value, set()).add(names[row_id])
    assert len(kept) >= 3, kept
    for value in {*kept, *more}:
        assert set(await matching(awkward, facet_token(key, value))) == kept.get(value, set()), (key, value)


@pytest.mark.asyncio
async def test_odd_track_json_breaks_no_filter(awkward):
    """A track list that's an object, a list of strings or not JSON: no
    tracks, for the lists and for the pills that read tracks."""
    from backend.routes.scan import _build_enrichment_context
    async with aiosqlite.connect(awkward) as db:
        db.row_factory = aiosqlite.Row
        ctx = await _build_enrichment_context(db)
        await count_all(db, ctx)  # every pill
    odd = {"list.mkv", "object.mkv", "broken.mkv", "scalar.mkv"}
    assert await matching(awkward, "alang:eng") == ["accent.mkv"]
    assert "accent.mkv" in await matching(awkward, "audio_71")
    for fid, f in FILTERS.items():
        if f.sql and "json_each" in f.sql:
            assert not odd & set(await matching(awkward, fid)), fid
    channels = encode_advanced([{"property": "audio_channels", "op": "gte", "value": 8}])
    assert "accent.mkv" in await matching(awkward, channels)
    assert not odd & set(await matching(awkward, channels))


@pytest.mark.asyncio
async def test_context_without_media_folders(test_db):
    from backend.routes.scan import _build_enrichment_context
    async with aiosqlite.connect(test_db) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("DROP TABLE media_dirs")
        ctx = await _build_enrichment_context(db)
    assert ctx["media_dir_ids"] == []
