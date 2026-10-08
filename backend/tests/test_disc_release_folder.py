"""v0.10.0: Blu-ray / DVD folder rips inside a release folder.

Radarr imports a BR-DISK as a release folder inside the movie folder:

    Rabbit Without Ears 2 (2009) [tt1343755]/
        Rabbit Without Ears 2 (2009) 1080p MLP 5.1 VC-1/
            BDMV/  CERTIFICATE/

The converted file landed in the release folder, named after it ("… 1080p MLP
5.1 VC-1 1080p Bluray AC3 5.1 h265.mkv"), CERTIFICATE was left behind, and the
Scanner's "already converted" check never matched names with [tt…] tags.
"""
from pathlib import Path

import pytest

from backend.converter import (
    _dispose_disc_source,
    build_disc_output_filename,
    disc_output_home,
)
from backend.scanner import _disc_converted_output

TITLE = "Rabbit Without Ears 2 (2009) [tt1343755]"
RELEASE = "Rabbit Without Ears 2 (2009) 1080p MLP 5.1 VC-1"
OUTPUT = "Rabbit Without Ears 2 (2009) 1080p Bluray AC3 5.1 h265.mkv"
PROBE = {
    "video_width": 1920,
    "video_height": 1080,
    "audio_tracks": [{"codec": "ac3", "channels": 6}],
}


def _bluray(root: Path) -> Path:
    """Minimal BD folder rip under `root`; returns its index.bdmv marker."""
    (root / "BDMV" / "STREAM").mkdir(parents=True)
    (root / "BDMV" / "STREAM" / "00000.m2ts").write_bytes(b"x" * 10)
    (root / "BDMV" / "index.bdmv").write_bytes(b"INDX0200")
    (root / "CERTIFICATE").mkdir()
    (root / "CERTIFICATE" / "id.bdmv").write_bytes(b"BDID")
    return root / "BDMV" / "index.bdmv"


def _dvd(root: Path) -> Path:
    (root / "VIDEO_TS").mkdir(parents=True)
    (root / "VIDEO_TS" / "VIDEO_TS.IFO").write_bytes(b"DVDVIDEO-VMG")
    (root / "AUDIO_TS").mkdir()
    return root / "VIDEO_TS" / "VIDEO_TS.IFO"


# --- where the output goes ------------------------------------------------

def test_release_folder_outputs_to_title_folder(tmp_path):
    title = tmp_path / TITLE
    _bluray(title / RELEASE)
    assert disc_output_home(title / RELEASE) == title


def test_disc_label_folder_outputs_to_title_folder(tmp_path):
    title = tmp_path / "Pure Country (1992) [tt0105191]"
    _bluray(title / "PURE_COUNTRY")
    assert disc_output_home(title / "PURE_COUNTRY") == title


def test_dvd_release_folder_outputs_to_title_folder(tmp_path):
    title = tmp_path / "Fast-Walking (1982) [tt0083930]"
    _dvd(title / "Fast-Walking (1982) DVD-R")
    assert disc_output_home(title / "Fast-Walking (1982) DVD-R") == title


def test_disc_directly_in_title_folder_stays(tmp_path):
    title = tmp_path / "Movies" / TITLE
    _bluray(title)
    assert disc_output_home(title) == title


@pytest.mark.parametrize("layout", ["no_year", "other_name", "two_discs", "extras", "video_file"])
def test_ambiguous_layouts_keep_output_next_to_the_disc(tmp_path, layout):
    parent = tmp_path / ("Alien" if layout == "no_year" else "Show (2010)")
    disc = parent / {"no_year": "Alien (1979)", "other_name": "Season 1"}.get(layout, "Show (2010) Disc 1")
    _bluray(disc)
    if layout == "two_discs":
        _bluray(parent / "Show (2010) Disc 2")  # same output name → would collide
    if layout == "extras":
        (disc / "Extras").mkdir()
    if layout == "video_file":
        (disc / "sample.m2ts").write_bytes(b"x")
    assert disc_output_home(disc) == disc


@pytest.mark.asyncio
async def test_output_named_after_the_movie_not_the_release(tmp_path):
    title = tmp_path / TITLE
    marker = _bluray(title / RELEASE)
    out = await build_disc_output_filename(str(marker), "bdmv", PROBE, encoder="nvenc")
    assert out == str(title / OUTPUT)


@pytest.mark.asyncio
async def test_never_targets_an_existing_file_in_the_title_folder(tmp_path):
    title = tmp_path / TITLE
    marker = _bluray(title / RELEASE)
    (title / OUTPUT).write_bytes(b"someone else's file")
    out = await build_disc_output_filename(str(marker), "bdmv", PROBE, encoder="nvenc")
    assert Path(out).parent == title / RELEASE
    assert not Path(out).exists()


@pytest.mark.asyncio
async def test_disc_in_title_folder_name_unchanged(tmp_path):
    title = tmp_path / "Movies" / TITLE
    marker = _bluray(title)
    out = await build_disc_output_filename(str(marker), "bdmv", PROBE, encoder="nvenc")
    assert out == str(title / OUTPUT)


# --- what happens to the disc after conversion ----------------------------

@pytest.mark.asyncio
async def test_delete_removes_the_whole_release_folder(tmp_path):
    title = tmp_path / TITLE
    marker = _bluray(title / RELEASE)
    (title / OUTPUT).write_bytes(b"converted")
    backup = await _dispose_disc_source(marker, title, "bdmv", 0, False, "")
    assert backup is None
    assert not (title / RELEASE).exists()
    assert (title / OUTPUT).exists()


@pytest.mark.asyncio
async def test_delete_removes_certificate_with_bdmv(tmp_path):
    title = tmp_path / "Movies" / TITLE
    marker = _bluray(title)
    (title / "poster.jpg").write_bytes(b"jpg")
    await _dispose_disc_source(marker, title, "bdmv", 0, False, "")
    assert sorted(p.name for p in title.iterdir()) == ["poster.jpg"]


@pytest.mark.asyncio
async def test_delete_removes_audio_ts_with_video_ts(tmp_path):
    title = tmp_path / "Movies" / "Fast-Walking (1982)"
    marker = _dvd(title)
    await _dispose_disc_source(marker, title, "dvd", 0, False, "")
    assert list(title.iterdir()) == []


@pytest.mark.asyncio
async def test_backup_moves_release_folder_beside_the_output(tmp_path):
    title = tmp_path / TITLE
    marker = _bluray(title / RELEASE)
    backup = await _dispose_disc_source(marker, title, "bdmv", 7, False, "")
    assert backup == str(title / ".shrinkerr_backup" / RELEASE)
    assert (title / ".shrinkerr_backup" / RELEASE / "CERTIFICATE" / "id.bdmv").exists()
    assert not (title / RELEASE).exists()


@pytest.mark.asyncio
async def test_backup_keeps_certificate_with_bdmv(tmp_path):
    title = tmp_path / "Movies" / TITLE
    marker = _bluray(title)
    backup = await _dispose_disc_source(marker, title, "bdmv", 7, False, "")
    assert backup == str(title / ".shrinkerr_backup" / "BDMV")
    assert sorted(p.name for p in (title / ".shrinkerr_backup").iterdir()) == ["BDMV", "CERTIFICATE"]


@pytest.mark.asyncio
async def test_trash_sends_the_release_folder(tmp_path, monkeypatch):
    import send2trash
    sent = []
    monkeypatch.setattr(send2trash, "send2trash", lambda p: sent.append(Path(p)))
    title = tmp_path / TITLE
    marker = _bluray(title / RELEASE)
    await _dispose_disc_source(marker, title, "bdmv", 0, True, "")
    assert sent == [title / RELEASE]


# --- Scanner: is this disc already converted? ------------------------------

def test_converted_output_found_in_title_folder(tmp_path):
    title = tmp_path / TITLE
    marker = _bluray(title / RELEASE)
    (title / OUTPUT).write_bytes(b"converted")
    assert _disc_converted_output(marker) == title / OUTPUT


def test_converted_output_found_despite_id_tags(tmp_path):
    title = tmp_path / "Movies" / TITLE
    marker = _bluray(title)
    (title / OUTPUT).write_bytes(b"converted")
    assert _disc_converted_output(marker) == title / OUTPUT


def test_unconverted_disc_has_no_output(tmp_path):
    title = tmp_path / TITLE
    marker = _bluray(title / RELEASE)
    (title / "Rabbit Without Ears 2 (2009) 1080p WEB-DL h264.mkv").write_bytes(b"x")
    assert _disc_converted_output(marker) is None


# --- end to end ------------------------------------------------------------

def _has_libx265() -> bool:
    import shutil
    import subprocess
    if not shutil.which("ffmpeg"):
        return False
    out = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
    return "libx265" in out


@pytest.mark.asyncio
@pytest.mark.skipif(not _has_libx265(), reason="needs ffmpeg with libx265")
async def test_dvd_release_folder_converts_into_the_movie_folder(test_db, tmp_path):
    import subprocess
    from backend.converter import convert_file

    title = tmp_path / "media" / "Fast-Walking (1982) [tt0083930]"
    marker = _dvd(title / "Fast-Walking (1982) DVD-R")
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=720x480:rate=30:duration=3",
         "-f", "lavfi", "-i", "sine=duration=3", "-c:v", "mpeg2video", "-b:v", "6M",
         "-c:a", "ac3", "-ac", "2", "-f", "vob", str(marker.parent / "VTS_01_1.VOB")],
        check=True,
    )
    result = await convert_file(str(marker), "libx265", 3.0, override_libx265_preset="ultrafast")

    assert result["success"], result.get("error")
    assert result["output_path"] == str(title / "Fast-Walking (1982) 480p DVDRip AC3 2.0 x265.mkv")
    assert [p.name for p in title.iterdir()] == ["Fast-Walking (1982) 480p DVDRip AC3 2.0 x265.mkv"]


def _has_libvmaf() -> bool:
    import shutil
    import subprocess
    if not shutil.which("ffmpeg"):
        return False
    out = subprocess.run(["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True).stdout
    return "libvmaf" in out


@pytest.mark.asyncio
@pytest.mark.skipif(not (_has_libx265() and _has_libvmaf()), reason="needs ffmpeg with libx265 and libvmaf")
async def test_disc_conversion_is_vmaf_checked(test_db, tmp_path):
    """VMAF was handed the disc's marker file (VIDEO_TS.IFO) as the reference,
    errored, and the encode was accepted unchecked (v0.10.0)."""
    import subprocess
    from backend.converter import convert_file

    title = tmp_path / "media" / "Fast-Walking (1982)"
    marker = _dvd(title)
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=720x480:rate=30:duration=3",
         "-f", "lavfi", "-i", "sine=duration=3", "-c:v", "mpeg2video", "-b:v", "6M",
         "-c:a", "ac3", "-ac", "2", "-f", "vob", str(marker.parent / "VTS_01_1.VOB")],
        check=True,
    )
    result = await convert_file(str(marker), "libx265", 3.0, override_libx265_preset="ultrafast")

    assert result["success"], result.get("error")
    assert result.get("vmaf_error") is None, result.get("vmaf_error")
    assert result.get("vmaf_score") is not None
