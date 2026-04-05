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


def get_resolution_tier(video_height: int) -> str:
    """Map video height to resolution tier.

    Args:
        video_height: Vertical resolution in pixels (e.g., 2160, 1080, 720).

    Returns:
        One of: "4k", "1080p", "720p", "sd"
    """
    if video_height >= 2000:
        return "4k"
    if video_height >= 900:
        return "1080p"
    if video_height >= 600:
        return "720p"
    return "sd"


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
