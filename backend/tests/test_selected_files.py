"""The files a Scanner selection stands for (v0.10.0). Trash, Remove and
Un-ignore read only the files loaded in the page, so titles selected in
the poster grid (their folder paths) — never opened — did nothing."""
import aiosqlite
import pytest
import pytest_asyncio

FILES = ["/m/Alpha (2001)/Alpha 1080p.mkv", "/m/Alpha (2001)/Alpha 2160p.mkv",
         "/m/Beta (2002)/Beta.mkv", "/m/Gamma (2003)/Gamma.mkv"]


@pytest_asyncio.fixture
async def library(test_db, monkeypatch):
    import sys
    for name, module in list(sys.modules.items()):
        if name.startswith("backend.") and isinstance(getattr(module, "DB_PATH", None), str):
            monkeypatch.setattr(module, "DB_PATH", test_db)
    async with aiosqlite.connect(test_db) as db:
        await db.executemany("INSERT INTO scan_results (file_path, file_size, video_codec, needs_conversion, "
                             "scan_timestamp) VALUES (?, 1, ?, ?, 't')",
                             [(p, "hevc" if "2160p" in p else "h264", 0 if "2160p" in p else 1) for p in FILES])
        await db.execute("INSERT INTO ignored_files (file_path, reason, ignored_at) VALUES ('/m/Beta (2002)/', 'manual', 't')")
        await db.commit()
    return test_db


async def _files(**request):
    from backend.routes.scan import SelectedFilesRequest, selected_files
    return {f["file_path"]: f["ignored"] for f in await selected_files(SelectedFilesRequest(**request))}


@pytest.mark.asyncio
async def test_selected_titles_stand_for_their_files(library):
    assert await _files(paths=["/m/Alpha (2001)/", "/m/Beta (2002)/"]) == {
        FILES[0]: False, FILES[1]: False, FILES[2]: True}  # Beta's folder is ignored


@pytest.mark.asyncio
async def test_through_the_filter_and_with_files_selected_on_their_own(library):
    # Alpha's 2160p copy doesn't need converting; Gamma was picked by itself.
    assert await _files(paths=["/m/Alpha (2001)/", FILES[3]], filter="needs_conversion") == {
        FILES[0]: False, FILES[3]: False}


@pytest.mark.asyncio
async def test_select_all_is_the_filtered_list(library):
    # (ignored Beta doesn't count as needing conversion)
    assert set(await _files(select_all=True, filter="needs_conversion")) == {FILES[0], FILES[3]}
    assert set(await _files(select_all=True)) == set(FILES)
