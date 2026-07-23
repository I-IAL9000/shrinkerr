import pytest, aiosqlite


@pytest.mark.asyncio
async def test_dubbed_and_not_api_matched_predicates(tmp_path):
    db = await aiosqlite.connect(str(tmp_path / "t.db"))
    try:
        await db.execute("CREATE TABLE scan_results (file_path TEXT, language_source TEXT, is_dubbed_flag INTEGER, tmdb_unresolved INTEGER, removed_from_list INTEGER DEFAULT 0)")
        await db.executemany("INSERT INTO scan_results (file_path, language_source, is_dubbed_flag, tmdb_unresolved) VALUES (?,?,?,?)", [
            ("/dub.mkv","api",1,0),
            ("/ok.mkv","api",0,0),
            ("/heur.mkv","heuristic",0,0),
            ("/heurfail.mkv","heuristic",0,1),
            ("/man.mkv","manual",0,0),
        ])
        await db.commit()
        async def sel(where):
            async with db.execute(f"SELECT file_path FROM scan_results WHERE 1=1 {where} ORDER BY file_path") as c:
                return [r[0] for r in await c.fetchall()]
        assert await sel("AND COALESCE(is_dubbed_flag, 0) = 1") == ["/dub.mkv"]
        assert await sel("AND (language_source IS NULL OR language_source NOT IN ('api','manual','tmdb-manual'))") == ["/heur.mkv","/heurfail.mkv"]
        # tried-no-match subset
        assert await sel("AND (language_source IS NULL OR language_source NOT IN ('api','manual','tmdb-manual')) AND COALESCE(tmdb_unresolved,0)=1") == ["/heurfail.mkv"]
    finally:
        await db.close()


def test_matches_single_filter_dubbed_and_unmatched():
    from backend.routes.scan import _matches_single_filter
    assert _matches_single_filter({"is_dubbed_flag": 1}, "dubbed") is True
    assert _matches_single_filter({"is_dubbed_flag": 0}, "dubbed") is False
    assert _matches_single_filter({"language_source": "heuristic"}, "not_api_matched") is True
    assert _matches_single_filter({"language_source": "api"}, "not_api_matched") is False
    assert _matches_single_filter({"language_source": "manual"}, "not_api_matched") is False


@pytest.mark.asyncio
async def test_refresh_default_selects_all_heuristic(tmp_path):
    """v0.9.91: the default refresh retries ALL still-heuristic rows each run
    (no permanent tmdb_unresolved skip) — the metadata_cache throttles real API
    load. Deep also re-processes api rows."""
    import aiosqlite
    db = await aiosqlite.connect(str(tmp_path / "t.db"))
    try:
        await db.execute("CREATE TABLE scan_results (id INTEGER PRIMARY KEY, language_source TEXT, tmdb_unresolved INTEGER DEFAULT 0, removed_from_list INTEGER DEFAULT 0)")
        await db.executemany("INSERT INTO scan_results (language_source, tmdb_unresolved, removed_from_list) VALUES (?,?,?)", [
            ("heuristic",0,0),  # 1 untried heuristic -> selected default
            ("heuristic",1,0),  # 2 previously-failed heuristic -> NOW retried
            ("api",0,0),        # 3 skipped default, selected deep
            ("heuristic",0,1),  # 4 removed -> never
        ])
        await db.commit()
        default_where = "WHERE language_source = 'heuristic' AND removed_from_list = 0"
        deep_where = "WHERE language_source IN ('heuristic','api') AND removed_from_list = 0"
        async def ids(where):
            async with db.execute(f"SELECT id FROM scan_results {where} ORDER BY id") as c:
                return [r[0] for r in await c.fetchall()]
        assert await ids(default_where) == [1, 2]   # both heuristic, tmdb_unresolved ignored
        assert await ids(deep_where) == [1, 2, 3]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_disc_iso_predicate(tmp_path):
    """disc_iso selects any row with disc_type set (dvd/bdmv, iso or folder)."""
    import aiosqlite
    db = await aiosqlite.connect(str(tmp_path / "t.db"))
    try:
        await db.execute("CREATE TABLE scan_results (file_path TEXT, disc_type TEXT)")
        await db.executemany(
            "INSERT INTO scan_results (file_path, disc_type) VALUES (?,?)",
            [("/a.iso", "bdmv"), ("/b.iso", "dvd"), ("/c.mkv", None), ("/d/VIDEO_TS", "dvd")])
        await db.commit()
        async with db.execute(
            "SELECT file_path FROM scan_results WHERE 1=1 AND disc_type IS NOT NULL ORDER BY file_path"
        ) as c:
            got = [r[0] for r in await c.fetchall()]
        assert got == ["/a.iso", "/b.iso", "/d/VIDEO_TS"]
    finally:
        await db.close()


def test_matches_single_filter_disc_iso():
    from backend.routes.scan import _matches_single_filter
    assert _matches_single_filter({"disc_type": "bdmv"}, "disc_iso") is True
    assert _matches_single_filter({"disc_type": "dvd"}, "disc_iso") is True
    assert _matches_single_filter({"disc_type": None}, "disc_iso") is False
    assert _matches_single_filter({}, "disc_iso") is False


@pytest.mark.asyncio
async def test_rescan_preserves_authoritative_language_source(tmp_path):
    """v0.9.94: a re-scan upsert must NOT downgrade an authoritative
    native/source (api/manual/tmdb-manual) back to the fresh heuristic guess,
    but SHOULD refresh a heuristic/null row."""
    import aiosqlite
    db = await aiosqlite.connect(str(tmp_path / "t.db"))
    try:
        await db.execute(
            "CREATE TABLE scan_results (file_path TEXT PRIMARY KEY, "
            "native_language TEXT, language_source TEXT)")
        await db.executemany(
            "INSERT INTO scan_results (file_path, native_language, language_source) VALUES (?,?,?)",
            [("/a.mkv", "kor", "api"),
             ("/b.mkv", "eng", "tmdb-manual"),
             ("/c.mkv", "eng", "heuristic")])
        await db.commit()

        # Mirror the persist upsert's CASE for native/source.
        async def rescan(fp, native, source):
            await db.execute(
                "INSERT INTO scan_results (file_path, native_language, language_source) "
                "VALUES (?, ?, ?) ON CONFLICT(file_path) DO UPDATE SET "
                "native_language = CASE WHEN scan_results.language_source IN ('api','manual','tmdb-manual') "
                "                       THEN scan_results.native_language ELSE excluded.native_language END, "
                "language_source = CASE WHEN scan_results.language_source IN ('api','manual','tmdb-manual') "
                "                       THEN scan_results.language_source ELSE excluded.language_source END",
                (fp, native, source))
            await db.commit()

        # Re-scan re-derives native from audio (heuristic) — must be ignored for
        # authoritative rows, applied for the heuristic one.
        await rescan("/a.mkv", "eng", "heuristic")
        await rescan("/b.mkv", "fra", "heuristic")
        await rescan("/c.mkv", "jpn", "heuristic")

        async def row(fp):
            async with db.execute(
                "SELECT native_language, language_source FROM scan_results WHERE file_path=?", (fp,)) as c:
                return tuple(await c.fetchone())
        assert await row("/a.mkv") == ("kor", "api")          # preserved
        assert await row("/b.mkv") == ("eng", "tmdb-manual")  # preserved
        assert await row("/c.mkv") == ("jpn", "heuristic")    # refreshed
    finally:
        await db.close()
