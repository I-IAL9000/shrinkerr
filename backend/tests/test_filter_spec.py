"""The Scanner's filters are defined once (F23, v0.10.0).

The pill counts, the folder tree, the file lists and the "selected folders /
select all + filter" actions used to be four implementations that drifted.
Now they evaluate the same definitions — these tests hold them together and
pin what each filter means, plus how a filter string combines: ids in a group
match any, groups must all match, "!id" excludes.
"""
import json
from datetime import datetime, timedelta, timezone

import aiosqlite
import pytest
import pytest_asyncio

import backend.routes.scan as scan_route
from backend import scan_filters
from backend.scan_filters import FILTERS, Filter, parse_filter

GB = 1024 ** 3
NOW = datetime.now(timezone.utc).isoformat()


def days_ago(n):
    return (datetime.now(timezone.utc) - timedelta(days=n)).isoformat()


def AGO(n):  # file_mtime (epoch seconds) n days ago
    import time
    return time.time() - n * 86400


def tracks(*specs):
    """JSON track list: (language, codec[, channels[, title]])."""
    out = []
    for spec in specs:
        lang, codec, channels, title = (*spec, None, None)[:4]
        out.append({"language": lang, "codec": codec, "channels": channels, "title": title or "", "profile": ""})
    return json.dumps(out)

# name -> scan_results columns
ROWS = {
    "dune": dict(file_path="/media/Movies/Dune (2021) [tt1160419]/Dune.2021.2160p.UHD.BluRay.REMUX.mkv",
                 file_size=60 * GB, duration=9000, video_codec="hevc", video_width=3840, video_height=1600,
                 needs_conversion=0, has_lossless_audio_flag=1, language_source="api", video_bit_depth=10,
                 audio_tracks_json=tracks(("eng", "truehd", 8, "TrueHD Atmos 7.1")),
                 subtitle_tracks_json=tracks(("eng", "hdmv_pgs_subtitle"))),
    "heat_br": dict(file_path="/media/Movies/Heat (1995)/Heat.1995.1080p.BluRay.x264.mkv",
                    file_size=12 * GB, duration=10000, video_codec="h264", video_width=1920, video_height=800,
                    needs_conversion=1, has_removable_tracks_flag=1, dup_count=2, language_source="api",
                    health_status="healthy", health_checked_at=days_ago(120), video_fps=50.0,
                    audio_tracks_json=tracks(("eng", "dts", 6, "DTS-HD MA 5.1"), ("eng", "ac3", 2, "Director's Commentary")),
                    subtitle_tracks_json=tracks(("eng", "subrip", None, "English SDH"))),
    "heat_web": dict(file_path="/media/Movies/Heat (1995)/Heat.1995.1080p.WEB-DL.mkv",
                     file_size=3 * GB, duration=10000, video_codec="h264", video_width=1920, video_height=1080,
                     needs_conversion=1, dup_count=2, language_source="api", file_mtime=AGO(20),
                     audio_tracks_json=tracks(("eng", "eac3", 6))),  # ~2.6 Mbps: low bitrate; failed before
    "ep1": dict(file_path="/media/TV/Show [tvdb-1]/S01/Show.S01E01.720p.HDTV.mkv",
                file_size=1 * GB, duration=2600, video_codec="h264", video_width=1280, video_height=720,
                needs_conversion=1, has_und_tracks_flag=1, hdr_format="hdr10", file_mtime=AGO(3), video_bit_depth=10,
                audio_tracks_json=tracks(("und", "aac", 2))),  # ignored below
    "ep2": dict(file_path="/media/TV/Show [tvdb-1]/S01/Show.S01E02.720p.HDTV.mkv",
                file_size=int(1.2 * GB), duration=2600, video_codec="h264", video_width=1280, video_height=720,
                needs_conversion=1, is_dubbed_flag=1, language_source="api", has_external_subs_flag=1,
                audio_tracks_json=tracks(("eng", "aac", 2))),  # queued below
    "clip": dict(file_path="/media/Other/Clip.avi", file_size=int(0.7 * GB), duration=1800,
                 video_codec="mpeg4", video_width=640, video_height=480, needs_conversion=1,
                 has_removable_subs_flag=1, audio_tracks_json=tracks(("fra", "mp3", 2))),
    "alien": dict(file_path="/media/Movies/Alien (1979)/VIDEO_TS/VIDEO_TS.IFO", file_size=7 * GB, duration=7000,
                  video_codec="mpeg2video", video_width=720, video_height=576, needs_conversion=1,
                  disc_type="dvd", new_detected_at=NOW, language_source="manual", video_interlaced=1,
                  audio_tracks_json=tracks(("eng", "ac3", 6)), subtitle_tracks_json=tracks(("fra", "dvd_subtitle"))),
    "old": dict(file_path="/media/Movies/Old (1950)/Old.1950.DVDRip.mkv", file_size=int(1.5 * GB), duration=5400,
                video_codec="hevc", video_width=720, video_height=540, needs_conversion=0, converted=1,
                vmaf_score=95.0, language_source="api", health_status="warnings", health_checked_at=days_ago(10),
                audio_tracks_json=tracks(("eng", "aac", 2))),  # came out larger once
    "broken": dict(file_path="/media/Movies/Broken/broken.mkv", file_size=0, duration=0, video_codec=None,
                   probe_status="error", needs_conversion=0),
    "corrupt": dict(file_path="/media/Movies/Corrupt/c.mkv", file_size=6 * GB, duration=6000, video_codec="av1",
                    video_width=1920, video_height=1080, needs_conversion=0, health_status="corrupt",
                    vmaf_score=85.0, vmaf_uncertain=1, language_source="api", hdr_format="hlg",
                    audio_tracks_json=tracks(("eng", "opus", 2))),
    "big": dict(file_path="/media/Movies/Big/Big.1080p.mp4", file_size=30 * GB, duration=7200, video_codec="h264",
                video_width=1920, video_height=1080, needs_conversion=1, vmaf_score=90.0, language_source="api",
                video_vfr=1, video_fps=23.976,
                audio_tracks_json=tracks(("spa", "aac", 2)),
                subtitle_tracks_json=json.dumps([{"language": "eng", "codec": "subrip", "title": "", "forced": True}])),  # VMAF-rejected
    # Two source tokens: counted once, by the first match.
    "web_in_br": dict(file_path="/media/Movies/Blu-ray Shelf/Film.2015.1080p.WEB-DL.mkv", file_size=4 * GB,
                      duration=7200, video_codec="h264", video_width=1920, video_height=1080,
                      needs_conversion=1, language_source="api", hdr_format="dv8",
                      audio_tracks_json=tracks(("eng", "aac", 2))),
    "wmv": dict(file_path="/media/Other/Home Video.wmv", file_size=GB // 2, duration=1800, video_codec="wmv3",
                video_width=640, video_height=480, needs_conversion=0, language_source="api",
                audio_tracks_json=tracks(("eng", "wmav2", 2))),
}
REMOVED = dict(file_path="/media/Movies/Gone/gone.mkv", file_size=5 * GB, duration=100, video_codec="h264",
               needs_conversion=1, removed_from_list=1)
PATH = {k: v["file_path"] for k, v in ROWS.items()}
NAME = {v: k for k, v in PATH.items()}


@pytest_asyncio.fixture
async def lib(test_db, monkeypatch):
    monkeypatch.setattr(scan_route, "DB_PATH", test_db)
    async with aiosqlite.connect(test_db) as db:
        for row in [*ROWS.values(), REMOVED]:
            cols = {"scan_timestamp": NOW, **row}
            await db.execute(
                f"INSERT INTO scan_results ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
                list(cols.values()))
        await db.executemany("INSERT INTO media_dirs (path, label) VALUES (?, ?)",
                             [("/media/Movies", "Movies"), ("/media/TV", "TV Shows"), ("/media/Other", "Other")])
        await db.execute("INSERT INTO ignored_files (file_path, reason, ignored_at) VALUES (?, 'manual', ?)",
                         (PATH["ep1"], NOW))
        await db.execute("INSERT INTO jobs (file_path, job_type, status, created_at) VALUES (?, 'convert', 'pending', ?)",
                         (PATH["ep2"], NOW))
        await db.execute(
            "INSERT INTO plex_metadata_cache (folder_path, metadata_type, metadata_value, synced_at) "
            "VALUES ('/media/Movies/Heat (1995)/', 'watch_status', 'watched', ?)", (NOW,))
        await db.execute("INSERT INTO jobs (file_path, job_type, status, created_at) VALUES (?, 'convert', 'failed', ?)",
                         (PATH["heat_web"], NOW))
        await db.execute("INSERT INTO jobs (file_path, job_type, status, created_at, error_key) "
                         "VALUES (?, 'convert', 'completed', ?, 'errors.vmafRejected')", (PATH["big"], NOW))
        await db.execute("INSERT INTO ignored_files (file_path, reason, ignored_at) VALUES (?, 'conversion_larger', ?)",
                         (PATH["old"], NOW))
        # Originals kept 7 days: Old's backup is fresh, Dune's has expired,
        # Heat's conversion was undone.
        await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('backup_original_days', '7')")
        for name, status, done in (("old", "completed", days_ago(2)), ("dune", "completed", days_ago(30)),
                                   ("heat_web", "reverted", days_ago(1))):
            await db.execute(
                "INSERT INTO jobs (file_path, job_type, status, created_at, completed_at, backup_path) "
                "VALUES (?, 'convert', ?, ?, ?, '/backups/x.mkv')", (PATH[name], status, done, done))
        await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('always_keep_languages', '[\"eng\"]')")
        await db.commit()
    return test_db


async def names(filter_text, folders=None):
    return {NAME[p] for p in await scan_route._paths_matching(filter_text, folders)}


@pytest.mark.asyncio
async def test_every_count_matches_its_lists(lib):
    """Pill count == Add-to-Queue resolution == the tree == the file lists,
    for every filter. This is what drifted before."""
    counts = (await scan_route.get_scan_stats())["counts"]
    assert counts["all"] == len(ROWS)
    for fid in FILTERS:
        listed = await names(fid)
        assert counts[fid] == len(listed), fid
        tree = (await scan_route.get_scan_tree(filter=fid))["folders"]
        folders = [f for f in tree if not f.get("is_file")]
        assert sum(f["file_count"] for f in folders) == len(listed), fid
        in_files = set()
        for f in folders:
            in_files |= {NAME[r["file_path"]] for r in await scan_route.get_scan_files(f["path"], filter=fid)}
        assert in_files == listed, fid


EXPECTED = {
    "x264": {"heat_br", "heat_web", "ep1", "ep2", "big", "web_in_br"},
    "x265": {"dune", "old"},
    "av1": {"corrupt"},
    "codec_mpeg2": {"alien"},
    "codec_mpeg4": {"clip"},
    "codec_vc1": {"wmv"},
    "misc_codec": {"broken"},  # no codec (probe failed) is "other"
    "container_mkv": {"dune", "heat_br", "heat_web", "ep1", "ep2", "old", "broken", "corrupt", "web_in_br"},
    "container_mp4": {"big"},
    "container_avi": {"clip"},
    "container_other": {"alien", "wmv"},  # discs too
    "res_4k": {"dune"},
    "res_1080p": {"heat_br", "heat_web", "corrupt", "big", "web_in_br"},  # 1920x800 scope is 1080p
    "res_720p": {"ep1", "ep2"},
    "needs_conversion": {"heat_br", "clip", "alien", "big", "ep2", "web_in_br"},  # not low-bitrate, not ignored
    "low_bitrate": {"heat_web"},
    "high_bitrate": {"big"},
    "ignored": {"ep1", "old"},
    "queued": {"ep2"},
    "converted": {"old"},
    "new": {"alien"},
    "disc_iso": {"alien"},
    "duplicates": {"heat_br", "heat_web"},
    "corrupt": {"broken", "corrupt"},
    # Cleanup and language filters include ignored titles (v0.9.26/31); the
    # audio-cleanup count used to leave out untagged-only files.
    "audio_cleanup": {"heat_br", "ep1"},
    "unknown_language": {"ep1"},
    "sub_cleanup": {"clip"},
    "dubbed": {"ep2"},
    "not_api_matched": {"ep1", "clip", "broken"},
    "lossless_audio": {"dune"},
    "object_audio": {"dune"},  # "Atmos" in the title; "DTS-HD MA" isn't DTS:X
    "audio_71": {"dune"},
    "image_subs": {"dune", "alien"},
    "external_subs": {"ep2"},
    "forced_subs": {"big"},
    "sdh_subs": {"heat_br"},
    "commentary": {"heat_br"},
    # The probe's HDR format (the name for files scanned before it was stored)
    "bit10": {"dune", "ep1"},
    "hi10p": {"ep1"},  # H.264 10-bit
    "interlaced": {"alien"},
    "vfr": {"big"},
    "hdr_dv": {"web_in_br"},
    "hdr_hdr10": {"ep1"},
    "hdr_hlg": {"corrupt"},
    # No audio or subtitle in the keep languages (eng): French, untagged, none.
    "missing_language": {"clip", "ep1", "broken"},
    "health_never": {"dune", "heat_web", "ep1", "ep2", "clip", "alien", "broken", "big", "web_in_br", "wmv"},
    "health_warnings": {"old"},
    "health_stale": {"heat_br"},  # checked 120 days ago
    "undo_possible": {"old"},
    # Found by the watcher (Alien, today) or written to disk lately.
    "added_7d": {"ep1", "alien"},
    "added_30d": {"ep1", "alien", "heat_web"},
    "added_90d": {"ep1", "alien", "heat_web"},
    "failed_before": {"heat_web"},
    "vmaf_rejected": {"big"},
    "no_savings": {"old"},
    # Sources: one each, first match wins (a Blu-ray remux is a remux).
    "src_remux": {"dune"},
    "src_bluray": {"heat_br", "web_in_br"},
    "src_webdl": {"heat_web"},
    "src_hdtv": {"ep1", "ep2"},
    "src_dvd": {"old"},
    "type_movie": {"dune", "heat_br", "heat_web", "alien", "old", "broken", "corrupt", "big", "web_in_br"},
    "type_tv": {"ep1", "ep2"},
    "type_other": {"clip", "wmv"},
    "plex_watched": {"heat_br", "heat_web"},
    "vmaf_excellent": {"old"},
    "vmaf_good": {"big"},
    "vmaf_poor": {"corrupt"},
    "vmaf_uncertain": {"corrupt"},
    "size_large": {"dune", "heat_br", "big"},
    "size_medium": {"alien", "corrupt"},
}


@pytest.mark.asyncio
@pytest.mark.parametrize("fid", sorted(EXPECTED))
async def test_what_each_filter_matches(lib, fid):
    assert await names(fid) == EXPECTED[fid]


@pytest.mark.asyncio
async def test_groups_or_inside_and_across_and_exclusions(lib):
    # Same group: any. "4K + 1080p" used to return nothing.
    assert await names("res_4k,res_1080p") == EXPECTED["res_4k"] | EXPECTED["res_1080p"]
    # Different groups: all.
    assert await names("res_1080p,x264") == EXPECTED["res_1080p"] & EXPECTED["x264"]
    assert await names("res_4k,res_1080p,x265") == {"dune"}
    # Status pills have no group: combining them narrows.
    assert await names("needs_conversion,duplicates") == {"heat_br"}
    # Exclusions, of SQL and of Python filters; a NULL is "not matched".
    assert await names("x264,!res_720p") == EXPECTED["x264"] - EXPECTED["res_720p"]
    assert await names("!x265") == set(ROWS) - EXPECTED["x265"]
    assert await names("!vmaf_poor") == set(ROWS) - {"corrupt"}
    assert await names("type_movie,!ignored,!converted") == EXPECTED["type_movie"] - {"old"}
    assert await names("!queued,!ignored") == set(ROWS) - {"ep1", "ep2", "old"}
    assert await names("container_avi,container_mp4,!missing_language") == {"big"}
    # "all", empty and unknown ids (an old bookmark) don't filter.
    for text in ("all", "", "no_such_filter", "all,no_such_filter"):
        assert await names(text) == set(ROWS), text


@pytest.mark.asyncio
async def test_folder_selections_go_through_the_filter(lib):
    folders = ["/media/Movies/Heat (1995)/", "/media/TV/"]
    assert await names("all", folders) == {"heat_br", "heat_web", "ep1", "ep2"}
    assert await names("dubbed", folders) == {"ep2"}  # matched nothing before (F23)
    assert await names("needs_conversion", folders) == {"heat_br", "ep2"}


@pytest.mark.asyncio
async def test_a_group_mixing_sql_and_python_filters(lib, monkeypatch):
    """An SQL filter that shares a group with a Python one is evaluated per
    row (selected as a column) instead of in the WHERE clause."""
    monkeypatch.setitem(FILTERS, "fake_clip", Filter("fake_clip", "codec", py=lambda r, c: r["file_path"].endswith(".avi")))
    expr = parse_filter("x265,fake_clip")
    assert expr.select_sql and not expr.where_sql
    assert await names("x265,fake_clip") == {"dune", "old", "clip"}
    assert await names("x265,fake_clip,res_720p") == set()
    assert await names("misc_codec,fake_clip") == {"clip", "broken"}


@pytest.mark.asyncio
async def test_python_filters_narrow_only_by_a_necessary_condition(lib):
    """pre_sql may only rule out rows the Python predicate would reject."""
    for f in FILTERS.values():
        if not f.pre_sql:
            continue
        async with aiosqlite.connect(lib) as db:
            async with db.execute(f"SELECT file_path FROM scan_results WHERE removed_from_list = 0 AND ({f.pre_sql})") as cur:
                candidates = {NAME[r[0]] for r in await cur.fetchall()}
        assert await names(f.id) <= candidates, f.id


def test_parse_filter_shapes_the_query():
    expr = parse_filter("res_4k,res_1080p,!x265,ignored")
    assert expr.where_sql.count(" AND ") >= 2 and "NOT COALESCE" in expr.where_sql
    assert expr.needs_ctx  # "ignored" needs the ignore list
    assert not parse_filter("res_4k,!x265").needs_ctx
    assert parse_filter("all").is_all and parse_filter("").is_all
    # needs_conversion narrows by its necessary condition.
    assert "needs_conversion != 0" in parse_filter("needs_conversion").where_sql


def test_every_filter_is_sql_or_python():
    for f in FILTERS.values():
        assert bool(f.sql) != bool(f.py), f.id
        assert not (f.pre_sql and f.sql), f.id
    assert scan_filters.FILTERS is FILTERS


@pytest.mark.asyncio
async def test_missing_language_needs_your_languages(lib):
    """With no keep languages set, "missing my language" can't know what's
    missing, so it matches nothing; subtitle languages count too."""
    async with aiosqlite.connect(lib) as db:
        await db.execute("UPDATE settings SET value = '[]' WHERE key = 'always_keep_languages'")
        await db.commit()
    assert await names("missing_language") == set()
    async with aiosqlite.connect(lib) as db:
        await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('sub_keep_languages', '[\"FRA\"]')")
        await db.commit()
    # French audio or subtitles now count as yours (case-insensitive).
    assert await names("missing_language") == set(ROWS) - {"clip", "alien"}


@pytest.mark.asyncio
async def test_extras_and_samples(lib):
    extras = ["/media/Movies/Heat (1995)/Featurettes/Making Of.mkv", "/media/Movies/Heat (1995)/Heat-trailer.mkv",
              "/media/Movies/Heat (1995)/Sample/heat.mkv", "/media/Movies/Film (2020)/film.sample.mkv",
              "/media/Movies/Film (2020)/sample.mkv", "/media/TV/Show/Season 1/Behind The Scenes/bts.mkv"]
    not_extras = ["/media/Other/Home Movie.mkv", "/media/TV/Show/Season 1/Show - S01E03 - The Scene.mkv",
                  "/media/Movies/Sampler (2019)/Sampler.mkv"]
    async with aiosqlite.connect(lib) as db:
        for fp in extras + not_extras:
            await db.execute("INSERT INTO scan_results (file_path, file_size, scan_timestamp) VALUES (?, 1, ?)",
                             (fp, NOW))
        await db.commit()
    assert set(await scan_route._paths_matching("extras")) == set(extras)


@pytest.mark.asyncio
async def test_undo_possible_follows_the_backup_setting(lib):
    async with aiosqlite.connect(lib) as db:  # originals kept: every backup counts
        await db.execute("UPDATE settings SET value = '0' WHERE key = 'backup_original_days'")
        await db.commit()
    assert await names("undo_possible") == {"old", "dune"}
    async with aiosqlite.connect(lib) as db:
        await db.execute("UPDATE settings SET value = '60' WHERE key = 'backup_original_days'")
        await db.commit()
    assert await names("undo_possible") == {"old", "dune"}


def test_the_filter_bar_groups_are_the_server_groups():
    """The pills shown under a group heading are the ones the server ORs
    together — the bar's hint says so. Every pill is a known filter."""
    import re
    from pathlib import Path
    src = (Path(__file__).resolve().parents[2] / "frontend/src/components/FilterBar.tsx").read_text()
    heading_group = {"_video": "codec", "_res": "resolution", "_size": "size", "_audio": "audio",
                     "_lang": "language", "_plex": "plex", "_type": "type", "_source": "source",
                     "_vmaf": "vmaf", "_container": "container", "_subs": "subtitles",
                     "_health": "health", "_outcome": "outcome", "_hdr": "hdr", "_added": "added", "_picture": "picture"}
    group = None
    seen = 0
    for key, divider in re.findall(r'\{ key: "([^"]+)"[^}]*?(group: "divider")?\s*\}', src):
        if divider:
            group = heading_group[key]
            continue
        if key == "all":
            continue
        assert key in FILTERS, key
        assert FILTERS[key].group == group, (key, FILTERS[key].group, group)
        seen += 1
    assert seen >= 50
