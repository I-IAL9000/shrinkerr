"""A resized encode is scored at its own size (v0.10.0). Every VMAF
comparison scaled the encode up to the source's size, so the resolution a
rule drops was counted as the encode's loss: Stolen Kisses, a 4K film
converted to 1080p, scored 68.8 after its conversion, and the VMAF target's
samples scored too low at every quality to be trusted — the search gave up
and the film was encoded at the rule's quality. The reference is brought
down to the encode's size instead, as ab-av1 scores a resized encode."""
import shutil
import subprocess

import pytest

from backend import vmaf_target
from backend.converter import _vmaf_pipelines


def test_the_reference_is_brought_to_the_encodes_size():
    src = {"width": 3840, "height": 2160, "fps": 24.0}
    ref, dist, _ = _vmaf_pipelines(src, {"width": 1920, "height": 1080, "fps": 24.0})
    assert "scale=1920:1080:flags=bicubic" in ref and "1920:1080" not in dist
    # The same size, or a size unknown: neither is scaled (scale2ref pairs them).
    for dst in ({"width": 3840, "height": 2160, "fps": 24.0}, {"width": 0, "height": 0, "fps": 24.0}):
        ref, dist, _ = _vmaf_pipelines(src, dst)
        assert ref == dist


def _has_libvmaf() -> bool:
    if not shutil.which("ffmpeg"):
        return False
    out = subprocess.run(["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True).stdout
    return " libvmaf " in out


@pytest.mark.asyncio
@pytest.mark.skipif(not _has_libvmaf(), reason="needs ffmpeg with libvmaf")
async def test_a_lossless_resized_encode_scores_as_one(tmp_path):
    """Half the source's size, losslessly: the encode lost nothing but the
    size it was asked to drop. Scaled up to the source's, it scored 86."""
    source, encoded = tmp_path / "source.mkv", tmp_path / "encoded.mkv"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    "testsrc2=s=1708x960:r=24:d=3,noise=alls=12:allf=t", "-c:v", "libx264", "-qp", "0",
                    "-preset", "ultrafast", str(source)], check=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(source), "-vf", "scale=854:-2",
                    "-c:v", "libx264", "-qp", "0", "-preset", "ultrafast", str(encoded)], check=True)
    score = await vmaf_target._vmaf(str(source), str(encoded), 3, str(tmp_path / "vmaf.json"))
    assert score is not None and score >= 97


@pytest.mark.asyncio
@pytest.mark.skipif(not _has_libvmaf(), reason="needs ffmpeg with libvmaf")
async def test_a_resized_test_encode_is_scored(test_db, tmp_path, monkeypatch):
    """Test encode compared the two sizes as they were: libvmaf refused
    ("input width must match") and a rule that resizes showed no VMAF."""
    import backend.test_encode as test_encode
    monkeypatch.setattr(test_encode, "TEMP_DIR", tmp_path / "test-encode")
    source = tmp_path / "Film (2020).mkv"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    "testsrc2=s=1708x960:r=24:d=12,noise=alls=12:allf=t", "-c:v", "libx264", "-crf", "8",
                    "-preset", "ultrafast", str(source)], check=True)
    result = await test_encode.run_test_encode(
        str(source), encoder="libx265", sample_seconds=4,
        job_overrides={"override_target_resolution": "480p", "override_libx265_preset": "ultrafast",
                       "override_crf": 16})
    assert result["status"] == "complete", result
    assert result["vmaf_score"] is not None and result["vmaf_score"] >= 90, result
