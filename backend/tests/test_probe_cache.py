"""SC-13 (v0.10.0): a full rescan reuses an unchanged file's stored probe
instead of running ffprobe (and subtitle language detection) again."""
import json
import os
import shutil
from collections import Counter

import aiosqlite
import pytest

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")


def _library(tmp_path):
    from backend.tests.test_same_name_conversion import _mkv
    paths = []
    for name, langs in (("Movie A (2001)", ["eng", "fre"]), ("Movie B (2002)", ["jpn"])):
        folder = tmp_path / "media" / name
        folder.mkdir(parents=True)
        _mkv(folder / f"{name}.mkv", langs)
        paths.append(str(folder / f"{name}.mkv"))
    return paths


@pytest.fixture
def probes(test_db, monkeypatch):
    import backend.config
    import backend.scanner as scanner
    # scanner keeps the settings object it was imported with: point it at
    # this test's database (else stored probes are looked up in another's).
    monkeypatch.setattr(scanner, "settings", backend.config.settings)
    real, calls = scanner.probe_file, Counter()

    async def probe(path, *args, **kwargs):
        calls[str(path)] += 1
        return await real(path, *args, **kwargs)
    monkeypatch.setattr(scanner, "probe_file", probe)
    return calls


async def _scan(test_db, tmp_path, reuse):
    from backend.routes.scan import _write_batch_sync
    from backend.scanner import scan_directory
    results = await scan_directory(str(tmp_path / "media"), reuse_probes=reuse)
    _write_batch_sync(test_db, results, "2026-10-09T00:00:00")
    return {r.file_path: r for r in results}


def _same(a, b):
    keys = ("video_codec", "needs_conversion", "file_size", "duration", "audio_tracks",
            "subtitle_tracks", "native_language", "video_height", "video_width")
    return all(getattr(a, k) == getattr(b, k) for k in keys)


async def _stored(test_db, path):
    async with aiosqlite.connect(test_db) as db:
        async with db.execute("SELECT probe_json FROM scan_results WHERE file_path = ?", (path,)) as cur:
            row = await cur.fetchone()
    return json.loads(row[0]) if row and row[0] else None


@pytest.mark.asyncio
async def test_an_unchanged_file_is_not_probed_again(test_db, tmp_path, probes):
    a, b = _library(tmp_path)
    first = await _scan(test_db, tmp_path, reuse=True)
    assert probes == {a: 1, b: 1}
    stored = await _stored(test_db, a)
    assert stored["size"] == os.stat(a).st_size and stored["mtime_ns"] == os.stat(a).st_mtime_ns
    assert stored["probe"]["video_codec"] == first[a].video_codec

    second = await _scan(test_db, tmp_path, reuse=True)
    assert probes == {a: 1, b: 1}  # nothing probed
    assert all(_same(first[p], second[p]) for p in (a, b))
    assert await _stored(test_db, a) == stored  # still there for the next scan

    # A file that changed is probed; so is everything in a folder rescan.
    st = os.stat(b)
    os.utime(b, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    await _scan(test_db, tmp_path, reuse=True)
    assert probes == {a: 1, b: 2}
    await _scan(test_db, tmp_path, reuse=False)
    assert probes == {a: 2, b: 3}


@pytest.mark.asyncio
async def test_stored_probes_from_another_version_are_not_reused(test_db, tmp_path, probes, monkeypatch):
    import backend.scanner as scanner
    a, b = _library(tmp_path)
    await _scan(test_db, tmp_path, reuse=True)
    monkeypatch.setattr(scanner, "PROBE_CACHE_VERSION", scanner.PROBE_CACHE_VERSION + 1)
    await _scan(test_db, tmp_path, reuse=True)
    assert probes == {a: 2, b: 2}


@pytest.mark.asyncio
async def test_a_row_written_without_a_probe_drops_the_stored_one(test_db, tmp_path, probes):
    """The watcher writes rows without one: the file may have changed."""
    from backend.routes.scan import _write_batch_sync
    a, _ = _library(tmp_path)
    first = await _scan(test_db, tmp_path, reuse=True)
    _write_batch_sync(test_db, [first[a].model_copy(update={"probe_cache": None})], "2026-10-09T00:00:01")
    assert await _stored(test_db, a) is None
    await _scan(test_db, tmp_path, reuse=True)
    assert probes[a] == 2


def test_the_scan_process_passes_the_choice_on(test_db, tmp_path, monkeypatch):
    import backend.scanner as scanner
    from backend.routes.scan import _scan_worker_process
    seen = []

    async def fake_scan_directory(path, **kwargs):
        seen.append(kwargs.get("reuse_probes"))
        return []
    monkeypatch.setattr(scanner, "scan_directory", fake_scan_directory)
    (tmp_path / "media").mkdir()
    for reuse in (True, False):
        _scan_worker_process([str(tmp_path / "media")], test_db, str(tmp_path / "progress.json"),
                             str(tmp_path / "cancel"), reuse)
    assert seen == [True, False]
