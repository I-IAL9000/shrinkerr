"""F18 (v0.10.0): temp outputs and AppleDouble files are kept out of
scan_results when written, instead of filtered from every list query by
three `NOT LIKE '%...%'` tests."""
import aiosqlite
import pytest

TEMP = ["/m/A/a.converting.mkv", "/m/A/a.remuxing.mkv", "/m/A/._a.mkv", "/m/._B/b.mkv"]


def test_is_temp_path():
    from backend.database import is_temp_path
    assert all(is_temp_path(p) for p in TEMP)
    assert not any(is_temp_path(p) for p in ["/m/A/a.mkv", "/m/A.B/a_b.mkv", "/m/Converting/a.mkv"])


async def _paths(db_path):
    async with aiosqlite.connect(db_path) as db:
        async with db.execute("SELECT file_path FROM scan_results ORDER BY file_path") as cur:
            return [r[0] for r in await cur.fetchall()]


@pytest.mark.asyncio
async def test_the_scan_writer_never_stores_temp_files(test_db):
    from backend.models import ScannedFile
    from backend.routes.scan import _write_batch_sync

    def scanned(path):
        return ScannedFile(
            file_path=path, file_name=path.rsplit("/", 1)[1], folder_name="A",
            file_size=1, file_size_gb=0.0, video_codec="h264", needs_conversion=True,
            audio_tracks=[], native_language="eng", has_removable_tracks=False,
            estimated_savings_bytes=0, estimated_savings_gb=0.0,
        )

    _write_batch_sync(test_db, [scanned(p) for p in TEMP + ["/m/A/a.mkv"]], "2026-10-09T00:00:00")
    assert await _paths(test_db) == ["/m/A/a.mkv"]


@pytest.mark.asyncio
async def test_existing_temp_rows_are_removed_once(test_db):
    from backend.database import remove_temp_scan_rows
    async with aiosqlite.connect(test_db) as db:
        await db.executemany(
            "INSERT INTO scan_results (file_path, file_size, scan_timestamp) VALUES (?, 1, '2026-01-01')",
            [(p,) for p in TEMP + ["/m/A/a.mkv"]])
        await db.commit()
    assert await remove_temp_scan_rows() == len(TEMP)
    assert await _paths(test_db) == ["/m/A/a.mkv"]
    # Sentinel: not run again.
    async with aiosqlite.connect(test_db) as db:
        await db.execute(
            "INSERT INTO scan_results (file_path, file_size, scan_timestamp) VALUES (?, 1, '2026-01-01')", (TEMP[0],))
        await db.commit()
    assert await remove_temp_scan_rows() == 0


def test_list_queries_have_no_contains_filters():
    from backend.routes.scan import _SCAN_WHERE, _SCAN_WHERE_IN_FOLDERS
    assert "LIKE" not in _SCAN_WHERE and "LIKE" not in _SCAN_WHERE_IN_FOLDERS
