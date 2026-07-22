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
         patch.object(ld, "_detect_clip_language", new=AsyncMock(return_value=("de", 0.92))), \
         patch("os.path.exists", return_value=True), \
         patch("os.unlink"):
        lang, conf, _note = await ld.detect_audio_language("/media/movie.mkv", 1, duration=1800.0)
    assert lang == "ger", f"got {lang!r}"
    assert conf == pytest.approx(0.92)


@pytest.mark.asyncio
async def test_detect_audio_language_low_confidence_returns_none():
    from backend import language_detection as ld
    with patch.object(ld, "_extract_audio_clip", new=AsyncMock(return_value="/tmp/clip.wav")), \
         patch.object(ld, "_detect_clip_language", new=AsyncMock(return_value=("de", 0.30))), \
         patch("os.path.exists", return_value=True), \
         patch("os.unlink"):
        lang, conf, _note = await ld.detect_audio_language("/media/movie.mkv", 1, duration=1800.0)
    assert lang is None


@pytest.mark.asyncio
async def test_detect_audio_language_failopen_on_extract_error():
    from backend import language_detection as ld
    with patch.object(ld, "_extract_audio_clip", new=AsyncMock(side_effect=OSError("ffmpeg gone"))):
        lang, conf, _note = await ld.detect_audio_language("/media/movie.mkv", 1, duration=1800.0)
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


# ---------------------------------------------------------------------------
# v0.8.3: writing detected languages back to the file. The command builders
# are pure/testable; actual mkvpropedit/ffmpeg execution is integration.
# ---------------------------------------------------------------------------

def test_build_mkvpropedit_cmd_maps_per_type_ordinals():
    from backend.language_detection import _build_mkvpropedit_cmd
    # 2 audio tracks (1st und→swe, 2nd already tagged→None),
    # 3 sub tracks (only the 2nd detected→chi).
    cmd = _build_mkvpropedit_cmd("/m/f.mkv", ["swe", None], [None, "chi", None])
    assert cmd[0] == "mkvpropedit" and cmd[1] == "/m/f.mkv"
    joined = " ".join(cmd)
    assert "--edit track:a1 --set language=swe" in joined
    assert "track:a2" not in joined  # None → not touched
    assert "--edit track:s2 --set language=chi" in joined
    assert "track:s1" not in joined and "track:s3" not in joined


def test_build_mkvpropedit_cmd_none_when_nothing_to_set():
    from backend.language_detection import _build_mkvpropedit_cmd
    assert _build_mkvpropedit_cmd("/m/f.mkv", [None, None], [None]) is None
    assert _build_mkvpropedit_cmd("/m/f.mkv", [], []) is None


def test_build_metadata_remux_cmd_zero_based_output_selectors():
    from backend.language_detection import _build_metadata_remux_cmd
    cmd = _build_metadata_remux_cmd("/m/f.mp4", "/m/tmp.mp4", ["swe", None], ["eng"])
    joined = " ".join(cmd)
    assert "-map 0" in joined and "-c copy" in joined
    assert "-metadata:s:a:0 language=swe" in joined
    assert "s:a:1" not in joined  # 2nd audio was None
    assert "-metadata:s:s:0 language=eng" in joined
    assert cmd[-1] == "/m/tmp.mp4"


@pytest.mark.asyncio
async def test_apply_track_languages_noop_when_all_none():
    from backend.language_detection import apply_track_languages_to_file
    # No languages to set → returns False without touching anything.
    assert await apply_track_languages_to_file("/m/f.mkv", [None], [None]) is False


@pytest.mark.asyncio
async def test_apply_skips_untaggable_container():
    """v0.9.26: AVI (and other containers with no per-track language field)
    return False without attempting a remux — the tag can't persist, so the
    caller must keep the track flagged rather than claim success."""
    from backend.language_detection import apply_track_languages_to_file
    assert await apply_track_languages_to_file("/m/movie.avi", ["eng"], [None]) is False
    assert await apply_track_languages_to_file("/m/clip.mpg", [None], ["eng"]) is False


def test_languages_present_verifies_intended_ordinals():
    """v0.9.26: only intended (non-None) tags must be present; und/missing on
    an intended ordinal fails, everything else passes."""
    from backend.language_detection import _languages_present
    # AVI outcome: intended eng, but the stream is still und -> not present.
    assert _languages_present(["und"], ["eng"]) is False
    # Written correctly.
    assert _languages_present(["eng"], ["eng"]) is True
    # Intended ordinal missing entirely (fewer streams than expected).
    assert _languages_present([], ["eng"]) is False
    # Nothing intended -> vacuously present regardless of file state.
    assert _languages_present(["und", "und"], [None, None]) is True
    # Mixed: only the None slot is und, the intended slot is set.
    assert _languages_present(["eng", "und"], ["eng", None]) is True


def test_unknown_language_filter_includes_ignored():
    """v0.9.26: the unknown-language filter includes ignored titles — an ignore
    rule means 'don't convert', not 'hide that the audio is untagged'."""
    from backend.routes.scan import _matches_single_filter
    assert _matches_single_filter({"has_und_tracks": True, "ignored": True}, "unknown_language") is True
    assert _matches_single_filter({"has_und_tracks": True, "ignored": False}, "unknown_language") is True
    assert _matches_single_filter({"has_und_tracks": False, "ignored": True}, "unknown_language") is False


# ---------------------------------------------------------------------------
# v0.8.4: multi-position audio sampling. A weak 33% window shouldn't doom
# detection — later windows are tried, most-confident wins, short-circuit
# once the threshold is cleared.
# ---------------------------------------------------------------------------

def test_sample_seeks_positions():
    from backend.language_detection import _sample_seeks
    assert _sample_seeks(0) == [0.0]
    assert _sample_seeks(60) == [0.0]           # short file → single sample
    seeks = _sample_seeks(1000.0)
    assert len(seeks) >= 3 and seeks[0] == 330.0  # first sample at 33%


@pytest.mark.asyncio
async def test_detect_audio_recovers_from_weak_first_window(monkeypatch):
    """First window low-confidence, a later window clears the threshold →
    detection succeeds with the confident result (the pt@0.40 scenario,
    where another window would have had clear speech)."""
    from backend import language_detection as ld
    calls = {"n": 0}
    async def fake_detect(clip, timeout=120):
        calls["n"] += 1
        # window 1 weak (0.40), window 2 strong (0.88)
        return ("it", 0.40) if calls["n"] == 1 else ("it", 0.88)
    monkeypatch.setattr(ld, "_extract_audio_clip",
                        __import__("unittest").mock.AsyncMock(return_value="/tmp/c.wav"))
    monkeypatch.setattr(ld, "_detect_clip_language", fake_detect)
    monkeypatch.setattr("os.path.exists", lambda p: True)
    monkeypatch.setattr("os.unlink", lambda p: None)
    lang, conf, _note = await ld.detect_audio_language("/m/f.mkv", 2, duration=1000.0)
    assert lang == "ita", f"got {lang!r}"
    assert conf == pytest.approx(0.88)
    assert calls["n"] == 2, "should have short-circuited after the confident window"


@pytest.mark.asyncio
async def test_detect_audio_all_windows_weak_stays_und(monkeypatch):
    """Every window below threshold → stays und (no wrong guess). Tries
    all sample positions."""
    from backend import language_detection as ld
    calls = {"n": 0}
    async def fake_detect(clip, timeout=120):
        calls["n"] += 1
        return ("pt", 0.40)  # always weak
    monkeypatch.setattr(ld, "_extract_audio_clip",
                        __import__("unittest").mock.AsyncMock(return_value="/tmp/c.wav"))
    monkeypatch.setattr(ld, "_detect_clip_language", fake_detect)
    monkeypatch.setattr("os.path.exists", lambda p: True)
    monkeypatch.setattr("os.unlink", lambda p: None)
    lang, conf, _note = await ld.detect_audio_language("/m/f.mkv", 2, duration=1000.0)
    assert (lang, conf) == (None, 0.0)
    assert calls["n"] >= 3, "should have tried multiple windows before giving up"


def test_detect_language_from_title():
    from backend.language_detection import detect_language_from_title
    # The reported case: forced subs whose titles name the language.
    assert detect_language_from_title("English") == "eng"
    assert detect_language_from_title("Traditional Chinese") == "chi"
    assert detect_language_from_title("Simplified Chinese") == "chi"
    assert detect_language_from_title("Romanian") == "rum"
    assert detect_language_from_title("Greek") == "gre"
    # Modifiers / suffixes don't matter.
    assert detect_language_from_title("English SDH") == "eng"
    assert detect_language_from_title("Brazilian Portuguese") == "por"
    assert detect_language_from_title("français (forced)") == "fre"
    # No language named → None (falls through to content detection).
    assert detect_language_from_title("Forced") is None
    assert detect_language_from_title("Commentary") is None
    assert detect_language_from_title("") is None
    assert detect_language_from_title(None) is None


def test_iso_to_iso639_2b_covers_whisper_codes():
    from backend.language_detection import _iso_to_iso639_2b
    # The reported bug: whisper detected "nn" (Nynorsk) and it was dropped.
    assert _iso_to_iso639_2b("nn") == "nno"
    assert _iso_to_iso639_2b("en") == "eng"
    assert _iso_to_iso639_2b("de") == "ger"        # B-form
    assert _iso_to_iso639_2b("zh-cn") == "chi"      # region stripped
    assert _iso_to_iso639_2b("ZH") == "chi"         # case-insensitive
    assert _iso_to_iso639_2b("ta") == "tam"
    assert _iso_to_iso639_2b("") is None
    assert _iso_to_iso639_2b(None) is None


def test_detect_language_from_title_extended():
    from backend.language_detection import detect_language_from_title
    # The reported bug: a track titled "Wolof" left und.
    assert detect_language_from_title("Wolof") == "wol"
    assert detect_language_from_title("Swahili") == "swa"
    assert detect_language_from_title("Amharic") == "amh"
    assert detect_language_from_title("Tamil (forced)") == "tam"
    assert detect_language_from_title("Welsh") == "wel"


def test_tuned_float_precedence(monkeypatch):
    from backend import language_detection as ld
    # env var wins (advanced override)
    monkeypatch.setenv("SHRINKERR_LANG_DETECT_AUDIO_MIN", "0.42")
    monkeypatch.setattr(ld, "_get_setting", lambda k: "0.55")
    assert ld._audio_min_confidence() == 0.42
    # no env -> Settings value
    monkeypatch.delenv("SHRINKERR_LANG_DETECT_AUDIO_MIN", raising=False)
    assert ld._audio_min_confidence() == 0.55
    # neither -> default
    monkeypatch.setattr(ld, "_get_setting", lambda k: None)
    assert ld._audio_min_confidence() == 0.6


def test_configured_whisper_model_precedence(monkeypatch):
    from backend import language_detection as ld
    monkeypatch.setattr(ld, "_get_setting", lambda k: "small")
    monkeypatch.delenv("SHRINKERR_WHISPER_MODEL", raising=False)
    assert ld._configured_whisper_model() == "small"   # Settings value
    monkeypatch.setenv("SHRINKERR_WHISPER_MODEL", "base")
    assert ld._configured_whisper_model() == "base"     # env override wins
    monkeypatch.delenv("SHRINKERR_WHISPER_MODEL", raising=False)
    monkeypatch.setattr(ld, "_get_setting", lambda k: None)
    assert ld._configured_whisper_model() == "tiny"     # default


@pytest.mark.asyncio
async def test_extract_audio_clip_kills_ffmpeg_on_timeout(monkeypatch):
    """Regression (v0.9.19): a hung ffmpeg must be killed on timeout, not
    orphaned to keep burning CPU."""
    import asyncio as _asyncio
    from backend import language_detection as ld
    state = {"killed": False}

    class _FakeProc:
        returncode = None
        def kill(self):
            state["killed"] = True
        async def wait(self):
            return 0
        async def communicate(self):
            return (b"", b"")

    async def _fake_exec(*a, **k):
        return _FakeProc()

    async def _fake_wait_for(coro, timeout):
        coro.close()  # we're simulating a hang; don't actually await
        raise _asyncio.TimeoutError()

    monkeypatch.setattr("asyncio.create_subprocess_exec", _fake_exec)
    monkeypatch.setattr("asyncio.wait_for", _fake_wait_for)
    with pytest.raises(OSError):
        await ld._extract_audio_clip("/x.mkv", 1, 0.0)
    assert state["killed"] is True


def test_audio_lang_worker_importable():
    """The killable worker module must import + expose main()."""
    import backend.audio_lang_worker as w
    assert callable(w.main)


@pytest.mark.asyncio
async def test_detect_audio_language_returns_below_threshold_note():
    """v0.9.44: a below-threshold result returns a human note (guess + %)."""
    from backend import language_detection as ld
    with patch.object(ld, "_extract_audio_clip", new=AsyncMock(return_value="/tmp/clip.wav")), \
         patch.object(ld, "_detect_clip_language", new=AsyncMock(return_value=("en", 0.30))), \
         patch("os.path.exists", return_value=True), patch("os.unlink"):
        lang, conf, note = await ld.detect_audio_language("/m/f.mkv", 1, duration=1800.0)
    assert lang is None
    assert note and "below" in note and "en" in note.lower()


def test_mkvpropedit_deletes_language_ietf():
    """v0.9.48: also delete the BCP-47 language-ietf element, which otherwise
    overrides the legacy `language` we set (mkvpropedit succeeds but the tag
    verify still reads und)."""
    from backend.language_detection import _build_mkvpropedit_cmd
    cmd = _build_mkvpropedit_cmd("/m/f.mkv", ["eng"], [None])
    joined = " ".join(cmd)
    assert "--set language=eng" in joined
    assert "--delete language-ietf" in joined


# --- v0.9.60: _verify_written must not report success it can't confirm ------
import asyncio as _asyncio_vw
from backend import language_detection as _ld_vw


@pytest.mark.asyncio
async def test_verify_written_confirmed_present(monkeypatch):
    async def fake_probe(fp):
        return (["eng"], ["eng"])
    monkeypatch.setattr(_ld_vw, "_probe_track_languages", fake_probe)
    assert await _ld_vw._verify_written("/f.mkv", ["eng"], ["eng"]) is True


@pytest.mark.asyncio
async def test_verify_written_absent_is_false(monkeypatch):
    async def fake_probe(fp):
        return (["und"], ["und"])
    monkeypatch.setattr(_ld_vw, "_probe_track_languages", fake_probe)
    assert await _ld_vw._verify_written("/f.mkv", ["eng"], [None]) is False


@pytest.mark.asyncio
async def test_verify_written_probe_failure_returns_none_after_retries(monkeypatch):
    calls = []
    async def fake_probe(fp):
        calls.append(1)
        return ([], [])  # probe failed / no streams
    async def no_sleep(_):
        return None
    monkeypatch.setattr(_ld_vw, "_probe_track_languages", fake_probe)
    monkeypatch.setattr(_asyncio_vw, "sleep", no_sleep)
    # None (not False, not True) => caller must treat as unconfirmed
    assert await _ld_vw._verify_written("/f.mkv", ["eng"], [None], retries=3) is None
    assert len(calls) == 3, "should retry the probe"


@pytest.mark.asyncio
async def test_verify_written_transient_probe_then_recovers(monkeypatch):
    seq = [([], []), (["eng"], [])]
    async def fake_probe(fp):
        return seq.pop(0)
    async def no_sleep(_):
        return None
    monkeypatch.setattr(_ld_vw, "_probe_track_languages", fake_probe)
    monkeypatch.setattr(_asyncio_vw, "sleep", no_sleep)
    assert await _ld_vw._verify_written("/f.mkv", ["eng"], [None], retries=3) is True


# --- v0.9.71: Malay subtitle detection (langdetect has no Malay model) ------
from backend.language_detection import detect_subtitle_language as _det_sub, _looks_malay


def test_subtitle_detects_malay():
    text = ("Perkara yang saya nak cakap akan jadi sukar untuk difahami. "
            "Apa pendapat awak sebagai wanita? Orang takkan tahu jika kita "
            "tak luahkan perasaan. Macam mana nak buat sekejap? ") * 3
    assert _looks_malay(text) is True
    assert _det_sub(text) == ("may", 0.99)


def test_subtitle_indonesian_not_mistagged_malay():
    text = ("Aku tidak tahu apa yang kamu mau bicarakan. Ini sangat sulit "
            "dimengerti. Kayak gitu banget sih. Aku udah bilang jangan begitu.") * 3
    assert _looks_malay(text) is False
    lang, _ = _det_sub(text)
    assert lang == "ind"


def test_looks_malay_needs_enough_text():
    assert _looks_malay("tak nak awak") is False  # < 20 words → don't trust it


# --- v0.9.75: remux transcodes mov_text/tx3g subs (matroska can't copy them) -
from backend.language_detection import _build_metadata_remux_cmd as _remux_cmd


def test_remux_transcodes_mov_text_sub():
    cmd = _remux_cmd("/f.mkv", "/tmp/o.mkv", [None], ["eng"], sub_codecs=["mov_text"])
    assert "-c:s:0" in cmd and cmd[cmd.index("-c:s:0") + 1] == "srt"
    assert "-metadata:s:s:0" in cmd and cmd[cmd.index("-metadata:s:s:0") + 1] == "language=eng"


def test_remux_copies_matroska_compatible_sub():
    cmd = _remux_cmd("/f.mkv", "/tmp/o.mkv", [None], ["eng"], sub_codecs=["subrip"])
    assert "-c:s:0" not in cmd  # srt/subrip copies fine — no transcode


def test_remux_transcodes_only_the_mov_text_stream():
    # sub 0 = image (copy), sub 1 = mov_text (transcode)
    cmd = _remux_cmd("/f.mkv", "/tmp/o.mkv", [None], [None, "eng"],
                     sub_codecs=["hdmv_pgs_subtitle", "mov_text"])
    assert "-c:s:1" in cmd and cmd[cmd.index("-c:s:1") + 1] == "srt"
    assert "-c:s:0" not in cmd


def test_parse_whisper_result_valid():
    from backend.language_detection import _parse_whisper_result
    assert _parse_whisper_result("RESULT\tde\t0.92\n") == ("de", 0.92)


def test_parse_whisper_result_empty_lang_and_bad_conf():
    from backend.language_detection import _parse_whisper_result
    assert _parse_whisper_result("RESULT\t\t\n") == (None, 0.0)
    assert _parse_whisper_result("RESULT\ten\tNaNlike\n") == ("en", 0.0)


def test_parse_whisper_result_non_result_line():
    from backend.language_detection import _parse_whisper_result
    assert _parse_whisper_result("[LANG-DETECT] model loaded\n") == (None, 0.0)


def test_model_device_reads_ct2_device():
    from backend.language_detection import _model_device
    class _M:
        class model:
            device = "cuda"
    assert _model_device(_M()) == "cuda"

    class _Bare:
        pass
    assert _model_device(_Bare()) == "unknown"


import sys as _sys

def _fake_worker(body: str) -> list[str]:
    """A `python -c` command acting as a whisper worker for tests."""
    return [_sys.executable, "-c", body]

_ECHO = ("import sys\n"
         "for l in sys.stdin:\n"
         "    if l.strip():\n"
         "        sys.stdout.write('RESULT\\tde\\t0.9\\n'); sys.stdout.flush()\n")

_NOISY = ("import sys\n"
          "for l in sys.stdin:\n"
          "    if l.strip():\n"
          "        sys.stdout.write('loading model...\\n'); sys.stdout.flush()\n"
          "        sys.stdout.write('RESULT\\tis\\t0.8\\n'); sys.stdout.flush()\n")

_HANG = "import time\ntime.sleep(999)\n"

_DIE_ONCE = ("import sys\n"
             "sys.stdin.readline()\n")


@pytest.mark.asyncio
async def test_worker_echo_roundtrip():
    from backend.language_detection import _WhisperWorker
    w = _WhisperWorker(cmd=_fake_worker(_ECHO))
    try:
        assert await w.detect("/tmp/a.wav") == ("de", 0.9)
        assert await w.detect("/tmp/b.wav") == ("de", 0.9)
        assert w._proc is not None
    finally:
        await w.shutdown()


@pytest.mark.asyncio
async def test_worker_skips_non_result_lines():
    from backend.language_detection import _WhisperWorker
    w = _WhisperWorker(cmd=_fake_worker(_NOISY))
    try:
        assert await w.detect("/tmp/a.wav") == ("is", 0.8)
    finally:
        await w.shutdown()


@pytest.mark.asyncio
async def test_worker_timeout_kills_and_raises():
    import asyncio
    from backend.language_detection import _WhisperWorker
    w = _WhisperWorker(cmd=_fake_worker(_HANG))
    try:
        with pytest.raises(asyncio.TimeoutError):
            await w.detect("/tmp/a.wav", timeout=0.5)
        assert w._proc is None
    finally:
        await w.shutdown()


@pytest.mark.asyncio
async def test_worker_respawns_after_crash_then_fails_open():
    from backend.language_detection import _WhisperWorker
    w = _WhisperWorker(cmd=_fake_worker(_DIE_ONCE))
    try:
        assert await w.detect("/tmp/a.wav", timeout=2) == (None, 0.0)
    finally:
        await w.shutdown()


@pytest.mark.asyncio
async def test_worker_respawns_on_model_change(monkeypatch):
    from backend import language_detection as ld
    w = ld._WhisperWorker(cmd=_fake_worker(_ECHO))
    try:
        monkeypatch.setattr(ld, "_configured_whisper_model", lambda: "tiny")
        await w.detect("/tmp/a.wav")
        pid1 = w._proc.pid
        monkeypatch.setattr(ld, "_configured_whisper_model", lambda: "large-v3")
        await w.detect("/tmp/b.wav")
        pid2 = w._proc.pid
        assert pid1 != pid2
    finally:
        await w.shutdown()


@pytest.mark.asyncio
async def test_worker_idle_shutdown(monkeypatch):
    import asyncio
    from backend import language_detection as ld
    monkeypatch.setenv("SHRINKERR_WHISPER_IDLE_SECONDS", "0.2")
    w = ld._WhisperWorker(cmd=_fake_worker(_ECHO))
    try:
        await w.detect("/tmp/a.wav")
        assert w._proc is not None
        await asyncio.sleep(0.5)
        assert w._proc is None
    finally:
        await w.shutdown()
