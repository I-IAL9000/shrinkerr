#!/usr/bin/env python3
#
# NZBGet Post-Processing Script
#
##############################################################################
### NZBGET POST-PROCESSING SCRIPT                                          ###
#
# Squeezarr Post-Processing
#
# After NZBGet completes a download, checks Sonarr for a matching tag.
# If tagged, sends the file to Squeezarr for HEVC conversion.
#
##############################################################################
### OPTIONS                                                                 ###

# Squeezarr URL (http://host:port).
#SqueezarrUrl=http://localhost:6680

# Squeezarr API Key (leave empty if no auth).
#SqueezarrApiKey=

# Sonarr URL (http://host:port).
#SonarrUrl=http://localhost:8989

# Sonarr API Key.
#SonarrApiKey=

# Sonarr tag name to match (case-insensitive).
#TagName=convert

# NZBGet categories to process (comma-separated, empty=all).
#Categories=TV

# Queue priority (Normal, High, Highest).
#Priority=High

# Wait for conversion to complete before exiting (Yes, No).
#
# Yes: Blocks Sonarr import until Squeezarr finishes.
# No: Queues and exits immediately.
#WaitForCompletion=Yes

# Path mapping from NZBGet to Squeezarr (NZBGet path=Squeezarr path).
#
# Maps the download path inside NZBGet to the path Squeezarr sees.
# Example: /Downloads/completed/TV=/downloads/tv
#PathMapping=/Downloads/completed/TV=/downloads/tv

### NZBGET POST-PROCESSING SCRIPT                                          ###
##############################################################################

import json
import os
import sys
import time
import glob
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError
from urllib.parse import quote

# NZBGet exit codes
POSTPROCESS_SUCCESS = 93
POSTPROCESS_ERROR = 94
POSTPROCESS_NONE = 95

# Read NZBGet environment
DOWNLOAD_DIR = os.environ.get("NZBPP_DIRECTORY", "")
NZB_NAME = os.environ.get("NZBPP_NZBNAME", "")
CATEGORY = os.environ.get("NZBPP_CATEGORY", "")
TOTAL_STATUS = os.environ.get("NZBPP_TOTALSTATUS", "")
NZB_ID = os.environ.get("NZBPP_NZBID", "")

# Read extension options (NZBPO_ prefix)
SQUEEZARR_URL = os.environ.get("NZBPO_SQUEEZARRURL", "http://localhost:6680").rstrip("/")
SQUEEZARR_KEY = os.environ.get("NZBPO_SQUEEZARRAPIKEY", "")
SONARR_URL = os.environ.get("NZBPO_SONARRURL", "http://localhost:8989").rstrip("/")
SONARR_KEY = os.environ.get("NZBPO_SONARRAPIKEY", "")
TAG_NAME = os.environ.get("NZBPO_TAGNAME", "convert").lower()
CATEGORIES = [c.strip().lower() for c in os.environ.get("NZBPO_CATEGORIES", "TV").split(",") if c.strip()]
PRIORITY_MAP = {"Normal": 0, "High": 1, "Highest": 2}
PRIORITY = PRIORITY_MAP.get(os.environ.get("NZBPO_PRIORITY", "High"), 1)
WAIT = os.environ.get("NZBPO_WAITFORCOMPLETION", "Yes") == "Yes"
PATH_MAPPING = os.environ.get("NZBPO_PATHMAPPING", "/Downloads/completed/TV=/downloads/tv")

# Check if this is a command test
COMMAND = os.environ.get("NZBCP_CONNECTIONTEST", "")


def log(msg):
    print(f"[INFO] {msg}", flush=True)


def error(msg):
    print(f"[ERROR] {msg}", flush=True)


def api_request(url, method="GET", data=None, headers=None):
    """Make an HTTP request and return parsed JSON."""
    if headers is None:
        headers = {}
    headers["Content-Type"] = "application/json"

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
    h = {}
    if SQUEEZARR_KEY:
        h["X-Api-Key"] = SQUEEZARR_KEY
    return h


def sonarr_headers():
    return {"X-Api-Key": SONARR_KEY} if SONARR_KEY else {}


def test_connections():
    """Test both Squeezarr and Sonarr connections."""
    ok = True

    # Test Squeezarr
    log(f"Testing Squeezarr at {SQUEEZARR_URL}...")
    result = api_request(f"{SQUEEZARR_URL}/api/health", headers=squeezarr_headers())
    if result:
        log("Squeezarr: Connected OK")
    else:
        error("Squeezarr: Connection failed")
        ok = False

    # Test Sonarr
    log(f"Testing Sonarr at {SONARR_URL}...")
    result = api_request(f"{SONARR_URL}/api/v3/system/status", headers=sonarr_headers())
    if result:
        log(f"Sonarr: Connected OK (v{result.get('version', '?')})")
    else:
        error("Sonarr: Connection failed")
        ok = False

    return ok


def get_sonarr_tags():
    """Get all tags from Sonarr, return dict of {id: name}."""
    data = api_request(f"{SONARR_URL}/api/v3/tag", headers=sonarr_headers())
    if not data:
        return {}
    return {t["id"]: t["label"].lower() for t in data}


def get_series_by_name(name):
    """Search Sonarr for a series matching the download name."""
    # Try to extract series name from NZB name (before season/episode info)
    import re
    # Common patterns: "Show Name S01E01" or "Show.Name.S01E01"
    match = re.match(r"^(.+?)[\s._-]+[Ss]\d+", name)
    search_name = match.group(1).replace(".", " ").replace("_", " ").strip() if match else name

    data = api_request(
        f"{SONARR_URL}/api/v3/series/lookup?term={quote(search_name)}",
        headers=sonarr_headers(),
    )
    if not data:
        return None

    # Also check the local series list for exact match
    all_series = api_request(f"{SONARR_URL}/api/v3/series", headers=sonarr_headers())
    if all_series:
        for series in all_series:
            title_clean = series.get("title", "").lower().replace(".", " ").replace("_", " ")
            search_clean = search_name.lower()
            if title_clean == search_clean or search_clean in title_clean:
                return series

    # Fallback: use lookup results
    for item in data:
        if item.get("id"):
            return item

    return None


def series_has_tag(series, tag_name, all_tags):
    """Check if a series has a specific tag."""
    if not series or not series.get("tags"):
        return False
    for tag_id in series["tags"]:
        if all_tags.get(tag_id, "").lower() == tag_name:
            return True
    return False


def find_media_files(directory):
    """Find video files in the download directory."""
    extensions = {".mkv", ".mp4", ".avi", ".ts", ".m2ts", ".wmv", ".flv", ".mov"}
    files = []
    for root, dirs, filenames in os.walk(directory):
        for fname in filenames:
            if os.path.splitext(fname)[1].lower() in extensions:
                files.append(os.path.join(root, fname))
    return files


def queue_in_squeezarr(file_paths):
    """Send files to Squeezarr for conversion. Returns job count."""
    data = {
        "file_paths": file_paths,
        "priority": PRIORITY,
        "force_reencode": False,
    }

    result = api_request(
        f"{SQUEEZARR_URL}/api/jobs/add-by-path",
        method="POST",
        data=data,
        headers=squeezarr_headers(),
    )

    if result:
        added = result.get("added", 0)
        log(f"Squeezarr: Queued {added} job(s)")
        return added
    else:
        error("Squeezarr: Failed to queue jobs")
        return 0


def wait_for_jobs(file_paths, timeout=7200):
    """Poll Squeezarr until the specific files we queued are completed."""
    log("Waiting for Squeezarr to finish converting our files...")
    start = time.time()
    check_interval = 15  # seconds

    # Normalize paths for matching
    our_files = set(os.path.basename(f) for f in file_paths)

    while time.time() - start < timeout:
        time.sleep(check_interval)

        # Get all jobs and check if our files are still pending/running
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
            log(f"Squeezarr: Our {our_done} file(s) completed ({elapsed}s)")
            return True

        if our_running == 0 and our_pending == 0 and our_done == 0:
            # Jobs might not have appeared yet, give it a moment
            if elapsed > 60:
                log("Squeezarr: Jobs not found — may have completed already")
                return True

        log(f"Squeezarr: {our_running} converting, {our_pending} pending, {our_done} done ({elapsed}s)")

    error(f"Squeezarr: Timed out after {timeout}s")
    return False


def main():
    # Handle connection test command
    if COMMAND:
        if test_connections():
            log("All connections OK")
            sys.exit(POSTPROCESS_SUCCESS)
        else:
            sys.exit(POSTPROCESS_ERROR)

    # Validate environment
    if not DOWNLOAD_DIR:
        error("Missing NZBPP_DIRECTORY — not running as NZBGet post-processing script?")
        sys.exit(POSTPROCESS_ERROR)

    # Only process successful downloads
    if TOTAL_STATUS != "SUCCESS":
        log(f"Download status: {TOTAL_STATUS} — skipping")
        sys.exit(POSTPROCESS_NONE)

    # Check category filter
    if CATEGORIES and CATEGORY.lower() not in CATEGORIES:
        log(f"Category '{CATEGORY}' not in filter [{', '.join(CATEGORIES)}] — skipping")
        sys.exit(POSTPROCESS_NONE)

    log(f"Processing: {NZB_NAME}")
    log(f"Directory: {DOWNLOAD_DIR}")
    log(f"Category: {CATEGORY}")

    # Check Sonarr for tag
    if SONARR_KEY:
        log(f"Checking Sonarr for '{TAG_NAME}' tag...")
        all_tags = get_sonarr_tags()
        if not all_tags:
            error("Could not fetch Sonarr tags")
            sys.exit(POSTPROCESS_NONE)

        series = get_series_by_name(NZB_NAME)
        if not series:
            log(f"Series not found in Sonarr for '{NZB_NAME}' — skipping")
            sys.exit(POSTPROCESS_NONE)

        log(f"Found series: {series.get('title', '?')}")

        if not series_has_tag(series, TAG_NAME, all_tags):
            log(f"Series does not have '{TAG_NAME}' tag — skipping")
            sys.exit(POSTPROCESS_NONE)

        log(f"Series has '{TAG_NAME}' tag — proceeding with conversion")
    else:
        log("No Sonarr API key — processing all downloads in matching categories")

    # Find media files
    media_files = find_media_files(DOWNLOAD_DIR)
    if not media_files:
        log("No media files found in download directory")
        sys.exit(POSTPROCESS_NONE)

    log(f"Found {len(media_files)} media file(s):")
    for f in media_files:
        log(f"  {os.path.basename(f)} ({os.path.getsize(f) / (1024**3):.1f} GB)")

    # Translate paths from NZBGet container paths to Squeezarr container paths
    squeezarr_files = []
    src_path, dst_path = "", ""
    if "=" in PATH_MAPPING:
        src_path, dst_path = PATH_MAPPING.split("=", 1)

    for f in media_files:
        if src_path and dst_path:
            squeezarr_path = f.replace(src_path, dst_path, 1)
        else:
            squeezarr_path = f
        squeezarr_files.append(squeezarr_path)

    log(f"Squeezarr paths: {squeezarr_files}")

    # Queue in Squeezarr
    added = queue_in_squeezarr(squeezarr_files)
    if added == 0:
        log("No files queued (may already be optimized)")
        sys.exit(POSTPROCESS_NONE)

    # Wait for completion if configured
    if WAIT:
        success = wait_for_jobs(squeezarr_files)
        if success:
            log("Conversion complete — Sonarr can now import")
            sys.exit(POSTPROCESS_SUCCESS)
        else:
            error("Conversion timed out or failed")
            sys.exit(POSTPROCESS_ERROR)
    else:
        log("Queued for conversion (not waiting)")
        sys.exit(POSTPROCESS_SUCCESS)


if __name__ == "__main__":
    main()
