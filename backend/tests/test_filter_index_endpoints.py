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
