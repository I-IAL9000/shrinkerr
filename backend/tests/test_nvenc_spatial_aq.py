"""NVENC encodes with spatial AQ (v0.10.0, the user's choice).

Without it NVENC smoothed the faint noise of flat dark areas into bands:
Blade Runner 2049's foggy scene at 13:19 scored 0.60 on CAMBI (Netflix's
banding score; the original 0.03), 0.19 with spatial AQ at strength 15 for
12% more bits, and 0.05 with the 4K ceiling lifted too. VMAF didn't notice
(95.18 banded, 94.40 not), so a VMAF target couldn't steer away from it.
libx265 has had the equivalent (aq-mode=3) all along.
"""
import pytest

from backend.converter import _build_ffmpeg_cmd_impl
from backend.tests.test_nvenc_level import _arg


@pytest.mark.parametrize("output_codec", [None, "av1"])
@pytest.mark.parametrize("width", [1920, 3840])
def test_flat_areas_get_their_bits(output_codec, width):
    cmd = _build_ffmpeg_cmd_impl("in.mkv", "out.mkv", encoder="nvenc", cq=18, output_codec=output_codec,
                                 output_width=width)
    assert (_arg(cmd, "-spatial-aq"), _arg(cmd, "-aq-strength")) == ("1", "15")
    assert cmd.index("-spatial-aq") < cmd.index("out.mkv")


def test_other_encoders_are_unchanged():
    for encoder in ("libx265", "qsv", "vaapi", "videotoolbox"):
        assert "-spatial-aq" not in _build_ffmpeg_cmd_impl("in.mkv", "out.mkv", encoder=encoder, output_width=1920)
