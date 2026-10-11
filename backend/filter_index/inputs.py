"""What the Scanner's filters read besides their own scan_results row
(scan_filters.Filter.inputs). Triggers count every change to each input's
table in change_counters as "fi:<input>"; the filter index re-evaluates a
filter when one of its inputs' counters moves. "time" has no table: those
filters are re-evaluated every few minutes."""

# input -> [(table, the columns an UPDATE must touch (None: any), a condition
# on the changed row, "{row}" being NEW or OLD (None: always))]
TABLES: dict[str, list[tuple[str, "str | None", "str | None"]]] = {
    # The queue (queued), conversions (converted, undo), failures and VMAF
    # rejections — not the progress a running job writes every second.
    "jobs": [("jobs", "status, job_type, file_path, original_file_path, space_saved, error_key, backup_path, "
                      "completed_at", None)],
    "ignored": [("ignored_files", None, None)],
    "rules": [("encoding_rules", None, None)],  # skip / ignore rules hide files from Needs conversion
    "plex": [("plex_metadata_cache", None, None)],  # watch status, and the labels rules match
    "media_dirs": [("media_dirs", "path, label, enabled", None)],  # the Type pills read the labels
    "watch": [("watch_activity", None, None)],
    "arr": [("arr_file_status", None, None)],
    "maintainerr": [("maintainerr_media", None, None)],
    "settings": [("settings", None,
                  "{row}.key IN ('always_keep_languages', 'sub_keep_languages', 'backup_original_days')")],
}
INPUTS = frozenset(TABLES) | {"time"}
