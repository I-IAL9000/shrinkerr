"""Content type detection and resolution-aware CQ/CRF recommendations.

Detects content type from filename patterns (anime, grain, animation, remux)
and recommends encoding quality settings per resolution tier.
"""

import os
import re

# ─── Content profiles ───

# Pattern groups: each entry is (compiled_regex, profile_key)
# Order matters — first match wins
_CONTENT_PATTERNS: list[tuple[re.Pattern, str]] = []

# Anime: explicit tag or known release groups
_ANIME_PATTERNS = [
    r"\banime\b",
    r"\[(?:SubsPlease|Erai-raws|HorribleSubs|Judas|ASW|Ember|EMBER|Setsugen|Tsundere|Commie)\]",
    r"\[(?:DB|Cleo|Kametsu|BlueLobster|Moozzi2|YURI|LostYears|SCY)\]",
    r"\bBDRip\b.*\[.*?\]",  # BDRip with bracket groups (common anime pattern)
]

# Grain / film grain: explicit tags
_GRAIN_PATTERNS = [
    r"\bgrain\b",
    r"\bfilm[._-]?grain\b",
    r"\bGrainFilter\b",
]

# Animation (non-anime): studio names, genre tags
_ANIMATION_PATTERNS = [
    r"\b(?:animation|cartoon)\b",
    r"\b(?:Pixar|Disney|DreamWorks|Illumination|BlueSky|Ghibli|Laika)\b",
]

# Remux: pristine source
_REMUX_PATTERNS = [
    r"\bremux\b",
    r"\bbdremux\b",
]

# Build compiled pattern list
for _pat in _ANIME_PATTERNS:
    _CONTENT_PATTERNS.append((re.compile(_pat, re.IGNORECASE), "anime"))
for _pat in _GRAIN_PATTERNS:
    _CONTENT_PATTERNS.append((re.compile(_pat, re.IGNORECASE), "grain"))
for _pat in _ANIMATION_PATTERNS:
    _CONTENT_PATTERNS.append((re.compile(_pat, re.IGNORECASE), "animation"))
for _pat in _REMUX_PATTERNS:
    _CONTENT_PATTERNS.append((re.compile(_pat, re.IGNORECASE), "remux"))


# ─── CQ/CRF recommendation tables ───

# Per-profile, per-resolution CQ values (NVENC hevc_nvenc -cq mode)
CQ_TABLE: dict[str, dict[str, int]] = {
    "anime":     {"4k": 24, "1080p": 22, "720p": 20, "sd": 18},
    "grain":     {"4k": 26, "1080p": 24, "720p": 22, "sd": 20},
    "animation": {"4k": 26, "1080p": 24, "720p": 22, "sd": 20},
    "remux":     {"4k": 22, "1080p": 20, "720p": 18, "sd": 16},
    "default":   {"4k": 24, "1080p": 20, "720p": 18, "sd": 16},
}

# CRF offset: libx265 CRF is generally ~2 higher than NVENC CQ for similar quality
CRF_OFFSET = 2

# Human-readable profile labels
PROFILE_LABELS: dict[str, str] = {
    "anime": "Anime",
    "grain": "Grain/Film",
    "animation": "Animation",
    "remux": "Remux",
    "default": "Live Action",
}


# ─── Public API ───

def detect_content_type(filename: str) -> str:
    """Detect content type from filename patterns. Returns profile key.

    Args:
        filename: Just the filename (not full path). Use os.path.basename() first.

    Returns:
        One of: "anime", "grain", "animation", "remux", "default"
    """
    # Also check parent folder name (often contains release group tags)
    for pattern, profile in _CONTENT_PATTERNS:
        if pattern.search(filename):
            return profile
    return "default"


def detect_content_type_from_path(file_path: str) -> str:
    """Detect content type from full file path (checks filename + parent folders).

    Useful when release group tags are in the folder name rather than the file.
    """
    # Check the last 3 path components (file + 2 parent dirs)
    parts = file_path.replace("\\", "/").split("/")
    search_text = "/".join(parts[-3:]) if len(parts) >= 3 else file_path
    for pattern, profile in _CONTENT_PATTERNS:
        if pattern.search(search_text):
            return profile
    return "default"


def get_recommended_cq(content_type: str, resolution_tier: str) -> int:
    """Get recommended NVENC CQ value for content type + resolution.

    Returns:
        CQ value (lower = better quality, larger files). Range: 16-26.
    """
    profile = CQ_TABLE.get(content_type, CQ_TABLE["default"])
    return profile.get(resolution_tier, profile.get("1080p", 20))


def get_recommended_crf(content_type: str, resolution_tier: str) -> int:
    """Get recommended libx265 CRF value for content type + resolution.

    CRF is generally ~2 higher than NVENC CQ for similar visual quality.

    Returns:
        CRF value (lower = better quality). Range: 18-28.
    """
    return get_recommended_cq(content_type, resolution_tier) + CRF_OFFSET


def get_profile_summary(content_type: str) -> dict:
    """Get a summary dict for a content profile (useful for API responses)."""
    return {
        "key": content_type,
        "label": PROFILE_LABELS.get(content_type, content_type.title()),
        "cq_table": CQ_TABLE.get(content_type, CQ_TABLE["default"]),
    }


# ─── Per-file CQ for jobs and the estimate ───

def smart_cq_settings(values: dict) -> dict:
    """The "Content type detection" and "Resolution-aware quality" settings,
    parsed from settings rows (string values), for smart_cq()."""
    return {
        "content_detect": str(values.get("content_type_detection", "true")).lower() == "true",
        "resolution_aware": str(values.get("resolution_aware_cq", "false")).lower() == "true",
        "resolution_cqs": {tier: int(values.get(f"resolution_cq_{tier}") or default)
                           for tier, default in (("4k", 24), ("1080p", 20), ("720p", 18), ("sd", 16))},
    }


def smart_cq(file_path: str, width, height, settings: dict) -> tuple[int | None, str | None]:
    """(NVENC CQ, content type) that content type detection or resolution-aware
    quality give a file, or (None, None) when neither applies and the global CQ
    does. A rule's or Add to Queue's own CQ wins; callers check those first.

    v0.10.0: only the queue estimate used these settings, so they never
    reached an encode. Add to Queue, auto-queue and webhooks now give jobs the
    same CQ the estimate shows."""
    from backend.resolution import resolution_tier
    tier = resolution_tier(width, height, file_path) or "sd"
    if settings["content_detect"]:
        ctype = detect_content_type_from_path(file_path)
        if ctype != "default":
            return get_recommended_cq(ctype, tier), ctype
    if settings["resolution_aware"]:
        return settings["resolution_cqs"][tier], None
    return None, None


def smart_quality(file_path: str, width, height, settings: dict) -> tuple[int | None, int | None]:
    """smart_cq() as a job's (nvenc_cq, libx265_crf); libx265's CRF runs
    CRF_OFFSET above NVENC's CQ for similar quality. (None, None) leaves the
    job on the global settings. QSV, VAAPI and VideoToolbox have no per-job
    quality, so they keep their own settings, as with rules."""
    cq, _ = smart_cq(file_path, width, height, settings)
    return (cq, cq + CRF_OFFSET) if cq is not None else (None, None)
