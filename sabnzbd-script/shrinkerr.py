#!/usr/bin/env python3
"""
Shrinkerr Post-Processing Script for SABnzbd

Place this script in your SABnzbd scripts folder and select it as a
post-processing script for the categories you want to convert.

SABnzbd passes job info as command-line arguments:
  1: Final directory (full path)
  2: NZB name (original title)
  3: Clean job name
  4: Indexer report number
  5: Category
  6: Group
  7: Post-process status (0 = OK)
  8: URL

Configuration is managed in Shrinkerr's web UI — only the connection
details below need to be set.
"""

import json
import os
import re
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

# ---------------------------------------------------------------------------
# Configuration — edit these or set as environment variables
# ---------------------------------------------------------------------------
SHRINKERR_URL = os.environ.get("SHRINKERR_URL", "__SHRINKERR_URL__").rstrip("/")
SHRINKERR_KEY = os.environ.get("SHRINKERR_API_KEY", "__SHRINKERR_API_KEY__")

# ---------------------------------------------------------------------------
# SABnzbd exit codes
# ---------------------------------------------------------------------------
EXIT_SUCCESS = 0
EXIT_ERROR = 1

# ---------------------------------------------------------------------------
# Video file extensions
# ---------------------------------------------------------------------------
MEDIA_EXTENSIONS = {".mkv", ".mp4", ".avi", ".ts", ".m2ts", ".wmv", ".flv", ".mov"}


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
def log(msg):
    print(f"[INFO] Shrinkerr: {msg}", flush=True)


def error(msg):
    print(f"[ERROR] Shrinkerr: {msg}", flush=True)


# ---------------------------------------------------------------------------
# HTTP helpers (stdlib only — no pip dependencies)
# ---------------------------------------------------------------------------
def api_request(url, method="GET", data=None, headers=None):
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


def shrinkerr_headers():
    h = {}
    if SHRINKERR_KEY and SHRINKERR_KEY != "__SHRINKERR_API_KEY__":
        h["X-Api-Key"] = SHRINKERR_KEY
    return h


# ---------------------------------------------------------------------------
# Shrinkerr API
# ---------------------------------------------------------------------------
def fetch_config():
    """Fetch the NZBGet/SABnzbd extension config from Shrinkerr's API."""
    url = f"{SHRINKERR_URL}/api/settings/nzbget-config"
    return api_request(url, headers=shrinkerr_headers())


def queue_files(file_paths, priority, category=None):
    """POST files to Shrinkerr for conversion."""
    data = {
        "file_paths": file_paths,
        "priority": priority,
        "force_reencode": False,
        "insert_next": True,
    }
    if category:
        data["nzbget_category"] = category

    result = api_request(
        f"{SHRINKERR_URL}/api/jobs/add-by-path",
        method="POST",
        data=data,
        headers=shrinkerr_headers(),
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
    """Poll Shrinkerr until the specific files are completed."""
    log("Waiting for Shrinkerr to finish converting...")
    start = time.time()
    check_interval = 15

    our_files = set(os.path.basename(f) for f in file_paths)

    while time.time() - start < timeout:
        time.sleep(check_interval)

        jobs = api_request(f"{SHRINKERR_URL}/api/jobs/", headers=shrinkerr_headers())
        if not jobs:
            continue

        our_pending = 0
        our_running = 0
        our_completed = 0
        our_failed = 0

        for job in jobs:
            fn = os.path.basename(job.get("file_path", ""))
            if fn not in our_files:
                continue
            status = job.get("status", "")
            if status == "pending":
                our_pending += 1
            elif status == "running":
                our_running += 1
            elif status == "completed":
                our_completed += 1
            elif status == "failed":
                our_failed += 1

        total_ours = our_pending + our_running + our_completed + our_failed
        if total_ours == 0:
            continue

        if our_pending == 0 and our_running == 0:
            log(f"All jobs finished: {our_completed} completed, {our_failed} failed")
            return our_failed == 0

        elapsed = int(time.time() - start)
        log(f"  [{elapsed}s] {our_running} running, {our_pending} pending, {our_completed} done")

    error(f"Timeout after {timeout}s")
    return False


# ---------------------------------------------------------------------------
# Sonarr / Radarr tag checking (reused from NZBGet script)
# ---------------------------------------------------------------------------
def find_series_in_sonarr(nzb_name, sonarr_url, sonarr_key):
    series_list = api_request(
        f"{sonarr_url}/api/v3/series",
        headers={"X-Api-Key": sonarr_key},
    )
    if not series_list:
        return None

    clean = re.sub(r"[.\-_]", " ", nzb_name.lower())
    for s in series_list:
        title = s.get("title", "").lower()
        if title and title in clean:
            return s
    return None


def find_movie_in_radarr(nzb_name, radarr_url, radarr_key):
    movie_list = api_request(
        f"{radarr_url}/api/v3/movie",
        headers={"X-Api-Key": radarr_key},
    )
    if not movie_list:
        return None

    clean = re.sub(r"[.\-_]", " ", nzb_name.lower())
    for m in movie_list:
        title = m.get("title", "").lower()
        if title and title in clean:
            return m
    return None


def get_sonarr_tags(sonarr_url, sonarr_key):
    return api_request(f"{sonarr_url}/api/v3/tag", headers={"X-Api-Key": sonarr_key}) or []


def get_radarr_tags(radarr_url, radarr_key):
    return api_request(f"{radarr_url}/api/v3/tag", headers={"X-Api-Key": radarr_key}) or []


def media_has_any_tag(media, target_tags, all_tags):
    tag_ids = media.get("tags", [])
    tag_names = [t["label"].lower() for t in all_tags if t["id"] in tag_ids]
    return any(t in tag_names for t in target_tags)


# ---------------------------------------------------------------------------
# File discovery
# ---------------------------------------------------------------------------
def find_media_files(directory):
    files = []
    for root, _, filenames in os.walk(directory):
        for fn in filenames:
            ext = os.path.splitext(fn)[1].lower()
            if ext in MEDIA_EXTENSIONS and not fn.startswith("."):
                files.append(os.path.join(root, fn))
    return files


def translate_paths(file_paths, path_mappings):
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
    # SABnzbd passes args as: dir, nzb_name, clean_name, indexer, category, group, status, url
    if len(sys.argv) < 2:
        error("Not enough arguments — must be run as a SABnzbd post-processing script")
        sys.exit(EXIT_ERROR)

    download_dir = sys.argv[1]
    nzb_name = sys.argv[2] if len(sys.argv) > 2 else ""
    category = sys.argv[5] if len(sys.argv) > 5 else ""
    pp_status = sys.argv[7] if len(sys.argv) > 7 else "0"

    # Check status — 0 means success in SABnzbd
    if pp_status != "0":
        log(f"Download status: {pp_status} — skipping (not successful)")
        sys.exit(EXIT_SUCCESS)

    if not os.path.isdir(download_dir):
        error(f"Download directory does not exist: {download_dir}")
        sys.exit(EXIT_ERROR)

    # Fetch config from Shrinkerr
    config = fetch_config()
    if not config:
        error("Failed to fetch config from Shrinkerr — check URL and API key")
        log("Shrinkerr unreachable — skipping conversion so Sonarr/Radarr can still import")
        sys.exit(EXIT_SUCCESS)

    # Check category filter
    categories = [c.lower() for c in config.get("categories", [])]
    if categories and category.lower() not in categories:
        log(f"Category '{category}' not in configured categories {categories} — skipping")
        sys.exit(EXIT_SUCCESS)

    log(f"Processing: {nzb_name}")
    log(f"Directory: {download_dir}")
    log(f"Category: {category}")

    # Check Sonarr/Radarr tags
    tags = [t.lower() for t in config.get("tags", ["convert"])]
    sonarr_url = config.get("sonarr_url", "")
    sonarr_key = config.get("sonarr_api_key", "")
    radarr_url = config.get("radarr_url", "")
    radarr_key = config.get("radarr_api_key", "")
    check_sonarr = config.get("check_sonarr_tags", True)
    check_radarr = config.get("check_radarr_tags", True)

    # Determine whether this NZB is a TV episode or a movie
    looks_like_tv = bool(re.search(r"[Ss]\d{1,2}[Ee]\d{1,2}|[Ss]\d{1,2}\.", nzb_name))
    looks_like_movie = not looks_like_tv

    has_tag = False
    found_anywhere = False

    # Sonarr check — only for TV-like NZBs
    if check_sonarr and sonarr_url and sonarr_key and looks_like_tv:
        series = find_series_in_sonarr(nzb_name, sonarr_url, sonarr_key)
        if series:
            found_anywhere = True
            log(f"Found series in Sonarr: {series.get('title', '?')}")
            all_tags = get_sonarr_tags(sonarr_url, sonarr_key)
            has_tag = media_has_any_tag(series, tags, all_tags)
            if has_tag:
                log("Series has matching tag — proceeding")
            else:
                log(f"Series does not have any of {tags} — skipping")
                sys.exit(EXIT_SUCCESS)
    elif check_sonarr and sonarr_url and sonarr_key and looks_like_movie:
        log("Skipping Sonarr check — NZB looks like a movie (no S##E## pattern)")

    # Radarr check — only for movie-like NZBs (or if not found in Sonarr)
    if not has_tag and check_radarr and radarr_url and radarr_key and (looks_like_movie or not found_anywhere):
        movie = find_movie_in_radarr(nzb_name, radarr_url, radarr_key)
        if movie:
            found_anywhere = True
            log(f"Found movie in Radarr: {movie.get('title', '?')}")
            all_tags = get_radarr_tags(radarr_url, radarr_key)
            has_tag = media_has_any_tag(movie, tags, all_tags)
            if has_tag:
                log("Movie has matching tag — proceeding")
            else:
                log(f"Movie does not have any of {tags} — skipping")
                sys.exit(EXIT_SUCCESS)
        elif looks_like_movie:
            log("Movie not found in Radarr — skipping")
            sys.exit(EXIT_SUCCESS)

    if not has_tag:
        if not (sonarr_url and sonarr_key) and not (radarr_url and radarr_key):
            log("No Sonarr/Radarr configured — processing all downloads")
        else:
            log("Not found in Sonarr or Radarr — skipping")
            sys.exit(EXIT_SUCCESS)

    # Find media files
    media_files = find_media_files(download_dir)
    if not media_files:
        log("No media files found")
        sys.exit(EXIT_SUCCESS)

    log(f"Found {len(media_files)} media file(s)")
    for f in media_files:
        log(f"  {os.path.basename(f)} ({os.path.getsize(f) / (1024**3):.1f} GB)")

    # Translate paths
    path_mappings = config.get("path_mappings", [])
    shrinkerr_files = translate_paths(media_files, path_mappings)
    log(f"Shrinkerr paths: {shrinkerr_files}")

    # Queue in Shrinkerr
    priority = config.get("priority", 1)
    added = queue_files(shrinkerr_files, priority, category=category)
    if added == 0:
        log("No files queued (may already be optimized)")
        sys.exit(EXIT_SUCCESS)

    # Wait if configured
    if config.get("wait_for_completion", True):
        success = wait_for_jobs(shrinkerr_files)
        if not success:
            log("Conversion wait failed or timed out — files were queued, exiting SUCCESS so Sonarr/Radarr can import")
        sys.exit(EXIT_SUCCESS)
    else:
        log("Queued for conversion (not waiting)")
        sys.exit(EXIT_SUCCESS)


if __name__ == "__main__":
    main()
