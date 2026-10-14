"""/api/scan/filter-counts and /api/scan/scan-stats answer from the filter
index once it's built, and from the SQL engine until then."""
import pytest
import pytest_asyncio

from backend.filter_index import service
from backend.tests import filter_index_library as library_data


@pytest_asyncio.fixture
async def library(test_db, monkeypatch):
    import backend.routes.scan as scan_route
    monkeypatch.setattr(scan_route, "DB_PATH", test_db)
    await library_data.seed(test_db)
    service.reset()
    yield test_db
    service.reset()


@pytest.mark.asyncio
async def test_the_counts_come_from_the_index_once_built(library, monkeypatch):
    import backend.routes.scan as scan_route
    expected = {text: await library_data.sql_counts(library, text) for text in ("all", "x264,!ignored")}
    savings = await library_data.sql_savings(library)
    # Not built yet: the SQL engine answers (and the build starts).
    assert await scan_route.get_filter_counts("x264,!ignored") == {"counts": expected["x264,!ignored"], "facets": None}
    await service.wait_rebuilt()

    async def sql_engine(*args, **kwargs):
        raise AssertionError("the SQL engine ran")
    monkeypatch.setattr(scan_route, "count_all", sql_engine)
    monkeypatch.setattr(scan_route, "facet_counts", sql_engine)
    for text, counts in expected.items():
        body = await scan_route.get_filter_counts(text)
        assert body["counts"] == counts and body["facets"]["alang"], text
    stats = await scan_route.get_scan_stats()
    assert stats["summary"]["files_to_convert"] == expected["all"]["needs_conversion"]
    assert stats["summary"]["estimated_savings_bytes"] == savings
    assert stats["counts"]["x264"] == expected["all"]["x264"]


@pytest.mark.asyncio
async def test_the_tree_lists_and_add_to_queue_list_the_index_rows(library, monkeypatch):
    """With Python filters in it, a filter's rows come from the index once
    it's built, not a Python pass: the same tree, files and paths as the SQL
    engine's."""
    import backend.routes.scan as scan_route
    from backend.scan_filters import parse_filter

    async def everything(text: str) -> tuple:
        paths = sorted(await scan_route._paths_matching(text))
        titles = sorted({p.rsplit("/", 2)[0] for p in paths})[:3]
        files = [[r["file_path"] for r in await scan_route.get_files_by_title(t, text)] for t in titles]
        folder_files = [[r["file_path"] for r in await scan_route.get_scan_files(p.rsplit("/", 1)[0], text)]
                        for p in paths[:3]]
        return await scan_route.get_scan_tree(text), paths, files, folder_files

    texts = [t for t in library_data.EXPRESSIONS if parse_filter(t).needs_ctx]
    assert len(texts) > 10
    by_sql = {text: await everything(text) for text in texts}  # the index isn't built yet
    assert any(answer[1] for answer in by_sql.values())
    await service.wait_rebuilt()
    from backend.scan_filters import FilterExpr

    def no_python_pass(*args):
        raise AssertionError("the rows went through the Python filters")
    monkeypatch.setattr(FilterExpr, "post_filter", no_python_pass)
    for text in texts:
        assert await everything(text) == by_sql[text], text
