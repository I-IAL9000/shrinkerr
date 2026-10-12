"""What the Scanner's filters read besides their own scan_results row
(scan_filters.Filter.inputs). Triggers count every change that matters to
each input's table in change_counters as "fi:<input>" (the jobs, media_dirs
and settings triggers are narrowed on purpose); the filter index re-evaluates a
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
    # The Title lists' metadata, not the posters.
    "titles": [("poster_cache", "year, rating, genres, country, network, status, folder_path", None)],
}
INPUTS = frozenset(TABLES) | {"time"}


def _trigger(name: str, definition: str) -> list[str]:
    """Dropped first, so a changed definition reaches existing databases."""
    return [f"DROP TRIGGER IF EXISTS {name}", f"CREATE TRIGGER {name} {definition}"]


# Every changed scan_results row, by id: the index re-evaluates those rows.
# AUTOINCREMENT keeps seq rising after rows are deleted (sqlite_sequence),
# so a restored backup shows up as a lower seq than the index has seen.
# INSERT OR REPLACE into scan_results would log only the new id (REPLACE's delete fires no trigger): don't use it.
CHANGES_SQL = [
    "CREATE TABLE IF NOT EXISTS filter_index_changes (seq INTEGER PRIMARY KEY AUTOINCREMENT, row_id INTEGER NOT NULL)",
    *_trigger("trg_fi_scan_insert", "AFTER INSERT ON scan_results "
              "BEGIN INSERT INTO filter_index_changes (row_id) VALUES (NEW.id); END"),
    *_trigger("trg_fi_scan_delete", "AFTER DELETE ON scan_results "
              "BEGIN INSERT INTO filter_index_changes (row_id) VALUES (OLD.id); END"),
    *_trigger("trg_fi_scan_update", "AFTER UPDATE ON scan_results "
              "BEGIN INSERT INTO filter_index_changes (row_id) SELECT OLD.id UNION SELECT NEW.id; END"),
]


def trigger_sql() -> list[str]:
    """The change log, the counters and their triggers (database.init_db).
    The triggers are recreated on every start."""
    out = list(CHANGES_SQL)
    for name, tables in TABLES.items():
        out.append(f"INSERT OR IGNORE INTO change_counters (name, n) VALUES ('fi:{name}', 0)")
        bump = f"BEGIN UPDATE change_counters SET n = n + 1 WHERE name = 'fi:{name}'; END"
        for table, columns, when in tables:
            for event, rows in (("INSERT", ("NEW",)), ("UPDATE", ("NEW", "OLD")), ("DELETE", ("OLD",))):
                of = f" OF {columns}" if event == "UPDATE" and columns else ""
                cond = f" WHEN {' OR '.join(when.format(row=r) for r in rows)}" if when else ""
                out += _trigger(f"trg_fi_{name}_{event.lower()}", f"AFTER {event}{of} ON {table}{cond} {bump}")
    return out
