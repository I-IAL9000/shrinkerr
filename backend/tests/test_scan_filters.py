"""Filter × ignored interaction (v0.9.31).

Cleanup and language filters include ignored titles — an ignore rule means
"don't convert", not "don't tidy tracks / don't tell me the audio is untagged".
Only the conversion-oriented filters keep excluding ignored.
"""
from backend.routes.scan import _matches_single_filter


def _row(**kw):
    base = {
        "has_removable_tracks": False, "has_und_tracks": False,
        "has_removable_subs": False, "needs_conversion": False,
        "low_bitrate": False, "ignored": False, "file_size": 0, "duration": 0,
    }
    base.update(kw)
    return base


def test_cleanup_and_language_filters_include_ignored():
    assert _matches_single_filter(_row(has_removable_tracks=True, ignored=True), "audio_cleanup") is True
    assert _matches_single_filter(_row(has_und_tracks=True, ignored=True), "audio_cleanup") is True
    assert _matches_single_filter(_row(has_removable_subs=True, ignored=True), "sub_cleanup") is True
    assert _matches_single_filter(_row(has_und_tracks=True, ignored=True), "unknown_language") is True


def test_cleanup_filters_still_require_the_condition():
    # Ignored is no longer the gate, but the actual cleanup condition still is.
    assert _matches_single_filter(_row(ignored=True), "audio_cleanup") is False
    assert _matches_single_filter(_row(ignored=True), "sub_cleanup") is False


def test_conversion_filters_still_exclude_ignored():
    assert _matches_single_filter(_row(needs_conversion=True, ignored=True), "needs_conversion") is False
    assert _matches_single_filter(_row(needs_conversion=True, ignored=False), "needs_conversion") is True
    assert _matches_single_filter(_row(low_bitrate=True, ignored=True), "low_bitrate") is False
    assert _matches_single_filter(_row(low_bitrate=True, ignored=False), "low_bitrate") is True
