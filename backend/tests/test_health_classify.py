"""The thorough health check's verdict from ffmpeg's output: a DVD rip's
MP3 track with a damaged frame or two plays fine, and one "Header missing"
had a playable Boy Meets World episode marked corrupt (v0.10.0)."""
from backend.health_check import AUDIO_GLITCHES, classify_errors

GLITCH = "[mp3float @ 0x5610b262e900] Header missing"
DTS = "[null @ 0x5610b2678c40] Application provided invalid, non monotonically increasing dts to muxer in stream 0: 4 >= 4"


def test_a_few_lost_audio_frames_are_a_warning():
    status, errors = classify_errors(f"{GLITCH}\n{DTS}\n", 0)
    assert status == "warnings" and errors == [GLITCH, DTS]


def test_many_are_damaged_audio():
    assert classify_errors("\n".join([GLITCH] * (AUDIO_GLITCHES + 1)), 0)[0] == "corrupt"
    assert classify_errors("\n".join([GLITCH] * AUDIO_GLITCHES), 0)[0] == "warnings"


def test_other_damage_still_is_corrupt():
    assert classify_errors("[mpeg @ 0x1] Header missing\n", 0)[0] == "corrupt"  # not an audio decoder's frame
    assert classify_errors(f"{GLITCH}\n[h264 @ 0x2] concealing 120 DC, 120 AC, 120 MV errors in P frame\n", 0)[0] \
        == "corrupt"
    assert classify_errors(f"{GLITCH}\n", 1)[0] == "corrupt"  # ffmpeg failed
    assert classify_errors("", 0) == ("healthy", [])


def test_an_eac3_exponent_out_of_range_is_a_warning():
    """E-AC3 from streaming services trips the decoder's exponent check two
    ways — "expacc N" (allowed since v0.3.32) and "exponent N" — and it skips
    the block; the file plays fine. A converted film was marked corrupt."""
    reported = ("[eac3 @ 0x55a119252540] exponent 25 is out-of-range\n"
                "[eac3 @ 0x55a119252540] error decoding the audio block\n")
    assert classify_errors(reported, 0)[0] == "warnings"
    assert classify_errors("[ac3 @ 0x1] exponent 25 is out-of-range\n", 0)[0] == "corrupt"  # E-AC3 only, as before
