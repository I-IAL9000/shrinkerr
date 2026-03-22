import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from backend.scanner import (
    classify_audio_tracks,
    detect_native_language,
    probe_file,
)


MOCK_FFPROBE_OUTPUT = {
    "streams": [
        {
            "index": 0,
            "codec_type": "video",
            "codec_name": "h264",
            "width": 1920,
            "height": 1080,
            "r_frame_rate": "24000/1001",
            "pix_fmt": "yuv420p",
            "bit_rate": "8000000",
            "tags": {},
        },
        {
            "index": 1,
            "codec_type": "audio",
            "codec_name": "dts",
            "channels": 6,
            "bit_rate": "1536000",
            "disposition": {"original": 0, "default": 1},
            "tags": {"language": "eng", "title": "English DTS 5.1"},
        },
        {
            "index": 2,
            "codec_type": "audio",
            "codec_name": "ac3",
            "channels": 6,
            "bit_rate": "640000",
            "disposition": {"original": 0, "default": 0},
            "tags": {"language": "chi", "title": "Mandarin"},
        },
        {
            "index": 3,
            "codec_type": "audio",
            "codec_name": "ac3",
            "channels": 2,
            "bit_rate": "192000",
            "disposition": {"original": 0, "default": 0},
            "tags": {"language": "tur", "title": "Turkish"},
        },
    ],
    "format": {"duration": "7200.000", "size": "4500000000"},
}


@pytest.mark.asyncio
async def test_probe_file_parses_streams():
    """probe_file correctly parses ffprobe JSON: video codec, audio count, and languages."""
    raw_output = json.dumps(MOCK_FFPROBE_OUTPUT).encode()

    mock_proc = AsyncMock()
    mock_proc.returncode = 0
    mock_proc.communicate = AsyncMock(return_value=(raw_output, b""))

    with patch(
        "asyncio.create_subprocess_exec",
        return_value=mock_proc,
    ):
        result = await probe_file("/fake/movie.mkv")

    assert result is not None
    assert result["video_codec"] == "h264"
    assert len(result["audio_tracks"]) == 3

    languages = [t["language"] for t in result["audio_tracks"]]
    assert "eng" in languages
    assert "chi" in languages
    assert "tur" in languages

    assert result["duration"] == pytest.approx(7200.0)
    assert result["file_size"] == 4500000000


def test_classify_audio_tracks_keeps_eng_isl():
    """eng and isl tracks are kept+locked; chi and tur are marked for removal."""
    tracks = [
        {
            "stream_index": 1,
            "language": "eng",
            "codec": "dts",
            "channels": 6,
            "title": "English",
            "bitrate": 1536000,
            "disposition": {"original": 0},
        },
        {
            "stream_index": 2,
            "language": "isl",
            "codec": "ac3",
            "channels": 6,
            "title": "Icelandic",
            "bitrate": 640000,
            "disposition": {"original": 1},
        },
        {
            "stream_index": 3,
            "language": "chi",
            "codec": "ac3",
            "channels": 6,
            "title": "Mandarin",
            "bitrate": 640000,
            "disposition": {"original": 0},
        },
        {
            "stream_index": 4,
            "language": "tur",
            "codec": "ac3",
            "channels": 2,
            "title": "Turkish",
            "bitrate": 192000,
            "disposition": {"original": 0},
        },
    ]
    # Native language detected as "isl" (disposition.original=1)
    native = detect_native_language(tracks)
    result = classify_audio_tracks(tracks, native)

    by_lang = {t.language: t for t in result}

    # eng: always keep, locked
    assert by_lang["eng"].keep is True
    assert by_lang["eng"].locked is True

    # isl: always keep, locked (also native, but always_keep takes priority)
    assert by_lang["isl"].keep is True
    assert by_lang["isl"].locked is True

    # chi: suggested for removal
    assert by_lang["chi"].keep is False
    assert by_lang["chi"].locked is False

    # tur: suggested for removal
    assert by_lang["tur"].keep is False
    assert by_lang["tur"].locked is False


def test_classify_audio_tracks_ignores_unknown():
    """und (unknown language) tracks are kept but not locked — not suggested for removal."""
    tracks = [
        {
            "stream_index": 1,
            "language": "eng",
            "codec": "dts",
            "channels": 6,
            "title": "English",
            "bitrate": 1536000,
            "disposition": {"original": 0},
        },
        {
            "stream_index": 2,
            "language": "und",
            "codec": "ac3",
            "channels": 2,
            "title": "Unknown",
            "bitrate": 192000,
            "disposition": {"original": 0},
        },
    ]
    native = detect_native_language(tracks)
    result = classify_audio_tracks(tracks, native)

    by_lang = {t.language: t for t in result}

    # eng: always keep, locked
    assert by_lang["eng"].keep is True
    assert by_lang["eng"].locked is True

    # und: kept (not suggested for removal), not locked
    assert by_lang["und"].keep is True
    assert by_lang["und"].locked is False
