"""The VMAF target on discs (v0.10.0): samples cut from the main title.

A disc is read as one stream (concat: over a DVD's VOBs, bluray: over a
Blu-ray's playlist), so samples can't be cut by time: a DVD's timestamps
restart in every VOB (ffprobe calls three 40 s VOBs 40 s long) and a seek
past that comes back empty. They're cut by position instead
(-skip_initial_bytes), which libbluray also accepts — it moves each seek to
the nearest entry point. Checked on a real Blu-ray ISO, 30 Oct 2026.
"""
import shutil
import subprocess

import pytest

from backend import vmaf_target
from backend.vmaf_target import DISC_ALIGN, disc_sample_offsets, sample_points

_has_ffmpeg = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def test_offsets_follow_the_sample_points_and_stay_aligned():
    size = 92_737_536
    offsets = disc_sample_offsets(size, 120.0)
    assert len(offsets) == len(sample_points(120.0)) == 3
    assert all(o % DISC_ALIGN == 0 for o in offsets)
    assert offsets == sorted(offsets) and len(set(offsets)) == 3
    for off, start in zip(offsets, sample_points(120.0)):
        assert abs(off - size * start / 120.0) < DISC_ALIGN


def _vob(path, colour: str) -> None:
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=25:duration=40",
         "-vf", f"drawbox=x=0:y=0:w=iw:h=ih*3/4:color={colour}@1:t=fill", "-c:v", "mpeg2video", "-b:v", "2M", "-g", "12", "-f", "vob", str(path)],
        check=True)


def _first_frame_uv(path) -> tuple[float, float]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-f", "lavfi", "-i", f"movie={path},signalstats", "-read_intervals", "%+#1",
         "-show_entries", "frame_tags=lavfi.signalstats.UAVG,lavfi.signalstats.VAVG", "-of", "csv=p=0"],
        capture_output=True, text=True, check=True).stdout
    u, v = out.split()[0].strip(",").split(",")[:2]
    return float(u), float(v)


@pytest.mark.asyncio
@pytest.mark.skipif(not _has_ffmpeg, reason="needs ffmpeg")
async def test_a_dvds_samples_come_from_across_the_title(tmp_path):
    """Three VOBs, each starting its timestamps at 0 and mostly its own
    colour: the samples at 15, 55 and 95 s of the 120 s title come from the
    first, second and third."""
    vobs = []
    for n, colour in enumerate(("red", "green", "blue"), start=1):
        vob = tmp_path / f"VTS_01_{n}.VOB"
        _vob(vob, colour)
        vobs.append(vob)
    work = tmp_path / "work"
    work.mkdir()

    samples = await vmaf_target._cut_samples("ignored", 120.0, str(work),
                                            disc_input="concat:" + "|".join(map(str, vobs)))

    assert samples and len(samples) == 3
    for sample, vob in zip(samples, vobs):
        dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                    "-of", "csv=p=0", sample], capture_output=True, text=True).stdout)
        assert 8.0 < dur < 11.0
        u, v = _first_frame_uv(sample)
        nearest = min(vobs, key=lambda b: (lambda bu, bv: (bu - u) ** 2 + (bv - v) ** 2)(*_first_frame_uv(b)))
        assert nearest == vob, (sample, u, v)


@pytest.mark.asyncio
@pytest.mark.skipif(not _has_ffmpeg, reason="needs ffmpeg")
async def test_identical_samples_mean_the_seeks_failed(tmp_path, monkeypatch):
    """A seek that silently fails reads from the start every time: the
    samples would all be the opening logos. The search doesn't trust them."""
    vob = tmp_path / "VTS_01_1.VOB"
    _vob(vob, "red")
    work = tmp_path / "work"
    work.mkdir()

    async def tiny(_src):
        return 100  # every offset rounds down to 0
    monkeypatch.setattr(vmaf_target, "_input_size", tiny)

    assert await vmaf_target._cut_samples("ignored", 120.0, str(work), disc_input=f"concat:{vob}") is None


@pytest.mark.asyncio
async def test_a_disc_of_unknown_size_is_not_searched(tmp_path, monkeypatch):
    async def unknown(_src):
        return None
    monkeypatch.setattr(vmaf_target, "_input_size", unknown)
    assert await vmaf_target._cut_samples("ignored", 600.0, str(tmp_path), disc_input="bluray:/nowhere") is None


def _has(kind: str, name: str) -> bool:
    if not _has_ffmpeg:
        return False
    out = subprocess.run(["ffmpeg", "-hide_banner", f"-{kind}"], capture_output=True, text=True).stdout
    return name in out


def _dvd_folder(root, seconds: int = 135):
    """A VIDEO_TS rip under `root`: one continuous main title (red, then
    green, then blue thirds) split into three VOB files at sector
    boundaries, as DVD authoring splits at 1 GB. Returns its VIDEO_TS.IFO
    marker, as the scanner lists a DVD."""
    (root / "VIDEO_TS").mkdir(parents=True)
    (root / "VIDEO_TS" / "VIDEO_TS.IFO").write_bytes(b"DVDVIDEO-VMG")
    third = seconds // 3
    parts = "".join(f"[{n}:v]drawbox=x=0:y=0:w=iw:h=ih*3/4:color={c}@1:t=fill[v{n}];"
                    for n, c in enumerate(("red", "green", "blue")))
    stream = root / "title.vob"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y",
         *[a for _ in range(3) for a in ("-f", "lavfi", "-i", f"testsrc2=size=320x240:rate=25:duration={third}")],
         "-f", "lavfi", "-i", f"sine=duration={third * 3}",
         "-filter_complex", parts + "[v0][v1][v2]concat=n=3:v=1:a=0[v]", "-map", "[v]", "-map", "3:a",
         "-c:v", "mpeg2video", "-b:v", "1M", "-g", "12", "-c:a", "ac3", "-ac", "2", "-f", "vob", str(stream)],
        check=True)
    data = stream.read_bytes()
    stream.unlink()
    cut = len(data) // 3 // 2048 * 2048
    for n, chunk in enumerate((data[:cut], data[cut:2 * cut], data[2 * cut:]), start=1):
        (root / "VIDEO_TS" / f"VTS_01_{n}.VOB").write_bytes(chunk)
    return root / "VIDEO_TS" / "VIDEO_TS.IFO"


async def _target_on(test_db, score="90"):
    import aiosqlite
    async with aiosqlite.connect(test_db) as db:
        for k, v in {"vmaf_target_enabled": "true", "vmaf_target_score": score}.items():
            await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (k, v))
        await db.commit()


@pytest.mark.asyncio
@pytest.mark.skipif(not _has("encoders", "libx265"), reason="needs ffmpeg with libx265")
async def test_a_dvd_is_searched_from_its_main_title_and_says_when_it_couldnt_be(test_db, tmp_path, monkeypatch):
    from backend.converter import convert_file
    await _target_on(test_db, "93")
    marker = _dvd_folder(tmp_path / "media" / "Fast-Walking (1982)")
    vobs = sorted(marker.parent.glob("VTS_01_*.VOB"))  # before the conversion disposes of the disc
    seen = {}

    async def search(input_path, encoder, duration, target, start, convert_kwargs=None, **kw):
        seen.update(kw, duration=duration, target=target)
        return None  # e.g. a sample couldn't be cut
    monkeypatch.setattr(vmaf_target, "find_quality", search)

    result = await convert_file(str(marker), "libx265", 135.0, override_libx265_preset="ultrafast")

    assert seen["disc_input"] == "concat:" + "|".join(map(str, vobs))
    assert seen["target"] == 93.0 and seen["duration"] > 120
    assert result["success"], result.get("error")
    assert result["encoding_stats"]["vmaf_target_untested"] == 93.0
    assert result["encoding_stats"]["vmaf_target"] is None


@pytest.mark.asyncio
@pytest.mark.skipif(not _has_ffmpeg, reason="needs ffmpeg")
async def test_a_dvd_isos_samples_come_from_its_main_title(tmp_path):
    """A DVD ISO is read as byte ranges of its VOBs (dvd_iso_concat_input);
    the samples are cut by position through them."""
    import io
    pycdlib = pytest.importorskip("pycdlib")
    from backend.disc_metadata import dvd_iso_concat_input
    vobs = {}
    for n, colour in enumerate(("red", "green", "blue"), start=1):
        _vob(tmp_path / f"v{n}.vob", colour)
        vobs[f"VTS_01_{n}.VOB"] = (tmp_path / f"v{n}.vob").read_bytes()
    iso_path = tmp_path / "Movie (2005) DVD.iso"
    iso = pycdlib.PyCdlib()
    iso.new(interchange_level=1)
    iso.add_directory("/VIDEO_TS")
    for name, data in {"VIDEO_TS.IFO": b"\0" * 2048, **vobs}.items():
        iso.add_fp(io.BytesIO(data), len(data), f"/VIDEO_TS/{name};1")
    iso.write(str(iso_path))
    iso.close()
    work = tmp_path / "work"
    work.mkdir()

    samples = await vmaf_target._cut_samples(str(iso_path), 120.0, str(work), disc_input=dvd_iso_concat_input(iso_path))

    assert samples and len(samples) == 3
    refs = [tmp_path / f"v{n}.vob" for n in (1, 2, 3)]
    for sample, ref in zip(samples, refs):
        u, v = _first_frame_uv(sample)
        assert min(refs, key=lambda b: (lambda bu, bv: (bu - u) ** 2 + (bv - v) ** 2)(*_first_frame_uv(b))) == ref


@pytest.mark.asyncio
@pytest.mark.skipif(not (_has("encoders", "libx265") and _has("filters", "libvmaf")),
                    reason="needs ffmpeg with libx265 and libvmaf")
async def test_a_dvd_is_converted_at_the_quality_its_search_found(test_db, tmp_path):
    """The whole path on a synthetic DVD: samples cut from the main title,
    encoded and scored, the disc encoded at the quality found."""
    from backend.converter import convert_file
    await _target_on(test_db, "90")
    marker = _dvd_folder(tmp_path / "media" / "Fast-Walking (1982)")

    result = await convert_file(str(marker), "libx265", 135.0, override_libx265_preset="ultrafast")

    assert result["success"], result.get("error")
    found = result["encoding_stats"]["vmaf_target"]
    assert found and found["samples"] == 3 and found["rounds"] >= 1 and 14 <= found["cq"] <= 42
    assert result["encoding_stats"]["vmaf_target_untested"] is None
    assert result.get("vmaf_score") is not None  # the finished disc checked against its main title
