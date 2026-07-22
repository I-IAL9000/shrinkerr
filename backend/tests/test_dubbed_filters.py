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
