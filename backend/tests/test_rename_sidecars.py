"""v0.10.0: a renamed video's subtitle files go with it.

Scanner → Rename and the rename after a conversion renamed only the video:
`Movie.en.srt` kept the old name, so Plex lost it, and the Scanner row's
external subtitle paths went stale, so merging left them out. The
conversion's own codec-tag rename matched by prefix ("Saw II.en.srt" went
with "Saw.mkv") and renamed onto existing files.
"""
import json

import aiosqlite
import pytest

import backend.rename as rename_mod
import backend.routes.rename as rename_route
from backend.converter import rename_external_subtitles
from backend.scanner import move_sidecar_subtitles
from backend.tests.test_rename_folders import FOLDERS, _meta, _touch, media  # noqa: F401 (fixture)


def test_the_subtitles_named_after_the_video_move_with_it(tmp_path):
    video = _touch(tmp_path / "Saw.mkv")
    mine = [_touch(tmp_path / n) for n in ("Saw.en.srt", "Saw.forced.en.srt", "Saw.is.idx", "Saw.is.sub")]
    others = [_touch(tmp_path / n) for n in ("Saw II.en.srt", "._Saw.en.srt", "Saw.nfo")]
    video.rename(tmp_path / "Saw (2004).mkv")

    moved = move_sidecar_subtitles(str(video), str(tmp_path / "Saw (2004).mkv"))

    assert sorted(new for _, new in moved) == sorted(
        str(tmp_path / n) for n in ("Saw (2004).en.srt", "Saw (2004).forced.en.srt", "Saw (2004).is.idx",
                                    "Saw (2004).is.sub"))
    assert not any(p.exists() for p in mine) and all(p.exists() for p in others)


def test_never_onto_an_existing_file(tmp_path):
    _touch(tmp_path / "Saw.en.srt").write_bytes(b"old name")
    (tmp_path / "Saw (2004).en.srt").write_bytes(b"already there")
    assert move_sidecar_subtitles(str(tmp_path / "Saw.mkv"), str(tmp_path / "Saw (2004).mkv")) == []
    assert (tmp_path / "Saw (2004).en.srt").read_bytes() == b"already there"


def test_the_conversions_rename_too(tmp_path):
    _touch(tmp_path / "Saw x264.en.srt")
    _touch(tmp_path / "Saw x264 II.en.srt")
    (tmp_path / "Saw x265.en.srt").write_bytes(b"keep me")
    _touch(tmp_path / "Saw x264.forced.en.srt")

    rename_external_subtitles(str(tmp_path / "Saw x264.mkv"), "Saw x265")

    assert (tmp_path / "Saw x265.en.srt").read_bytes() == b"keep me"  # not replaced
    assert (tmp_path / "Saw x265.forced.en.srt").exists() and (tmp_path / "Saw x264 II.en.srt").exists()


async def _row(db_path, path, subs):
    async with aiosqlite.connect(db_path) as db:
        await db.execute("INSERT INTO scan_results (file_path, file_size, scan_timestamp, subtitle_tracks_json) "
                         "VALUES (?, 1, '2026-10-08T00:00:00', ?)", (str(path), json.dumps(subs)))
        await db.commit()


@pytest.mark.asyncio
async def test_scanner_rename_moves_them_and_the_row_follows(media, test_db, monkeypatch):  # noqa: F811
    season = media / "tv" / "show" / "season 1"
    ep = _touch(season / "Show.S01E01.mkv")
    srt = _touch(season / "Show.S01E01.en.srt")
    await _row(test_db, ep, [{"external": True, "external_path": str(srt), "language": "eng"},
                             {"stream_index": 2, "language": "eng"}])

    async def fake_resolve(fp, probe=None):
        return _meta("tv", "Show", "2019", 1, 1)

    async def folders_on():
        return FOLDERS

    monkeypatch.setattr(rename_mod, "resolve_metadata", fake_resolve)
    monkeypatch.setattr(rename_route, "get_settings", folders_on)
    res = await rename_route.apply_rename(rename_route.ApplyRequest(
        file_paths=[str(ep)], rescan_arr=False, rescan_plex=False))

    new_path = res["results"][0]["new_path"]
    new_srt = new_path[:-len(".mkv")] + ".en.srt"
    import os
    assert os.path.exists(new_srt) and not srt.exists()
    async with aiosqlite.connect(test_db) as db:
        async with db.execute("SELECT file_path, subtitle_tracks_json FROM scan_results") as cur:
            (path, subs_json), = await cur.fetchall()
    assert path == new_path
    assert json.loads(subs_json)[0]["external_path"] == new_srt


@pytest.mark.asyncio
async def test_a_renamed_folder_repoints_its_rows_subtitles(test_db):
    await _row(test_db, "/media/Old/Movie.mkv", [{"external": True, "external_path": "/media/Old/Movie.en.srt"}])
    await _row(test_db, "/media/Older/Other.mkv", [{"external": True, "external_path": "/media/Older/Other.en.srt"}])
    async with aiosqlite.connect(test_db) as db:
        await rename_route._move_rows_under(db, "/media/Old", "/media/New")
        await db.commit()
        async with db.execute("SELECT file_path, subtitle_tracks_json FROM scan_results ORDER BY file_path") as cur:
            rows = {p: json.loads(j)[0]["external_path"] for p, j in await cur.fetchall()}
    assert rows == {"/media/New/Movie.mkv": "/media/New/Movie.en.srt",
                    "/media/Older/Other.mkv": "/media/Older/Other.en.srt"}


@pytest.mark.asyncio
@pytest.mark.skipif(not __import__("shutil").which("ffmpeg"), reason="needs ffmpeg")
async def test_the_rename_after_a_conversion_moves_them_too(media, test_db, tmp_path, monkeypatch):  # noqa: F811
    import backend.converter as converter
    from backend.queue import JobQueue, QueueWorker
    from backend.tests.test_same_name_conversion import _mkv
    movie = media / "movies" / "dune" / "Dune.2021.mkv"
    movie.parent.mkdir(parents=True)
    _mkv(movie, ["eng"])
    srt = _touch(movie.parent / "Dune.2021.en.srt")
    await _row(test_db, movie, [{"external": True, "external_path": str(srt), "language": "eng"}])
    await rename_mod.save_settings({"enabled_auto": True})

    async def fake_resolve(fp, probe=None):
        return _meta("movie", "Dune", "2021")

    async def fake_convert_file(**kwargs):
        await kwargs["on_output_placed"]()
        return {"success": True, "output_path": kwargs["input_path"], "space_saved": 100, "error": None}

    monkeypatch.setattr(rename_mod, "resolve_metadata", fake_resolve)
    monkeypatch.setattr(converter, "convert_file", fake_convert_file)
    queue = JobQueue(test_db)
    job_id = await queue.add_job(str(movie), "convert", encoder="libx265")
    job = next(j for j in await queue.get_all_jobs() if j["id"] == job_id)
    await QueueWorker(test_db)._worker_task(job)

    async with aiosqlite.connect(test_db) as db:
        async with db.execute("SELECT file_path, subtitle_tracks_json FROM scan_results") as cur:
            (path, subs_json), = await cur.fetchall()
    assert path != str(movie), "the auto-rename didn't run"
    new_srt = path[:-len(".mkv")] + ".en.srt"
    import os
    assert os.path.exists(new_srt) and not srt.exists()
    assert all(t["external_path"] == new_srt for t in json.loads(subs_json or "[]") if t.get("external"))


@pytest.mark.asyncio
async def test_the_preview_lists_them(media, test_db, monkeypatch):  # noqa: F811
    """d13: the preview listed the video alone ("1 file will change") and
    the subtitle went with it anyway. It lists each one and where it goes,
    and one whose new name is taken stays."""
    season = media / "tv" / "show" / "season 1"
    ep = _touch(season / "Show.S01E01.mkv")
    _touch(season / "Show.S01E01.en.srt")
    _touch(season / "Show.S01E01.forced.en.srt")

    async def fake_resolve(fp, probe=None):
        return _meta("tv", "Show", "2019", 1, 1)

    async def folders_off():
        return rename_mod.RenameSettings()

    monkeypatch.setattr(rename_mod, "resolve_metadata", fake_resolve)
    monkeypatch.setattr(rename_route, "get_settings", folders_off)
    plan, = (await rename_route.preview_rename(rename_route.PreviewRequest(file_paths=[str(ep)])))["plans"]
    stem = plan["new_path"][:-len(".mkv")]
    _touch(season / (stem.rsplit("/", 1)[1] + ".forced.en.srt"))  # taken meanwhile
    plan, = (await rename_route.preview_rename(rename_route.PreviewRequest(file_paths=[str(ep)])))["plans"]
    assert plan["changed"] and plan["sidecars"] == [
        {"old_path": str(season / "Show.S01E01.en.srt"), "new_path": stem + ".en.srt", "free": True},
        {"old_path": str(season / "Show.S01E01.forced.en.srt"), "new_path": stem + ".forced.en.srt", "free": False},
    ]
