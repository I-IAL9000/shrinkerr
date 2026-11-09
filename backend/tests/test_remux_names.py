"""A disc rip a clean-up remuxes to MKV is named for what it is (v0.10.0):
"UFO Abduction (1989) BR-DISK … .m2ts" came out as "… BR-DISK … .mkv", and
Radarr took the MKV for a full disc. Its streams are untouched, so it's a
Bluray Remux; a conversion's is a Bluray (rename_source_quality_in_filename).
The remux never replaces a file that already has its output's name."""
import shutil
import subprocess

import pytest

from backend.converter import remux_output_stem

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")


def test_a_bluray_disc_becomes_a_bluray_remux():
    assert remux_output_stem("UFO Abduction (1989) BR-DISK DTS-HD MA 2.0 x264-LsNM", 1920, 1080) == \
        "UFO Abduction (1989) 1080p Bluray Remux DTS-HD MA 2.0 x264-LsNM"
    assert remux_output_stem("Movie (2009) 1080p BR-DISK", 1920, 1080) == "Movie (2009) 1080p Bluray Remux"
    assert remux_output_stem("Movie (2009) BD50 AVC", 3840, 1600) == "Movie (2009) 2160p Bluray Remux AVC"
    assert remux_output_stem("Movie.2009.BR-DISK.AVC-GRP", 1920, 1080) == "Movie.2009.1080p.Bluray.Remux.AVC-GRP"
    assert remux_output_stem("Movie (2009) BR-DISK", None, None) == "Movie (2009) Bluray Remux"


def test_a_dvd_becomes_a_dvd_remux():
    assert remux_output_stem("Movie (1988) DVD-R AC3 2.0", 720, 576) == "Movie (1988) 576p DVD Remux AC3 2.0"


def test_other_names_stay():
    for stem in ("Movie (2009) Bluray-1080p", "Movie (2009) WEBDL-1080p x264", "Show - S01E01 HDTV-720p"):
        assert remux_output_stem(stem, 1920, 1080) == stem


def _m2ts(path):
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=25:duration=2",
                    "-f", "lavfi", "-i", "sine=duration=2", "-c:v", "mpeg2video", "-c:a", "mp2", str(path)],
                   check=True)


@needs_ffmpeg
@pytest.mark.asyncio
async def test_the_remux_names_its_mkv_a_remux(test_db, tmp_path):
    from backend.audio import remux_audio
    src = tmp_path / "Film (1989) BR-DISK x264.m2ts"
    _m2ts(src)
    result = await remux_audio(input_path=str(src), keep_audio_indices=[1], duration=2)
    assert result["success"], result.get("error")
    assert result["output_path"] == str(tmp_path / "Film (1989) 480p Bluray Remux x264.mkv")
    assert sorted(p.name for p in tmp_path.iterdir() if p.suffix in (".mkv", ".m2ts")) == [
        "Film (1989) 480p Bluray Remux x264.mkv"]


@needs_ffmpeg
@pytest.mark.asyncio
async def test_an_mkv_cleaned_up_in_place_keeps_its_name(test_db, tmp_path):
    from backend.audio import remux_audio
    from backend.tests.test_same_name_conversion import _mkv
    src = tmp_path / "Film (1989) BR-DISK x264.mkv"
    _mkv(src, ["eng", "fre"])
    result = await remux_audio(input_path=str(src), keep_audio_indices=[1], duration=2)
    assert result["success"], result.get("error")
    assert result["output_path"] == str(src)


@needs_ffmpeg
@pytest.mark.asyncio
async def test_the_remux_never_replaces_a_file_with_its_name(test_db, tmp_path):
    from backend.audio import remux_audio
    src = tmp_path / "Film (1989).m2ts"
    _m2ts(src)
    taken = tmp_path / "Film (1989).mkv"
    taken.write_bytes(b"another film")
    result = await remux_audio(input_path=str(src), keep_audio_indices=[1], duration=2)
    assert (result["success"], result.get("error_key")) == (False, "errors.outputExists")
    assert taken.read_bytes() == b"another film" and src.exists()
    assert sorted(p.name for p in tmp_path.iterdir() if p.suffix in (".mkv", ".m2ts")) == [
        "Film (1989).m2ts", "Film (1989).mkv"]
