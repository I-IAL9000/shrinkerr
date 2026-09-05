"""Tests for detect_external_subtitles.

v0.7.33: external-sub detection must skip hidden / AppleDouble
companion files (`._<name>.srt`) that macOS-formatted volumes create.
They share the real subtitle's extension but are resource-fork junk;
feeding one to ffmpeg as `-i` fails the whole conversion with exit 183.
"""
from backend.scanner import detect_external_subtitles, is_hidden_sidecar


def test_is_hidden_sidecar():
    """The shared hidden-file predicate used by both scan-time detection
    and the convert-time merge guard."""
    assert is_hidden_sidecar("/media/Show/._Ep.eng.srt") is True
    assert is_hidden_sidecar("._Ep.eng.srt") is True
    assert is_hidden_sidecar("/media/Show/.DS_Store") is True
    assert is_hidden_sidecar("/media/Show/._Ep.idx") is True
    # Real files must NOT be flagged.
    assert is_hidden_sidecar("/media/Show/Ep.eng.srt") is False
    assert is_hidden_sidecar("Ep.idx") is False
    # A dot in a parent dir (not the basename) must not trip it.
    assert is_hidden_sidecar("/media/.hidden_dir/Ep.srt") is False


def test_appledouble_srt_is_ignored(tmp_path):
    """A `._<name>.eng.srt` AppleDouble companion must NOT be returned as
    an external subtitle, even though it carries the episode key that the
    S##E## match strategy would otherwise catch."""
    video = tmp_path / "Show - S02E01 - Title - 1080p Webrip x264.mkv"
    video.write_bytes(b"\x00")  # content irrelevant; only existence + name
    real_sub = tmp_path / "Show - S02E01 - Title - 1080p Webrip x264.eng.srt"
    real_sub.write_text("1\n00:00:01,000 --> 00:00:02,000\nhi\n")
    apple_sub = tmp_path / "._Show - S02E01 - Title - 1080p Webrip x264.eng.srt"
    apple_sub.write_bytes(b"\x00\x05\x16\x07")  # AppleDouble magic-ish junk

    results = detect_external_subtitles(str(video))
    paths = [r["external_path"] for r in results]

    assert str(real_sub) in paths, f"real sub missing: {paths}"
    assert str(apple_sub) not in paths, (
        f"AppleDouble companion leaked into external subs → would be fed "
        f"to ffmpeg -i and fail the conversion (exit 183): {paths}"
    )
    # No returned path may start the basename with a dot.
    assert all(not p.rsplit("/", 1)[-1].startswith(".") for p in paths), paths


def test_plain_dotfile_srt_ignored(tmp_path):
    """A generic hidden `.something.srt` is also skipped."""
    video = tmp_path / "Movie (2020) 1080p x264.mkv"
    video.write_bytes(b"\x00")
    hidden = tmp_path / ".hidden.srt"
    hidden.write_text("1\n00:00:01,000 --> 00:00:02,000\nx\n")

    results = detect_external_subtitles(str(video))
    assert results == [] or all(
        not r["external_path"].rsplit("/", 1)[-1].startswith(".")
        for r in results
    )


# ---------------------------------------------------------------------------
# v0.8.1: subtitle text extraction must survive non-UTF-8 charsets.
# ffmpeg's srt DECODER rejects Windows-1252/Latin-1 subs ("Invalid UTF-8
# in decoded subtitles text") and yields zero output, leaving the track
# und. _clean_srt_bytes decodes tolerantly so language detection works.
# ---------------------------------------------------------------------------

def test_clean_srt_bytes_decodes_non_utf8():
    from backend.scanner import _clean_srt_bytes
    # 0xe9 is 'é' in latin-1/cp1252 but an invalid standalone UTF-8 byte —
    # exactly what broke ffmpeg's decoder.
    raw = (b"1\r\n00:00:01,000 --> 00:00:04,000\r\n"
           b"Bonjour, je m\xe9appelle Antoine et voici mon histoire.\r\n\r\n"
           b"2\r\n00:00:05,000 --> 00:00:08,000\r\n"
           b"Nous partons demain matin pour la montagne.\r\n")
    txt = _clean_srt_bytes(raw)
    assert txt is not None, "non-UTF-8 bytes must still decode (not None)"
    assert "Antoine" in txt and "montagne" in txt
    # sequence numbers + timestamp lines stripped
    assert "00:00:01,000" not in txt
    assert "\n1\n" not in txt


def test_clean_srt_bytes_utf8_still_works():
    from backend.scanner import _clean_srt_bytes
    raw = b"1\n00:00:01,000 --> 00:00:04,000\nA straightforward English line.\n"
    assert _clean_srt_bytes(raw) == "A straightforward English line."


def test_clean_srt_bytes_empty_returns_none():
    from backend.scanner import _clean_srt_bytes
    assert _clean_srt_bytes(b"") is None
    assert _clean_srt_bytes(b"1\n00:00:01,000 --> 00:00:02,000\n\n") is None


# ---------------------------------------------------------------------------
# v0.8.2: legacy non-Unicode CJK subtitle charsets. Blind latin-1 turns
# double-byte GB2312/Big5/Shift-JIS text into mojibake langdetect can't
# read; charset-normalizer decodes them correctly so detection works.
# ---------------------------------------------------------------------------

def test_clean_srt_bytes_gb2312_chinese():
    from backend.scanner import _clean_srt_bytes
    from backend.language_detection import detect_subtitle_language
    srt = ("1\n00:00:05,000 --> 00:00:08,000\n这从未发生过 从未发生\n\n"
           "2\n00:00:09,000 --> 00:00:12,000\n这学校这棒 真是他妈的棒 你看到了吗\n\n"
           "3\n00:00:13,000 --> 00:00:16,000\n这儿真漂亮 你来这儿上学的吗\n")
    txt = _clean_srt_bytes(srt.encode("gb2312"))
    assert txt is not None and "这" in txt, "GB2312 must decode to real Chinese, not mojibake"
    lang, conf = detect_subtitle_language(txt)
    assert lang == "chi", f"expected chi, got {lang!r} (conf {conf})"


def test_clean_srt_bytes_shift_jis_japanese():
    from backend.scanner import _clean_srt_bytes
    from backend.language_detection import detect_subtitle_language
    srt = "1\n00:00:01,000 --> 00:00:04,000\nこれは日本語の字幕です。テストのために書いています。\n"
    txt = _clean_srt_bytes(srt.encode("shift-jis"))
    assert txt is not None
    lang, _ = detect_subtitle_language(txt)
    assert lang == "jpn", f"expected jpn, got {lang!r}"


def test_detect_subtitle_regional_code_normalized():
    """langdetect returns zh-cn/zh-tw for Chinese — both must map to chi."""
    from backend.language_detection import detect_subtitle_language
    zh = "这是一段中文字幕 用来测试语言检测功能 希望能够正确识别为中文"
    lang, _ = detect_subtitle_language(zh)
    assert lang == "chi", f"expected chi from zh-xx, got {lang!r}"


def test_detect_external_subtitles_uses_provided_siblings(tmp_path):
    """v0.9.107: when a sibling list is passed, detection matches against it
    (no directory read) — lets the watcher reuse its walk's file listing."""
    (tmp_path / "Movie.mkv").write_text("")
    (tmp_path / "Movie.eng.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\nHi\n")
    res = detect_external_subtitles(str(tmp_path / "Movie.mkv"), siblings=list(tmp_path.iterdir()))
    assert any(r["external_path"].endswith("Movie.eng.srt") for r in res)


def test_merge_external_subs_no_change_skips_write():
    """Identical external-sub set → changed=False so the watcher won't write."""
    from backend.scanner import merge_external_subs
    stored = [
        {"stream_index": 0, "language": "eng", "codec": "subrip", "external": False, "keep": True},
        {"stream_index": -1, "language": "eng", "codec": "subrip", "external": True,
         "external_path": "/m/Ep.eng.srt", "keep": True},
    ]
    cur_ext = [{"language": "eng", "codec": "subrip", "external_path": "/m/Ep.eng.srt", "forced": False}]
    changed, new_subs, has_ext, has_rem = merge_external_subs(stored, "eng", cur_ext)
    assert changed is False
    assert new_subs is stored
    assert has_ext is True


def test_merge_external_subs_adds_sub():
    """A new sidecar sub is classified and merged; embedded tracks preserved."""
    from unittest.mock import patch
    from backend.scanner import merge_external_subs
    stored = [{"stream_index": 0, "language": "eng", "codec": "subrip", "external": False, "keep": True}]
    cur_ext = [{"language": "eng", "codec": "subrip", "title": "", "forced": False,
                "external_path": "/m/Ep.eng.srt"}]
    with patch("backend.scanner._load_sub_settings", return_value=({"eng"}, True)), \
         patch("backend.scanner._is_cleanup_enabled", return_value=True):
        changed, new_subs, has_ext, has_rem = merge_external_subs(stored, "eng", cur_ext)
    assert changed is True and has_ext is True
    assert len(new_subs) == 2                                   # embedded + new external
    ext = [t for t in new_subs if t.get("external")]
    assert len(ext) == 1 and ext[0]["external_path"] == "/m/Ep.eng.srt"
    assert ext[0]["stream_index"] == -1


def test_merge_external_subs_removes_sub():
    """A deleted sidecar sub is dropped and has_external clears; embedded kept."""
    from backend.scanner import merge_external_subs
    stored = [
        {"stream_index": 0, "language": "eng", "codec": "subrip", "external": False, "keep": True},
        {"stream_index": -1, "language": "eng", "codec": "subrip", "external": True,
         "external_path": "/m/Ep.eng.srt", "keep": True},
    ]
    changed, new_subs, has_ext, has_rem = merge_external_subs(stored, "eng", [])
    assert changed is True and has_ext is False
    assert len(new_subs) == 1 and all(not t.get("external") for t in new_subs)


# --- v0.9.114: convert-time readability guard -----------------------------
import asyncio


def test_external_sub_is_readable_valid_srt(tmp_path):
    """A well-formed .srt probes as a subtitle stream → readable."""
    from backend.converter import _external_sub_is_readable
    srt = tmp_path / "Movie.eng.srt"
    srt.write_text("1\n00:00:01,000 --> 00:00:02,000\nhello\n\n"
                   "2\n00:00:03,000 --> 00:00:04,000\nworld\n")
    assert asyncio.run(_external_sub_is_readable(str(srt))) is True


def test_external_sub_is_readable_empty_file(tmp_path):
    """An empty sidecar (0 bytes) is unreadable → dropped, not merged.
    This is the class of file that aborted the encode with exit 183."""
    from backend.converter import _external_sub_is_readable
    srt = tmp_path / "Movie.eng.srt"
    srt.write_bytes(b"")
    assert asyncio.run(_external_sub_is_readable(str(srt))) is False


def test_external_sub_is_readable_garbage(tmp_path):
    """A binary-garbage file with a .srt name is unreadable → dropped."""
    from backend.converter import _external_sub_is_readable
    srt = tmp_path / "Movie.eng.srt"
    srt.write_bytes(bytes(range(256)) * 8)
    assert asyncio.run(_external_sub_is_readable(str(srt))) is False
