"""Direct play (v0.10.0): what makes Plex, Jellyfin and Emby transcode a
file on many TVs and streamers, and the fix.

  video      Hi10P (10-bit H.264): almost no hardware decoder plays it.
             Fixed by converting it (to HEVC).
  subs       an image subtitle (PGS, VobSub) with no text subtitle in its
             language: shown, it's burned into the picture. Fixed by reading
             it into SRT (OCR), next to the original.
  audio      no kept audio track in the main language that every player
             decodes (AAC, AC3, E-AC3, MP3): DTS or TrueHD only, say. Fixed by
             adding a stereo compatibility track.
  container  AVI, WMV, MPEG, TS and other old containers. Fixed by the remux
             to MKV every fix does.

Each problem is an SQL condition over scan_results, for the Scanner's
"Direct play" filters and for "Fix for direct play" alike.
"""
from __future__ import annotations

PROBLEMS = ("video", "subs", "audio", "container")
# Audio codecs every player direct-plays.
DIRECT_PLAY_AUDIO = ("aac", "ac3", "eac3", "mp3")
LEGACY_CONTAINERS = (".avi", ".wmv", ".asf", ".mpg", ".mpeg", ".vob", ".flv", ".divx", ".ogm", ".ts",
                     ".m2ts", ".mts")
_IMAGE_SUBS = "('hdmv_pgs_subtitle', 'pgssub', 'dvd_subtitle', 'vobsub', 'dvb_subtitle', 'xsub')"


def _each(column: str, alias: str) -> str:
    # A list only: an object or a list of strings has no tracks.
    return (f"json_each(CASE WHEN json_valid({column}) AND json_type({column}) = 'array' "
            f"THEN {column} ELSE '[]' END) {alias}")


def _kept(alias: str) -> str:
    # First in every WHERE: json_extract only ever sees a track (an object).
    return f"{alias}.type = 'object' AND COALESCE(json_extract({alias}.value, '$.keep'), 1) != 0"


def _field(alias: str, key: str, default: str = "") -> str:
    return f"lower(COALESCE(json_extract({alias}.value, '$.{key}'), '{default}'))"


_CODEC = "lower(COALESCE(video_codec, ''))"

SQL = {
    "video": f"COALESCE(video_bit_depth, 0) >= 10 AND ({_CODEC} LIKE '%264%' OR {_CODEC} LIKE '%avc%')",
    "subs": (
        f"EXISTS (SELECT 1 FROM {_each('subtitle_tracks_json', 't')} WHERE {_kept('t')} "
        f"AND {_field('t', 'codec')} IN {_IMAGE_SUBS} "
        f"AND NOT EXISTS (SELECT 1 FROM {_each('subtitle_tracks_json', 'u')} WHERE {_kept('u')} "
        f"AND {_field('u', 'codec')} NOT IN {_IMAGE_SUBS} "
        f"AND {_field('u', 'language', 'und')} = {_field('t', 'language', 'und')}))"
    ),
    # The main (first kept) track's language has no track every player decodes.
    "audio": (
        f"EXISTS (SELECT 1 FROM (SELECT {_field('t', 'language', 'und')} AS lang FROM "
        f"{_each('audio_tracks_json', 't')} WHERE {_kept('t')} ORDER BY t.key LIMIT 1) main "
        f"WHERE NOT EXISTS (SELECT 1 FROM {_each('audio_tracks_json', 'u')} WHERE {_kept('u')} "
        f"AND {_field('u', 'language', 'und')} = main.lang "
        f"AND {_field('u', 'codec')} IN {tuple(DIRECT_PLAY_AUDIO)}))"
    ),
    # (a disc is its VIDEO_TS.IFO / index.bdmv / .iso: none of these)
    "container": "(" + " OR ".join(f"file_path LIKE '%{ext}'" for ext in LEGACY_CONTAINERS) + ")",  # any case
}


def job_fixes(job: dict) -> set[str]:
    """The problems a job fixes: its `direct_play` — 1 for every one (jobs
    queued before they could be chosen, and "Fix for direct play when
    converting"), else the ones Add to Queue chose that the file has,
    comma-separated (v0.10.0)."""
    value = job.get("direct_play")
    if not value:
        return set()
    if str(value) == "1":
        return set(PROBLEMS)
    return {p for p in str(value).split(",") if p in PROBLEMS}


def remux_fixes(job: dict) -> bool:
    """Whether the job remuxes for a fix — every one but Hi10P, which is the
    conversion's."""
    return bool(job_fixes(job) - {"video"})


async def problems_for(db, file_paths: list[str]) -> dict[str, list[str]]:
    """file_path → its direct-play problems (only files that have some)."""
    cols = ", ".join(f"({SQL[p]}) AS {p}" for p in PROBLEMS)
    out: dict[str, list[str]] = {}
    for i in range(0, len(file_paths), 900):
        chunk = file_paths[i:i + 900]
        async with db.execute(
            f"SELECT file_path, {cols} FROM scan_results WHERE file_path IN ({','.join('?' * len(chunk))})", chunk,
        ) as cur:
            for row in await cur.fetchall():
                found = [p for p in PROBLEMS if row[p]]
                if found:
                    out[row["file_path"]] = found
    return out
