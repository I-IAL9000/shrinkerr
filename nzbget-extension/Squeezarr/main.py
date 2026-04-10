#!/usr/bin/env python3
#
# Squeezarr NZBGet Post-Processing Script
#
##############################################################################
### NZBGET POST-PROCESSING SCRIPT                                          ###
#
# Squeezarr Post-Processing
#
# Converts media files to x265/HEVC after download completes.
# Configuration is managed in Squeezarr's web UI — only the connection
# details below need to be set in NZBGet.
#
##############################################################################
### OPTIONS                                                                 ###

# Squeezarr server URL.
#SqueezarrUrl=__SQUEEZARR_URL__

# Squeezarr API key. Leave empty if authentication is disabled.
#SqueezarrApiKey=__SQUEEZARR_API_KEY__

### NZBGET POST-PROCESSING SCRIPT                                          ###
##############################################################################

import json
import os
import re
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

# ---------------------------------------------------------------------------
# NZBGet exit codes
# ---------------------------------------------------------------------------
POSTPROCESS_SUCCESS = 93
POSTPROCESS_ERROR = 94
POSTPROCESS_NONE = 95

# ---------------------------------------------------------------------------
# NZBGet environment variables
# ---------------------------------------------------------------------------
DOWNLOAD_DIR = os.environ.get("NZBPP_DIRECTORY", "")
NZB_NAME = os.environ.get("NZBPP_NZBNAME", "")
CATEGORY = os.environ.get("NZBPP_CATEGORY", "")
TOTAL_STATUS = os.environ.get("NZBPP_TOTALSTATUS", "")

SQUEEZARR_URL = os.environ.get("NZBPO_SQUEEZARRURL", "__SQUEEZARR_URL__").rstrip("/")
SQUEEZARR_KEY = os.environ.get("NZBPO_SQUEEZARRAPIKEY", "__SQUEEZARR_API_KEY__")

# ---------------------------------------------------------------------------
# Video file extensions we consider "media"
# ---------------------------------------------------------------------------
MEDIA_EXTENSIONS = {".mkv", ".mp4", ".avi", ".ts", ".m2ts", ".wmv", ".flv", ".mov"}


# ---------------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------------
def log(msg):
    print(f"[INFO] {msg}", flush=True)


def error(msg):
    print(f"[ERROR] {msg}", flush=True)


# ---------------------------------------------------------------------------
# HTTP helpers (stdlib only)
# ---------------------------------------------------------------------------
def api_request(url, method="GET", data=None, headers=None):
    """Make an HTTP request and return parsed JSON, or None on failure."""
    if headers is None:
        headers = {}
    headers.setdefault("Content-Type", "application/json")

    body = json.dumps(data).encode("utf-8") if data else None
    req = Request(url, data=body, headers=headers, method=method)

    try:
        with urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except HTTPError as e:
        error(f"HTTP {e.code}: {e.read().decode()[:200]}")
        return None
    except URLError as e:
        error(f"Connection failed: {e.reason}")
        return None
    except Exception as e:
        error(f"Request failed: {e}")
        return None


def squeezarr_headers():
    """Return headers dict with the Squeezarr API key (if configured)."""
    h = {}
    if SQUEEZARR_KEY and SQUEEZARR_KEY != "__SQUEEZARR_API_KEY__":
        h["X-Api-Key"] = SQUEEZARR_KEY
    return h


# ---------------------------------------------------------------------------
# Squeezarr API
# ---------------------------------------------------------------------------
def fetch_config():
    """Fetch the NZBGet extension config from Squeezarr's API."""
    url = f"{SQUEEZARR_URL}/api/settings/nzbget-config"
    return api_request(url, headers=squeezarr_headers())


def queue_files(file_paths, priority, category=None):
    """POST files to Squeezarr for conversion. Returns number of jobs added."""
    data = {
        "file_paths": file_paths,
        "priority": priority,
        "force_reencode": False,
        "insert_next": True,
    }
    if category:
        data["nzbget_category"] = category

    result = api_request(
        f"{SQUEEZARR_URL}/api/jobs/add-by-path",
        method="POST",
        data=data,
        headers=squeezarr_headers(),
    )

    if result:
        added = result.get("added", 0)
        errs = result.get("errors", [])
        log(f"Queued {added} job(s)")
        for e in errs:
            error(f"  {e}")
        return added
    else:
        error("Failed to queue jobs")
        return 0


def wait_for_jobs(file_paths, timeout=7200):
    """Poll Squeezarr until the specific files we queued are completed."""
    log("Waiting for Squeezarr to finish converting...")
    start = time.time()
    check_interval = 15

    our_files = set(os.path.basename(f) for f in file_paths)

    while time.time() - start < timeout:
        time.sleep(check_interval)

        jobs = api_request(f"{SQUEEZARR_URL}/api/jobs/", headers=squeezarr_headers())
        if not jobs:
            continue

        our_pending = 0
        our_running = 0
        our_done = 0
        for job in jobs:
            job_file = os.path.basename(job.get("file_path", ""))
            if job_file not in our_files:
                continue
            status = job.get("status", "")
            if status == "pending":
                our_pending += 1
            elif status == "running":
                our_running += 1
            elif status in ("completed", "failed"):
                our_done += 1

        elapsed = int(time.time() - start)

        if our_running == 0 and our_pending == 0 and our_done > 0:
            log(f"All {our_done} file(s) completed ({elapsed}s)")
            return True

        if our_running == 0 and our_pending == 0 and our_done == 0:
            if elapsed > 60:
                log("Jobs not found — may have completed already")
                return True

        log(f"  {our_running} converting, {our_pending} pending, {our_done} done ({elapsed}s)")

    error(f"Timed out after {timeout}s")
    return False


# ---------------------------------------------------------------------------
# Sonarr helpers
# ---------------------------------------------------------------------------
def get_sonarr_tags(sonarr_url, sonarr_key):
    """GET /api/v3/tag from Sonarr. Returns {id: label} dict."""
    headers = {"X-Api-Key": sonarr_key} if sonarr_key else {}
    data = api_request(f"{sonarr_url.rstrip('/')}/api/v3/tag", headers=headers)
    if not data:
        return {}
    return {t["id"]: t["label"].lower() for t in data}


def find_series_in_sonarr(nzb_name, sonarr_url, sonarr_key):
    """Search Sonarr for a series matching the NZB name.

    Uses three passes:
      1. Parse series name from NZB, exact title match against local series list
      2. Alternate title match
      3. Sonarr's /api/v3/parse endpoint
    """
    sonarr_url = sonarr_url.rstrip("/")
    headers = {"X-Api-Key": sonarr_key} if sonarr_key else {}

    # Extract series name (everything before S##E##)
    match = re.match(r"^(.+?)[\s._-]+[Ss]\d+", nzb_name)
    search_name = match.group(1).replace(".", " ").replace("_", " ").strip() if match else nzb_name
    search_clean = search_name.lower()

    log(f"Parsed series name: '{search_name}'")

    all_series = api_request(f"{sonarr_url}/api/v3/series", headers=headers)
    if all_series:
        # Pass 1: exact title match
        for series in all_series:
            title = series.get("title", "").lower()
            if title == search_clean:
                log(f"Exact match: '{series.get('title')}'")
                return series

        # Pass 2: alternate title match
        for series in all_series:
            alt_titles = series.get("alternateTitles", [])
            for alt in alt_titles:
                if alt.get("title", "").lower() == search_clean:
                    log(f"Alternate title match: '{series.get('title')}' (via '{alt.get('title')}')")
                    return series

    # Pass 3: Sonarr's own parse endpoint
    parse_result = api_request(
        f"{sonarr_url}/api/v3/parse?title={quote(nzb_name)}",
        headers=headers,
    )
    if parse_result and parse_result.get("series", {}).get("id"):
        parsed_series = parse_result["series"]
        log(f"Sonarr parse match: '{parsed_series.get('title')}'")
        return parsed_series

    return None


# ---------------------------------------------------------------------------
# Radarr helpers
# ---------------------------------------------------------------------------
def get_radarr_tags(radarr_url, radarr_key):
    """GET /api/v3/tag from Radarr. Returns {id: label} dict."""
    headers = {"X-Api-Key": radarr_key} if radarr_key else {}
    data = api_request(f"{radarr_url.rstrip('/')}/api/v3/tag", headers=headers)
    if not data:
        return {}
    return {t["id"]: t["label"].lower() for t in data}


def find_movie_in_radarr(nzb_name, radarr_url, radarr_key):
    """Search Radarr for a movie matching the NZB name.

    Uses three passes:
      1. Parse movie name + year from NZB, exact title match against local movie list
      2. Title-only match (no year) against local movie list
      3. Radarr's /api/v3/parse endpoint
    """
    radarr_url = radarr_url.rstrip("/")
    headers = {"X-Api-Key": radarr_key} if radarr_key else {}

    # Extract movie title and optional year
    # "Movie.Name.2024.1080p..." -> "Movie Name", "2024"
    match = re.match(r"^(.+?)[\s._-]+(\d{4})[\s._-]", nzb_name)
    if match:
        search_name = match.group(1).replace(".", " ").replace("_", " ").strip()
        year = match.group(2)
    else:
        search_name = nzb_name
        year = None

    search_clean = search_name.lower()
    log(f"Parsed movie name: '{search_name}'" + (f" ({year})" if year else ""))

    all_movies = api_request(f"{radarr_url}/api/v3/movie", headers=headers)
    if all_movies:
        # Pass 1: exact title + year match
        for movie in all_movies:
            title = movie.get("title", "").lower()
            movie_year = str(movie.get("year", ""))
            if title == search_clean and (not year or movie_year == year):
                log(f"Exact match: '{movie.get('title')}' ({movie_year})")
                return movie

        # Pass 2: title-only match (if year didn't narrow it down)
        if year:
            for movie in all_movies:
                title = movie.get("title", "").lower()
                movie_year = str(movie.get("year", ""))
                if title == search_clean and movie_year == year:
                    log(f"Title+year match: '{movie.get('title')}' ({movie_year})")
                    return movie

    # Pass 3: Radarr's own parse endpoint
    parse_result = api_request(
        f"{radarr_url}/api/v3/parse?title={quote(nzb_name)}",
        headers=headers,
    )
    if parse_result and parse_result.get("movie", {}).get("id"):
        parsed_movie = parse_result["movie"]
        log(f"Radarr parse match: '{parsed_movie.get('title')}'")
        return parsed_movie

    return None


# ---------------------------------------------------------------------------
# Tag matching
# ---------------------------------------------------------------------------
def media_has_any_tag(media, tag_names, all_tags):
    """Check if a media item (series or movie) has ANY of the configured tags."""
    if not media or not media.get("tags"):
        return False
    for tag_id in media["tags"]:
        tag_label = all_tags.get(tag_id, "").lower()
        if tag_label in tag_names:
            return True
    return False


# ---------------------------------------------------------------------------
# File discovery & path translation
# ---------------------------------------------------------------------------
def find_media_files(directory):
    """Walk directory for video files."""
    files = []
    for root, _dirs, filenames in os.walk(directory):
        for fname in filenames:
            if os.path.splitext(fname)[1].lower() in MEDIA_EXTENSIONS:
                files.append(os.path.join(root, fname))
    return files


def translate_paths(file_paths, path_mappings):
    """Translate file paths using the configured path mappings.

    Each mapping is a dict with 'from' and 'to' keys. The first matching
    mapping wins for each file path.
    """
    result = []
    for f in file_paths:
        translated = f
        for mapping in path_mappings:
            src = mapping.get("from", "").rstrip("/")
            dst = mapping.get("to", "").rstrip("/")
            if src and dst and f.startswith(src + "/"):
                translated = dst + f[len(src):]
                break
        result.append(translated)
    return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    # 1. Validate NZBGet environment
    if not DOWNLOAD_DIR:
        error("Not running as NZBGet post-processing script")
        sys.exit(POSTPROCESS_ERROR)

    if TOTAL_STATUS != "SUCCESS":
        log(f"Download status: {TOTAL_STATUS} — skipping")
        sys.exit(POSTPROCESS_NONE)

    # 2. Fetch config from Squeezarr API
    config = fetch_config()
    if not config:
        error("Failed to fetch config from Squeezarr — check URL and API key")
        sys.exit(POSTPROCESS_ERROR)

    # 3. Check category filter
    categories = [c.lower() for c in config.get("categories", [])]
    if categories and CATEGORY.lower() not in categories:
        log(f"Category '{CATEGORY}' not in configured categories {categories} — skipping")
        sys.exit(POSTPROCESS_NONE)

    log(f"Processing: {NZB_NAME}")
    log(f"Directory: {DOWNLOAD_DIR}")
    log(f"Category: {CATEGORY}")

    # 4. Check Sonarr/Radarr tags
    tags = [t.lower() for t in config.get("tags", ["convert"])]

    sonarr_url = config.get("sonarr_url", "")
    sonarr_key = config.get("sonarr_api_key", "")
    radarr_url = config.get("radarr_url", "")
    radarr_key = config.get("radarr_api_key", "")
    check_sonarr = config.get("check_sonarr_tags", True)
    check_radarr = config.get("check_radarr_tags", True)

    has_tag = False

    if check_sonarr and sonarr_url and sonarr_key:
        series = find_series_in_sonarr(NZB_NAME, sonarr_url, sonarr_key)
        if series:
            log(f"Found series in Sonarr: {series.get('title', '?')}")
            all_tags = get_sonarr_tags(sonarr_url, sonarr_key)
            has_tag = media_has_any_tag(series, tags, all_tags)
            if has_tag:
                log("Series has matching tag — proceeding")
            else:
                log(f"Series does not have any of {tags} — skipping")
                sys.exit(POSTPROCESS_NONE)

    if not has_tag and check_radarr and radarr_url and radarr_key:
        movie = find_movie_in_radarr(NZB_NAME, radarr_url, radarr_key)
        if movie:
            log(f"Found movie in Radarr: {movie.get('title', '?')}")
            all_tags = get_radarr_tags(radarr_url, radarr_key)
            has_tag = media_has_any_tag(movie, tags, all_tags)
            if has_tag:
                log("Movie has matching tag — proceeding")
            else:
                log(f"Movie does not have any of {tags} — skipping")
                sys.exit(POSTPROCESS_NONE)

    if not has_tag:
        if not (sonarr_url and sonarr_key) and not (radarr_url and radarr_key):
            log("No Sonarr/Radarr configured — processing all downloads")
        else:
            log("Not found in Sonarr or Radarr — skipping")
            sys.exit(POSTPROCESS_NONE)

    # 5. Find media files
    media_files = find_media_files(DOWNLOAD_DIR)
    if not media_files:
        log("No media files found")
        sys.exit(POSTPROCESS_NONE)

    log(f"Found {len(media_files)} media file(s)")
    for f in media_files:
        log(f"  {os.path.basename(f)} ({os.path.getsize(f) / (1024**3):.1f} GB)")

    # 6. Translate paths
    path_mappings = config.get("path_mappings", [])
    squeezarr_files = translate_paths(media_files, path_mappings)
    log(f"Squeezarr paths: {squeezarr_files}")

    # 7. Queue in Squeezarr
    priority = config.get("priority", 1)
    added = queue_files(squeezarr_files, priority, category=CATEGORY)
    if added == 0:
        log("No files queued (may already be optimized)")
        sys.exit(POSTPROCESS_NONE)

    # 8. Wait if configured
    if config.get("wait_for_completion", True):
        success = wait_for_jobs(squeezarr_files)
        sys.exit(POSTPROCESS_SUCCESS if success else POSTPROCESS_ERROR)
    else:
        log("Queued for conversion (not waiting)")
        sys.exit(POSTPROCESS_SUCCESS)


if __name__ == "__main__":
    main()
