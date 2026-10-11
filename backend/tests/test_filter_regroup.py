"""Phase 2 of the filter overhaul: each section of the new filter bar is one
server group, so ticking more in a section shows more (Queue, Health,
Bitrate, Language, Added and File options used to narrow each other); and
the bitrate tiers."""
import aiosqlite
import pytest
import pytest_asyncio

import backend.routes.scan as scan_route
from backend.scan_filters import FILTERS, HIGH_BITRATE, LOW_BITRATE
from backend.tests import filter_index_library as library_data

SECTIONS = {
    "queue": ["needs_conversion", "queued", "converted", "ignored"],
    "health": ["corrupt", "health_never", "health_warnings", "health_stale"],
    "bitrate": ["bitrate_low", "bitrate_medium", "bitrate_high", "bloated"],
    "language": ["unknown_language", "missing_language", "dubbed", "not_api_matched"],
    "added": ["new", "added_7d", "added_30d", "added_90d"],
    "file": ["disc_iso", "extras", "duplicates", "hardlinked"],
}


@pytest_asyncio.fixture
async def library(test_db, monkeypatch):
    monkeypatch.setattr(scan_route, "DB_PATH", test_db)
    await library_data.seed(test_db)
    return test_db


async def paths(text: str) -> set[str]:
    return set(await scan_route._paths_matching(text))


def test_the_regrouped_sections():
    for group, ids in SECTIONS.items():
        assert [FILTERS[i].group for i in ids] == [group] * len(ids), group
    # Older ids, no longer shown, keep their meaning (their own groups).
    for fid in ("low_bitrate", "high_bitrate", "recent", "large_files"):
        assert FILTERS[fid].group is None, fid


@pytest.mark.asyncio
async def test_ticking_more_in_a_section_shows_more(library):
    for group, ids in SECTIONS.items():
        first, second = await paths(ids[0]), await paths(ids[1])
        assert await paths(f"{ids[0]},{ids[1]}") == first | second, group


@pytest.mark.asyncio
async def test_the_bitrate_tiers(library):
    async with aiosqlite.connect(library) as db:
        # Files exactly on the two boundaries (bitrate = size in bytes at 8 s): Medium includes both.
        for name, bits in (("at_low", LOW_BITRATE), ("at_high", HIGH_BITRATE)):
            await db.execute("INSERT INTO scan_results (file_path, file_size, duration, scan_timestamp) "
                             "VALUES (?, ?, 8, '2026-01-01T00:00:00+00:00')", (f"/demo/{name}.mkv", bits))
        await db.commit()
        async with db.execute("SELECT file_path, file_size * 8.0 / duration FROM scan_results "
                              "WHERE removed_from_list = 0 AND duration > 0") as cur:
            rates = dict(await cur.fetchall())
    low, medium, high = await paths("bitrate_low"), await paths("bitrate_medium"), await paths("bitrate_high")
    assert low == {p for p, r in rates.items() if r < LOW_BITRATE}
    assert medium == {p for p, r in rates.items() if LOW_BITRATE <= r <= HIGH_BITRATE}
    assert high == {p for p, r in rates.items() if r > HIGH_BITRATE}
    assert low and medium and high  # the test library has each
    assert {"/demo/at_low.mkv", "/demo/at_high.mkv"} <= medium
    # Needs conversion + High is what the older High bitrate pill showed.
    assert await paths("needs_conversion,bitrate_high") == await paths("high_bitrate")
