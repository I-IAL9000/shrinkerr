"""Image subtitles to SRT (v0.10.0): kept PGS / VobSub tracks are read with
OCR into SRT tracks in the same language when a file is converted or
cleaned up. The OCR tests use real tracks (pgs_fixture) and run where
tesseract and pgsrip (or subtile-ocr, for VobSub) are installed — CI
installs tesseract."""
import json
import os
import re
import shutil
import subprocess
import time
from types import SimpleNamespace

import aiosqlite
import pytest

from backend import image_sub_ocr
from backend.tests.pgs_fixture import make_video

ENGLISH = [(0.5, 2.5, "The weather is lovely today"), (3.0, 5.0, "I would like a cup of tea please"),
           (5.5, 7.5, "Where did you put the keys")]
PHRASES = ["weather is lovely", "cup of tea", "put the keys"]  # OCR isn't letter-perfect
FRENCH = [(0.5, 2.5, "Nous allons manger ce soir"), (3.0, 5.0, "Il fait beau aujourd hui")]


def _t(index, lang, codec="hdmv_pgs_subtitle", forced=False, title=""):
    return {"stream_index": index, "language": lang, "codec": codec, "forced": forced, "title": title}


def test_the_plan(monkeypatch):
    monkeypatch.setattr(image_sub_ocr, "tesseract_languages", lambda: frozenset({"eng", "fra", "isl", "chi_sim", "spa"}))
    tracks = [
        _t(2, "eng", title="English"),
        _t(3, "fre"), _t(6, "fre", "subrip"),     # a text track in its language already
        _t(13, "fr", forced=True),                # but not a forced one
        _t(4, "is", forced=True),                 # Icelandic, read with the isl pack
        _t(5, "chi", "dvd_subtitle"),             # VobSub can use chi_sim…
        _t(7, "chi"),                             # …pgsrip can't
        _t(8, "ger"),                             # no pack
        _t(9, "eng"),                             # removed
        _t(10, "spa"),                            # a sidecar in Spanish is merged
        _t(11, "und"),                            # unknown: English
    ]
    plan = image_sub_ocr.srt_plan(tracks, kept={2, 3, 6, 13, 4, 5, 7, 8, 10, 11},
                                  merged=[{"path": "/m/a.es.srt", "codec": "subrip", "language": "spa"}])
    assert [(p["stream_index"], p["pack"], p["forced"]) for p in plan] == [
        (2, "eng", False), (13, "fra", True), (4, "isl", True), (5, "chi_sim", False), (11, "eng", False)]
    assert plan[0]["title"] == "English" and plan[2]["language"] == "ice"
    monkeypatch.setattr(image_sub_ocr, "tesseract_languages", lambda: frozenset())
    assert image_sub_ocr.srt_plan(tracks) == []  # no tesseract


def test_the_tracks_are_titled():
    from backend.audio import build_remux_cmd
    from backend.converter import _build_ffmpeg_cmd_impl
    srt = [{"path": "/t/s2.en.srt", "codec": "subrip", "language": "eng", "forced": True, "title": "English (OCR)"}]
    convert = " ".join(_build_ffmpeg_cmd_impl("/m/in.mkv", "/m/out.mkv", encoder="libx265", external_subtitle_files=srt))
    remux = " ".join(build_remux_cmd("/m/in.mkv", "/m/out.mkv", [1], external_subtitle_files=srt))
    for cmd in (convert, remux):
        assert "-i /t/s2.en.srt" in cmd and "-metadata:s:s:0 title=English (OCR)" in cmd
        assert "-metadata:s:s:0 language=eng" in cmd and "-disposition:s:0 forced" in cmd


def test_the_work_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(image_sub_ocr.tempfile, "gettempdir", lambda: str(tmp_path))
    stale, fresh = tmp_path / "shrinkerr-ocr" / "old", tmp_path / "shrinkerr-ocr" / "running"
    stale.mkdir(parents=True)
    fresh.mkdir()
    os.utime(stale, (time.time() - 2 * 86400,) * 2)
    work = image_sub_ocr.ocr_workdir("/m/Film/film.mkv")
    (tmp_path / "shrinkerr-ocr" / os.path.basename(work) / "left.srt").write_text("x")
    assert image_sub_ocr.ocr_workdir("/m/Film/film.mkv") == work and os.listdir(work) == []  # fresh each time
    assert not stale.exists() and fresh.exists()


@pytest.mark.asyncio
async def test_remote_workers_get_the_settings(test_db, monkeypatch):
    import backend.nodes as nodes
    import backend.routes.nodes as nodes_route
    from backend.queue import JobQueue
    monkeypatch.setattr(nodes, "DB_PATH", test_db)

    async def no_token(*a, **kw):
        return None
    monkeypatch.setattr(nodes_route, "_require_node_token", no_token)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(node_manager=nodes.NodeManager())))
    await JobQueue(test_db).add_job("/m/Film/film.mkv", "convert", encoder="libx265")
    async with aiosqlite.connect(test_db) as db:
        await db.execute("INSERT INTO worker_nodes (id, name, capabilities, status, registered_at) "
                         "VALUES ('n1', 'n1', '[\"libx265\"]', 'online', 'x')")
        for k, v in (("image_subs_to_srt", "true"), ("image_subs_keep_original", "false")):
            await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (k, v))
        await db.commit()
    job = (await nodes_route.request_job(nodes_route.RequestJobBody(node_id="n1"), request))["job"]
    assert (job["image_subs_to_srt"], job["image_subs_keep_original"]) == (True, False)


@pytest.mark.asyncio
async def test_remote_workers_remux_for_it(tmp_path, monkeypatch):
    """A worker's cleanup job with nothing else to do still remuxes when
    there are image subtitles to read."""
    import backend.audio
    import backend.scanner
    from backend import worker_mode
    from backend.tests.test_worker_audio_remux import FakeClient
    monkeypatch.setattr(image_sub_ocr, "tesseract_languages", lambda: frozenset({"eng"}))
    src = tmp_path / "Film (2020).mkv"
    src.write_bytes(b"x")
    calls = []

    async def probe(path, *a, **kw):
        return {"duration": 100.0, "file_size": 1000, "video_codec": "hevc",
                "audio_tracks": [{"stream_index": 1, "language": "eng"}],
                "subtitle_tracks": [_t(2, "eng")]}

    async def remux(input_path, keep_audio_indices, **kw):
        calls.append((kw["image_subs_to_srt"], kw["keep_image_subs"]))
        return {"success": True, "output_path": input_path, "space_saved": 0}
    monkeypatch.setattr(backend.scanner, "probe_file", probe)
    monkeypatch.setattr(backend.audio, "remux_audio", remux)
    job = {"id": 7, "file_path": str(src), "job_type": "audio", "audio_tracks_to_remove": "[]",
           "subtitle_tracks_to_remove": "[]", "image_subs_to_srt": True, "image_subs_keep_original": False}
    await worker_mode.execute_job(FakeClient(), "node-1", job, ["libx265"])
    assert calls == [(True, False)]
    await worker_mode.execute_job(FakeClient(), "node-1", {**job, "image_subs_to_srt": False}, ["libx265"])
    assert len(calls) == 1  # nothing to do


# ── Real OCR ─────────────────────────────────────────────────────────────

def _has(*tools) -> bool:
    return all(shutil.which(t) for t in tools)


def _pgsrip() -> bool:
    try:
        import pgsrip  # noqa: F401
    except ImportError:
        return False
    return {"eng", "fra"} <= image_sub_ocr.tesseract_languages()


needs_ocr = pytest.mark.skipif(not (_has("ffmpeg", "mkvextract", "tesseract") and _pgsrip()),
                               reason="needs tesseract (eng, fra), pgsrip, ffmpeg and mkvtoolnix")
needs_vobsub = pytest.mark.skipif(not (_has("ffmpeg", "mkvextract", "subtile-ocr") and _pgsrip()),
                                  reason="needs subtile-ocr")


def _cues(srt_text: str) -> list[tuple[float, str]]:
    out = []
    for block in srt_text.strip().split("\n\n"):
        lines = block.strip().splitlines()
        h, m, s, ms = map(int, re.match(r"(\d+):(\d+):(\d+),(\d+)", lines[1]).groups())
        out.append((h * 3600 + m * 60 + s + ms / 1000, " ".join(lines[2:])))
    return out


def _words(text: str) -> str:
    return re.sub(r"[^a-z ]", "", text.lower())


async def _read(path, monkeypatch, tmp_path):
    from backend.scanner import probe_file
    monkeypatch.setattr(image_sub_ocr.tempfile, "gettempdir", lambda: str(tmp_path / "tmp"))
    plan = image_sub_ocr.srt_plan((await probe_file(str(path)))["subtitle_tracks"])
    return await image_sub_ocr.image_subs_to_srt(str(path), plan, image_sub_ocr.ocr_workdir(str(path)))


@needs_ocr
@pytest.mark.asyncio
async def test_pgs_tracks_are_read(tmp_path, monkeypatch):
    film = tmp_path / "Film (2020).mkv"
    make_video(film, [("eng", ENGLISH), ("fre", FRENCH)])
    out = await _read(film, monkeypatch, tmp_path)
    assert [(s["stream_index"], s["language"], s["title"]) for s in out] == [(1, "eng", "OCR"), (2, "fre", "OCR")]
    english = _cues(open(out[0]["path"], encoding="utf-8").read())
    assert [start for start, _ in english] == [0.5, 3.0, 5.5]
    assert all(phrase in _words(text) for phrase, (_, text) in zip(PHRASES, english, strict=True))
    assert "manger ce soir" in _words(open(out[1]["path"], encoding="utf-8").read())
    assert sorted(os.listdir(os.path.dirname(out[0]["path"]))) == ["s1.en.srt", "s2.fr.srt"]  # .sup files gone


@needs_vobsub
@pytest.mark.asyncio
async def test_vobsub_tracks_are_read(tmp_path, monkeypatch):
    film = tmp_path / "Film (2020).mkv"
    make_video(film, [("eng", ENGLISH)], vobsub=True)
    [out] = await _read(film, monkeypatch, tmp_path)
    english = _cues(open(out["path"], encoding="utf-8").read())
    assert all(phrase in _words(text) for phrase, (_, text) in zip(PHRASES, english, strict=True))


@needs_ocr
@pytest.mark.asyncio
async def test_detection_still_reads_them(tmp_path):
    """The language detection shares the pgsrip / subtile-ocr helpers."""
    film = tmp_path / "Film (2020).mkv"
    make_video(film, [("und", ENGLISH)])
    lang, confidence = await image_sub_ocr.detect_image_sub_language(str(film), 1, "hdmv_pgs_subtitle")
    assert lang == "eng" and confidence > 0.5


@needs_vobsub
@pytest.mark.asyncio
async def test_detection_still_reads_vobsub(tmp_path):
    film = tmp_path / "Film (2020).mkv"
    make_video(film, [("und", ENGLISH)], vobsub=True)
    lang, _ = await image_sub_ocr.detect_image_sub_language(str(film), 1, "dvd_subtitle")
    assert lang == "eng"


def _subs(path) -> list[dict]:
    return json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "s", "-show_entries",
         "stream=codec_name:stream_tags=language,title", "-of", "json", str(path)],
        capture_output=True, text=True, check=True).stdout)["streams"]


async def _settings(db_path, **values):
    async with aiosqlite.connect(db_path) as db:
        for k, v in values.items():
            await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (k, v))
        await db.commit()


def _libx265() -> bool:
    return _has("ffmpeg") and "libx265" in subprocess.run(
        ["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True).stdout


@needs_ocr
@pytest.mark.skipif(not _libx265(), reason="needs libx265")
@pytest.mark.parametrize("keep", [True, False])
@pytest.mark.asyncio
async def test_a_conversion_adds_srt_tracks(test_db, tmp_path, monkeypatch, keep):
    from backend.converter import convert_file
    monkeypatch.setattr(image_sub_ocr.tempfile, "gettempdir", lambda: str(tmp_path / "tmp"))
    film = tmp_path / "Film (2020).mkv"
    make_video(film, [("eng", ENGLISH), ("fre", FRENCH)])
    await _settings(test_db, image_subs_to_srt="true", image_subs_keep_original=str(keep).lower())
    result = await convert_file(str(film), "libx265", 2.0, override_libx265_preset="ultrafast")
    assert result["success"], result.get("error")
    subs = _subs(result["output_path"])
    images = [("hdmv_pgs_subtitle", "eng", None), ("hdmv_pgs_subtitle", "fre", None)] if keep else []
    assert [(s["codec_name"], s["tags"]["language"], s["tags"].get("title")) for s in subs] == [
        *images, ("subrip", "eng", "OCR"), ("subrip", "fre", "OCR")]
    srt = subprocess.run(["ffmpeg", "-v", "error", "-i", result["output_path"], "-map", f"0:s:{len(images)}", "-f", "srt", "-"],
                         capture_output=True, text=True, check=True).stdout
    assert "cup of tea" in _words(srt)
    assert os.listdir(tmp_path / "tmp" / "shrinkerr-ocr") == []  # its work folder is gone


@needs_ocr
@pytest.mark.asyncio
async def test_a_cleanup_job_replaces_them(test_db, tmp_path, monkeypatch):
    """A file with nothing else to do is queued (Scanner → Image subtitles →
    Add to Queue) as a cleanup: the queue remuxes it for the OCR alone, and
    without "keep the image subtitles too" the SRT tracks replace them."""
    import sys
    from backend.queue import JobQueue, QueueWorker
    for name, module in list(sys.modules.items()):
        if name.startswith("backend.") and isinstance(getattr(module, "DB_PATH", None), str):
            monkeypatch.setattr(module, "DB_PATH", test_db)
    monkeypatch.setattr(image_sub_ocr.tempfile, "gettempdir", lambda: str(tmp_path / "tmp"))
    film = tmp_path / "Film (2020).mkv"
    make_video(film, [("eng", ENGLISH)])
    await _settings(test_db, image_subs_to_srt="true", image_subs_keep_original="false",
                    backup_original_days="0", trash_original_after_conversion="false")
    async with aiosqlite.connect(test_db) as db:
        await db.execute("INSERT INTO scan_results (file_path, file_size, video_codec, needs_conversion, "
                         "audio_tracks_json, scan_timestamp) VALUES (?, ?, 'hevc', 0, '[]', 'x')",
                         (str(film), film.stat().st_size))
        await db.commit()
    queue = JobQueue(test_db)
    job_id = await queue.add_job(str(film), "audio")
    async with aiosqlite.connect(test_db) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)) as cur:
            job = dict(await cur.fetchone())
    await QueueWorker(test_db)._process_job(job)
    async with aiosqlite.connect(test_db) as db:
        async with db.execute("SELECT status, error_log FROM jobs WHERE id = ?", (job_id,)) as cur:
            status, error = await cur.fetchone()
    assert status == "completed", error
    assert [(s["codec_name"], s["tags"].get("title")) for s in _subs(film)] == [("subrip", "OCR")]
