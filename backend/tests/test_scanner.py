import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from backend.scanner import (
    classify_audio_tracks,
    classify_subtitle_tracks,
    clamp_future_mtime,
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
    # Native language detected as "isl" (disposition.original=1).
    # always_keep_languages is a DB-backed setting (_load_audio_keep_languages);
    # provide it via patch so the unit test has the eng+isl keep-list it asserts
    # — in production this comes from Settings → Audio cleanup.
    native = detect_native_language(tracks)
    with patch("backend.scanner._load_audio_keep_languages", return_value={"eng", "isl"}), \
         patch("backend.scanner._is_cleanup_enabled", return_value=True):
        result = classify_audio_tracks(tracks, native)

    by_lang = {t.language: t for t in result}

    # eng: always-keep language → kept. `locked` is deprecated to always-False
    # since v0.5.16 (the UI dropped lock rendering); keep is what matters.
    assert by_lang["eng"].keep is True
    assert by_lang["eng"].locked is False

    # isl: always-keep language → kept (also native).
    assert by_lang["isl"].keep is True
    assert by_lang["isl"].locked is False

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
    with patch("backend.scanner._load_audio_keep_languages", return_value={"eng"}), \
         patch("backend.scanner._is_cleanup_enabled", return_value=True):
        result = classify_audio_tracks(tracks, native)

    by_lang = {t.language: t for t in result}

    # eng: always-keep language → kept (locked deprecated to False, v0.5.16).
    assert by_lang["eng"].keep is True
    assert by_lang["eng"].locked is False

    # und: kept (not suggested for removal), not locked
    assert by_lang["und"].keep is True
    assert by_lang["und"].locked is False


def test_classify_subtitle_removes_empty_forced_und():
    """v0.9.99: a 'NO SUBS' placeholder (forced, und, <=1 cue) is marked
    removable even though sub_keep_unknown=True would keep a normal und sub."""
    tracks = [
        {"stream_index": 2, "language": None, "codec": "subrip",
         "title": "NO SUBS", "forced": True, "num_frames": 1},
        {"stream_index": 3, "language": "eng", "codec": "subrip",
         "title": "English", "forced": False, "num_frames": 992},
    ]
    with patch("backend.scanner._load_sub_settings", return_value=({"eng"}, True)), \
         patch("backend.scanner._is_cleanup_enabled", return_value=True):
        result = classify_subtitle_tracks(tracks, "eng")
    by_idx = {t.stream_index: t for t in result}
    # placeholder → suggested for removal, and NOT locked (so cleanup honours it)
    assert by_idx[2].keep is False
    assert by_idx[2].locked is False
    # the real English sub is untouched
    assert by_idx[3].keep is True


def test_classify_subtitle_empty_rule_spares_named_language():
    """The <=1-cue rule is gated to forced/und — a sparse *named*-language
    sub (a 1-cue English 'signs' track) is left to the normal keep rules."""
    tracks = [
        {"stream_index": 2, "language": "eng", "codec": "subrip",
         "title": "Signs", "forced": False, "num_frames": 1},
    ]
    with patch("backend.scanner._load_sub_settings", return_value=({"eng"}, True)), \
         patch("backend.scanner._is_cleanup_enabled", return_value=True):
        result = classify_subtitle_tracks(tracks, "eng")
    # eng is a keep-language → kept, not swept by the empty rule
    assert result[0].keep is True


def test_classify_subtitle_empty_rule_needs_known_cue_count():
    """When NUMBER_OF_FRAMES is absent (num_frames=None) the empty rule can't
    fire — a forced und track keeps its legacy always-keep-forced default."""
    tracks = [
        {"stream_index": 2, "language": None, "codec": "subrip",
         "title": "Forced", "forced": True, "num_frames": None},
    ]
    with patch("backend.scanner._load_sub_settings", return_value=({"eng"}, True)), \
         patch("backend.scanner._is_cleanup_enabled", return_value=True):
        result = classify_subtitle_tracks(tracks, "eng")
    assert result[0].keep is True


def test_metadata_refresh_reclassifies_on_native_change(monkeypatch):
    """v0.9.69: correcting a title's native language during metadata refresh
    must recompute which tracks are kept — and preserve per-track extras."""
    import json
    from backend.routes.scan import _reclassify_keep_flags
    # Deterministic classify: cleanup on, keep native, no always-keep list.
    monkeypatch.setattr(
        "backend.scanner._is_cleanup_enabled",
        lambda k, default=True: {"audio_cleanup_enabled": True, "keep_native_language": True}.get(k, default),
    )
    monkeypatch.setattr("backend.scanner._load_audio_keep_languages", lambda: set())
    monkeypatch.setattr("backend.scanner._load_sub_settings", lambda: (set(), False))
    # Stale state: classified under the wrong native 'por' (por kept, kor removed).
    audio = json.dumps([
        {"stream_index": 1, "language": "por", "codec": "eac3", "channels": 6, "keep": True, "locked": False},
        {"stream_index": 2, "language": "kor", "codec": "eac3", "channels": 6, "keep": False, "locked": False, "detected_language": "kor", "detect_note": "x"},
    ])
    a_json, s_json, rem_a, rem_s, und = _reclassify_keep_flags(audio, "[]", "kor", 0)
    by = {t["language"]: t for t in json.loads(a_json)}
    assert by["kor"]["keep"] is True, "native track must be kept after native correction"
    assert by["por"]["keep"] is False, "non-native track marked for removal"
    assert by["kor"]["detected_language"] == "kor", "per-track extras preserved"
    assert by["kor"]["detect_note"] == "x"
    assert rem_a == 1


def test_reclass_item_heals_stale_api_and_skips_correct(monkeypatch):
    """v0.9.70: the refresh re-classifies already-'api' titles against their
    stored native, rewriting only when the classification drifted (heals a
    title whose native was corrected before track re-classification existed),
    and returns None for correctly-classified rows (no needless write)."""
    import json
    from backend.routes import scan as _scan
    monkeypatch.setattr(
        "backend.scanner._is_cleanup_enabled",
        lambda k, default=True: {"audio_cleanup_enabled": True, "keep_native_language": True}.get(k, default),
    )
    monkeypatch.setattr("backend.scanner._load_audio_keep_languages", lambda: set())
    monkeypatch.setattr("backend.scanner._load_sub_settings", lambda: (set(), False))

    def audio(por_keep, kor_keep):
        return json.dumps([
            {"stream_index": 1, "language": "por", "codec": "eac3", "channels": 6, "keep": por_keep, "locked": False},
            {"stream_index": 2, "language": "kor", "codec": "eac3", "channels": 6, "keep": kor_keep, "locked": False},
        ])

    # Stale: native is kor but por is kept / kor removed → heal.
    stale = {"id": 1, "audio_tracks_json": audio(True, False), "subtitle_tracks_json": "[]", "duration": 0}
    item = _scan._reclass_item(stale, "kor")
    assert item is not None
    by = {t["language"]: t for t in json.loads(item["a_json"])}
    assert by["kor"]["keep"] is True and by["por"]["keep"] is False

    # Already correct → no write.
    good = {"id": 2, "audio_tracks_json": audio(False, True), "subtitle_tracks_json": "[]", "duration": 0}
    assert _scan._reclass_item(good, "kor") is None


def test_clean_srt_bytes_strips_ass_structure():
    """v0.9.71: raw ASS must be reduced to dialogue text only — the English
    script structure ([Script Info], Format:/Style:, 'Dialogue:' prefixes,
    {\\...} tags) otherwise makes langdetect read the track as English."""
    from backend.scanner import _clean_srt_bytes
    ass = (
        "[Script Info]\nScriptType: v4.00+\n[V4+ Styles]\n"
        "Format: Name, Fontname\nStyle: Default,Arial\n[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        "Dialogue: 0,0:00:01.00,0:00:04.00,Default,,0,0,0,,{\\an8}Perkara yang saya nak cakap.\n"
        "Dialogue: 0,0:00:05.00,0:00:08.00,Default,,0,0,0,,Apa pendapat awak?\n"
    )
    out = _clean_srt_bytes(ass.encode("utf-8"))
    assert "Script Info" not in out and "Dialogue:" not in out and "Format:" not in out
    assert "an8" not in out  # override tag stripped
    assert "Perkara yang saya nak cakap." in out
    assert "Apa pendapat awak?" in out


def test_clean_srt_bytes_strips_font_and_override_tags():
    """v0.9.76: ffmpeg's ass->srt fallback wraps lines in <font …> tags and
    leaves {\\an} overrides — over many lines the repeated 'font size color'
    tokens skewed langdetect to English. They must be stripped."""
    from backend.scanner import _clean_srt_bytes
    srt = (
        '1\n00:00:01,000 --> 00:00:03,000\n'
        '<font size="48" color="#000000">{\\an8}Isang serye mula sa Netflix.</font>\n\n'
        '2\n00:00:04,000 --> 00:00:06,000\n'
        '<font size="48" color="#000000"><i>Ang mga bata ay naglalaro.</i></font>\n'
    )
    out = _clean_srt_bytes(srt.encode("utf-8"))
    assert "<font" not in out and "</font>" not in out and "<i>" not in out
    assert "{" not in out and "an8" not in out
    assert "Isang serye mula sa Netflix." in out
    assert "Ang mga bata ay naglalaro." in out


@pytest.mark.asyncio
async def test_recompute_needs_conversion_works_without_row_factory(tmp_path):
    """Regression: recompute must work on a plain-tuple connection. The
    settings-update handler opens its db without `aiosqlite.Row`, so named
    row access raised 'tuple indices must be integers or slices, not str' and
    the source_codecs recompute silently no-op'd on every save."""
    import aiosqlite
    from backend.scanner import recompute_needs_conversion
    db = await aiosqlite.connect(str(tmp_path / "t.db"))
    # Intentionally NO db.row_factory = aiosqlite.Row — reproduces the caller.
    try:
        await db.execute(
            "CREATE TABLE scan_results (file_path TEXT, video_codec TEXT, "
            "needs_conversion INTEGER, converted INTEGER)"
        )
        await db.execute("INSERT INTO scan_results VALUES ('/a.mkv','h264',0,0)")
        await db.execute("INSERT INTO scan_results VALUES ('/b.mkv','hevc',0,0)")
        await db.execute("INSERT INTO scan_results VALUES ('/c.mkv','h264',0,1)")  # converted → skipped
        await db.commit()
        flipped = await recompute_needs_conversion(db, ["h264"])
        assert flipped == 1
        async with db.execute("SELECT file_path, needs_conversion FROM scan_results ORDER BY file_path") as cur:
            got = {r[0]: r[1] for r in await cur.fetchall()}
        assert got == {"/a.mkv": 1, "/b.mkv": 0, "/c.mkv": 0}
    finally:
        await db.close()


def test_is_dubbed_core():
    from backend.scanner import _is_dubbed
    assert _is_dubbed(["eng"], "kor", "api") == 1
    assert _is_dubbed(["kor", "eng"], "kor", "api") == 0
    assert _is_dubbed(["cmn"], "zho", "api") == 0
    assert _is_dubbed(["eng"], "kor", "manual") == 1
    assert _is_dubbed(["eng"], "kor", "tmdb-manual") == 1
    assert _is_dubbed(["eng"], "kor", "heuristic") == 0
    assert _is_dubbed(["eng"], "und", "api") == 0
    assert _is_dubbed(["eng"], "", "api") == 0
    assert _is_dubbed(["eng", "und"], "kor", "api") == 0
    assert _is_dubbed([], "kor", "api") == 0
    assert _is_dubbed(["ENG"], "KOR", "api") == 1


def test_clamp_future_mtime():
    """v0.9.102: a bogus future mtime (ripped media dated 2036) is clamped to
    now so it can't pin the title to the top of the 'Newest' sort; past and
    None values pass through unchanged."""
    now = 1_000_000.0
    assert clamp_future_mtime(now - 500, now) == now - 500   # past → unchanged
    assert clamp_future_mtime(now + 10**9, now) == now       # future → clamped
    assert clamp_future_mtime(now, now) == now               # exactly now → unchanged
    assert clamp_future_mtime(None, now) is None             # missing → passes through
