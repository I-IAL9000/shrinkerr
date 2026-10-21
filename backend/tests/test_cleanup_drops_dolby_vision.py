"""A clean-up drops Dolby Vision under Settings → Video → Dolby Vision →
"Convert to HDR10" (v0.10.0), with no re-encode. Plex on the Apple TV
wouldn't play files with Dolby Vision and HDR10+, but played HDR10+ alone:
filtered to Dolby Vision in the Scanner, Add to Queue's Cleanup only now
removes it, and the HDR10 and HDR10+ under it are copied as they are."""
import hashlib
import json
import shutil
import subprocess

import aiosqlite
import pytest
import pytest_asyncio

from backend import dovi
from backend.audio import build_remux_cmd


def test_what_a_cleanup_does_with_dolby_vision(monkeypatch):
    monkeypatch.setattr(dovi, "can_strip", lambda: True)
    monkeypatch.setattr(dovi, "_mode", "hdr10")
    assert [dovi.remux_outcome(f) for f in ("dv8", "dv7", "dv5", "hdr10", None)] == \
        ["removed", "removed", "profile5", None, None]
    for mode in ("keep", "keep_or_hdr10"):  # a remux can always keep it
        monkeypatch.setattr(dovi, "_mode", mode)
        assert dovi.remux_outcome("dv8") == "kept"
    monkeypatch.setattr(dovi, "_mode", "hdr10")
    monkeypatch.setattr(dovi, "can_strip", lambda: False)  # ffmpeg before 7.1
    assert dovi.remux_outcome("dv8") == "ffmpeg"


def test_the_job_drops_what_has_hdr10_under_it(monkeypatch):
    monkeypatch.setattr(dovi, "can_strip", lambda: True)
    assert dovi.remux_drop({"hdr_format": "dv8", "dv_base": "hdr10"}, "hdr10") == "hdr10"
    assert dovi.remux_drop({"hdr_format": "dv8", "dv_base": "hlg"}, "hdr10") == "hlg"
    assert dovi.remux_drop({"hdr_format": "dv5", "dv_base": None}, "hdr10") is None
    assert dovi.remux_drop({"hdr_format": "dv8", "dv_base": "hdr10"}, "keep_or_hdr10") is None
    assert dovi.remux_drop({"hdr_format": "dv8", "dv_base": "hdr10"}, None) is None
    monkeypatch.setattr(dovi, "can_strip", lambda: False)
    assert dovi.remux_drop({"hdr_format": "dv8", "dv_base": "hdr10"}, "hdr10") is None


def test_the_remux_command():
    plain = build_remux_cmd("/m/F.mkv", "/m/F.remuxing.mkv", [1])
    dropped = build_remux_cmd("/m/F.mkv", "/m/F.remuxing.mkv", [1], drop_dolby_vision=True)
    assert "-bsf:v" not in plain
    assert dropped[dropped.index("-bsf:v") + 1] == "dovi_rpu=strip=1,filter_units=remove_types=63"
    assert dropped[-1] == "/m/F.remuxing.mkv" and "copy" in dropped


# --- the real remux: a synthetic HDR10 encode with an HDR10+ SEI before each
# picture, and Dolby Vision NAL units after it (an enhancement layer, type 63,
# and an RPU, type 62 — stand-ins: their bytes aren't parsed)

def _nals(data: bytes) -> list[bytes]:
    starts, i = [], 0
    while (j := data.find(b"\x00\x00\x01", i)) >= 0:
        starts.append(j + 3)
        i = j + 3
    return [data[s:(starts[k + 1] - 3 if k + 1 < len(starts) else len(data))].rstrip(b"\x00")
            for k, s in enumerate(starts)]


def _epb(payload: bytes) -> bytes:
    out, zeros = bytearray(), 0
    for b in payload:
        if zeros >= 2 and b <= 3:
            out.append(3)
            zeros = 0
        out.append(b)
        zeros = zeros + 1 if b == 0 else 0
    return bytes(out)


def _hdr10plus_sei() -> bytes:
    fields = [(1, 8), (1, 2), (400, 27), (0, 1),  # version, one window, target display 400 nits, no peak flag
              (40000, 17), (30000, 17), (20000, 17), (5000, 17),  # maxscl, average maxrgb
              (3, 4), (1, 7), (100, 17), (50, 7), (4000, 17), (99, 7), (30000, 17),  # percentiles
              (10, 10), (0, 1), (0, 1), (0, 1)]  # bright pixels; no peak, tone mapping or saturation
    value, width = 0, 0
    for v, w in fields:
        value, width = (value << w) | v, width + w
    pad = -width % 8
    body = (value << pad).to_bytes((width + pad) // 8, "big")
    t35 = bytes([0xB5, 0x00, 0x3C, 0x00, 0x01, 0x04]) + body  # US, Samsung, ST 2094-40
    return b"\x4e\x01" + _epb(bytes([4, len(t35)]) + t35 + b"\x80")  # prefix SEI, payload type 4


HDR10PLUS_ID = bytes([0xB5, 0x00, 0x3C, 0x00, 0x01, 0x04])
EL = b"\x7e\x01" + _epb(b"\x40\x01\x0c\x01\xff\xff\x80")
RPU = b"\x7c\x01" + _epb(b"\x19\x08\x09\x00\x00\x00\x00\x00\x12\x34\x56\x78\x80")


def _dv_hdr10plus_clip(path):
    base = path.with_suffix(".base.hevc")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=256x144:r=24:d=0.5",
                    "-pix_fmt", "yuv420p10le", "-c:v", "libx265", "-preset", "ultrafast", "-x265-params",
                    "log-level=error:bframes=0:hdr10=1:master-display=G(13250,34500)B(7500,3000)R(34000,16000)"
                    "WP(15635,16450)L(10000000,1):max-cll=1000,400:colorprim=bt2020:transfer=smpte2084:"
                    "colormatrix=bt2020nc:repeat-headers=1", "-f", "hevc", str(base)], check=True)
    out = bytearray()
    for nal in _nals(base.read_bytes()):
        vcl = (nal[0] >> 1) & 0x3F < 32
        if vcl and nal[2] & 0x80:  # a picture's first slice
            out += b"\x00\x00\x00\x01" + _hdr10plus_sei()
        out += b"\x00\x00\x00\x01" + nal
        if vcl:
            out += b"\x00\x00\x00\x01" + EL + b"\x00\x00\x00\x01" + RPU
    stream = path.with_suffix(".hevc")
    stream.write_bytes(out)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-fflags", "+genpts", "-r", "24", "-f", "hevc", "-i", str(stream),
                    "-c", "copy", str(path)], check=True)
    base.unlink()
    stream.unlink()


def _units(path) -> tuple[dict, str]:
    """NAL units by type, HDR10+ SEIs as "hdr10+", and the pictures' hash."""
    data = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-c", "copy", "-bsf:v", "hevc_mp4toannexb",
                           "-f", "hevc", "-"], capture_output=True, check=True).stdout
    counts: dict = {}
    pictures = hashlib.md5()
    for nal in _nals(data):
        kind = (nal[0] >> 1) & 0x3F
        if kind == 39 and HDR10PLUS_ID in nal:
            kind = "hdr10+"
        counts[kind] = counts.get(kind, 0) + 1
        if isinstance(kind, int) and kind < 32:
            pictures.update(nal)
    return counts, pictures.hexdigest()


def _frame_side_data(path) -> set:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-read_intervals", "%+#1",
                          "-show_frames", "-show_entries", "frame=side_data_list", "-of", "json", str(path)],
                         capture_output=True, text=True, check=True).stdout
    return {s.get("side_data_type") for f in json.loads(out)["frames"] for s in f.get("side_data_list") or []}


@pytest.mark.skipif(not (shutil.which("ffmpeg") and dovi.can_strip()), reason="needs ffmpeg 7.1+ (dovi_rpu)")
@pytest.mark.asyncio
async def test_only_the_dolby_vision_goes(test_db, tmp_path):
    from backend.audio import remux_audio
    src = tmp_path / "Film (2024) 2160p WEB-DL DV HDR10+.mkv"
    _dv_hdr10plus_clip(src)
    before, pictures = _units(src)
    assert (before[62], before[63], before["hdr10+"]) == (12, 12, 12)

    result = await remux_audio(str(src), [], duration=0.5, drop_dolby_vision=True)

    assert result["success"], result.get("error")
    after, same_pictures = _units(result["output_path"])
    assert 62 not in after and 63 not in after
    assert after["hdr10+"] == 12 and same_pictures == pictures  # copied, not re-encoded
    assert {"HDR Dynamic Metadata SMPTE2094-40 (HDR10+)", "Mastering display metadata"} <= \
        _frame_side_data(result["output_path"])


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
@pytest.mark.asyncio
async def test_a_remux_still_carrying_it_keeps_the_original(test_db, tmp_path, monkeypatch):
    from backend.audio import remux_audio
    from backend.tests.test_remux_guards import _clip
    src = _clip(tmp_path)
    before = src.read_bytes()

    async def still_there(path):
        return True
    monkeypatch.setattr(dovi, "left_in", still_there)
    monkeypatch.setattr(dovi, "REMUX_STRIP_ARGS", [])  # any ffmpeg

    result = await remux_audio(str(src), [1], duration=3.0, drop_dolby_vision=True)

    assert (result["success"], result["error_key"]) == (False, "errors.dolbyVisionNotDropped")
    assert src.read_bytes() == before and not list(tmp_path.glob("*.remuxing.mkv"))


# --- Add to Queue: Cleanup only on a Dolby Vision filter

FILES = {  # file: hdr_format — none has a track to remove
    "/m/Dv8 (2024)/Dv8.mkv": "dv8",
    "/m/Dv5 (2024)/Dv5.mkv": "dv5",
    "/m/Hdr (2024)/Hdr.mkv": "hdr10",
}


@pytest_asyncio.fixture
async def library(test_db, monkeypatch):
    import sys
    from backend.queue import JobQueue, QueueWorker
    from backend.routes.jobs import init_job_routes
    for name, module in list(sys.modules.items()):
        if name.startswith("backend.") and isinstance(getattr(module, "DB_PATH", None), str):
            monkeypatch.setattr(module, "DB_PATH", test_db)
    worker = QueueWorker(test_db)
    monkeypatch.setattr(worker, "start", lambda *a, **kw: None)
    init_job_routes(worker, JobQueue(test_db))
    monkeypatch.setattr(dovi, "can_strip", lambda: True)
    aac = json.dumps([{"stream_index": 1, "language": "eng", "codec": "aac", "channels": 2}])
    async with aiosqlite.connect(test_db) as db:
        for path, hdr in FILES.items():
            await db.execute(
                "INSERT INTO scan_results (file_path, file_size, needs_conversion, video_codec, hdr_format, "
                "audio_tracks_json, subtitle_tracks_json, scan_timestamp) VALUES (?, 1, 0, 'hevc', ?, ?, '[]', 't')",
                (path, hdr, aac))
        await db.commit()
    return test_db


async def _queued(db_path) -> dict:
    from backend.routes.jobs import BulkQueueFromScanRequest, add_jobs_from_scan
    await add_jobs_from_scan(BulkQueueFromScanRequest(file_paths=list(FILES), cleanup_only=True))
    async with aiosqlite.connect(db_path) as db:
        async with db.execute("SELECT file_path, job_type FROM jobs") as cur:
            return {p.rsplit("/", 1)[1]: t for p, t in await cur.fetchall()}


@pytest.mark.asyncio
async def test_cleanup_only_removes_it_under_convert_to_hdr10(library, monkeypatch):
    from backend.routes.jobs import EstimateRequest, estimate_jobs
    monkeypatch.setattr(dovi, "_mode", "hdr10")
    estimate = await estimate_jobs(EstimateRequest(file_paths=list(FILES), cleanup_only=True))
    assert (estimate["total_files"], estimate["by_type"]["audio"]) == (1, 1)
    assert estimate["dolby_vision"] == {"removed": 1, "kept": 0, "ffmpeg": 0, "profile5": 1}
    assert await _queued(library) == {"Dv8.mkv": "audio"}


@pytest.mark.asyncio
async def test_kept_by_the_setting_its_left_out(library, monkeypatch):
    from backend.routes.jobs import EstimateRequest, estimate_jobs
    monkeypatch.setattr(dovi, "_mode", "keep")
    estimate = await estimate_jobs(EstimateRequest(file_paths=list(FILES), cleanup_only=True))
    assert estimate["total_files"] == 0
    assert estimate["dolby_vision"] == {"removed": 0, "kept": 1, "ffmpeg": 0, "profile5": 1}
    assert await _queued(library) == {}


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
@pytest.mark.asyncio
async def test_the_cleanup_job_drops_it_and_the_scanner_says_hdr10(test_db, tmp_path, monkeypatch):
    import sys

    import backend.audio
    import backend.scanner
    from backend.queue import JobQueue, QueueWorker
    from backend.tests.test_remux_guards import _clip
    for name, module in list(sys.modules.items()):
        if name.startswith("backend.") and isinstance(getattr(module, "DB_PATH", None), str):
            monkeypatch.setattr(module, "DB_PATH", test_db)
    monkeypatch.setattr(dovi, "can_strip", lambda: True)
    src = _clip(tmp_path)
    tracks = json.dumps([{"stream_index": 1, "language": "ger", "codec": "aac", "keep": True},
                         {"stream_index": 2, "language": "rus", "codec": "aac", "keep": True}])
    async with aiosqlite.connect(test_db) as db:
        await db.execute("INSERT INTO scan_results (file_path, file_size, needs_conversion, video_codec, hdr_format, "
                         "audio_tracks_json, subtitle_tracks_json, scan_timestamp) VALUES (?, 1, 0, 'hevc', 'dv8', ?, "
                         "'[]', 't')", (str(src), tracks))
        await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('dolby_vision_mode', 'hdr10')")
        await db.commit()
    real_probe, probes = backend.scanner.probe_file, []

    async def probe(path, *args, **kwargs):  # Dolby Vision before the remux, HDR10 after
        found = await real_probe(path, *args, **kwargs)
        probes.append(path)
        return {**found, "hdr_format": "dv8" if len(probes) == 1 else "hdr10", "dv_base": "hdr10"}
    monkeypatch.setattr(backend.scanner, "probe_file", probe)
    remuxes = []

    async def remux(input_path, keep_audio_indices, **kwargs):
        remuxes.append(kwargs)
        return {"success": True, "output_path": input_path, "space_saved": 0}
    monkeypatch.setattr(backend.audio, "remux_audio", remux)
    job_id = await JobQueue(test_db).add_job(str(src), "audio")
    async with aiosqlite.connect(test_db) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)) as cur:
            job = dict(await cur.fetchone())

    await QueueWorker(test_db)._process_job(job)

    assert [r["drop_dolby_vision"] for r in remuxes] == [True]
    async with aiosqlite.connect(test_db) as db:
        async with db.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)) as cur:
            assert (await cur.fetchone())[0] == "completed"
        async with db.execute("SELECT hdr_format FROM scan_results") as cur:
            assert (await cur.fetchone())[0] == "hdr10"  # out of the Dolby Vision filter
