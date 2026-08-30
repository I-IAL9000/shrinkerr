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


def test_path_scope_clause_builds_fragment():
    """v0.9.106: the health-check scope fragment matches files under each given
    folder path; empty paths match nothing."""
    from backend.routes.scan import _path_scope_clause
    frag, params = _path_scope_clause(["/media/Movies/HD 2020", "/media/TV1/TV1/"])
    assert frag == "(file_path LIKE ? OR file_path LIKE ?)"
    assert params == ["/media/Movies/HD 2020/%", "/media/TV1/TV1/%"]
    assert _path_scope_clause([]) == ("0", [])


def test_path_scope_clause_filters_rows():
    """The scope fragment, run against SQLite, returns only files under the
    scanned folder — a one-folder rescan won't sweep other folders."""
    import sqlite3
    from backend.routes.scan import _path_scope_clause
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE scan_results (file_path TEXT)")
    rows = [
        "/media/Misc/Movies2/Rear Window (1998) [tt0166322]/VIDEO_TS/VIDEO_TS.IFO",
        "/media/Misc/Movies2/Rear Window (1998) [tt0166322]/extra.mkv",
        "/media/M2T2/TV4/Jane the Virgin/s.mkv",   # unrelated folder
    ]
    db.executemany("INSERT INTO scan_results VALUES (?)", [(r,) for r in rows])
    frag, params = _path_scope_clause(["/media/Misc/Movies2/Rear Window (1998) [tt0166322]"])
    got = [r[0] for r in db.execute(
        f"SELECT file_path FROM scan_results WHERE {frag}", params).fetchall()]
    assert got == rows[:2]           # only the Rear Window folder's files
    assert "/media/M2T2/TV4/Jane the Virgin/s.mkv" not in got


def test_preserve_authoritative_tracks_same_layout():
    """v0.9.112: a heuristic re-scan of an authoritative row with unchanged
    stream layout keeps the STORED tracks (no chi drift, manual edits kept)."""
    from backend.routes.scan import _maybe_preserve_authoritative_tracks
    fresh = '[{"stream_index":1,"language":"chi","keep":true},{"stream_index":5,"language":"eng","keep":true}]'
    stored = '[{"stream_index":1,"language":"chi","keep":false},{"stream_index":5,"language":"eng","keep":true}]'
    a, s = _maybe_preserve_authoritative_tracks("heuristic", "api", fresh, None, stored, None)
    assert a == stored and s is None       # preserved: chi stays removed


def test_preserve_authoritative_tracks_layout_changed_uses_fresh():
    """If the stream layout changed, re-classify (use the fresh tracks)."""
    from backend.routes.scan import _maybe_preserve_authoritative_tracks
    fresh = '[{"stream_index":1,"language":"chi","keep":true},{"stream_index":9,"language":"jpn","keep":true}]'
    stored = '[{"stream_index":1,"language":"chi","keep":false},{"stream_index":5,"language":"eng","keep":true}]'
    a, _ = _maybe_preserve_authoritative_tracks("heuristic", "api", fresh, None, stored, None)
    assert a == fresh


def test_preserve_authoritative_tracks_api_scan_uses_fresh():
    """When the scan itself resolved an authoritative native, use its fresh tracks."""
    from backend.routes.scan import _maybe_preserve_authoritative_tracks
    fresh = '[{"stream_index":1,"language":"chi","keep":false}]'
    stored = '[{"stream_index":1,"language":"chi","keep":true}]'
    a, _ = _maybe_preserve_authoritative_tracks("api", "api", fresh, None, stored, None)
    assert a == fresh


def test_preserve_authoritative_tracks_nonauth_existing_uses_fresh():
    """A heuristic existing row isn't authoritative — nothing to preserve."""
    from backend.routes.scan import _maybe_preserve_authoritative_tracks
    fresh = '[{"stream_index":1,"language":"chi","keep":true}]'
    stored = '[{"stream_index":1,"language":"chi","keep":false}]'
    a, _ = _maybe_preserve_authoritative_tracks("heuristic", "heuristic", fresh, None, stored, None)
    assert a == fresh
