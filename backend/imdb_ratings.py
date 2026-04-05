"""IMDb ratings from the official dataset (datasets.imdbws.com).

Downloads title.ratings.tsv.gz (~20MB) and loads it into memory as a dict
keyed by IMDb ID (e.g. "tt1234567") → {"rating": 8.1, "votes": 12345}.

The dataset is refreshed daily by IMDb. We re-download every 24 hours.
"""

import gzip
import os
import time
from pathlib import Path

_RATINGS_URL = "https://datasets.imdbws.com/title.ratings.tsv.gz"
_CACHE_DIR = Path("/app/data")
_CACHE_FILE = _CACHE_DIR / "imdb_ratings.tsv.gz"
_REFRESH_INTERVAL = 86400  # 24 hours

# In-memory cache
_ratings: dict[str, dict] = {}
_last_loaded: float = 0
_loading: bool = False


async def download_ratings():
    """Download the IMDb ratings dataset."""
    import httpx

    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    print("[IMDB] Downloading ratings dataset...", flush=True)
    try:
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            resp = await client.get(_RATINGS_URL)
            if resp.status_code == 200:
                _CACHE_FILE.write_bytes(resp.content)
                size_mb = len(resp.content) / (1024 * 1024)
                print(f"[IMDB] Downloaded {size_mb:.1f} MB", flush=True)
                return True
            else:
                print(f"[IMDB] Download failed: HTTP {resp.status_code}", flush=True)
                return False
    except Exception as exc:
        print(f"[IMDB] Download failed: {exc}", flush=True)
        return False


def _parse_ratings():
    """Parse the TSV.gz file into the in-memory dict."""
    global _ratings, _last_loaded

    if not _CACHE_FILE.exists():
        return

    start = time.time()
    ratings = {}
    try:
        with gzip.open(_CACHE_FILE, "rt", encoding="utf-8") as f:
            header = f.readline()  # tconst\taverageRating\tnumVotes
            for line in f:
                parts = line.strip().split("\t")
                if len(parts) >= 3:
                    tconst = parts[0]  # e.g. tt1234567
                    try:
                        rating = float(parts[1])
                        votes = int(parts[2])
                        ratings[tconst] = {"rating": rating, "votes": votes}
                    except (ValueError, IndexError):
                        pass

        _ratings = ratings
        _last_loaded = time.time()
        elapsed = time.time() - start
        print(f"[IMDB] Loaded {len(ratings):,} ratings in {elapsed:.1f}s", flush=True)
    except Exception as exc:
        print(f"[IMDB] Parse failed: {exc}", flush=True)


async def ensure_ratings():
    """Ensure ratings are loaded and fresh. Downloads if needed."""
    global _loading

    if _loading:
        return

    # Check if we need to download
    needs_download = False
    if not _CACHE_FILE.exists():
        needs_download = True
    elif time.time() - os.path.getmtime(str(_CACHE_FILE)) > _REFRESH_INTERVAL:
        needs_download = True

    if needs_download:
        _loading = True
        try:
            await download_ratings()
        finally:
            _loading = False

    # Parse if not loaded or stale
    if not _ratings or (_CACHE_FILE.exists() and os.path.getmtime(str(_CACHE_FILE)) > _last_loaded):
        _parse_ratings()


def get_rating(imdb_id: str) -> dict | None:
    """Get rating for an IMDb ID. Returns {"rating": 8.1, "votes": 12345} or None."""
    if not imdb_id:
        return None
    return _ratings.get(imdb_id)


def get_ratings_count() -> int:
    """Get number of loaded ratings."""
    return len(_ratings)
