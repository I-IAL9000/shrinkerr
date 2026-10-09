"""One resolution classifier (v0.10.0, SC-22).

Rules, content-aware CQ, the Scanner's resolution filters and counts, and the
converted file's name each classified by height alone, with different
cut-offs: a 1920x800 scope film was "720p" (and was encoded at the 720p CQ),
1280x534 was "SD" and 2560x1440 "4K". A video's tier is the highest one whose
width OR height it reaches, so a wide frame is judged by its width.

Rows scanned before widths were stored only have a height until the next full
scan. For those, a resolution tag in the path can raise the tier (the Scanner
filters' rule since v0.9.116), and an explicit HD/SD tag wins over a stray
4K/UHD one, so a "1080p" file in a "/4K/" folder stays 1080p (v0.9.117).
"""

# (tier, rank, min width, min height), highest first. The heights are the
# long-standing ones: 1900 catches 2:1 4K (3840x1920), 900 2:1 1080p.
_TIERS = (("4k", 4, 3200, 1900), ("1080p", 3, 1800, 900), ("720p", 2, 1200, 600))
_TIER_BY_RANK = {4: "4k", 3: "1080p", 2: "720p", 1: "sd"}
RANKS = {tier: rank for rank, tier in _TIER_BY_RANK.items()}
# Path tags in the order they're tried: explicit HD/SD tags before 4K ones.
_PATH_TAGS = (
    (3, ("1080p", "1080i")),
    (2, ("720p",)),
    (1, ("576p", "480p")),
    (4, ("2160p", "uhd", "4k")),
)


def _size_rank(w: int, h: int) -> int:
    if not (w or h):
        return 0
    for _, rank, min_w, min_h in _TIERS:
        if w >= min_w or h >= min_h:
            return rank
    return 1


def _tag_rank(file_path: str) -> int:
    p = (file_path or "").lower()
    for rank, tags in _PATH_TAGS:
        if any(t in p for t in tags):
            return rank
    return 0


def resolution_tier(width, height, file_path: str = "") -> str | None:
    """"4k", "1080p", "720p" or "sd" — None when neither the size nor the path
    tells. With a known width the size decides alone; path tags only help
    rows that have a height but no width yet (or neither)."""
    w, h = int(width or 0), int(height or 0)
    rank = _size_rank(w, h)
    if not w:
        rank = max(rank, _tag_rank(file_path))
    return _TIER_BY_RANK.get(rank)


def sql_resolution_rank() -> str:
    """SQL for resolution_tier's rank over scan_results' video_width,
    video_height and file_path (4 = 4k, 3 = 1080p, 2 = 720p, 1 = sd,
    0 = unknown). Generated from the same tables, so the Scanner's counts
    and lists agree with each other and with the Python side."""
    w, h = "COALESCE(video_width, 0)", "COALESCE(video_height, 0)"
    size = ("CASE " + " ".join(f"WHEN {w} >= {mw} OR {h} >= {mh} THEN {r}" for _, r, mw, mh in _TIERS)
            + f" WHEN {w} > 0 OR {h} > 0 THEN 1 ELSE 0 END")
    tag = ("CASE " + " ".join(
        "WHEN " + " OR ".join(f"LOWER(file_path) LIKE '%{t}%'" for t in tags) + f" THEN {r}"
        for r, tags in _PATH_TAGS) + " ELSE 0 END")
    return f"(CASE WHEN {w} > 0 THEN {size} ELSE MAX({size}, {tag}) END)"
