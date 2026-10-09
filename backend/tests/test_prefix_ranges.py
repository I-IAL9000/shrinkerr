"""F6 (v0.10.0): folder-scoped queries use file_path index ranges instead of
`LIKE 'prefix%'`, which can't use the index (and treated `%` / `_` in folder
names as wildcards)."""
import sqlite3

import aiosqlite
import pytest

from backend.database import prefix_clause, prefix_range

PATHS = [
    "/m/Show/a.mkv", "/m/Show/S01/e1.mkv", "/m/Show 2/a.mkv", "/m/Show-x/a.mkv",
    "/m/Show0/a.mkv", "/m/show/a.mkv", "/m/Show", "/m/Shox/a.mkv",
    "/m/100% Wolf/a.mkv", "/m/100X Wolf/a.mkv", "/m/a_b/a.mkv", "/m/aXb/a.mkv",
    "/m/Þáttur/ö.mkv", "/m/Þáttur/\U0001F600.mkv", "/m/Þátturx/a.mkv",
]


def _matching(prefixes):
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE t (file_path TEXT)")
    con.executemany("INSERT INTO t VALUES (?)", [(p,) for p in PATHS])
    sql, params = prefix_clause(prefixes)
    return sorted(r[0] for r in con.execute(f"SELECT file_path FROM t WHERE {sql}", params))


@pytest.mark.parametrize("prefix", [
    "/m/Show/", "/m/Show", "/m/100% Wolf/", "/m/a_b/", "/m/Þáttur/", "/m/", "/",
])
def test_a_range_matches_exactly_the_paths_with_the_prefix(prefix):
    assert _matching([prefix]) == sorted(p for p in PATHS if p.startswith(prefix))


def test_wildcards_and_case_in_folder_names_are_literal():
    # LIKE matched "/m/100X Wolf" for "100%", "/m/aXb" for "a_b" and
    # "/m/show" for "/m/Show".
    assert _matching(["/m/100% Wolf/"]) == ["/m/100% Wolf/a.mkv"]
    assert _matching(["/m/a_b/"]) == ["/m/a_b/a.mkv"]
    assert _matching(["/m/Show/"]) == ["/m/Show/S01/e1.mkv", "/m/Show/a.mkv"]


def test_several_prefixes_and_none():
    assert _matching(["/m/a_b/", "/m/aXb/"]) == ["/m/aXb/a.mkv", "/m/a_b/a.mkv"]
    assert _matching([]) == []
    assert prefix_clause([]) == ("0", [])


def test_the_upper_bound_skips_surrogates():
    assert prefix_range("/a퟿") == ("/a퟿", "/a")


async def _seed(db_path, paths):
    async with aiosqlite.connect(db_path) as db:
        await db.executemany(
            "INSERT INTO scan_results (file_path, file_size, scan_timestamp) VALUES (?, 1, '2026-01-01')",
            [(p,) for p in paths])
        await db.commit()


@pytest.mark.asyncio
async def test_folder_expand_lists_only_the_direct_children(test_db, monkeypatch):
    import backend.routes.scan as scan_route
    monkeypatch.setattr(scan_route, "DB_PATH", test_db)
    await _seed(test_db, PATHS)
    files = await scan_route.get_scan_files(folder="/m/Show")
    assert [f["file_path"] for f in files] == ["/m/Show", "/m/Show/a.mkv"]
    files = await scan_route.get_scan_files(folder="/m/100% Wolf/")
    assert [f["file_path"] for f in files] == ["/m/100% Wolf/a.mkv"]
    files = await scan_route.get_scan_files(folder="/m/Þáttur")
    assert [f["file_path"] for f in files] == ["/m/Þáttur/ö.mkv", "/m/Þáttur/\U0001F600.mkv"]


@pytest.mark.asyncio
async def test_title_files_include_seasons_but_not_lookalike_titles(test_db, monkeypatch):
    import backend.routes.scan as scan_route
    monkeypatch.setattr(scan_route, "DB_PATH", test_db)
    await _seed(test_db, PATHS)
    files = await scan_route.get_files_by_title(prefix="/m/Show")
    assert [f["file_path"] for f in files] == ["/m/Show", "/m/Show/S01/e1.mkv", "/m/Show/a.mkv"]


@pytest.mark.asyncio
async def test_folder_queries_use_the_file_path_index(test_db):
    """Without ANALYZE stats SQLite prefers the removed_from_list equality
    to a range, which reads the whole table on a real library."""
    from backend.routes.scan import _SCAN_WHERE_IN_FOLDERS
    # SQLite sizes the table from its pages: an empty one is scanned anyway.
    await _seed(test_db, [f"/m/Title {i:05d}/file {i}.mkv" for i in range(5000)])
    sql, params = prefix_clause(["/m/Show/", "/m/a_b/"])
    one_sql, one_params = prefix_clause(["/m/Show/"])
    con = sqlite3.connect(test_db)
    try:
        for q, p in [
            (f"SELECT file_path FROM scan_results WHERE {_SCAN_WHERE_IN_FOLDERS} AND ({sql})", params),
            (f"SELECT file_path FROM scan_results WHERE +removed_from_list = 0 AND ({sql})", params),
            (f"SELECT file_path FROM scan_results WHERE {_SCAN_WHERE_IN_FOLDERS} "
             f"AND (file_path = ? OR ({one_sql} AND instr(substr(file_path, ?), '/') = 0))", ["/m/Show", *one_params, 9]),
        ]:
            plan = " ".join(r[3] for r in con.execute("EXPLAIN QUERY PLAN " + q, p))
            assert "SCAN scan_results" not in plan, plan
            assert "removed" not in plan, plan
            assert "file_path>?" in plan, plan
    finally:
        con.close()
