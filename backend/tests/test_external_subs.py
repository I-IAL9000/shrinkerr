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
