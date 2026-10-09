"""Filter × ignored interaction (v0.9.31).

Cleanup and language filters include ignored titles — an ignore rule means
"don't convert", not "don't tidy tracks / don't tell me the audio is untagged".
Only the conversion-oriented filters keep excluding ignored.
"""
import sqlite3
from backend.routes.scan import _matches_single_filter
from backend.resolution import RANKS, resolution_tier, sql_resolution_rank


def _is_4k(height, path, width=0):
    return resolution_tier(width, height, path) == "4k"


def test_is_4k_by_height_and_tag():
    """v0.9.116: 4K can't be judged by height alone. Height >= 1900 OR a
    2160p/UHD/4K path tag; QHD/1080p are excluded. (Rows without a stored
    width; v0.10.0 classifies through backend/resolution.py.)"""
    # 16:9 and flat 4K caught by height.
    assert _is_4k(2160, "/m/Movie.mkv") is True
    assert _is_4k(1920, "/m/Movie 2.00to1.mkv") is True          # 2.00:1 4K
    # Scope 4K (height < 1900) rescued by the filename/path tag — the whole bug.
    assert _is_4k(1600, "/m/Movie 2160p BluRay REMUX.mkv") is True  # 2.40:1
    assert _is_4k(1634, "/m/Movie.UHD.mkv") is True
    assert _is_4k(0, "/media/4K/Movie.mkv") is True                 # unprobed, folder tag
    # Not 4K: QHD and 1080p without any 4K tag.
    assert _is_4k(1440, "/m/Show QHD 1440p.mkv") is False
    assert _is_4k(1080, "/m/Movie 1080p BluRay.mkv") is False
    assert _is_4k(800, "/m/Scope 1080p 1920x800.mkv") is False


def test_is_4k_sub4k_tag_wins_over_stray_4k_token():
    """v0.9.117: an explicit sub-4K tag in the path beats a stray 4K/UHD
    token elsewhere — a 1080p title under a /4K/ folder (or a UHD-edition
    tag on a 1080p rip) must NOT count as 4K."""
    assert _is_4k(1080, "/media/4K/Dune (2021)/Dune 1080p BluRay.mkv") is False
    assert _is_4k(1080, "/media/UHD Remux/Movie 1080p edition.mkv") is False
    assert _is_4k(720, "/media/Movies/Old 4K restoration 720p.mkv") is False
    # A real scope-4K release (2160p, no sub-4K tag) is still caught.
    assert _is_4k(1600, "/media/4K/Dune (2021)/Dune 2160p UHD.mkv") is True


def test_width_decides_the_tier():
    """SC-22: with the width stored, wide frames land in their real tier and
    path tags no longer matter."""
    assert resolution_tier(1920, 800) == "1080p"     # was 720p by height
    assert resolution_tier(1280, 534) == "720p"      # was SD
    assert resolution_tier(2560, 1440) == "1080p"    # was 4K for rules and CQ
    assert resolution_tier(3840, 1600) == "4k"       # scope 4K, no tag needed
    assert resolution_tier(3840, 1920) == "4k"
    assert resolution_tier(1440, 1080) == "1080p"    # 4:3 HD
    assert resolution_tier(720, 576) == "sd"
    assert resolution_tier(0, 0) is None
    # A 1080p file in a "/4K/" library folder is 1080p once its width is known.
    assert resolution_tier(1920, 1080, "/media/4K/Movie.mkv") == "1080p"
    # Width unknown (scanned before v0.10.0): a 1080p tag lifts a scope film.
    assert resolution_tier(0, 800, "/m/Scope 1080p.mkv") == "1080p"
    assert resolution_tier(0, 534, "/m/Scope 720p.mkv") == "720p"


def test_resolution_python_matches_sql():
    """The SQL used for the Scanner's counts and lists must agree with the
    Python classifier for every row — drift is what made chip counts and
    lists disagree."""
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE t(file_path TEXT, video_width INT, video_height INT)")
    rows = [
        ("/media/4K/Dune 1080p.mkv", 0, 1080), ("/m/Dune 2160p UHD.mkv", 0, 1600),
        ("/m/Flat 2160p.mkv", 0, 2160), ("/m/Regular 1080p.mkv", 0, 1080),
        ("/m/QHD 1440p.mkv", 0, 1440), ("/media/UHD/Movie 1080p.mkv", 0, 1080),
        ("/m/no tags.mkv", 0, 1080), ("/m/Old 4K 720p.mkv", 0, 720),
        ("/m/scope untagged.mkv", 0, 1600), ("/media/4k/unprobed.mkv", 0, 0),
        ("/m/nothing.mkv", 0, 0), ("/m/Scope 1080p.mkv", 0, 800), ("/m/dvd 480p.mkv", 0, 0),
        ("/m/Scope.mkv", 1920, 800), ("/m/Scope 720p.mkv", 1280, 534),
        ("/m/QHD.mkv", 2560, 1440), ("/media/4K/Movie.mkv", 1920, 1080),
        ("/m/scope4k.mkv", 3840, 1600), ("/m/dvd.mkv", 720, 576), ("/m/null.mkv", None, None),
    ]
    c.executemany("INSERT INTO t VALUES(?,?,?)", rows)
    rank_sql = sql_resolution_rank()
    for p, w, h in rows:
        py = RANKS.get(resolution_tier(w, h, p), 0)
        sql = c.execute(f"SELECT {rank_sql} FROM t WHERE file_path = ?", (p,)).fetchone()[0]
        assert py == sql, f"drift on {p!r} {w}x{h}: py={py} sql={sql}"


def test_res_4k_filter_includes_scope_4k():
    """The res_4k filter matches a tagged scope-4K title (vh 1600), which the
    old height>=2000/height>=1400 rules under- or mis-counted."""
    assert _matches_single_filter(
        {"video_height": 1600, "file_path": "/m/Dune 2160p UHD BluRay.mkv"}, "res_4k") is True
    assert _matches_single_filter(
        {"video_height": 2160, "file_path": "/m/x.mkv"}, "res_4k") is True
    assert _matches_single_filter(
        {"video_height": 1440, "file_path": "/m/QHD 1440p.mkv"}, "res_4k") is False


def test_res_1080p_excludes_tagged_4k():
    """A tagged scope-4K row (vh 1600) must NOT also count as 1080p."""
    assert _matches_single_filter(
        {"video_height": 1600, "file_path": "/m/Dune 2160p UHD.mkv"}, "res_1080p") is False
    assert _matches_single_filter(
        {"video_height": 1080, "file_path": "/m/Movie 1080p.mkv"}, "res_1080p") is True


def test_res_filters_use_the_width():
    scope = {"video_width": 1920, "video_height": 800, "file_path": "/m/Scope.mkv"}
    assert _matches_single_filter(scope, "res_1080p") is True
    assert _matches_single_filter(scope, "res_720p") is False
    qhd = {"video_width": 2560, "video_height": 1440, "file_path": "/m/QHD.mkv"}
    assert _matches_single_filter(qhd, "res_4k") is False
    assert _matches_single_filter(qhd, "res_1080p") is True
    small = {"video_width": 1280, "video_height": 534, "file_path": "/m/x.mkv"}
    assert _matches_single_filter(small, "res_720p") is True
    assert _matches_single_filter(small, "res_sd") is False


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
    assert frag == "((file_path >= ? AND file_path < ?) OR (file_path >= ? AND file_path < ?))"
    assert params == ["/media/Movies/HD 2020/", "/media/Movies/HD 20200", "/media/TV1/TV1/", "/media/TV1/TV10"]
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


def test_rule_resolution_uses_the_width():
    from backend.rule_resolver import _detect_resolution
    assert _detect_resolution(1920, 800, "/m/Scope.mkv") == "1080p"
    assert _detect_resolution(2560, 1440, "/m/QHD.mkv") == "1080p"
    assert _detect_resolution(1280, 534, "/m/x.mkv") == "720p"
    assert _detect_resolution(0, 0, "/m/x.mkv") == "SD"  # nothing known: SD, as before
    assert _detect_resolution(0, 0, "/m/Movie.2160p.mkv") == "4K"


import pytest  # noqa: E402


@pytest.mark.asyncio
async def test_resolution_counts_match_the_filtered_lists(test_db, monkeypatch):
    """The chip counts (scan-stats) and the lists behind them (the SQL tree
    filter) come from the same classifier."""
    import aiosqlite
    import backend.routes.scan as scan_route
    monkeypatch.setattr(scan_route, "DB_PATH", test_db)
    rows = [
        ("/m/Scope.mkv", 1920, 800), ("/m/QHD.mkv", 2560, 1440), ("/m/4k.mkv", 3840, 1600),
        ("/m/small.mkv", 1280, 534), ("/m/dvd.mkv", 720, 576),
        ("/m/old 1080p.mkv", 0, 800), ("/media/4K/old.mkv", 0, 1080), ("/m/old.mkv", 0, 480),
    ]
    async with aiosqlite.connect(test_db) as db:
        await db.executemany(
            "INSERT INTO scan_results (file_path, file_size, video_width, video_height, scan_timestamp) "
            "VALUES (?, 1, ?, ?, '2026-01-01')", rows)
        await db.commit()
    stats = (await scan_route.get_scan_stats())["counts"]
    async with aiosqlite.connect(test_db) as db:
        for f in ("res_4k", "res_1080p", "res_720p", "res_sd"):
            frag, params, _ = scan_route._build_tree_sql_filter(f)
            async with db.execute(f"SELECT COUNT(*) FROM scan_results WHERE 1=1 {frag}", params) as cur:
                listed = (await cur.fetchone())[0]
            assert stats[f] == listed, f
    assert (stats["res_4k"], stats["res_1080p"], stats["res_720p"], stats["res_sd"]) == (2, 3, 1, 2)


@pytest.mark.asyncio
async def test_a_scan_stores_the_width(test_db):
    import aiosqlite
    from backend.models import ScannedFile
    from backend.routes.scan import _write_batch_sync

    def scanned(width):
        return ScannedFile(
            file_path="/m/Scope (2010)/scope.mkv", file_name="scope.mkv", folder_name="Scope (2010)",
            file_size=1, file_size_gb=0.0, video_codec="h264", needs_conversion=True,
            audio_tracks=[], native_language="eng", has_removable_tracks=False,
            estimated_savings_bytes=0, estimated_savings_gb=0.0,
            video_width=width, video_height=800,
        )

    _write_batch_sync(test_db, [scanned(1920)], "2026-10-09T00:00:00")
    _write_batch_sync(test_db, [scanned(1916)], "2026-10-09T00:00:01")  # a rescan updates it
    async with aiosqlite.connect(test_db) as db:
        async with db.execute("SELECT video_width, video_height FROM scan_results") as cur:
            assert await cur.fetchall() == [(1916, 800)]
