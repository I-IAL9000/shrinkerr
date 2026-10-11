"""Filter.inputs: what a filter reads besides its own scan_results row. The
filter index re-evaluates a filter when one of its inputs changes, so a
filter that reads another table, a setting or the time and doesn't say so
would go stale."""
import re

from backend.filter_index.inputs import INPUTS, TABLES
from backend.scan_filters import _FILTER_LIST

# Python predicates that read only the row's own columns.
ROW_ONLY_PY = {"hdr_dv", "hdr_hdr10", "hdr_hlg"}


def test_every_filter_that_reads_beyond_its_row_says_what():
    for f in _FILTER_LIST:
        other_table = re.search(r"\bFROM\s+(?!json_each\b)[a-z_]+", f.sql or "")
        reads_more = bool(other_table or "'now'" in (f.sql or "") or f.params
                          or (f.py and f.id not in ROW_ONLY_PY))
        assert bool(f.inputs) == reads_more, f.id
        assert set(f.inputs) <= INPUTS, f.id


def test_the_inputs():
    assert INPUTS == set(TABLES) | {"time"}
    assert {f.id for f in _FILTER_LIST if "jobs" in f.inputs} == {
        "converted", "queued", "failed_before", "vmaf_rejected", "undo_possible"}
    assert {f.id for f in _FILTER_LIST if "time" in f.inputs} == {
        "new", "recent", "added_7d", "added_30d", "added_90d", "health_stale", "undo_possible",
        "not_watched_6m", "not_watched_12m", "not_watched_24m"}
