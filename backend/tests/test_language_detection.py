import pytest
from backend.language_detection import detect_subtitle_language


def test_detect_english_subtitle():
    text = (
        "You need to understand what happened here. "
        "The lawyer said the case was closed months ago, "
        "but nobody told the family about the decision."
    )
    lang, conf = detect_subtitle_language(text)
    assert lang == "eng", f"got {lang!r} conf={conf}"
    assert conf >= 0.7


def test_detect_german_subtitle():
    text = (
        "Du musst verstehen, was hier passiert ist. "
        "Der Anwalt sagte, der Fall sei vor Monaten abgeschlossen worden, "
        "aber niemand hat der Familie von der Entscheidung erzählt."
    )
    lang, conf = detect_subtitle_language(text)
    assert lang == "ger", f"got {lang!r} conf={conf}"


def test_detect_spanish_subtitle():
    text = (
        "Tienes que entender lo que pasó aquí. "
        "El abogado dijo que el caso se cerró hace meses, "
        "pero nadie le contó a la familia sobre la decisión."
    )
    lang, conf = detect_subtitle_language(text)
    assert lang == "spa", f"got {lang!r} conf={conf}"


def test_empty_or_garbage_text_returns_none():
    assert detect_subtitle_language("") == (None, 0.0)
    assert detect_subtitle_language("   \n\n  ") == (None, 0.0)
    lang, conf = detect_subtitle_language("123 456 --- >>> 00:00:01,000")
    assert lang is None


from unittest.mock import patch, MagicMock, AsyncMock


def test_audio_clip_ffmpeg_command_is_wellformed():
    from backend.language_detection import _build_audio_clip_cmd
    cmd = _build_audio_clip_cmd("/media/movie.mkv", stream_index=1, seek=600.0, out_path="/tmp/clip.wav")
    assert cmd[0] == "ffmpeg"
    assert "-ss" in cmd and cmd[cmd.index("-ss") + 1] == "600.0"
    assert "-t" in cmd and cmd[cmd.index("-t") + 1] == "30"
    assert "-map" in cmd and cmd[cmd.index("-map") + 1] == "0:1"
    assert "-ac" in cmd and cmd[cmd.index("-ac") + 1] == "1"
    assert "-ar" in cmd and cmd[cmd.index("-ar") + 1] == "16000"
    assert cmd[-1] == "/tmp/clip.wav"


@pytest.mark.asyncio
async def test_detect_audio_language_maps_result_to_iso2():
    from backend import language_detection as ld
    with patch.object(ld, "_extract_audio_clip", new=AsyncMock(return_value="/tmp/clip.wav")), \
         patch.object(ld, "_run_whisper_lang", return_value=("de", 0.92)), \
         patch("os.path.exists", return_value=True), \
         patch("os.unlink"):
        lang, conf = await ld.detect_audio_language("/media/movie.mkv", 1, duration=1800.0)
    assert lang == "ger", f"got {lang!r}"
    assert conf == pytest.approx(0.92)


@pytest.mark.asyncio
async def test_detect_audio_language_low_confidence_returns_none():
    from backend import language_detection as ld
    with patch.object(ld, "_extract_audio_clip", new=AsyncMock(return_value="/tmp/clip.wav")), \
         patch.object(ld, "_run_whisper_lang", return_value=("de", 0.30)), \
         patch("os.path.exists", return_value=True), \
         patch("os.unlink"):
        lang, conf = await ld.detect_audio_language("/media/movie.mkv", 1, duration=1800.0)
    assert lang is None


@pytest.mark.asyncio
async def test_detect_audio_language_failopen_on_extract_error():
    from backend import language_detection as ld
    with patch.object(ld, "_extract_audio_clip", new=AsyncMock(side_effect=OSError("ffmpeg gone"))):
        lang, conf = await ld.detect_audio_language("/media/movie.mkv", 1, duration=1800.0)
    assert (lang, conf) == (None, 0.0)


def test_maybe_detect_sub_language_only_text_codecs():
    from backend.language_detection import maybe_detect_subtitle_track_language
    from unittest.mock import patch
    with patch("backend.language_detection.detect_subtitle_language", return_value=("eng", 0.95)):
        assert maybe_detect_subtitle_track_language("und", "subrip", "some english text") == "eng"
    assert maybe_detect_subtitle_track_language("swe", "subrip", "text") == "swe"
    assert maybe_detect_subtitle_track_language("und", "hdmv_pgs_subtitle", "text") == "und"
    with patch("backend.language_detection.detect_subtitle_language", return_value=(None, 0.0)):
        assert maybe_detect_subtitle_track_language("und", "subrip", "garbage") == "und"
