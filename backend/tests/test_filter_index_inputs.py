"""Filter.inputs: what a filter reads besides its own scan_results row. The
filter index re-evaluates a filter when one of its inputs changes, so a
filter that reads another table, a setting or the time and doesn't say so
(or says only some of it) would go stale."""
import re

from backend.filter_index.inputs import INPUTS, TABLES
from backend.scan_filters import _FILTER_LIST

# What each Python predicate reads: they can't be inferred from SQL.
PY_INPUTS = {
    **{i: {"ignored", "rules", "plex"} for i in ("needs_conversion", "low_bitrate", "high_bitrate", "ignored")},
    **{i: {"jobs"} for i in ("converted", "queued")},
    **{f"plex_{s}": {"plex"} for s in ("watched", "unwatched", "watchlist")},
    **{f"not_watched_{n}m": {"watch", "time"} for n in (6, 12, 24)},
    **{f"type_{k}": {"media_dirs"} for k in ("movie", "tv", "other")},
    **{f"hdr_{k}": set() for k in ("dv", "hdr10", "hlg")},  # only the row's own columns
}


def _input_of_table():
    out = {}
    for name, triggers in TABLES.items():
        for table, _cols, _cond in triggers:
            assert table not in out, f"{table} is under both {out[table]} and {name}"
            out[table] = name
    return out


def _tables_read(sql):
    found = re.findall(r"\b(?:FROM|JOIN)\s+([a-z_]+)", sql, re.I)
    return {t.lower() for t in found} - {"json_each", "scan_results"}


def test_every_sql_filter_says_exactly_what_it_reads():
    input_of = _input_of_table()
    for f in _FILTER_LIST:
        if f.py:
            continue
        expected = set()
        for table in _tables_read(f.sql or ""):
            assert table in input_of, f"{f.id} reads {table}, which no input covers"
            expected.add(input_of[table])
        if re.search(r"\bnow\b", f.sql or "", re.I) or f.params:
            expected.add("time")
        assert set(f.inputs) == expected, f.id
        assert set(f.inputs) <= INPUTS, f.id


def test_every_python_filter_says_what_it_reads():
    py_ids = {f.id for f in _FILTER_LIST if f.py}
    assert py_ids == set(PY_INPUTS)
    for f in _FILTER_LIST:
        if f.py:
            assert set(f.inputs) == PY_INPUTS[f.id], f.id


def test_the_triggers_cover_what_the_sql_touches():
    """A trigger that ignores the settings keys or job columns a filter reads
    would never wake it."""
    setting_conditions = " ".join(c or "" for _t, _cols, c in TABLES["settings"])
    job_columns = {c.strip() for _t, cols, _c in TABLES["jobs"] for c in (cols or "").split(",")}
    for f in _FILTER_LIST:
        sql = f.sql or ""
        if "settings" in f.inputs:
            keys = re.findall(r"\bkey\s*=\s*'([^']*)'", sql)
            for group in re.findall(r"\bkey\s+IN\s*\(([^)]*)\)", sql):
                keys += re.findall(r"'([^']*)'", group)
            assert keys, f.id
            for key in keys:
                assert f"'{key}'" in setting_conditions, f"{f.id} reads setting {key}"
        for col in re.findall(r"\bj\.(\w+)", sql):
            assert col in job_columns, f"{f.id} reads jobs.{col}"


def test_the_inputs():
    assert {f.id for f in _FILTER_LIST if "jobs" in f.inputs} == {
        "converted", "queued", "failed_before", "vmaf_rejected", "undo_possible"}
    assert {f.id for f in _FILTER_LIST if "time" in f.inputs} == {
        "new", "recent", "added_7d", "added_30d", "added_90d", "health_stale", "undo_possible",
        "not_watched_6m", "not_watched_12m", "not_watched_24m"}
