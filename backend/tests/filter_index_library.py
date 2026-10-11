"""A library in which every Scanner filter matches some files: the demo's
(backend/demo.py), plus what the demo leaves out — ignored files and folders,
Plex, Sonarr / Radarr, Maintainerr, watch history, health checks, VMAF,
HDR, extras, other containers and codecs, and a skip rule. And helpers: the
SQL engine's counts to compare with, and changes like a day of use."""
import json
import time
from datetime import datetime, timedelta, timezone

import aiosqlite

from backend.scan_filters import count_all, encode_advanced, facet_counts, parse_filter

_ADV_ALL = encode_advanced([{"property": "file_name", "op": "contains", "value": "S01"},
                            {"property": "video_height", "op": "gte", "value": 720}])
_ADV_ANY = encode_advanced([{"property": "release_group", "op": "contains", "value": "a"},
                            {"property": "audio_lang", "op": "eq", "value": "fre"}], "any")

_ADV_TITLE = encode_advanced([{"property": "year", "op": "gte", "value": 2000}])

# Filter strings to compare the index with the SQL engine on: groups, OR
# within a group, exclusions, Python filters, Advanced Search (SQL and
# Python conditions, all and any, title metadata).
EXPRESSIONS = [
    "x264", "x264,x265", "x264,res_1080p", "needs_conversion,!ignored", "!x264", "queued",
    "plex_watched,type_tv", "hdr_dv,hdr_hdr10,!res_sd", "audio_cleanup,sub_cleanup",
    "not_watched_6m,leaving_soon", "converted,!vmaf_poor", "failed_before", "missing_language,dubbed",
    "!ignored,!corrupt", _ADV_ALL, f"{_ADV_ALL},x265", f"{_ADV_ANY},!x264",
    _ADV_TITLE,
]


async def sql_counts(db_path: str, text: str = "all") -> dict:
    """The counts the SQL engine gives (count_all, or facet_counts under a filter)."""
    from backend.routes.scan import _build_enrichment_context
    expr = parse_filter(text)
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        ctx = await _build_enrichment_context(db, titles=expr.needs_titles)
        if expr.is_all:
            return (await count_all(db, ctx))[0]
        return await facet_counts(db, ctx, expr)


async def sql_savings(db_path: str) -> int:
    from backend.routes.scan import _build_enrichment_context
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        ctx = await _build_enrichment_context(db)
        return (await count_all(db, ctx))[1][1]


async def seed(db_path: str) -> None:
    from backend import demo
    await demo.seed(db_path)
    now = datetime.now(timezone.utc)
    stamp = now.isoformat()
    async with aiosqlite.connect(db_path) as db:
        async with db.execute("SELECT id, file_path FROM scan_results ORDER BY id") as cur:
            rows = await cur.fetchall()
        path = {rid: fp for rid, fp in rows}
        ids = [rid for rid, _ in rows]

        def pick(start: int, step: int = 13, n: int = 6) -> list[int]:
            return ids[start::step][:n]

        def rename(rid: int, new: str) -> tuple:
            path[rid] = new
            return ("file_path = ?", (new,), [rid])

        atmos = json.dumps([{"stream_index": 1, "language": "eng", "codec": "truehd", "channels": 8, "keep": True,
                             "profile": "Dolby TrueHD + Dolby Atmos", "title": ""}])
        french_only = json.dumps([{"stream_index": 1, "language": "fre", "codec": "ac3", "channels": 6,
                                   "keep": True, "profile": "", "title": ""}])
        subs = json.dumps([{"stream_index": 2, "language": "eng", "codec": "subrip", "forced": True, "title": "SDH",
                            "keep": True}])
        updates = [
            ("new_detected_at = ?", ((now - timedelta(hours=2)).isoformat(),), pick(0)),
            ("file_mtime = ?", (time.time() - 3600,), pick(1)),
            ("disc_type = 'bdmv'", (), pick(2, n=2)),
            ("dup_count = 2", (), pick(3, n=4)),
            ("probe_status = 'corrupt'", (), pick(4, n=2)),
            ("health_status = 'warnings', health_checked_at = ?", ((now - timedelta(days=120)).isoformat(),), pick(5)),
            ("health_status = 'healthy', health_checked_at = ?", (stamp,), pick(6)),
            ("link_count = 2", (), pick(7)),
            ("has_und_tracks_flag = 1", (), pick(8)),
            ("has_external_subs_flag = 1", (), pick(9)),
            ("is_dubbed_flag = 1", (), pick(10)),
            ("language_source = 'heuristic'", (), pick(11)),
            ("video_bit_depth = 10", (), pick(12, step=7, n=12)),
            ("video_bit_depth = 10, video_codec = 'h264'", (), pick(14, n=2)),
            ("video_interlaced = 1", (), pick(0, step=17)),
            ("video_vfr = 1", (), pick(1, step=19)),
            ("hdr_format = 'hlg'", (), pick(4, step=31, n=2)),
            ("vmaf_score = 95", (), pick(5, step=11)),
            ("vmaf_score = 89", (), pick(6, step=11)),
            ("vmaf_score = 80, vmaf_uncertain = 1", (), pick(7, step=11, n=3)),
            ("video_codec = 'av1'", (), pick(15, n=2)),
            ("video_codec = 'vp9'", (), pick(16, n=2)),
            ("video_codec = 'mjpeg'", (), pick(17, n=2)),
            ("audio_tracks_json = ?, has_lossless_audio_flag = 1", (atmos,), pick(18, n=3)),
            ("audio_tracks_json = ?, subtitle_tracks_json = '[]'", (french_only,), pick(19, n=3)),
            ("subtitle_tracks_json = ?", (subs,), pick(20, n=3)),
            ("removed_from_list = 1", (), pick(9, step=41, n=3)),
        ]
        movie = path[ids[21]].rsplit("/", 1)[0]
        updates += [
            rename(ids[22], f"{movie}/Extras/Behind the scenes.mkv"),
            rename(ids[23], path[ids[23]].replace(".mkv", ".mp4")),
            rename(ids[24], path[ids[24]].replace(".mkv", ".avi")),
            rename(ids[25], path[ids[25]].replace(".mkv", ".ts")),
            rename(ids[26], "/demo/Other/Home Video (2001) HDTV.mkv"),
        ]
        for sets, params, which in updates:
            await db.execute(f"UPDATE scan_results SET {sets} WHERE id IN ({','.join('?' * len(which))})",
                             (*params, *which))

        await db.executemany("INSERT INTO ignored_files (file_path, reason, ignored_at) VALUES (?, ?, ?)", [
            (path[ids[30]], "manual", stamp),
            ("/demo/TV/Static/", "manual", stamp),
            (path[ids[31]], "conversion_larger", stamp),
        ])
        await db.executemany(
            "INSERT INTO plex_metadata_cache (folder_path, metadata_type, metadata_value, synced_at) "
            "VALUES (?, 'watch_status', ?, ?)", [
                ("/demo/TV/Night Shift/", "watched", stamp),
                ("/demo/TV/Copper Hill/", "unwatched", stamp),
                (movie + "/", "watchlist", stamp),
            ])
        days = 86400
        await db.executemany(
            "INSERT INTO watch_activity (folder_path, server, last_viewed, added_at) VALUES (?, 'plex', ?, ?)", [
                ("/demo/TV/Deep Field/", int(time.time() - 400 * days), int(time.time() - 900 * days)),
                ("/demo/TV/Northbound/", int(time.time() - 800 * days), int(time.time() - 900 * days)),
                ("/demo/TV/Parallel Lines/", int(time.time() - days), int(time.time() - 900 * days)),
            ])
        await db.executemany(
            "INSERT INTO arr_file_status (file_path, service, monitored, cutoff_unmet, synced_at) "
            "VALUES (?, ?, ?, ?, ?)", [
                (path[ids[40]], "radarr", 1, 1, stamp),
                (path[ids[41]], "sonarr", 0, 0, stamp),
            ])
        await db.execute("INSERT INTO maintainerr_media (folder_path, collection, synced_at) "
                         "VALUES ('/demo/TV/Static/', 'Leaving soon', ?)", (stamp,))
        await db.executemany(
            "INSERT INTO jobs (file_path, job_type, status, created_at, completed_at, error_key, backup_path, "
            "space_saved, audio_tracks_to_remove) VALUES (?, 'convert', ?, ?, ?, ?, ?, ?, '[]')", [
                (path[ids[52]], "failed", stamp, stamp, "errors.vmafRejected", None, 0),
                (path[ids[51]], "completed", stamp, stamp, None, "/backups/kept.mkv", 1),
            ])
        await db.execute(
            "INSERT INTO encoding_rules (name, match_type, match_value, action, enabled, created_at) "
            "VALUES ('Skip The Glass House', 'directory', '/demo/TV/The Glass House', 'skip', 1, ?)", (stamp,))
        await db.commit()


async def mutate(db_path: str) -> None:
    """Changes like a day of use: rows edited, hidden, deleted and added; a
    file ignored, one queued, the keep languages changed, a show watched.
    Each step picks its own rows, by what they need to change."""
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(db_path) as db:
        used: list[int] = [-1]

        async def pick(where: str, n: int = 1) -> list[tuple[int, str]]:
            async with db.execute(
                    f"SELECT id, file_path FROM scan_results WHERE removed_from_list = 0 AND ({where}) "
                    f"AND id NOT IN ({','.join('?' * len(used))}) ORDER BY id LIMIT ?", (*used, n)) as cur:
                found = await cur.fetchall()
            used.extend(r[0] for r in found)
            return found

        not_ignored = ("file_path NOT IN (SELECT file_path FROM ignored_files) "
                       "AND file_path NOT LIKE '/demo/TV/Static/%' AND file_path NOT LIKE '/demo/TV/The Glass House/%'")
        edited = await pick(f"needs_conversion = 1 AND video_codec LIKE '%264%' AND {not_ignored}", 2)
        hidden = await pick("1")
        deleted = await pick("1")
        copied = await pick("1")
        ignored = await pick(not_ignored)
        queued = await pick("file_path NOT IN (SELECT file_path FROM jobs WHERE status IN ('pending', 'running'))")
        await db.execute(f"UPDATE scan_results SET video_codec = 'hevc', needs_conversion = 0 "
                         f"WHERE id IN ({','.join('?' * len(edited))})", [r[0] for r in edited])
        await db.execute("UPDATE scan_results SET removed_from_list = 1 WHERE id = ?", (hidden[0][0],))
        await db.execute("DELETE FROM scan_results WHERE id = ?", (deleted[0][0],))
        await db.execute(
            "INSERT INTO scan_results (file_path, file_size, video_codec, needs_conversion, duration, video_height, "
            "video_width, audio_tracks_json, subtitle_tracks_json, scan_timestamp, new_detected_at, "
            "video_conv_savings_bytes) SELECT file_path || '.new.mkv', file_size, video_codec, needs_conversion, "
            "duration, video_height, video_width, audio_tracks_json, subtitle_tracks_json, scan_timestamp, ?, "
            "video_conv_savings_bytes FROM scan_results WHERE id = ?", (now, copied[0][0]))
        await db.execute("INSERT INTO ignored_files (file_path, reason, ignored_at) VALUES (?, 'manual', ?)",
                         (ignored[0][1], now))
        await db.execute("INSERT INTO jobs (file_path, job_type, status, created_at, audio_tracks_to_remove) "
                         "VALUES (?, 'convert', 'pending', ?, '[]')", (queued[0][1], now))
        await db.execute("UPDATE settings SET value = '[\"fre\"]' WHERE key = 'always_keep_languages'")
        await db.execute("INSERT INTO plex_metadata_cache (folder_path, metadata_type, metadata_value, synced_at) "
                         "VALUES ('/demo/TV/Northbound/', 'watch_status', 'watched', ?)", (now,))
        await db.commit()
