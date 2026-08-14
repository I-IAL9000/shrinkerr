import asyncio
import json
import os
from pathlib import Path
from typing import Callable, Optional

from backend.config import settings
from backend.encoding_estimates import video_conv_savings_bytes
from backend.models import AudioTrack, ScannedFile

# v0.7.9: how many ffprobe subprocesses to run concurrently within a scan.
# Default 4 chosen as a balance — meaningful speedup over serial (~4×) without
# overwhelming the host's I/O or memory. Set to 1 to restore pre-v0.7.9 serial
# behavior (useful if you see contention with a busy queue worker on the same
# DB, though WAL + busy_timeout=60s should already prevent the "database is
# locked" errors that bit older parallel scans). Tune via the
# SHRINKERR_SCAN_CONCURRENCY env var.
try:
    SCAN_CONCURRENCY = max(1, int(os.environ.get("SHRINKERR_SCAN_CONCURRENCY", "4")))
except ValueError:
    SCAN_CONCURRENCY = 4


def _classify_disc(folder: Path) -> Optional[str]:
    """Return 'bdmv', 'dvd', or None for a candidate folder.
    BDMV wins on combo discs (Blu-ray is the main feature). v0.6.0+."""
    bdmv_index = folder / "BDMV" / "index.bdmv"
    if bdmv_index.is_file():
        return "bdmv"
    dvd_ifo = folder / "VIDEO_TS" / "VIDEO_TS.IFO"
    if dvd_ifo.is_file():
        return "dvd"
    return None


def _disc_marker_path(folder: Path, disc_type: str) -> Path:
    """Return the real-file path Shrinkerr stores as `scan_results.file_path`
    for a disc item. Always inside the disc subdirectory; this lets every
    existing `os.path.dirname(file_path)` consumer keep working without
    awareness of disc items. v0.6.0+."""
    if disc_type == "bdmv":
        return folder / "BDMV" / "index.bdmv"
    return folder / "VIDEO_TS" / "VIDEO_TS.IFO"


def _disc_total_size(folder: Path, disc_type: str) -> int:
    """Sum bytes of all media-payload files in the disc structure.
    DVD → all `*.VOB` files under VIDEO_TS/; BDMV → all `*.m2ts` under
    BDMV/STREAM/. Used as `scan_results.file_size` for disc items since
    ffprobe's format.size only covers the main title. Returns 0 on any
    error (caller falls back to whatever size estimate they have). v0.6.0+."""
    try:
        if disc_type == "bdmv":
            stream_dir = folder / "BDMV" / "STREAM"
            return sum(f.stat().st_size for f in stream_dir.glob("*.m2ts") if f.is_file())
        return sum(f.stat().st_size for f in (folder / "VIDEO_TS").glob("*.VOB") if f.is_file())
    except OSError:
        return 0


def _dvd_main_title_vobs(disc_root: Path) -> list[Path]:
    """Return the ordered VOB chunks of the main-feature title set on a DVD.

    DVD-Video layout: <disc_root>/VIDEO_TS/VTS_NN_M.VOB where NN is the
    title-set number and M is the chunk (M=0 is the menu, M=1..N is the
    actual payload). The main feature is the title set with the largest
    total VOB-1..N size. Returns chunks in order. Empty list if no
    candidate found.

    v0.6.2: replaces the fictional `dvd:/` protocol used in v0.6.0-0.6.1.
    Verified against real DVD via `concat:` protocol; libdvdread's
    `dvdvideo` demuxer was discarded because it requires an ISO/block
    device, not a folder.
    """
    video_ts = disc_root / "VIDEO_TS"
    if not video_ts.is_dir():
        return []
    title_sets: dict[str, list[tuple[int, Path]]] = {}
    for vob in video_ts.glob("VTS_*.VOB"):
        # Skip macOS AppleDouble companions (`._VTS_*.VOB`)
        if vob.name.startswith("."):
            continue
        parts = vob.stem.split("_")  # "VTS_01_2" → ["VTS", "01", "2"]
        if len(parts) != 3:
            continue
        ts_num = parts[1]
        try:
            chunk = int(parts[2])
        except ValueError:
            continue
        if chunk == 0:
            continue  # skip menu chunk
        title_sets.setdefault(ts_num, []).append((chunk, vob))
    if not title_sets:
        return []
    sizes = {ts: sum(v.stat().st_size for _, v in chunks) for ts, chunks in title_sets.items()}
    main_ts = max(sizes, key=sizes.get)
    return [v for _, v in sorted(title_sets[main_ts])]


def _dvd_concat_input(disc_root: Path) -> Optional[str]:
    """Build the ffmpeg `concat:` protocol input string for a DVD's main
    feature, or None if no VOBs found. v0.6.2."""
    vobs = _dvd_main_title_vobs(disc_root)
    if not vobs:
        return None
    return "concat:" + "|".join(str(v) for v in vobs)


def _disc_display_name(file_path: Path, disc_type: Optional[str]) -> str:
    """Return the user-facing display name for a scan-result row.

    Folder disc (marker path: `.../<MovieFolder>/VIDEO_TS/VIDEO_TS.IFO`
    or `.../<MovieFolder>/BDMV/index.bdmv`) → the MovieFolder name,
    which is parent.parent of the marker. The folder name IS the
    user's only handle on a folder disc; the marker basename is
    cosmetic.

    ISO disc (file_path IS a `.iso` file, the disc itself) → the ISO's
    own filename, e.g. `rz0u.iso`. v0.7.2-7.9 returned the parent
    folder name (the movie folder), but that's often duplicated
    elsewhere in the UI and obscures which actual disc image is
    referenced — important for folders holding multiple ISOs or for
    matching on disk against a release-named .iso.

    Non-disc file → the file's own name.

    v0.7.10+: ISO branch returns the .iso basename, not the parent
    folder. v0.7.2-7.9: ISO branch was `file_path.parent.name`.
    v0.7.0-7.1: ISO branch was `file_path.parent.parent.name`
    (media-dir-named, wrong).
    """
    if disc_type:
        if file_path.is_file() and file_path.suffix.lower() == ".iso":
            return file_path.name
        return file_path.parent.parent.name
    return file_path.name


async def probe_file(file_path: str, detect_und_subs: bool = True) -> Optional[dict]:
    """Run ffprobe on a file and return parsed metadata dict, or None on failure.

    `detect_und_subs` (v0.9.17): when True (scan/convert), und text subs are
    detected inline and their language filled in. The on-demand detect
    endpoint passes False so it sees the RAW und language and does the
    detection itself — otherwise probe_file's inline result masks the und
    track and detect_languages skips it (never persisting or writing it).

    v0.6.0: when `file_path` points at a disc-marker file
    (VIDEO_TS.IFO inside a VIDEO_TS/ folder, or index.bdmv inside a
    BDMV/ folder), probe the disc-folder via ffmpeg's `dvd:/` or
    `bluray:/` protocol instead. ffmpeg auto-picks the longest title;
    the resulting streams + duration are the main feature only.
    `file_size` is patched to the disc-total via `_disc_total_size()`
    since ffprobe's `format.size` only covers the main title.
    `disc_type` ('dvd' / 'bdmv') is added to the return dict so
    downstream consumers (scanner walk, converter) can branch on it.
    """
    p = Path(file_path)
    disc_type: Optional[str] = None
    disc_folder: Optional[Path] = None
    ffprobe_input_args: list[str] = []  # v0.7.0: extra args before -i for disc ISO routing
    # v0.7.0: ISO file support. If file_path is a .iso, peek inside via
    # pycdlib to determine disc_type, then route to the appropriate
    # ffmpeg input syntax. DVD ISO uses `-f dvdvideo -i /path.iso`,
    # BD ISO uses `bluray:/path.iso`. No mount, no extraction at probe
    # time. Checked BEFORE folder-marker branches because an .iso file
    # never has VIDEO_TS/ or BDMV/ as its parent directory.
    if p.is_file() and p.suffix.lower() == ".iso":
        from backend.disc_metadata import _classify_disc_iso
        disc_type = _classify_disc_iso(p)
        if disc_type == "dvd":
            disc_folder = p           # ISO IS the disc — disc_folder points at the .iso file, not a dir
            probe_input = str(p)
            ffprobe_input_args = ["-f", "dvdvideo"]
        elif disc_type == "bdmv":
            disc_folder = p
            probe_input = f"bluray:{p}"
        else:
            # Not a video ISO — fall through to regular-file probe (will
            # likely fail; but caller treats failures as 'corrupt' and
            # surfaces the row).
            probe_input = file_path
    # v0.6.0: case-insensitive marker comparison — DVD-Video / BDMV
    # specs mandate exact casing on the disc, but case-insensitive
    # filesystems (macOS HFS+/APFS, Windows NTFS) can store the names
    # with different casing after rename/extract. Matches the .lower()
    # convention used elsewhere in this file for filename matching.
    elif p.name.lower() == "index.bdmv" and p.parent.name.lower() == "bdmv":
        disc_type = "bdmv"
        disc_folder = p.parent.parent
        probe_input = f"bluray:{disc_folder}"
    elif p.name.lower() == "video_ts.ifo" and p.parent.name.lower() == "video_ts":
        disc_type = "dvd"
        disc_folder = p.parent.parent
        probe_input = _dvd_concat_input(disc_folder)
        if probe_input is None:
            print(f"[PROBE] DVD probe failed: no main-feature VOBs found in {disc_folder}/VIDEO_TS/", flush=True)
            return None
    else:
        probe_input = file_path
    cmd = [
        "ffprobe",
        "-v", "quiet",
        "-print_format", "json",
        "-show_streams",
        "-show_format",
    ]
    # v0.6.2: disc inputs need a deeper analysis window for ffprobe to
    # compute duration. The `concat:` protocol over VOBs (DVD) and the
    # `bluray:` protocol both surface duration only after ffprobe has
    # read enough of the stream.
    if disc_type:
        cmd.extend(["-analyzeduration", "200M", "-probesize", "200M"])
    # v0.7.0: DVD ISO needs `-f dvdvideo` before `-i` so ffmpeg's
    # demuxer treats the .iso as a DVD-Video disc image rather than a
    # raw file. BD ISO uses the `bluray:` protocol on probe_input and
    # needs no extra args here.
    if ffprobe_input_args:
        cmd.extend(ffprobe_input_args)
    cmd.extend(["-i", probe_input])
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=settings.ffprobe_timeout
        )
        if proc.returncode != 0:
            return None
        data = json.loads(stdout.decode())
    except asyncio.TimeoutError:
        if proc and proc.returncode is None:
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass
        print(f"[SCANNER] ffprobe timeout on: {file_path}", flush=True)
        return None
    except Exception:
        if proc and proc.returncode is None:
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass
        return None

    streams = data.get("streams", [])
    fmt = data.get("format", {})

    video_codec = ""
    video_pix_fmt = ""
    video_width = 0
    video_height = 0
    video_fps: float = 0.0
    audio_tracks = []
    subtitle_tracks = []

    for stream in streams:
        codec_type = stream.get("codec_type", "")
        if codec_type == "video" and not video_codec:
            video_codec = stream.get("codec_name", "")
            # v0.5.9: pix_fmt drives the NVENC bit-depth `auto` mode
            # ("yuv420p10le" / "yuv420p12le" → 10-bit out, anything else
            # → 8-bit out). Captured here once at probe time so every
            # consumer downstream sees the same value.
            video_pix_fmt = stream.get("pix_fmt", "") or ""
            video_width = stream.get("width", 0) or 0
            video_height = stream.get("height", 0) or 0
            # Frame rate: prefer r_frame_rate ("24000/1001" → 23.976),
            # fall back to avg_frame_rate. Used by progress estimation
            # downstream when ffmpeg's `time=` field is N/A (v0.3.43+).
            fr = stream.get("r_frame_rate") or stream.get("avg_frame_rate") or ""
            try:
                if "/" in fr:
                    num, den = fr.split("/")
                    den_v = float(den)
                    video_fps = float(num) / den_v if den_v else 0.0
                elif fr:
                    video_fps = float(fr)
            except (ValueError, ZeroDivisionError):
                video_fps = 0.0
        elif codec_type == "audio":
            tags = stream.get("tags", {}) or {}
            disposition = stream.get("disposition", {}) or {}
            lang = (tags.get("language") or "und").lower()
            bitrate = stream.get("bit_rate")
            try:
                bitrate = int(bitrate) if bitrate else None
            except (ValueError, TypeError):
                bitrate = None
            audio_tracks.append({
                "stream_index": stream.get("index", len(audio_tracks) + 1),
                "language": lang,
                "codec": stream.get("codec_name", ""),
                "profile": stream.get("profile", ""),
                "channels": stream.get("channels", 2),
                "title": tags.get("title", ""),
                "bitrate": bitrate,
                "disposition": disposition,
            })
        elif codec_type == "subtitle":
            tags = stream.get("tags", {}) or {}
            disposition = stream.get("disposition", {}) or {}
            lang = (tags.get("language") or "und").lower()
            # v0.8.0: give und TEXT subtitles a real language by detecting
            # from their extracted text. Image subs pass through (OCR is
            # v0.8.1). Fail-open: extraction/detection failures leave "und".
            if detect_und_subs and lang == "und":
                from backend.language_detection import maybe_detect_subtitle_track_language, _TEXT_SUB_CODECS
                _codec_l = (stream.get("codec_name") or "").lower()
                if _codec_l in _TEXT_SUB_CODECS:
                    _txt = await _extract_embedded_sub_text(file_path, stream.get("index"))
                    lang = maybe_detect_subtitle_track_language(lang, _codec_l, _txt)
            # v0.9.99: mkvmerge writes per-stream NUMBER_OF_FRAMES (and a
            # language-suffixed variant) as statistics tags — a free cue count
            # already in the probe. Used to flag empty placeholder subs.
            num_frames = None
            for _k, _v in tags.items():
                if _k.upper().startswith("NUMBER_OF_FRAMES"):
                    try:
                        num_frames = int(_v)
                        break
                    except (ValueError, TypeError):
                        pass
            subtitle_tracks.append({
                "stream_index": stream.get("index"),
                "language": lang,
                "codec": stream.get("codec_name", ""),
                "title": tags.get("title", ""),
                "forced": bool(disposition.get("forced", 0)),
                "num_frames": num_frames,
            })

    try:
        duration = float(fmt.get("duration", 0))
    except (ValueError, TypeError):
        duration = 0.0

    try:
        raw_size = fmt.get("size")
        if raw_size is not None:
            file_size = int(raw_size)
        else:
            file_size = os.path.getsize(file_path)
    except (ValueError, TypeError, OSError):
        file_size = 0

    # Corruption heuristic: a media file with no video stream at all is almost
    # always a container that ffprobe couldn't fully parse (damaged headers,
    # truncated download, etc). Treat like a probe failure so it lands in the
    # corrupt branch of scan_directory() and shows up under the Corrupt filter.
    # We check the raw streams list (not just video_codec) so cover-art / image
    # attachments don't fool us.
    has_real_video = any(
        s.get("codec_type") == "video"
        and s.get("codec_name") not in ("mjpeg", "png", "bmp", "gif", "ansi")
        and s.get("disposition", {}).get("attached_pic", 0) != 1
        for s in streams
    )
    if not has_real_video:
        print(f"[SCANNER] No decodable video stream in: {file_path} — marking corrupt", flush=True)
        return None

    # v0.6.5: discs don't carry per-track language in their ffmpeg
    # output (VOBs/M2TSes lack the tags; libbluray sees what the BD
    # authored, which is often nothing). Read IFO/mpls sidecar and
    # patch language fields by stream-order index. Fail-open: parser
    # errors leave tracks as "und".
    if disc_type:
        try:
            from backend.disc_metadata import parse_disc_languages
            langs = parse_disc_languages(disc_folder, disc_type)
            for i, t in enumerate(audio_tracks):
                if i < len(langs["audio"]) and langs["audio"][i]:
                    t["language"] = langs["audio"][i]
            for i, t in enumerate(subtitle_tracks):
                if i < len(langs["subtitle"]) and langs["subtitle"][i]:
                    t["language"] = langs["subtitle"][i]
            if len(audio_tracks) != len(langs["audio"]) or len(subtitle_tracks) != len(langs["subtitle"]):
                print(
                    f"[DISC-META] count mismatch for {disc_folder}: "
                    f"ffmpeg audio={len(audio_tracks)}/IFO {len(langs['audio'])}, "
                    f"ffmpeg sub={len(subtitle_tracks)}/IFO {len(langs['subtitle'])}",
                    flush=True,
                )
        except Exception as exc:
            print(f"[DISC-META] failed for {disc_folder}: {exc}", flush=True)

    result = {
        "video_codec": video_codec,
        "video_pix_fmt": video_pix_fmt,
        "video_width": video_width,
        "video_height": video_height,
        "video_fps": video_fps,
        "audio_tracks": audio_tracks,
        "subtitle_tracks": subtitle_tracks,
        "duration": duration,
        "file_size": file_size,
    }
    # v0.6.0: disc-specific result patches
    if disc_type:
        result["disc_type"] = disc_type
        # ffprobe's format.size for dvd:/bluray: only covers the main title.
        # Use the on-disk size of all VOB/M2TS payload files for accurate
        # disk-usage display in the UI.
        # v0.7.0: gate on .is_dir() — for ISO inputs disc_folder is the
        # .iso file itself, and ffprobe's format.size already reflects
        # the ISO file's bytes (no patch needed).
        # v0.7.2: actually ffprobe's format.size for `bluray:/some.iso` still
        # reports only the main-title bytes (~19 GB on a 30 GB BD ISO),
        # NOT the ISO file's bytes. Stat the ISO file directly for the
        # accurate on-disk size.
        if disc_folder is not None and disc_folder.is_dir():
            total = _disc_total_size(disc_folder, disc_type)
            if total > 0:
                result["file_size"] = total
        elif p.is_file() and p.suffix.lower() == ".iso":
            try:
                result["file_size"] = p.stat().st_size
            except OSError:
                pass
    return result


def detect_native_language(audio_tracks: list[dict]) -> str:
    """Detect native language: prefer disposition.original=1, else first track."""
    for track in audio_tracks:
        disposition = track.get("disposition", {}) or {}
        if disposition.get("original") == 1:
            return track.get("language", "und")
    if audio_tracks:
        return audio_tracks[0].get("language", "und")
    return "und"


def is_x264(codec: str) -> bool:
    """Return True if codec string represents H.264/AVC."""
    c = codec.lower()
    return c in ("h264", "x264", "avc", "avc1")


def is_x265(codec: str) -> bool:
    """Return True if codec string represents H.265/HEVC."""
    c = codec.lower()
    return c in ("h265", "x265", "hevc")


# Map settings source_codecs values to ffprobe codec names
CODEC_FAMILIES = {
    "h264": ("h264", "x264", "avc", "avc1"),
    "hevc": ("hevc", "h265", "x265"),
    "av1": ("av1", "av01", "libaom-av1", "libsvtav1", "svt-av1"),
    "mpeg2": ("mpeg2video", "mpeg2"),
    "mpeg4": ("mpeg4", "xvid", "divx", "dx50", "mp4v"),
    "vc1": ("vc1", "wmv3", "wmv2", "wmv1"),
    "msmpeg4v3": ("msmpeg4v3", "msmpeg4v2", "msmpeg4"),
    "vp9": ("vp9",),
}


def codec_matches_source(video_codec: str, source_codecs: list[str]) -> bool:
    """Check if a video codec matches any of the configured source codecs to convert."""
    c = video_codec.lower()
    for source in source_codecs:
        if source in CODEC_FAMILIES:
            if c in CODEC_FAMILIES[source]:
                return True
        elif c == source.lower():
            return True
    return False


def is_av1(codec: str) -> bool:
    """Return True if codec string represents AV1."""
    c = codec.lower()
    return c in ("av1", "av01", "libaom-av1", "libsvtav1", "svt-av1")


def expand_source_codecs(source_codecs: list[str]) -> set[str]:
    """Expand a list of source-codec family names (e.g. 'mpeg2') into the
    full set of lowercase ffprobe codec strings they cover (e.g. 'mpeg2video',
    'mpeg2'). Used for recomputing scan_results.needs_conversion in bulk
    when source_codecs changes."""
    names: set[str] = set()
    for source in source_codecs:
        if source in CODEC_FAMILIES:
            names.update(CODEC_FAMILIES[source])
        else:
            names.add(source.lower())
    return names


async def recompute_needs_conversion(db, source_codecs: list[str]) -> int:
    """Recompute `needs_conversion` on every non-converted scan_results row
    based on the current source_codecs setting. Returns the number of rows
    whose value flipped.

    `needs_conversion` is derived from `video_codec` × `source_codecs`, but
    it's stored on the row at scan time. When the user widens/narrows the
    source_codecs setting (Settings → Convert From) the stored values go
    stale: a file scanned under `["h264"]` keeps `needs_conversion=0` even
    after the user enables MPEG-2. The queue UI then estimates 0 savings
    and skips the file to cleanup-only. This helper realigns the stored
    values with the current setting in one pass."""
    codec_names = expand_source_codecs(source_codecs)
    # Compute the new needs_conversion column for every converted=0 row in
    # Python (small DB, simple compare). Then UPDATE only the rows that
    # actually flip — cheaper than blanket UPDATE.
    flipped = 0
    async with db.execute(
        "SELECT file_path, video_codec, needs_conversion FROM scan_results "
        "WHERE converted = 0"
    ) as cur:
        rows = await cur.fetchall()
    # Index by position, not column name: callers pass their own connection and
    # not all set `row_factory = aiosqlite.Row` (the settings-update handler
    # doesn't), so named access raised "tuple indices must be integers or
    # slices, not str" and the recompute silently no-op'd on every save. The
    # SELECT fixes the column order, so positional access is unambiguous and
    # works under any row_factory.
    for row in rows:
        file_path, video_codec, needs_conversion = row[0], row[1], row[2]
        vc = (video_codec or "").lower()
        should_convert = 1 if vc in codec_names else 0
        if int(needs_conversion or 0) != should_convert:
            await db.execute(
                "UPDATE scan_results SET needs_conversion = ? WHERE file_path = ?",
                (should_convert, file_path),
            )
            flipped += 1
    return flipped


# Language variant groups — codes that represent the same spoken language
LANGUAGE_EQUIVALENTS = {
    # Norwegian
    "nor": {"nor", "nob", "nno"},
    "nob": {"nor", "nob", "nno"},
    "nno": {"nor", "nob", "nno"},
    # Chinese
    "zho": {"zho", "chi", "cmn", "yue", "wuu", "cn"},
    "chi": {"zho", "chi", "cmn", "yue", "wuu", "cn"},
    "cmn": {"zho", "chi", "cmn", "cn"},
    "yue": {"zho", "chi", "yue", "cn"},
    "cn": {"zho", "chi", "cmn", "yue", "wuu", "cn"},
    # Czech
    "ces": {"ces", "cze"},
    "cze": {"ces", "cze"},
    # Dutch
    "nld": {"nld", "dut"},
    "dut": {"nld", "dut"},
    # French
    "fra": {"fra", "fre"},
    "fre": {"fra", "fre"},
    # German
    "deu": {"deu", "ger"},
    "ger": {"deu", "ger"},
    # Greek
    "ell": {"ell", "gre"},
    "gre": {"ell", "gre"},
    # Icelandic
    "isl": {"isl", "ice"},
    "ice": {"isl", "ice"},
    # Persian
    "fas": {"fas", "per"},
    "per": {"fas", "per"},
    # Romanian
    "ron": {"ron", "rum"},
    "rum": {"ron", "rum"},
    # Slovak
    "slk": {"slk", "slo"},
    "slo": {"slk", "slo"},
    # Malay
    "msa": {"msa", "may"},
    "may": {"msa", "may"},
    # Portuguese (includes Brazilian Portuguese)
    "por": {"por", "pt", "pt-br", "pt-pt", "ptb"},
    "pt": {"por", "pt", "pt-br", "pt-pt", "ptb"},
    "pt-br": {"por", "pt", "pt-br", "pt-pt", "ptb"},
    "pt-pt": {"por", "pt", "pt-br", "pt-pt", "ptb"},
    "ptb": {"por", "pt", "pt-br", "pt-pt", "ptb"},
    # Spanish (includes Latin American variants)
    "spa": {"spa", "es", "es-mx", "es-419", "es-es"},
    "es": {"spa", "es", "es-mx", "es-419", "es-es"},
    "es-mx": {"spa", "es", "es-mx", "es-419", "es-es"},
    "es-419": {"spa", "es", "es-mx", "es-419", "es-es"},
    # English variants
    "eng": {"eng", "en", "en-us", "en-gb", "en-au"},
    "en": {"eng", "en", "en-us", "en-gb", "en-au"},
    "en-us": {"eng", "en", "en-us", "en-gb", "en-au"},
    "en-gb": {"eng", "en", "en-us", "en-gb", "en-au"},
    # Serbo-Croatian
    "srp": {"srp", "hrv", "bos", "hbs"},
    "hrv": {"srp", "hrv", "bos", "hbs"},
    "bos": {"srp", "hrv", "bos", "hbs"},
    "hbs": {"srp", "hrv", "bos", "hbs"},
}


_DUBBED_NATIVE_SOURCES = ("api", "tmdb-manual", "manual")


def _is_dubbed(audio_langs: list[str], native_language: str, language_source: str) -> int:
    """1 if this item is dubbed: its native (original) language is known from a
    source independent of the audio present, it has audio, every audio track
    has a known language, and none match native (using LANGUAGE_EQUIVALENTS).
    0 otherwise (incl. when the status is uncertain). See the design spec."""
    if (language_source or "") not in _DUBBED_NATIVE_SOURCES:
        return 0
    native = (native_language or "").lower()
    if not native or native == "und":
        return 0
    langs = [(l or "und").lower() for l in audio_langs]
    if not langs:
        return 0
    if any(l == "und" for l in langs):
        return 0
    native_equiv = LANGUAGE_EQUIVALENTS.get(native, {native})
    for l in langs:
        if l in native_equiv or native in LANGUAGE_EQUIVALENTS.get(l, {l}):
            return 0
    return 1


def languages_match(lang1: str, lang2: str) -> bool:
    """Check if two language codes represent the same language, accounting for variants."""
    l1 = lang1.lower()
    l2 = lang2.lower()
    if l1 == l2:
        return True
    # Check equivalence groups
    group = LANGUAGE_EQUIVALENTS.get(l1)
    if group and l2 in group:
        return True
    return False


# ── External subtitle detection ──────────────────────────────────────────────

SUBTITLE_EXTENSIONS = {".srt", ".ass", ".ssa", ".sub", ".vtt", ".sup", ".idx"}


def is_hidden_sidecar(path: str) -> bool:
    """True if `path`'s basename is a hidden / AppleDouble file (starts
    with a dot: `._<name>.srt`, `.DS_Store`, `.hidden.idx`, …).

    macOS-formatted volumes create `._<name>` resource-fork companions
    next to every file; they share the real file's extension but are
    binary junk. Feeding one to ffmpeg as an input fails the whole
    conversion. Both the scan-time external-sub detection and the
    convert-time merge guard use this so they agree. v0.7.34+.
    """
    import os as _os
    return _os.path.basename(path).startswith(".")

# Known ISO 639-1 (2-letter) and 639-2/B (3-letter) codes for validation.
# We only need enough to distinguish real language tags from random filename parts.
_KNOWN_LANG_CODES = {
    # 2-letter
    "en", "es", "fr", "de", "it", "pt", "ru", "ja", "ko", "zh", "ar", "hi",
    "nl", "sv", "no", "da", "fi", "pl", "cs", "sk", "hu", "ro", "bg", "el",
    "tr", "he", "th", "vi", "id", "ms", "is", "hr", "sr", "sl", "uk", "ca",
    "et", "lv", "lt", "ga", "af", "sw", "tl", "bn",
    # 3-letter (common ones used in media)
    "eng", "spa", "fre", "fra", "ger", "deu", "ita", "por", "rus", "jpn",
    "kor", "zho", "chi", "ara", "hin", "nld", "dut", "swe", "nor", "nob",
    "dan", "fin", "pol", "cze", "ces", "slo", "slk", "hun", "rum", "ron",
    "bul", "gre", "ell", "tur", "heb", "tha", "vie", "ind", "msa", "may",
    "ice", "isl", "hrv", "srp", "slv", "ukr", "cat", "est", "lav", "lit",
    "gle", "afr", "swa", "tgl", "ben", "und",
}

import re as _re
_EXT_SUB_LANG_RE = _re.compile(
    r"\.([a-zA-Z]{2,3})"           # language code (2-3 letters)
    r"(?:\.(forced|sdh|cc|hi))?"    # optional flag
    r"$",
    _re.IGNORECASE,
)

_EXT_CODEC_MAP = {
    ".srt": "subrip",
    ".ass": "ass",
    ".ssa": "ass",
    ".vtt": "webvtt",
    ".sub": "subviewer",
    ".sup": "hdmv_pgs_subtitle",
    ".idx": "dvd_subtitle",
}


def _clean_srt_bytes(raw: bytes, max_chars: int = 4000) -> str | None:
    """Decode raw subtitle bytes tolerantly and strip srt sequence numbers
    + timestamp lines, leaving dialogue for language detection.

    v0.8.1: decode is tolerant of non-UTF-8 charsets. Subtitles in the
    wild are frequently Windows-1252 / ISO-8859-1 (older + non-English
    releases). Try utf-8, then cp1252, then latin-1 (which maps all 256
    byte values and never raises). langdetect only needs the letters,
    which survive a latin-1 decode of cp1252 text even if a few accented
    punctuation glyphs shift."""
    import re as _re
    if not raw:
        return None
    text = None
    # Fast path: most modern subs are UTF-8.
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = None
    if text is None:
        # v0.8.2: detect the real charset for legacy non-Unicode subs —
        # GB2312/GBK/Big5 (Chinese), Shift-JIS (Japanese), EUC-KR (Korean),
        # cp1252/Latin-1 (Western). Blind latin-1 turns CJK double-byte
        # text into mojibake that langdetect can't identify; charset-
        # normalizer picks the right codec so detection actually works.
        try:
            from charset_normalizer import from_bytes
            best = from_bytes(raw).best()
            if best is not None:
                text = str(best)
        except Exception:
            text = None
    if text is None:
        # Last resort: latin-1 maps every byte and never raises.
        text = raw.decode("latin-1", errors="replace")
    # v0.9.71: if the bytes are raw ASS/SSA (the `-c:s copy` extraction path
    # yields the whole script, not decoded dialogue), pull out ONLY the
    # dialogue text. Otherwise the English structure ([Script Info],
    # Format:/Style: with font names, "Dialogue:" prefixes) dominates and
    # langdetect mis-reads the track as English (observed: a Malay ass sub
    # detected en@0.57 → stayed und).
    if "[Events]" in text or "[Script Info]" in text or "\nDialogue:" in text or text.startswith("Dialogue:"):
        dlg = []
        for line in text.splitlines():
            if line.startswith("Dialogue:"):
                parts = line.split(",", 9)  # 9 fields precede the Text field
                if len(parts) == 10:
                    body = _re.sub(r"\{[^}]*\}", "", parts[9])  # drop {\...} tags
                    body = body.replace("\\N", " ").replace("\\n", " ").replace("\\h", " ")
                    body = body.strip()
                    if body:
                        dlg.append(body)
        text = "\n".join(dlg)
    else:
        text = _re.sub(r"^\d+\s*$", "", text, flags=_re.MULTILINE)
        text = _re.sub(r"\d{2}:\d{2}:\d{2},\d{3} --> .*$", "", text, flags=_re.MULTILINE)
    # v0.9.76: strip markup that skews language detection. ffmpeg's ass→srt
    # decode wraps every line in <font size=".." color="..">…</font> (from the
    # ASS style) and leaves {\an8}-style override tags — over a full episode
    # the repeated "font size color" English tokens tipped langdetect to
    # en@0.57 and the track stayed und (e.g. a Tagalog .ass). Drop HTML-like
    # tags and any residual {\...} overrides so only dialogue text remains.
    text = _re.sub(r"<[^>]+>", "", text)
    text = _re.sub(r"\{[^}]*\}", "", text)
    return text[:max_chars].strip() or None


async def _extract_embedded_sub_text(file_path: str, stream_index: int, max_chars: int = 4000) -> str | None:
    """Extract up to ~max_chars of text from an embedded text subtitle
    stream for language detection. Async so it doesn't block the event
    loop during concurrent scans. Returns None on failure/empty.

    v0.8.1: extract with `-c:s copy` to a temp file (raw bytes, NO decode)
    rather than `-f srt` (which runs ffmpeg's srt DECODER and rejects
    non-UTF-8 text with "Invalid UTF-8 in decoded subtitles text",
    producing zero output — the bug that left legacy-charset subrip
    tracks stuck at `und`). Python then decodes tolerantly via
    `_clean_srt_bytes`. Falls back to the decode path (`-f srt` to
    stdout) for text codecs that can't be copied into an srt container
    (e.g. ass/ssa), which are usually UTF-8 anyway."""
    import asyncio
    import os as _os
    import tempfile as _tempfile

    async def _run(cmd: list, timeout: int = 180):
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            return out
        except (asyncio.TimeoutError, OSError):
            if proc is not None:
                try:
                    proc.kill()
                    await proc.wait()
                except Exception:
                    pass
            return None

    # Primary: copy the stream (no decode) to a temp .srt, read raw bytes.
    fd, tmp = _tempfile.mkstemp(suffix=".srt", prefix="shrinkerr_sub_")
    _os.close(fd)
    try:
        # v0.9.16: bound the read to the first 30 min. A `-t` cap is needed —
        # without it (v0.9.15) ffmpeg demuxes the WHOLE multi-GB file to
        # collect sub packets and blows the timeout on large files, extracting
        # empty. 30 min covers dialogue start for essentially all content
        # (v0.8.1's 10 min was too short for some forced subs).
        # v0.9.97: even 30 min of a large Bluray is ~GBs; reading it COLD over a
        # CIFS/SMB mount exceeded the old 60s timeout (killed → empty → stuck
        # und). The `_run` timeout above is now 180s so cold networked reads
        # finish (a warm re-read is a few seconds).
        await _run([
            "ffmpeg", "-y", "-v", "quiet", "-i", file_path,
            "-map", f"0:{stream_index}", "-t", "1800", "-c:s", "copy", tmp,
        ])
        raw = b""
        try:
            with open(tmp, "rb") as fh:
                raw = fh.read()
        except OSError:
            raw = b""
    finally:
        try:
            _os.unlink(tmp)
        except OSError:
            pass

    if raw:
        return _clean_srt_bytes(raw, max_chars)

    # Fallback: decode to srt on stdout (handles ass/ssa copy can't put in .srt).
    out = await _run([
        "ffmpeg", "-v", "quiet", "-i", file_path, "-map", f"0:{stream_index}",
        "-t", "1800", "-f", "srt", "-",
    ])
    if not out:
        print(f"[LANG-DETECT] sub s{stream_index}: no text extracted (copy + srt-decode both empty)", flush=True)
        return None
    return _clean_srt_bytes(out, max_chars)


def detect_external_subtitles(video_path: str) -> list[dict]:
    """Detect external subtitle files alongside a video file.

    Matching strategies (in order):
      1. Sub filename starts with the full video stem (strictest)
      2. Sub filename shares the same S##E## pattern as the video (TV)
      3. If only one video file in the folder, all sub files belong to it
    """
    video = Path(video_path)
    video_stem = video.stem
    parent = video.parent
    results: list[dict] = []

    if not parent.exists():
        return results

    try:
        siblings = list(parent.iterdir())
    except OSError:
        return results

    # v0.7.33: skip hidden / AppleDouble companion files. macOS-formatted
    # volumes litter every directory with `._<name>` resource-fork files
    # that share the real file's extension — so `._Movie.eng.srt` looks
    # like a subtitle by suffix and (via the S##E## episode-key match
    # below) gets fed to ffmpeg as `-i`, which fails with "Invalid data
    # found when processing input" (exit 183) and kills the whole
    # conversion. The scanner's directory walk already filters
    # `name.startswith(".")`; mirror that here so external-sub detection
    # agrees with it.
    sub_files = [
        f for f in siblings
        if f.suffix.lower() in SUBTITLE_EXTENSIONS and not is_hidden_sidecar(f.name)
    ]
    if not sub_files:
        return results

    # VobSub is a paired format: `.idx` (index/metadata) + `.sub` (bitmap
    # data) must both exist or ffmpeg's vobsub demuxer fails. ffmpeg
    # auto-resolves the `.sub` partner from disk when given the `.idx`
    # path, so we represent each VobSub pair via its `.idx` file alone.
    # Drop any `.sub` whose `.idx` partner is missing (orphan or
    # subviewer-format text — both unsafe to feed in blindly), and drop
    # any `.sub` whose `.idx` partner exists (the `.idx` will represent
    # the pair). Same for `.idx` without `.sub`. v0.3.46+.
    available_stems_lower = {f.stem.lower(): f for f in siblings}
    filtered: list[Path] = []
    for f in sub_files:
        ext = f.suffix.lower()
        if ext == ".sub":
            # Skip — the `.idx` partner (if it exists) will represent the pair.
            # If no `.idx` partner exists, this is an orphan we can't safely use.
            partner = f.parent / (f.stem + ".idx")
            if not partner.exists():
                print(f"[EXT-SUBS]   Skip '{f.name}' — VobSub `.sub` without paired `.idx`", flush=True)
            continue
        if ext == ".idx":
            partner = f.parent / (f.stem + ".sub")
            if not partner.exists():
                print(f"[EXT-SUBS]   Skip '{f.name}' — VobSub `.idx` without paired `.sub`", flush=True)
                continue
        filtered.append(f)
    sub_files = filtered
    if not sub_files:
        return results

    video_files = [f for f in siblings if f.suffix.lower() in {".mkv", ".mp4", ".avi", ".mov", ".ts", ".m4v", ".webm"}]
    only_one_video = len(video_files) == 1

    # Extract S##E## pattern from the video filename for TV episode matching
    import re as _re
    ep_match = _re.search(r"[Ss](\d{1,2})[Ee](\d{1,3})", video_stem)
    video_ep_key = f"s{int(ep_match.group(1)):02d}e{int(ep_match.group(2)):02d}" if ep_match else None

    print(f"[EXT-SUBS] {video.name}: {len(sub_files)} sub file(s), {len(video_files)} video file(s) in folder", flush=True)

    for f in sub_files:
        fname = f.name
        match_reason = None

        # Strategy 1: full stem match
        if fname.lower().startswith(video_stem.lower()):
            match_reason = "stem"
        # Strategy 2: same episode key
        elif video_ep_key:
            sub_ep = _re.search(r"[Ss](\d{1,2})[Ee](\d{1,3})", fname)
            if sub_ep:
                sub_ep_key = f"s{int(sub_ep.group(1)):02d}e{int(sub_ep.group(2)):02d}"
                if sub_ep_key == video_ep_key:
                    match_reason = "episode"
        # Strategy 3: only one video in folder
        if not match_reason and only_one_video:
            match_reason = "single-video"

        if not match_reason:
            print(f"[EXT-SUBS]   Skip '{fname}' — no match (stem/episode/single)", flush=True)
            continue
        print(f"[EXT-SUBS]   Match '{fname}' via {match_reason}", flush=True)
        # Don't match the video file itself
        if f == video:
            continue

        # Parse language from the end of the sub's stem.
        # For stem match: "Video.eng.srt" → stem "Video.eng" → last segment "eng"
        # For episode match: "Show.S01E01.eng.srt" → stem "Show.S01E01.eng" → last "eng"
        # For single-video match: "subs.eng.forced.srt" → stem "subs.eng.forced" → "eng" + forced
        sub_stem = f.stem  # e.g. "Movie.eng.forced" or "Show.S01E01.eng"

        language = "und"
        forced = False
        sdh = False
        title_parts = []

        # Try the end of the sub stem (matches stem-match case): .eng[.forced|.sdh]?
        if True:
            m = _EXT_SUB_LANG_RE.search(sub_stem)
            if m:
                lang_candidate = m.group(1).lower()
                if lang_candidate in _KNOWN_LANG_CODES:
                    language = lang_candidate
                flag = (m.group(2) or "").lower()
                if flag == "forced":
                    forced = True
                elif flag in ("sdh", "hi", "cc"):
                    sdh = True
                    title_parts.append(flag.upper())

        codec = _EXT_CODEC_MAP.get(f.suffix.lower(), "subrip")

        results.append({
            "language": language,
            "codec": codec,
            "title": " ".join(title_parts) if title_parts else f.name,
            "forced": forced,
            "external_path": str(f),
            "stream_index": 0,  # placeholder, assigned by caller
        })

    # Sort by language then filename for deterministic order
    results.sort(key=lambda x: (x["language"], x["external_path"]))
    return results


_cleanup_enabled_cache: dict[str, bool] = {}


def _is_cleanup_enabled(key: str, default: bool = True) -> bool:
    """Check if a boolean cleanup-related setting is enabled in the DB.
    Cached per key. v0.5.20+: `default` lets callers override the
    missing-row fallback per key — most settings default True, but new
    additions like `keep_native_subs` default False."""
    if key in _cleanup_enabled_cache:
        return _cleanup_enabled_cache[key]
    try:
        import sqlite3
        db = sqlite3.connect(settings.db_path)
        try:
            cur = db.execute("SELECT value FROM settings WHERE key = ?", (key,))
            row = cur.fetchone()
            val = row[0].lower() == "true" if row else default
        finally:
            db.close()
    except Exception:
        val = default
    _cleanup_enabled_cache[key] = val
    return val


def classify_audio_tracks(
    tracks: list[dict], native_language: str, duration: float = 0
) -> list[AudioTrack]:
    """
    Classify audio tracks for keep/remove.

    Rules:
    - Always keep (locked=True): languages in settings.always_keep_languages
    - Keep native language (keep=True, locked=False unless native is in always_keep)
    - Ignore (keep=True, locked=False) und/unknown tracks
    - Everything else: keep=False
    """
    # If audio cleanup is disabled, keep all tracks
    if not _is_cleanup_enabled("audio_cleanup_enabled"):
        return [
            AudioTrack(
                stream_index=t.get("stream_index", 0),
                language=(t.get("language") or "und").lower(),
                codec=t.get("codec", ""),
                channels=t.get("channels", 2),
                title=t.get("title", ""),
                bitrate=t.get("bitrate"),
                profile=t.get("profile"),
                keep=True, locked=True,
            ) for t in tracks
        ]

    always_keep = _load_audio_keep_languages()
    native = native_language.lower() if native_language else "und"
    auto_keep_native = _is_cleanup_enabled("keep_native_language")  # defaults True

    # v0.5.16: smart selection per always-keep language.
    # Pre-v0.5.16: any track in an always-keep language got `locked=True`
    # and `keep=True`. The lock prevented users from deleting duplicates
    # ("3 English tracks, can't remove the AAC 2.0 because eng is on
    # always-keep" — issue #11). Now: for each always-keep language with
    # multiple tracks, pick the highest-quality one (channels desc, then
    # codec rank) as the default-kept track; other tracks in that
    # language fall through to the standard rules. The `locked` field is
    # kept for back-compat but always False — the UI dropped its lock
    # rendering in the same release, so all tracks render as editable
    # checkboxes regardless.
    def _audio_track_rank(t: dict) -> tuple:
        """Higher tuple → better track. Channels first (5.1 > 2.0),
        then codec quality ranking (lossless object > lossless > lossy)."""
        channels = t.get("channels", 0) or 0
        codec = (t.get("codec") or "").lower()
        profile = (t.get("profile") or "").lower()
        # Codec ranking matches the ladder Shrinkerr uses elsewhere
        # for is_lossless_audio() / lossless-conversion heuristics.
        if "truehd" in codec:
            codec_score = 100
        elif "flac" in codec or "pcm" in codec or "alac" in codec:
            codec_score = 95
        elif "dts" in codec:
            if "ma" in profile or "master" in profile:
                codec_score = 92   # DTS-HD MA
            elif "hra" in profile:
                codec_score = 85
            else:
                codec_score = 80   # plain DTS
        elif "eac3" in codec or "e-ac-3" in codec:
            codec_score = 70       # Dolby Digital Plus
        elif "ac3" in codec:
            codec_score = 65       # Dolby Digital
        elif "aac" in codec:
            codec_score = 55
        elif "opus" in codec:
            codec_score = 50
        else:
            codec_score = 30
        return (channels, codec_score)

    # v0.5.17: `always_keep_dedup` toggles whether multi-track
    # always-keep languages get smart selection (only best kept) or the
    # pre-v0.5.16 behaviour (every track kept). Default True. When off,
    # every always-keep-language track is added to the winners set so
    # the keep=True branch fires for all of them.
    dedup_enabled = _is_cleanup_enabled("always_keep_dedup")  # defaults True

    # First pass: for each always-keep language, decide which tracks
    # default to keep=True.
    always_keep_by_lang: dict[str, list[int]] = {}
    for idx, track in enumerate(tracks):
        lang = (track.get("language") or "und").lower()
        if any(languages_match(lang, k) for k in always_keep):
            always_keep_by_lang.setdefault(lang, []).append(idx)
    always_keep_winners: set[int] = set()
    for lang, indices in always_keep_by_lang.items():
        if not dedup_enabled or len(indices) == 1:
            # Keep every track when dedup is off, or there's only one.
            always_keep_winners.update(indices)
        else:
            # Highest-ranking track wins; ties broken by earliest stream
            # (`-i` to keep the rank-key max stable across equal ranks).
            best = max(indices, key=lambda i: (_audio_track_rank(tracks[i]), -i))
            always_keep_winners.add(best)

    result = []
    for idx, track in enumerate(tracks):
        lang = (track.get("language") or "und").lower()
        bitrate = track.get("bitrate")
        size_estimate = None
        if bitrate and duration > 0:
            try:
                # bitrate is in bits/second from ffprobe; convert to bytes for the full duration
                size_estimate = int(int(bitrate) * duration / 8)
            except (ValueError, TypeError):
                size_estimate = None

        # Smart-selection result: only the per-language winner counts as
        # an always-keep default. Other duplicates fall through to the
        # standard rules below.
        if idx in always_keep_winners:
            keep = True
        elif auto_keep_native and languages_match(lang, native):
            keep = True
        elif lang == "und":
            keep = True
        else:
            keep = False

        result.append(
            AudioTrack(
                stream_index=track.get("stream_index", 0),
                language=lang,
                codec=track.get("codec", ""),
                channels=track.get("channels", 2),
                title=track.get("title", ""),
                bitrate=bitrate,
                profile=track.get("profile"),
                size_estimate_bytes=size_estimate,
                keep=keep,
                locked=False,  # v0.5.16: lock semantic removed; UI now allows override
            )
        )

    # Safety: if only 1 track, always keep it regardless of language
    if len(result) == 1 and not result[0].keep:
        result[0] = result[0].model_copy(update={"keep": True, "locked": True})

    # Safety: never remove ALL tracks — if all would be removed, keep the first one
    if len(result) > 1 and all(not t.keep for t in result):
        result[0] = result[0].model_copy(update={"keep": True, "locked": False})

    # Reorder: move native language tracks to the top so they become the default
    # playback track. Even if an always_keep language (e.g. English) is first,
    # the native language (original audio) should be the primary track.
    if native and native != "und":
        native_tracks = [t for t in result if languages_match(t.language, native)]
        other_tracks = [t for t in result if not languages_match(t.language, native)]
        result = native_tracks + other_tracks

    return result


_sub_settings_cache: dict | None = None
_sub_settings_loaded = False


def _load_sub_settings() -> tuple[set[str], bool]:
    """Load subtitle settings from DB once and cache."""
    global _sub_settings_cache, _sub_settings_loaded
    if _sub_settings_loaded and _sub_settings_cache is not None:
        return _sub_settings_cache["langs"], _sub_settings_cache["keep_unknown"]

    sub_keep_langs = {lang.lower() for lang in settings.always_keep_languages}
    sub_keep_unknown = True

    try:
        import sqlite3
        db = sqlite3.connect(settings.db_path)
        try:
            cur = db.execute("SELECT key, value FROM settings WHERE key IN ('sub_keep_languages', 'sub_keep_unknown')")
            for row in cur:
                if row[0] == "sub_keep_languages":
                    sub_keep_langs = {l.lower() for l in json.loads(row[1])}
                elif row[0] == "sub_keep_unknown":
                    sub_keep_unknown = row[1].lower() == "true"
        finally:
            db.close()
    except Exception:
        pass

    _sub_settings_cache = {"langs": sub_keep_langs, "keep_unknown": sub_keep_unknown}
    _sub_settings_loaded = True
    return sub_keep_langs, sub_keep_unknown


_audio_settings_cache: set[str] | None = None
_audio_settings_loaded = False


def _load_audio_keep_languages() -> set[str]:
    """Load the `always_keep_languages` list from the DB once and cache.

    Pre-v0.3.113 `classify_audio_tracks` read `settings.always_keep_languages`
    directly — but `settings` is the in-memory pydantic-settings object
    populated only from env vars at startup, NOT from the DB. So the user's
    UI-set list (e.g. "eng" added via Settings → Audio cleanup) was
    silently ignored for audio classification, and English audio tracks
    on a Japanese-native film would get marked for removal even though
    English was supposed to be always-kept. Subtitle classification was
    immune because it had a parallel `_load_sub_settings()` helper that
    DID read the DB; this brings audio onto the same pattern.
    """
    global _audio_settings_cache, _audio_settings_loaded
    if _audio_settings_loaded and _audio_settings_cache is not None:
        return _audio_settings_cache

    langs = {lang.lower() for lang in settings.always_keep_languages}  # env fallback

    try:
        import sqlite3
        db = sqlite3.connect(settings.db_path)
        try:
            cur = db.execute(
                "SELECT value FROM settings WHERE key = 'always_keep_languages'"
            )
            row = cur.fetchone()
            if row and row[0]:
                langs = {l.lower() for l in json.loads(row[0])}
        finally:
            db.close()
    except Exception:
        pass

    _audio_settings_cache = langs
    _audio_settings_loaded = True
    return langs


def invalidate_sub_settings_cache():
    """Call when subtitle/audio cleanup settings are updated to force a reload."""
    global _sub_settings_cache, _sub_settings_loaded, _cleanup_enabled_cache
    global _audio_settings_cache, _audio_settings_loaded
    _sub_settings_cache = None
    _sub_settings_loaded = False
    _audio_settings_cache = None
    _audio_settings_loaded = False
    _cleanup_enabled_cache.clear()


def classify_subtitle_tracks(
    tracks: list[dict], native_language: str
) -> list["SubtitleTrack"]:
    """
    Classify subtitle tracks for keep/remove.

    Uses separate settings: sub_keep_languages and sub_keep_unknown.
    Forced subtitles are always kept.
    """
    from backend.models import SubtitleTrack

    # If subtitle cleanup is disabled, keep all tracks
    if not _is_cleanup_enabled("sub_cleanup_enabled"):
        return [
            SubtitleTrack(
                stream_index=t.get("stream_index", 0),
                language=(t.get("language") or "und").lower(),
                codec=t.get("codec", ""),
                title=t.get("title", ""),
                forced=t.get("forced", False),
                num_frames=t.get("num_frames"),
                keep=True, locked=True,
            ) for t in tracks
        ]

    sub_keep_langs, sub_keep_unknown = _load_sub_settings()
    native = native_language.lower() if native_language else "und"
    # v0.5.20: subs use a SEPARATE native-language toggle from audio.
    # Pre-v0.5.20 they shared `keep_native_language`, which meant
    # toggling on "keep native audio" also kept native-language subs —
    # almost always noise (German subs on a German movie etc.). The new
    # `keep_native_subs` defaults False; users who want native subs for
    # SDH / hearing-impaired reasons can opt in explicitly.
    auto_keep_native = _is_cleanup_enabled("keep_native_subs", default=False)

    result = []
    for track in tracks:
        lang = (track.get("language") or "und").lower()
        forced = track.get("forced", False)

        # v0.9.99: an empty placeholder subtitle (a "NO SUBS" forced/und track
        # with a single dummy cue, or none) is junk regardless of the
        # forced/keep-language rules below. Drop it when the cue count is known
        # (<= 1) AND the track is forced or unknown-language — the narrow gate
        # that avoids touching a genuine sparse named-language "signs" sub.
        num_frames = track.get("num_frames")
        if num_frames is not None and num_frames <= 1 and (forced or lang == "und"):
            result.append(
                SubtitleTrack(
                    stream_index=track.get("stream_index", 0),
                    language=lang,
                    codec=track.get("codec", ""),
                    title=track.get("title", ""),
                    forced=forced,
                    num_frames=num_frames,
                    keep=False,
                    locked=False,
                )
            )
            continue

        # Forced subs only kept if they match native language or user's keep languages
        if forced:
            is_relevant = (lang == "und"
                or (auto_keep_native and languages_match(lang, native))
                or any(languages_match(lang, k) for k in sub_keep_langs))
            keep = is_relevant
            locked = is_relevant
        elif any(languages_match(lang, k) for k in sub_keep_langs):
            keep = True
            locked = True
        elif auto_keep_native and languages_match(lang, native):
            keep = True
            locked = False
        elif lang == "und":
            keep = sub_keep_unknown
            locked = False
        else:
            keep = False
            locked = False

        result.append(
            SubtitleTrack(
                stream_index=track.get("stream_index", 0),
                language=lang,
                codec=track.get("codec", ""),
                title=track.get("title", ""),
                forced=forced,
                num_frames=num_frames,
                keep=keep,
                locked=locked,
            )
        )

    return result


def estimate_savings(
    file_size: int,
    needs_conversion: bool,
    tracks_to_remove: list[AudioTrack],
    duration: float,
    cq: int = 25,
) -> int:
    """Estimate bytes saved by re-encoding + removing tracks.

    v0.6.7+: video-conversion portion now uses the CQ-calibrated curve
    from backend.encoding_estimates instead of a flat 0.30 default.
    `cq` defaults to 25 (a reasonable middle-ground for NVENC); callers
    should pass the user's actual configured global CQ when available.
    """
    from backend.encoding_estimates import total_estimated_savings_bytes
    return total_estimated_savings_bytes(
        file_size, needs_conversion, cq, tracks_to_remove, duration,
    )


async def scan_directory(
    dir_path: str,
    progress_callback: Optional[Callable] = None,
    result_callback: Optional[Callable] = None,
    cancel_check: Optional[Callable[[], bool]] = None,
) -> list[ScannedFile]:
    """
    Walk dir_path, probe video files, classify tracks, build ScannedFile list.

    If result_callback is provided, each result is passed to it immediately
    (for streaming/batched DB writes). Results are still returned as a list
    for backward compatibility, but with result_callback the list is empty
    to save memory.

    Skips files that are already x265 with no removable tracks.
    """
    dir_path = Path(dir_path)
    extensions = {ext.lower() for ext in settings.video_extensions}
    extensions.add(".iso")  # v0.7.0: include ISO files in the walk for
                            # disc-image classification (separate from
                            # user-configured video extensions)

    # Load configured source codecs from DB
    source_codecs = ["h264"]  # default
    # v0.6.7: also load the user's global NVENC CQ once per scan so the
    # video-conversion savings estimate matches what the encoder will
    # actually do. Was hardcoded to a flat 0.30 reduction pre-v0.6.7.
    global_cq = 25
    try:
        import json as _json
        import aiosqlite as _aiosqlite
        _db = await _aiosqlite.connect(settings.db_path)
        try:
            async with _db.execute("SELECT value FROM settings WHERE key = 'source_codecs'") as _cur:
                _row = await _cur.fetchone()
                if _row and _row[0]:
                    source_codecs = _json.loads(_row[0])
            async with _db.execute("SELECT value FROM settings WHERE key = 'nvenc_cq'") as _cur:
                _cqrow = await _cur.fetchone()
                if _cqrow and _cqrow[0]:
                    try:
                        global_cq = int(_cqrow[0])
                    except (TypeError, ValueError):
                        pass
        finally:
            await _db.close()
    except Exception:
        pass
    print(f"[SCANNER] Source codecs to convert: {source_codecs} (cq={global_cq})", flush=True)

    # v0.9.28: signal discovery before the (silent, possibly minutes-long on
    # spun-down disks) walk of this directory, so a multi-path scan shows
    # "Discovering files…" between directories instead of freezing the UI on
    # the previous directory's last probed file.
    if progress_callback:
        await progress_callback(
            status="discovering", current_file=str(dir_path),
            files_found=0, files_probed=0, total_files=0,
        )

    # Collect all candidate files first
    all_files = []
    for root, dirs, files in os.walk(dir_path):
        root_path = Path(root)

        # v0.6.0: disc-folder detection. When a directory contains a
        # VIDEO_TS/VIDEO_TS.IFO or BDMV/index.bdmv marker, register the
        # marker file as a single scan item and skip descent into the
        # disc subdirectory — its VOBs / M2TS are opaque to Shrinkerr.
        # BDMV wins on combo discs (handled inside _classify_disc).
        disc_type = _classify_disc(root_path)
        if disc_type:
            marker = _disc_marker_path(root_path, disc_type)
            if marker.is_file():
                all_files.append(marker)
            # Don't recurse INTO VIDEO_TS/BDMV — they're internal to the disc.
            # Mutating `dirs` in-place is the documented way to prune os.walk
            # descent.
            dirs[:] = [d for d in dirs if d not in ("VIDEO_TS", "BDMV")]
            # Skip the files-in-this-dir loop too — disc-root folders typically
            # don't have video files alongside VIDEO_TS/BDMV, but if they did
            # they'd be part of the disc release (subtitle sidecars, etc.) and
            # not standalone media. They get picked up on subsequent walks of
            # the same dir if separate (sibling MKVs etc).
            continue

        for name in files:
            if name.startswith("."):
                continue  # Skip hidden/dot files (macOS resource forks, etc.)
            if Path(name).suffix.lower() in extensions:
                all_files.append(root_path / name)

    # Detect duplicate x264 / HEVC pairs — if an HEVC version of the same
    # release already exists next to the x264 source, skip the x264. This
    # happens when a conversion was interrupted after writing the output
    # but before deleting the original. The HEVC output's filename tag
    # depends on the encoder used:
    #   - libx265 output → `x265` in the filename
    #   - NVENC   output → `h265`
    # so we check BOTH possibilities, not just `x265`.
    from backend.converter import rename_source_to_target_codec, rename_source_quality_in_filename
    all_paths_set = {str(f) for f in all_files}
    skip_paths: set[str] = set()
    for f in all_files:
        # v0.6.0: disc-marker sibling detection. For a disc item, the
        # converted output is in the PARENT folder of VIDEO_TS/ or
        # BDMV/ (one level up from the marker's parent). We prefix-match
        # on the parent-folder name + DVDRip/Bluray token because the
        # full constructed name depends on probe-time data (resolution,
        # audio codec, channels) that can drift between scans.
        if f.name.lower() == "video_ts.ifo" and f.parent.name.lower() == "video_ts":
            disc_root = f.parent.parent
            disc_root_name = disc_root.name
            try:
                for sibling in disc_root.iterdir():
                    if sibling.suffix.lower() != ".mkv":
                        continue
                    if not sibling.stem.startswith(disc_root_name):
                        continue
                    if "dvdrip" in sibling.stem.lower():
                        skip_paths.add(str(f))
                        print(
                            f"[SCANNER] Skipping DVD (converted version exists: "
                            f"{sibling.name}): {disc_root_name}",
                            flush=True,
                        )
                        break
            except OSError:
                pass
            continue
        if f.name.lower() == "index.bdmv" and f.parent.name.lower() == "bdmv":
            disc_root = f.parent.parent
            disc_root_name = disc_root.name
            try:
                for sibling in disc_root.iterdir():
                    if sibling.suffix.lower() != ".mkv":
                        continue
                    if not sibling.stem.startswith(disc_root_name):
                        continue
                    if "bluray" in sibling.stem.lower():
                        skip_paths.add(str(f))
                        print(
                            f"[SCANNER] Skipping Blu-ray (converted version exists: "
                            f"{sibling.name}): {disc_root_name}",
                            flush=True,
                        )
                        break
            except OSError:
                pass
            continue

        name = f.name
        candidates: set[str] = set()
        for encoder in ("libx265", "nvenc"):
            renamed = rename_source_to_target_codec(name, encoder=encoder)
            # v0.5.18: match get_output_path()'s rename chain so disc-tier
            # source siblings (e.g. "X.BR-DISK.x264.mkv" → "X.Bluray.x265.mkv")
            # are correctly detected and the source gets skip-flagged.
            renamed = rename_source_quality_in_filename(renamed)
            if renamed != name:
                # The conversion pipeline always writes .mkv regardless of
                # source container, so match the HEVC sibling with that
                # extension explicitly.
                stem_only = renamed.rsplit(".", 1)[0] if "." in renamed else renamed
                candidates.add(str(f.parent / f"{stem_only}.mkv"))
        hits = [c for c in candidates if c in all_paths_set and str(f) != c]
        if hits:
            skip_paths.add(str(f))
            print(
                f"[SCANNER] Skipping duplicate x264 (HEVC version exists: "
                f"{Path(hits[0]).name}): {f.name}",
                flush=True,
            )

    all_files = [f for f in all_files if str(f) not in skip_paths]
    total = len(all_files)
    results: list[ScannedFile] = []

    # Load ignored files set
    import aiosqlite
    ignored_paths: set[str] = set()
    try:
        db = await aiosqlite.connect(settings.db_path)
        db.row_factory = aiosqlite.Row
        try:
            async with db.execute("SELECT file_path FROM ignored_files") as cur:
                rows = await cur.fetchall()
                ignored_paths = {row["file_path"] for row in rows}
        finally:
            await db.close()
    except Exception:
        pass

    # v0.7.9: parallel ffprobe pre-pass.
    # Pre-v0.7.9 the per-file loop did probe → classify → emit serially, so a
    # 120K-file library took ~12h on a NUC. ffprobe is the dominant cost (~300ms
    # each); classify is cheap (<5ms). Pre-probe in Semaphore-bounded chunks,
    # then run the existing serial classify loop with the cached probe results.
    # DB writes still flow through result_callback's batched single-writer path,
    # so this adds zero new DB contention with a concurrent queue worker.
    PROBE_CHUNK = max(SCAN_CONCURRENCY * 50, 100)  # ~100-200 files per gather
    probe_sem = asyncio.Semaphore(SCAN_CONCURRENCY)

    async def _do_probe(fp):
        async with probe_sem:
            if cancel_check and cancel_check():
                return None
            return await probe_file(str(fp))

    print(
        f"[SCANNER] Pre-probing {total} files with concurrency={SCAN_CONCURRENCY}",
        flush=True,
    )
    probes: dict[str, Optional[dict]] = {}
    for chunk_start in range(0, len(all_files), PROBE_CHUNK):
        if cancel_check and cancel_check():
            print(
                f"[SCANNER] Cancelled during pre-probe after {chunk_start} files in {dir_path}",
                flush=True,
            )
            break
        chunk = all_files[chunk_start : chunk_start + PROBE_CHUNK]
        chunk_probes = await asyncio.gather(*[_do_probe(fp) for fp in chunk])
        for fp, pr in zip(chunk, chunk_probes):
            probes[str(fp)] = pr
        if progress_callback:
            await progress_callback(
                status="scanning",
                current_file=str(chunk[-1]),
                files_found=total,
                files_probed=min(chunk_start + PROBE_CHUNK, total),
                total_files=total,
            )

    for idx, file_path in enumerate(all_files):
        if cancel_check and cancel_check():
            print(f"[SCANNER] Cancelled after {idx} files in {dir_path}", flush=True)
            break

        # Yield to event loop so other tasks (queue worker, websocket, API) can run
        await asyncio.sleep(0.005)

        # v0.9.30: the parallel ffprobe pre-pass above fills the bar to
        # total/total, then this serial classify + DB-write pass ran silently —
        # the UI froze on the last probed file for however long classification
        # took (minutes on large directories). Emit a throttled "finalizing"
        # update so the current file and count keep moving.
        if progress_callback and (idx % 20 == 0 or idx + 1 == total):
            await progress_callback(
                status="finalizing",
                current_file=str(file_path),
                files_found=total,
                files_probed=idx + 1,
                total_files=total,
            )

        # v0.7.9: use the pre-computed probe. Falls back to a fresh probe only
        # if the pre-probe phase was cancelled mid-way and this file's key is
        # missing — keeps behavior correct on cancel paths.
        probe = probes.get(str(file_path))
        if probe is None and str(file_path) not in probes:
            probe = await probe_file(str(file_path))
        if idx == 0:
            if probe is None:
                print(f"[SCANNER] WARNING: First file probe FAILED: {file_path}", flush=True)
            else:
                print(f"[SCANNER] First file probe OK: codec={probe.get('video_codec')}, dur={probe.get('duration')}", flush=True)
            # Test ffprobe binary
            import shutil
            ffprobe_path = shutil.which("ffprobe")
            print(f"[SCANNER] ffprobe binary: {ffprobe_path}", flush=True)
            if probe is None:
                try:
                    test_proc = await asyncio.create_subprocess_exec(
                        "ffprobe", "-version",
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    test_out, test_err = await asyncio.wait_for(test_proc.communicate(), timeout=5)
                    print(f"[SCANNER] ffprobe -version: rc={test_proc.returncode}, {test_out.decode()[:200]}", flush=True)
                except Exception as test_exc:
                    print(f"[SCANNER] ffprobe binary ERROR: {test_exc}", flush=True)
                # Also try probing with full stderr to see what's wrong
                try:
                    test_proc2 = await asyncio.create_subprocess_exec(
                        "ffprobe", "-v", "error", "-show_format", str(file_path),
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    t_out, t_err = await asyncio.wait_for(test_proc2.communicate(), timeout=10)
                    print(f"[SCANNER] ffprobe debug: rc={test_proc2.returncode}, stderr={t_err.decode()[:300]}", flush=True)
                except Exception as te:
                    print(f"[SCANNER] ffprobe debug failed: {te}", flush=True)
        if probe is None:
            # Store corrupt file entry so it appears in the Corrupt filter.
            # Even for unprobed files, still detect external subtitles so they
            # show up in the UI (independent of ffprobe success).
            try:
                file_size_corrupt = os.path.getsize(str(file_path))
                file_mtime_corrupt = os.path.getmtime(str(file_path))
            except OSError:
                file_size_corrupt = 0
                file_mtime_corrupt = None

            # Detect external subs even for corrupt files
            corrupt_subs: list = []
            corrupt_has_ext = False
            try:
                ext_subs_raw_c = detect_external_subtitles(str(file_path))
                corrupt_has_ext = len(ext_subs_raw_c) > 0
                if ext_subs_raw_c:
                    for i, es in enumerate(ext_subs_raw_c):
                        es["stream_index"] = -(i + 1)
                    for cls_track, raw in zip(classify_subtitle_tracks(ext_subs_raw_c, "und"), ext_subs_raw_c):
                        cls_track = cls_track.model_copy(update={
                            "external": True,
                            "external_path": raw["external_path"],
                        })
                        corrupt_subs.append(cls_track)
            except Exception as exc:
                print(f"[SCANNER] Ext sub detection failed for unprobed {file_path.name}: {exc}", flush=True)

            corrupt_entry = ScannedFile(
                file_path=str(file_path),
                file_name=file_path.name,
                folder_name=file_path.parent.name,
                file_size=file_size_corrupt,
                file_size_gb=round(file_size_corrupt / (1024 ** 3), 3),
                video_codec="unknown",
                needs_conversion=False,
                audio_tracks=[],
                subtitle_tracks=corrupt_subs,
                native_language="und",
                has_removable_tracks=False,
                has_removable_subs=False,
                has_external_subs=corrupt_has_ext,
                estimated_savings_bytes=0,
                estimated_savings_gb=0,
                file_mtime=file_mtime_corrupt,
                duration=0,
                probe_status="corrupt",
            )
            if result_callback:
                await result_callback(corrupt_entry)
            else:
                results.append(corrupt_entry)
            continue

        video_codec = probe["video_codec"]
        raw_tracks = probe["audio_tracks"]
        duration = probe["duration"]
        file_size = probe["file_size"]

        # Try API-based language detection first (skip if cancelled to allow fast exit).
        # Also skip when the file lives in a directory the user marked
        # "Other" — those directories hold non-movie/non-tv content (home
        # videos, music videos, lectures, etc.) and TMDB matches against
        # them produce spurious results. v0.3.33+.
        api_lang = None
        if not (cancel_check and cancel_check()):
            try:
                from backend.media_paths import is_other_typed_dir
                if await is_other_typed_dir(str(file_path)):
                    pass  # Skip TMDB lookup — directory is non-cataloguable
                else:
                    from backend.metadata import lookup_original_language
                    api_lang = await asyncio.wait_for(
                        lookup_original_language(str(file_path)),
                        timeout=10,
                    )
            except asyncio.TimeoutError:
                print(f"[SCANNER] Metadata lookup timed out for {file_path.name}", flush=True)
            except Exception as exc:
                print(f"[SCANNER] Metadata lookup failed for {file_path.name}: {exc}", flush=True)

        native_lang = api_lang if api_lang else detect_native_language(raw_tracks)
        language_source = "api" if api_lang else "heuristic"

        needs_conversion = codec_matches_source(video_codec, source_codecs)
        audio_tracks = classify_audio_tracks(raw_tracks, native_lang, duration)
        raw_subs = probe.get("subtitle_tracks", [])
        subtitle_tracks = classify_subtitle_tracks(raw_subs, native_lang)

        # Detect external subtitle files (.srt, .ass, etc.) alongside the video
        ext_subs_raw = detect_external_subtitles(str(file_path))
        has_external_subs = len(ext_subs_raw) > 0
        if ext_subs_raw:
            # Assign negative stream indices to avoid collision with embedded
            for i, es in enumerate(ext_subs_raw):
                es["stream_index"] = -(i + 1)
            # Classify with the same language rules as embedded subs
            ext_classified = classify_subtitle_tracks(ext_subs_raw, native_lang)
            # Carry over external fields that classify doesn't know about
            for cls_track, raw in zip(ext_classified, ext_subs_raw):
                cls_track = cls_track.model_copy(update={
                    "external": True,
                    "external_path": raw["external_path"],
                })
                subtitle_tracks.append(cls_track)

        tracks_to_remove = [t for t in audio_tracks if not t.keep]
        has_removable = len(tracks_to_remove) > 0
        has_removable_subs = any(not t.keep for t in subtitle_tracks)

        # Check if native-language audio isn't the first stream (needs reorder)
        needs_audio_reorder = False
        if _is_cleanup_enabled("reorder_native_audio") and len(audio_tracks) > 1 and native_lang and native_lang.lower() != "und":
            first_lang = (audio_tracks[0].language or "").lower()
            needs_audio_reorder = not languages_match(first_lang, native_lang.lower())

        savings_bytes = estimate_savings(file_size, needs_conversion, tracks_to_remove, duration, cq=global_cq)
        savings_gb = round(savings_bytes / (1024 ** 3), 3)
        # v0.6.7: video-only portion of savings stored separately so the
        # frontend file-detail panel (which shows the "Convert to x265
        # (est. save ~X GB)" hint) reads the same CQ-calibrated number
        # instead of recomputing file_size * 0.3 client-side.
        video_conv_bytes = video_conv_savings_bytes(file_size, global_cq) if needs_conversion else 0

        # For disc items the file_path is the marker (.../<Disc Root>/VIDEO_TS/VIDEO_TS.IFO
        # or .../<Disc Root>/BDMV/index.bdmv). The user-facing name should be the
        # disc-root folder (file_path.parent.parent.name), not "VIDEO_TS.IFO". v0.6.0+.
        # v0.7.2: helper handles ISO inputs correctly (parent vs parent.parent).
        disc_type_val = probe.get("disc_type")
        display_name = _disc_display_name(file_path, disc_type_val)

        # Get file modification time from disk. For discs, the marker file
        # (VIDEO_TS.IFO / index.bdmv) keeps the original DVD/BDMV authoring
        # timestamp — often decades old — which makes "Newest" sort treat
        # freshly-added discs as ancient. Use the disc-root folder's mtime
        # instead, which reflects when the user actually copied the disc
        # into their library. v0.6.3+.
        try:
            if disc_type_val:
                file_mtime = file_path.parent.parent.stat().st_mtime
            else:
                file_mtime = os.path.getmtime(str(file_path))
        except OSError:
            file_mtime = None

        scanned = ScannedFile(
            file_path=str(file_path),
            file_name=display_name,
            folder_name=file_path.parent.name,
            file_size=file_size,
            file_size_gb=round(file_size / (1024 ** 3), 3),
            video_codec=video_codec,
            needs_conversion=needs_conversion,
            audio_tracks=audio_tracks,
            subtitle_tracks=subtitle_tracks,
            native_language=native_lang,
            language_source=language_source,
            has_removable_tracks=has_removable,
            has_removable_subs=has_removable_subs,
            has_external_subs=has_external_subs,
            estimated_savings_bytes=savings_bytes,
            estimated_savings_gb=savings_gb,
            video_conv_savings_bytes=video_conv_bytes,
            ignored=str(file_path) in ignored_paths,
            file_mtime=file_mtime,
            duration=duration,
            probe_status="ok",
            video_height=probe.get("video_height", 0),
            disc_type=disc_type_val,  # v0.6.0
        )
        if result_callback:
            await result_callback(scanned)
            # v0.7.2: after a disc row is written, clear any stale
            # health_status='corrupt' left over from a previous health-check
            # that ran while VIDEO_TS/BDMV was deleted mid-conversion. The
            # fresh probe success means the disc is fine; the row must not
            # inherit the previous corrupt flag. No-op for non-disc rows
            # (helper filters on disc_type IS NOT NULL).
            if disc_type_val:
                await _clear_stale_disc_health_status(
                    str(settings.db_path), str(file_path)
                )
        else:
            results.append(scanned)

    # v0.9.28: emit the directory's final counts as a non-terminal "scanning"
    # update — NOT "done". This function scans a single directory inside a
    # multi-directory loop; the terminal "done" is written once by the worker
    # after every directory and the post-scan phases finish. Emitting "done"
    # per directory told the UI the whole scan was complete after the first
    # directory (premature "Scan"/idle flip).
    if progress_callback:
        await progress_callback(
            status="scanning",
            current_file="",
            files_found=total,
            files_probed=total,
            total_files=total,
        )

    return results


async def _clear_stale_disc_health_status(db_path: str, file_path: str) -> None:
    """Reset stale corrupt markers on a disc row at `file_path`.

    Called after a disc re-probe succeeds, so previously-stuck flags
    (from when the disc subdirectory was deleted mid-conversion in
    v0.6.x, or from an aborted health-check) get cleared on next
    discovery. v0.7.8 extended this to also clear `probe_status` and
    `health_errors_json` — the UI's `isCorrupt` derives from EITHER
    `health_status='corrupt'` OR `probe_status='corrupt'`, so leaving
    `probe_status` stuck while `health_status` cleared left the
    "ffprobe couldn't read a video stream" banner showing on a disc
    that actually probes clean. v0.7.2+ (extended v0.7.8+).

    No-op for rows where disc_type IS NULL — file-level health checks
    are independent.
    """
    import aiosqlite
    db = await aiosqlite.connect(db_path)
    try:
        cur = await db.execute(
            "UPDATE scan_results SET "
            "  health_status = NULL, "
            "  probe_status = 'ok', "
            "  health_errors_json = NULL "
            "WHERE file_path = ? AND disc_type IS NOT NULL "
            "  AND ("
            "    health_status IS NOT NULL "
            "    OR (probe_status IS NOT NULL AND probe_status != 'ok') "
            "    OR health_errors_json IS NOT NULL"
            "  )",
            (file_path,),
        )
        if cur.rowcount > 0:
            print(
                f"[SCANNER] cleared stale corrupt markers on disc row {file_path}",
                flush=True,
            )
        await db.commit()
    finally:
        await db.close()
