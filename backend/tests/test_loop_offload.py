"""Chapter 4 (0.11 "fast & steady"): slow NAS work must not run on the event
loop — one stalled mount used to freeze the whole app (requests, live
progress, and the conversion's database writes: "database is locked")."""
import asyncio
import time

import pytest


async def _max_loop_gap(coro, tick=0.01):
    """Run `coro` while measuring the longest the event loop went without
    running a 10 ms ticker; returns (result, longest gap in seconds)."""
    gaps = []
    stop = False

    async def ticker():
        last = time.monotonic()
        while not stop:
            await asyncio.sleep(tick)
            now = time.monotonic()
            gaps.append(now - last)
            last = now

    t = asyncio.create_task(ticker())
    try:
        result = await coro
    finally:
        stop = True
        await t
    return result, max(gaps or [0])


@pytest.mark.asyncio
async def test_a_stalled_mount_does_not_freeze_the_dashboard(monkeypatch, tmp_path):
    """F4: the dashboard's disk-space card stat()ed every media folder on the
    event loop, every 10 s."""
    import backend.routes.stats as stats
    stalled = {"calls": 0}

    def slow_disk_usage(path):
        stalled["calls"] += 1
        time.sleep(1.0)  # a NAS mount that hangs
        raise OSError("stalled")
    monkeypatch.setattr(stats.shutil, "disk_usage", slow_disk_usage)
    monkeypatch.setattr(stats, "_DISK_WAIT", 0.2)
    monkeypatch.setattr(stats, "_disk_cache", {"at": 0.0, "dirs": None, "info": []})
    monkeypatch.setattr(stats, "_disk_task", None)

    info, gap = await _max_loop_gap(stats._disk_info([str(tmp_path)]))
    assert info == [] and gap < 0.15            # answered after 0.2 s; the loop kept running
    # A second poll while the first is still stuck doesn't start another.
    await stats._disk_info([str(tmp_path)])
    assert stalled["calls"] == 1
    await asyncio.sleep(1.0)  # let the stuck thread finish before teardown


@pytest.mark.asyncio
async def test_disk_space_is_cached_between_polls(monkeypatch, tmp_path):
    import backend.routes.stats as stats
    calls = []
    real = stats.shutil.disk_usage
    monkeypatch.setattr(stats.shutil, "disk_usage", lambda p: calls.append(p) or real(p))
    monkeypatch.setattr(stats, "_disk_cache", {"at": 0.0, "dirs": None, "info": []})
    monkeypatch.setattr(stats, "_disk_task", None)
    first = await stats._disk_info([str(tmp_path)])
    second = await stats._disk_info([str(tmp_path)])
    assert first == second and len(first) == 1 and len(calls) == 1


@pytest.mark.asyncio
async def test_disc_languages_are_read_off_the_loop_and_cached(tmp_path, monkeypatch):
    """SC-07: a disc's IFO / mpls / ISO language parse (pycdlib, bsdtar,
    libbluray) ran on the event loop at every disc probe."""
    import shutil
    import subprocess
    if not shutil.which("ffmpeg"):
        pytest.skip("needs ffmpeg")
    import backend.disc_metadata as disc_metadata
    import backend.scanner as scanner
    from backend.tests.test_disc_release_folder import _dvd
    marker = _dvd(tmp_path / "Film (1999)")
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=720x480:rate=25:duration=2",
                    "-f", "lavfi", "-i", "sine=duration=2", "-c:v", "mpeg2video", "-c:a", "ac3",
                    "-f", "vob", str(marker.parent / "VTS_01_1.VOB")], check=True)
    calls = []

    def slow_parse(folder, kind):
        calls.append(folder)
        time.sleep(0.5)  # hundreds of playlists over a NAS
        return {"audio": ["eng"], "subtitle": []}
    monkeypatch.setattr(disc_metadata, "parse_disc_languages", slow_parse)
    monkeypatch.setattr(scanner, "_DISC_LANG_CACHE", type(scanner._DISC_LANG_CACHE)())

    probe, gap = await _max_loop_gap(scanner.probe_file(str(marker)))
    assert probe["audio_tracks"][0]["language"] == "eng"
    assert gap < 0.3                    # the 0.5 s parse didn't hold the loop
    await scanner.probe_file(str(marker))
    assert len(calls) == 1              # the second probe of the same disc is cached
