"""Parse media titles from scene-style filenames and folder names.

Handles patterns like:
  Movie.Name.2024.1080p.BluRay.x265.DDP5.1-GROUP.mkv
  Show.Name.S01E02.720p.HDTV.x264-LOL.mkv
  Movie Name (2024) [1080p] [BluRay].mkv
  2001.A.Space.Odyssey.1968.1080p.BluRay.x265-GROUP.mkv
"""

import os
import re
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ParsedMedia:
    title: str = ""
    year: Optional[str] = None
    season: Optional[int] = None
    episode: Optional[int] = None
    resolution: Optional[str] = None
    source: Optional[str] = None
    codec: Optional[str] = None
    audio: Optional[str] = None
    release_group: Optional[str] = None
    media_type: str = "movie"  # "tv" or "movie"


# ── Compiled patterns (module-level for performance) ──

_EPISODE_RE = re.compile(r'[Ss](\d{1,2})[Ee](\d{1,3})')
_YEAR_RE = re.compile(r'(?:^|[.\s\-_\(])((?:19|20)\d{2})(?=[.\s\-_\)\]]|$)')
_YEAR_PAREN_RE = re.compile(r'\(?((?:19|20)\d{2})\)?')

_RESOLUTIONS = r'2160p|1080p|1080i|720p|480p|4K|UHD'
_SOURCES = (
    r'BluRay|Blu[\-\s]?Ray|BDRip|BRRip|BDRemux|HDTV|'
    r'WEB[\-\s]?DL|WEBRip|WEB|DVDRip|DVD|Remux|PDTV|SDTV|'
    # Rip / lower-quality sources
    r'HDRip|HDRIP|TVRip|TVRIP|SATRip|SATRIP|DSRip|DSR|'
    # Cam / telecine / telesync
    r'HDTS|TELESYNC|TS|TELECINE|TC|CAM|HDCAM|HDTC|HDLine|R5|'
    # Scene-mangled WEB marker — ends up as bare "www" in many TV rips
    r'WWW|www'
)
_CODECS = r'[xX]\.?264|[xX]\.?265|H\.?264|H\.?265|HEVC|AVC|AV1|XviD|DivX|MPEG2|VP9'
_AUDIO = (
    r'DTS[\-\s]?HD[\.\s]?(?:MA|HR)|TrueHD\.?Atmos|TrueHD|Atmos|'
    r'DDP\d[\.\d]*|DD\d[\.\d]*|DDP|DD|DTS|'
    r'EAC3|AC3|AAC\d[\.\d]*|AAC|FLAC|LPCM|PCM|OPUS'
)
_TAGS = (
    r'REPACK|PROPER|EXTENDED|UNRATED|DC|DIRECTORS[\.\s]CUT|IMAX|'
    r'HDR10Plus|HDR10|HDR|DV|DoVi|Hybrid|'
    r'AMZN|NF|DSNP|HMAX|ATVP|PMTP|APTV|iT|ROKU|PCOK|CRAV|MA'
)

# Combined "scene token" pattern — first occurrence marks end of title
_SCENE_TOKEN_RE = re.compile(
    rf'(?:^|[.\s\-_\[])({_RESOLUTIONS}|{_SOURCES}|{_CODECS}|{_AUDIO}|{_TAGS})(?:[.\s\-_\]\)]|$)',
    re.IGNORECASE,
)

# Individual extraction patterns
_RES_RE = re.compile(rf'\b({_RESOLUTIONS})\b', re.IGNORECASE)
_SRC_RE = re.compile(rf'(?:^|[.\s\-_\[])({_SOURCES})(?:[.\s\-_\]\)]|$)', re.IGNORECASE)
_CODEC_RE = re.compile(rf'(?:^|[.\s\-_\[])({_CODECS})(?:[.\s\-_\]\)]|$)', re.IGNORECASE)
_AUDIO_RE = re.compile(rf'(?:^|[.\s\-_\[])({_AUDIO})(?:[.\s\-_\]\)]|$)', re.IGNORECASE)
_GROUP_RE = re.compile(r'-([A-Za-z0-9]{2,15})$')

# Bracket content to strip
_BRACKETS_RE = re.compile(r'\[([^\]]*)\]')


def parse_media_name(name: str) -> ParsedMedia:
    """Parse a media filename or folder name into structured metadata.

    Args:
        name: Filename (with or without extension) or folder name.
              Examples: "Movie.Name.2024.1080p.BluRay.x265-GROUP.mkv"
                       "Show Name S01E02"
                       "Movie Name (2024)"
    """
    result = ParsedMedia()

    # Strip extension
    base, _ = os.path.splitext(name)
    if not base:
        return result

    # Extract release group (last -Token before extension)
    group_match = _GROUP_RE.search(base)
    if group_match:
        candidate = group_match.group(1)
        # Don't treat known tokens as release groups
        if not re.match(rf'^(?:{_RESOLUTIONS}|{_CODECS})$', candidate, re.IGNORECASE):
            result.release_group = candidate

    # Try S##E## first (TV detection)
    ep_match = _EPISODE_RE.search(base)
    if ep_match:
        result.season = int(ep_match.group(1))
        result.episode = int(ep_match.group(2))
        result.media_type = "tv"
        title_end = ep_match.start()
        remainder = base[ep_match.end():]
    else:
        # Find year candidates — pick the rightmost one that precedes a scene token
        title_end = None
        remainder = ""

        # First try parenthesized year: "Movie Name (2024)"
        year_paren = re.search(r'\((\d{4})\)', base)
        if year_paren:
            y = year_paren.group(1)
            if 1920 <= int(y) <= 2030:
                result.year = y
                title_end = year_paren.start()
                remainder = base[year_paren.end():]

        if title_end is None:
            # Find all bare year matches
            year_matches = list(_YEAR_RE.finditer(base))
            if year_matches:
                # For each year candidate (from right to left), check if a scene token follows
                for ym in reversed(year_matches):
                    y = ym.group(1)
                    if not (1920 <= int(y) <= 2030):
                        continue
                    after_year = base[ym.end():]
                    # Check if a scene token follows within a few chars
                    if _SCENE_TOKEN_RE.search(after_year[:30]) or not after_year.strip(".-_ "):
                        result.year = y
                        title_end = ym.start()
                        remainder = after_year
                        break
                # If no year preceded a scene token, use the first valid year
                if title_end is None and year_matches:
                    for ym in year_matches:
                        y = ym.group(1)
                        if 1920 <= int(y) <= 2030:
                            result.year = y
                            title_end = ym.start()
                            remainder = base[ym.end():]
                            break

        if title_end is None:
            # No year found — find first scene token
            token_match = _SCENE_TOKEN_RE.search(base)
            if token_match:
                title_end = token_match.start()
                remainder = base[title_end:]
            else:
                # No scene tokens at all — use full name minus release group
                if group_match:
                    title_end = group_match.start()
                    remainder = ""
                else:
                    title_end = len(base)
                    remainder = ""

    # Clean title
    raw_title = base[:title_end]
    # Remove brackets and their contents from title
    raw_title = _BRACKETS_RE.sub(" ", raw_title)
    # Replace dots, underscores, hyphens used as separators with spaces
    title = re.sub(r'[._]', ' ', raw_title)
    title = re.sub(r'\s*-\s*', ' - ', title)  # preserve intentional hyphens
    title = re.sub(r'\s{2,}', ' ', title)
    title = title.strip().rstrip(" -–—")
    result.title = title

    # Extract metadata from remainder
    full_meta = remainder + (" " + base[title_end:] if not remainder else "")

    res_match = _RES_RE.search(full_meta)
    if res_match:
        result.resolution = res_match.group(1)

    src_match = _SRC_RE.search(full_meta)
    if src_match:
        result.source = src_match.group(1)

    codec_match = _CODEC_RE.search(full_meta)
    if codec_match:
        result.codec = codec_match.group(1)

    audio_match = _AUDIO_RE.search(full_meta)
    if audio_match:
        result.audio = audio_match.group(1)

    return result


def parse_media_path(file_path: str) -> ParsedMedia:
    """Parse a full file path, trying filename first then falling back to folder name.

    Merges metadata from both filename and parent folder for best results.
    """
    basename = os.path.basename(file_path)
    result = parse_media_name(basename)

    # If filename parsing didn't find a year or title looks generic, try parent folder
    if not result.year and not result.season:
        parent = os.path.basename(os.path.dirname(file_path))
        if parent and parent not in (".", ""):
            folder_result = parse_media_name(parent)
            if folder_result.year and not result.year:
                result.year = folder_result.year
            if folder_result.title and (
                not result.title or len(result.title) < 3
            ):
                result.title = folder_result.title
            if folder_result.media_type == "tv" and result.media_type == "movie":
                result.media_type = folder_result.media_type

    return result
