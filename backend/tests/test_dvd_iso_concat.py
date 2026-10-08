"""v0.9.152: DVD ISOs read via the main title's VOB extents, not dvdvideo.

ffmpeg's dvdvideo demuxer failed on real ISOs: one opens title 1, which was
only padding cells; another has a damaged UDF tree, and libdvdread reads
UDF only ("Unable to open the VMG"). The ISO 9660 directory still locates
every VOB as a contiguous extent, so the main title set is read the same
way folder DVDs are — a concat: over the VOBs — via subfile: byte ranges.
"""
import io
import shutil
import subprocess
from pathlib import Path

import pytest

from backend.disc_metadata import dvd_iso_concat_input


def _vob(tmp: Path, name: str, seconds: int) -> bytes:
    out = tmp / name
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=size=320x240:rate=25:duration={seconds}",
         "-f", "lavfi", "-i", f"sine=duration={seconds}", "-c:v", "mpeg2video", "-c:a", "ac3",
         "-f", "vob", str(out)],
        check=True,
    )
    return out.read_bytes()


def _make_iso(path: Path, files: dict[str, bytes]) -> None:
    import pycdlib
    iso = pycdlib.PyCdlib()
    iso.new(interchange_level=1)
    iso.add_directory("/VIDEO_TS")
    for name, data in files.items():
        iso.add_fp(io.BytesIO(data), len(data), f"/VIDEO_TS/{name};1")
    iso.write(str(path))
    iso.close()


@pytest.mark.skipif(not shutil.which("ffprobe"), reason="needs ffmpeg")
def test_main_title_vobs_read_straight_from_the_iso(tmp_path):
    main1 = _vob(tmp_path, "m1.vob", 3)
    main2 = _vob(tmp_path, "m2.vob", 3)
    iso = tmp_path / "Movie (2005) DVD.iso"
    _make_iso(iso, {
        "VIDEO_TS.IFO": b"\0" * 2048,
        "VTS_01_0.VOB": main1,            # menu chunk — never part of the feature
        "VTS_01_1.VOB": main1,            # main feature, split across two VOBs
        "VTS_01_2.VOB": main2,
        "VTS_02_1.VOB": main1[: len(main1) // 2],  # smaller extra
    })

    src = dvd_iso_concat_input(iso)

    assert src.startswith("concat:")
    parts = src[len("concat:"):].split("|")
    assert len(parts) == 2 and all(p.startswith("subfile,,start,") and p.endswith(f",,:{iso}") for p in parts)
    data = iso.read_bytes()
    for part, expected in zip(parts, (main1, main2)):
        start = int(part.split(",")[3]); end = int(part.split(",")[5])
        assert data[start:end] == expected

    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type", "-of", "csv=p=0",
                        "-i", src], capture_output=True, text=True)
    assert r.returncode == 0 and "video" in r.stdout and "audio" in r.stdout


def test_iso_without_vobs_returns_none(tmp_path):
    iso = tmp_path / "data.iso"
    _make_iso(iso, {"VIDEO_TS.IFO": b"\0" * 2048})
    assert dvd_iso_concat_input(iso) is None
