# Filter Index (Phase 1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Scanner's filter counts instant (today ~12 s per click on a 166K-file library) with a persisted, incrementally updated filter index, behind today's pills — same API, same numbers.

**Architecture:** Each Scanner filter (`backend/scan_filters.py` `_FILTER_LIST`) gets a bitset of the listed `scan_results` row ids it matches (a Python `int`; bit n = row id n). Counts become ANDs and `int.bit_count()`. SQLite triggers log changed `scan_results` ids and count changes to the other tables filters read; the index re-evaluates only changed rows and only the filters whose inputs changed, plus the time-based filters every 5 minutes. It's saved to the data folder and loaded at start-up; the first build runs in the background while the old SQL engine answers.

**Tech Stack:** Python 3.12 (`int.bit_count`), aiosqlite / SQLite triggers, FastAPI, pytest + pytest-asyncio.

**Spec:** `.superpowers/specs/2026-10-11-scanner-filter-overhaul-design.md` (sections "Filter index", "Testing", "Rollout" step 1). Phases 2–5 (new bar, list facets, title credits, tree / lists through the index) get their own plans.

**Measured on the prototype** (`scratchpad/fi/proto.py`, synthetic 165,741-file library, M1 Pro): full build 13.0 s + 0.2 s bitsets; counts after a click 0.1–0.3 ms; re-evaluating 1,000 changed rows 83 ms + 0.5 ms patch; re-evaluating the filters of one changed input over the whole library 0.7–1.0 s; summing savings 10 ms. Every count equal to `count_all` / `facet_counts` on the test library below, Advanced Search conditions included.

## Conventions for every task

- Work in the `develop` worktree. Run Python with `../venv/bin/python` from the worktree root.
- One test file: `../venv/bin/python -m pytest backend/tests/<file> -q -p no:warnings`
- Full suite (never pipe pytest into `tail`, it hides the exit code): `../venv/bin/python -m pytest backend/tests -q -p no:warnings > ../suite.txt 2>&1; echo "EXIT=$?"; grep -E "passed|failed" ../suite.txt | tail -1`
- Don't import `backend.main` at a test module's import time.
- Comments and docstrings: plain, short, say why; match the surrounding code.
- Commit messages end with: `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`

## File structure

| File | Responsibility |
|---|---|
| `backend/scan_filters.py` (modify) | `Filter.inputs`: what a filter reads besides its row; `FILTERS_VERSION` |
| `backend/filter_index/__init__.py` (create) | package docstring only (no imports: `database.py` imports `inputs`) |
| `backend/filter_index/inputs.py` (create) | the input names, their tables, the trigger SQL |
| `backend/filter_index/bits.py` (create) | bitset helpers: `from_ids`, `ids_of` |
| `backend/filter_index/evaluate.py` (create) | run filter definitions over all listed rows or some ids |
| `backend/filter_index/index.py` (create) | `FilterIndex`: bitsets, patch / replace, counts, save / load |
| `backend/filter_index/service.py` (create) | build, refresh, counts / stats API, save, start-up loop |
| `backend/database.py` (modify) | create the change log, counters and triggers in `init_db` |
| `backend/routes/scan.py` (modify) | `/filter-counts` and `/scan-stats` answer from the index |
| `backend/main.py` (modify) | start the loop; save on shutdown |
| `backend/tests/filter_index_library.py` (create) | test library where every filter matches, shared helpers |
| `backend/tests/test_filter_index_*.py` (create) | tests per unit |
| `scripts/bench_filter_index.py` (create) | the by-hand benchmark against the spec's targets |

---

### Task 1: Say what each filter reads besides its row

**Files:**
- Create: `backend/filter_index/__init__.py`
- Create: `backend/filter_index/inputs.py`
- Modify: `backend/scan_filters.py` (the `Filter` dataclass ~line 35; `_FILTER_LIST` ~lines 404–538)
- Test: `backend/tests/test_filter_index_inputs.py`

- [ ] **Step 1: Write the failing test**

`backend/tests/test_filter_index_inputs.py`:

```python
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
```

- [ ] **Step 2: Run it to verify it fails**

Run: `../venv/bin/python -m pytest backend/tests/test_filter_index_inputs.py -q -p no:warnings`
Expected: FAIL — `ModuleNotFoundError: No module named 'backend.filter_index'`

- [ ] **Step 3: Create the package and the inputs**

`backend/filter_index/__init__.py`:

```python
"""The Scanner's filter index (spec: .superpowers/specs/2026-10-11-scanner-filter-overhaul-design.md).

No imports here: backend/database.py imports filter_index.inputs while it
creates the tables, before anything else is loaded."""
```

`backend/filter_index/inputs.py`:

```python
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
```

- [ ] **Step 4: Add `Filter.inputs` and `FILTERS_VERSION`**

In `backend/scan_filters.py`, add the field as the dataclass's last line:

```python
    titles: bool = False  # py filters that read title metadata (ctx["title_meta"], v0.10.0)
    # What it reads besides its own row: other tables, settings, the time
    # (backend/filter_index/inputs.py). The filter index re-evaluates it when
    # one of them changes (v0.10.0).
    inputs: tuple[str, ...] = ()
```

Directly above `_FILTER_LIST = [`, add:

```python
# Bump when a Python predicate's logic changes: a saved filter index made
# with the old one is rebuilt (SQL changes are noticed on their own).
FILTERS_VERSION = 1
```

- [ ] **Step 5: Annotate the filters**

In `_FILTER_LIST`, give these entries `inputs` (everything else stays as it is):

```python
    Filter("new", sql="new_detected_at > ?", params=_new_cutoff, inputs=("time",)),
    Filter("recent", sql="file_mtime > ?", params=_recent_cutoff, inputs=("time",)),
    Filter("needs_conversion", pre_sql=_NEEDS, inputs=("ignored", "rules", "plex"),
           py=lambda r, c: bool(r.get("needs_conversion")) and not row_low_bitrate(r) and not row_ignored(r, c)),
    Filter("low_bitrate", pre_sql=_NEEDS_TIMED, inputs=("ignored", "rules", "plex"),
           py=lambda r, c: row_low_bitrate(r) and not row_ignored(r, c)),
    Filter("high_bitrate", pre_sql=_NEEDS_TIMED, inputs=("ignored", "rules", "plex"),
           py=lambda r, c: bool(r.get("needs_conversion")) and not row_ignored(r, c) and row_bitrate(r) > HIGH_BITRATE),
```

```python
    Filter("ignored", py=lambda r, c: row_ignored(r, c), inputs=("ignored", "rules", "plex")),
```

```python
    Filter("converted", py=row_converted, inputs=("jobs",)),
    Filter("queued", py=lambda r, c: r["file_path"] in c["queued_paths"], inputs=("jobs",)),
```

```python
    *(Filter(f"added_{n}d", "added", sql="(new_detected_at > ? OR file_mtime > ?)", params=_added_within(n),
             inputs=("time",))
      for n in (7, 30, 90)),
```

```python
    Filter("missing_language", "language", inputs=("settings",), sql=(
        f"EXISTS {_MY_LANGS} "
        f"AND NOT {_tracks('audio_tracks_json', f'{_TRACK_LANG} IN {_MY_LANGS}')} "
        f"AND NOT {_tracks('subtitle_tracks_json', f'{_TRACK_LANG} IN {_MY_LANGS}')}")),
```

```python
    Filter("health_stale", "health", sql="julianday(health_checked_at) < julianday('now') - 90", inputs=("time",)),
    # What happened when Shrinkerr tried
    Filter("failed_before", "outcome", sql=_job_exists("j.status = 'failed'"), inputs=("jobs",)),
    Filter("vmaf_rejected", "outcome", inputs=("jobs",),
           sql=_job_exists("j.error_key IN ('errors.vmafRejected', 'errors.vmafBelowThreshold')")),
    Filter("undo_possible", "outcome", sql=_UNDO_POSSIBLE, inputs=("jobs", "settings", "time")),
    Filter("no_savings", "outcome", inputs=("ignored",), sql=(
        "EXISTS (SELECT 1 FROM ignored_files i WHERE i.file_path = scan_results.file_path "
        "AND i.reason = 'conversion_larger')")),
    # What Sonarr / Radarr say (v0.10.0): below the cutoff they'll replace it
    Filter("arr_cutoff_unmet", "arr", inputs=("arr",), sql=(
        "EXISTS (SELECT 1 FROM arr_file_status a WHERE a.file_path = scan_results.file_path AND a.cutoff_unmet = 1)")),
    Filter("arr_unmonitored", "arr", inputs=("arr",), sql=(
        "EXISTS (SELECT 1 FROM arr_file_status a WHERE a.file_path = scan_results.file_path AND a.monitored = 0)")),
    # In a Maintainerr collection: about to be removed (v0.10.0)
    Filter("leaving_soon", "maintainerr", inputs=("maintainerr",), sql=(
        "EXISTS (SELECT 1 FROM maintainerr_media m "
        "WHERE substr(scan_results.file_path, 1, length(m.folder_path)) = m.folder_path)")),
    # Plex
    *(Filter(f"plex_{s}", "plex", py=(lambda s: lambda r, c: row_watch_status(r, c) == s)(s), inputs=("plex",))
      for s in ("watched", "unwatched", "watchlist")),
    # Nothing played on Plex / Jellyfin / Emby in 6 / 12 / 24 months, and in
    # the library that long (v0.10.0)
    *(Filter(f"not_watched_{n}m", "not_watched", py=_not_watched(n), inputs=("watch", "time")) for n in (6, 12, 24)),
    # Type (path IDs, else the media folder's label)
    *(Filter(f"type_{k}", "type", py=(lambda k: lambda r, c: row_type(r, c) == k)(k), inputs=("media_dirs",))
      for k in ("movie", "tv", "other")),
```

- [ ] **Step 6: Run the test to verify it passes**

Run: `../venv/bin/python -m pytest backend/tests/test_filter_index_inputs.py -q -p no:warnings`
Expected: `2 passed`

- [ ] **Step 7: Check that nothing else changed**

Run: `../venv/bin/python -m pytest backend/tests -q -p no:warnings -k "filter or scan" > ../suite.txt 2>&1; echo "EXIT=$?"; grep -E "passed|failed" ../suite.txt | tail -1`
Expected: `EXIT=0`

- [ ] **Step 8: Commit**

```bash
git add backend/filter_index/__init__.py backend/filter_index/inputs.py backend/scan_filters.py backend/tests/test_filter_index_inputs.py
git commit -m "feat(filters): each filter says what it reads besides its row

For the filter index (phase 1 of the filter overhaul): a filter is
re-evaluated when one of its inputs changes.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Bitset helpers

**Files:**
- Create: `backend/filter_index/bits.py`
- Test: `backend/tests/test_filter_index_bits.py`

- [ ] **Step 1: Write the failing test**

`backend/tests/test_filter_index_bits.py`:

```python
"""Sets of scan_results row ids as Python ints (bit n = row id n)."""
from backend.filter_index.bits import from_ids, ids_of


def test_round_trip():
    ids = [0, 1, 7, 8, 9, 63, 64, 1000, 165_741]
    bits = from_ids(ids)
    assert bits.bit_count() == len(ids)
    assert ids_of(bits) == ids
    assert ids_of(from_ids(reversed(ids))) == ids  # any order in, ascending out


def test_empty_and_duplicates():
    assert from_ids([]) == 0
    assert ids_of(0) == []
    assert ids_of(from_ids([5, 5, 5])) == [5]


def test_set_algebra_is_int_algebra():
    a, b = from_ids([1, 2, 3, 500]), from_ids([2, 3, 4])
    assert ids_of(a & b) == [2, 3]
    assert ids_of(a | b) == [1, 2, 3, 4, 500]
    assert ids_of(a & ~b) == [1, 500]  # what an exclusion does
```

- [ ] **Step 2: Run it to verify it fails**

Run: `../venv/bin/python -m pytest backend/tests/test_filter_index_bits.py -q -p no:warnings`
Expected: FAIL — `ModuleNotFoundError: No module named 'backend.filter_index.bits'`

- [ ] **Step 3: Implement**

`backend/filter_index/bits.py`:

```python
"""Sets of scan_results row ids as Python ints: bit n is row id n. AND, OR
and int.bit_count() run at C speed over a whole library's worth of bits."""
from typing import Iterable

_BITS_OF_BYTE = tuple(tuple(j for j in range(8) if byte >> j & 1) for byte in range(256))


def from_ids(ids: Iterable[int]) -> int:
    ids = list(ids)
    if not ids:
        return 0
    buf = bytearray(max(ids) // 8 + 1)
    for i in ids:
        buf[i >> 3] |= 1 << (i & 7)
    return int.from_bytes(buf, "little")


def ids_of(bits: int) -> list[int]:
    """The ids in `bits`, ascending."""
    out: list[int] = []
    for n, byte in enumerate(bits.to_bytes((bits.bit_length() + 7) // 8, "little")):
        if byte:
            out.extend((n << 3) + j for j in _BITS_OF_BYTE[byte])
    return out
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `../venv/bin/python -m pytest backend/tests/test_filter_index_bits.py -q -p no:warnings`
Expected: `3 passed`

- [ ] **Step 5: Commit**

```bash
git add backend/filter_index/bits.py backend/tests/test_filter_index_bits.py
git commit -m "feat(filters): bitset helpers for the filter index

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Log changed rows and count input changes in the database

**Files:**
- Modify: `backend/filter_index/inputs.py` (add `CHANGES_SQL`, `trigger_sql()`)
- Modify: `backend/database.py` (end of `init_db`, just before its last `await db.commit()`, ~line 1210)
- Test: `backend/tests/test_filter_index_triggers.py`

- [ ] **Step 1: Write the failing test**

`backend/tests/test_filter_index_triggers.py`:

```python
"""The filter index's change signals: triggers log every changed scan_results
row id (filter_index_changes) and count changes to the other tables filters
read (change_counters "fi:<input>")."""
import aiosqlite
import pytest

from backend.filter_index.inputs import TABLES


async def _counters(db_path):
    async with aiosqlite.connect(db_path) as db:
        async with db.execute("SELECT name, n FROM change_counters WHERE name LIKE 'fi:%'") as cur:
            return dict(await cur.fetchall())


@pytest.mark.asyncio
async def test_scan_results_changes_are_logged(test_db):
    async with aiosqlite.connect(test_db) as db:
        for path in ("/m/a.mkv", "/m/b.mkv"):
            await db.execute("INSERT INTO scan_results (file_path, file_size, scan_timestamp) VALUES (?, 1, 'x')",
                             (path,))
        await db.execute("UPDATE scan_results SET file_size = 2 WHERE file_path = '/m/a.mkv'")
        await db.execute("DELETE FROM scan_results WHERE file_path = '/m/b.mkv'")
        await db.commit()
        async with db.execute("SELECT row_id FROM filter_index_changes ORDER BY seq") as cur:
            logged = [r[0] for r in await cur.fetchall()]
        async with db.execute("SELECT id FROM scan_results WHERE file_path = '/m/a.mkv'") as cur:
            a = (await cur.fetchone())[0]
    assert logged == [a, a + 1, a, a + 1]  # inserted, inserted, updated, deleted


@pytest.mark.asyncio
async def test_the_tables_filters_read_count_their_changes(test_db):
    before = await _counters(test_db)
    assert set(before) == {f"fi:{name}" for name in TABLES}
    async with aiosqlite.connect(test_db) as db:
        await db.execute("INSERT INTO jobs (file_path, job_type, status, created_at, audio_tracks_to_remove) "
                         "VALUES ('/m/a.mkv', 'convert', 'pending', 'x', '[]')")
        # A running encode's progress, every second: not a change filters see.
        await db.execute("UPDATE jobs SET progress = 50, fps = 30, eta_seconds = 60 WHERE file_path = '/m/a.mkv'")
        await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('queue_run_state', 'paused')")  # no
        await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('always_keep_languages', '[\"ice\"]')")
        await db.execute("INSERT INTO ignored_files (file_path, reason, ignored_at) VALUES ('/m/a.mkv', 'manual', 'x')")
        await db.execute("INSERT INTO plex_metadata_cache (folder_path, metadata_type, metadata_value, synced_at) "
                         "VALUES ('/m/', 'watch_status', 'watched', 'x')")
        await db.execute("INSERT INTO watch_activity (folder_path, server, last_viewed, added_at) "
                         "VALUES ('/m/', 'plex', 1, 1)")
        await db.execute("INSERT INTO arr_file_status (file_path, service, monitored, cutoff_unmet, synced_at) "
                         "VALUES ('/m/a.mkv', 'radarr', 1, 0, 'x')")
        await db.execute("INSERT INTO maintainerr_media (folder_path, collection, synced_at) VALUES ('/m/', 'c', 'x')")
        await db.execute("INSERT INTO media_dirs (path, label) VALUES ('/m', 'Movies')")
        await db.execute("INSERT INTO encoding_rules (name, action, created_at) VALUES ('r', 'skip', 'x')")
        await db.commit()
    after = await _counters(test_db)
    assert {k for k in after if after[k] != before[k]} == {f"fi:{name}" for name in TABLES}
    assert after["fi:jobs"] == before["fi:jobs"] + 1          # the insert, not the progress
    assert after["fi:settings"] == before["fi:settings"] + 1  # the keep languages, not the queue state
```

- [ ] **Step 2: Run it to verify it fails**

Run: `../venv/bin/python -m pytest backend/tests/test_filter_index_triggers.py -q -p no:warnings`
Expected: FAIL — `no such table: filter_index_changes`

- [ ] **Step 3: Add the trigger SQL**

Append to `backend/filter_index/inputs.py`:

```python
# Every changed scan_results row, by id: the index re-evaluates those rows.
# AUTOINCREMENT keeps seq rising after rows are deleted (sqlite_sequence),
# so a restored backup shows up as a lower seq than the index has seen.
CHANGES_SQL = [
    "CREATE TABLE IF NOT EXISTS filter_index_changes (seq INTEGER PRIMARY KEY AUTOINCREMENT, row_id INTEGER NOT NULL)",
    "CREATE TRIGGER IF NOT EXISTS trg_fi_scan_insert AFTER INSERT ON scan_results "
    "BEGIN INSERT INTO filter_index_changes (row_id) VALUES (NEW.id); END",
    "CREATE TRIGGER IF NOT EXISTS trg_fi_scan_delete AFTER DELETE ON scan_results "
    "BEGIN INSERT INTO filter_index_changes (row_id) VALUES (OLD.id); END",
    "CREATE TRIGGER IF NOT EXISTS trg_fi_scan_update AFTER UPDATE ON scan_results "
    "BEGIN INSERT INTO filter_index_changes (row_id) SELECT OLD.id UNION SELECT NEW.id; END",
]


def trigger_sql() -> list[str]:
    """The change log, the counters and their triggers (database.init_db).
    Rename a trigger when changing it: they're created IF NOT EXISTS."""
    out = list(CHANGES_SQL)
    for name, tables in TABLES.items():
        out.append(f"INSERT OR IGNORE INTO change_counters (name, n) VALUES ('fi:{name}', 0)")
        bump = f"BEGIN UPDATE change_counters SET n = n + 1 WHERE name = 'fi:{name}'; END"
        for table, columns, when in tables:
            for event, rows in (("INSERT", ("NEW",)), ("UPDATE", ("NEW", "OLD")), ("DELETE", ("OLD",))):
                of = f" OF {columns}" if event == "UPDATE" and columns else ""
                cond = f" WHEN {' OR '.join(when.format(row=r) for r in rows)}" if when else ""
                out.append(f"CREATE TRIGGER IF NOT EXISTS trg_fi_{name}_{event.lower()} "
                           f"AFTER {event}{of} ON {table}{cond} {bump}")
    return out
```

- [ ] **Step 4: Create them in `init_db`**

In `backend/database.py`, `init_db`, find the end of the function:

```python
        except Exception as exc:
            print(f"[DB] v0.9.85 backfill skipped: {exc}", flush=True)

        await db.commit()
    finally:
        await db.close()
```

and insert before `await db.commit()`:

```python
        # The Scanner's filter index (backend/filter_index): a log of changed
        # scan_results rows, and counters for the other tables filters read.
        # Last, after every table it watches exists.
        from backend.filter_index.inputs import trigger_sql
        for statement in trigger_sql():
            await db.execute(statement)

```

- [ ] **Step 5: Run the test to verify it passes**

Run: `../venv/bin/python -m pytest backend/tests/test_filter_index_triggers.py -q -p no:warnings`
Expected: `2 passed`

- [ ] **Step 6: Run the full suite (the triggers fire on every write in every test)**

Run the full suite command from "Conventions".
Expected: `EXIT=0`, no failures.

- [ ] **Step 7: Commit**

```bash
git add backend/filter_index/inputs.py backend/database.py backend/tests/test_filter_index_triggers.py
git commit -m "feat(filters): log changed scan_results rows and count input changes

Triggers for the filter index: changed row ids in filter_index_changes,
and change_counters 'fi:<input>' for the tables filters read (not a
running job's progress, not unrelated settings).

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: The test library, and evaluating filters over rows

**Files:**
- Create: `backend/tests/filter_index_library.py`
- Create: `backend/filter_index/evaluate.py`
- Test: `backend/tests/test_filter_index_evaluate.py`

- [ ] **Step 1: Create the test library**

`backend/tests/filter_index_library.py` (checked: with it, every filter in `_FILTER_LIST` matches at least one of the 300 files):

```python
"""A library in which every Scanner filter matches some files: the demo's
(backend/demo.py), plus what the demo leaves out — ignored files and folders,
Plex, Sonarr / Radarr, Maintainerr, watch history, health checks, VMAF,
HDR, extras, other containers and codecs, and a skip rule. And helpers: the
SQL engine's counts to compare with, and changes like a day of use."""
import json
import time
from datetime import datetime, timedelta, timezone

import aiosqlite

from backend.scan_filters import count_all, encode_advanced, facet_counts, parse_filter

_ADV_ALL = encode_advanced([{"property": "file_name", "op": "contains", "value": "S01"},
                            {"property": "video_height", "op": "gte", "value": 720}])
_ADV_ANY = encode_advanced([{"property": "release_group", "op": "contains", "value": "a"},
                            {"property": "audio_lang", "op": "eq", "value": "fre"}], "any")

# Filter strings to compare the index with the SQL engine on: groups, OR
# within a group, exclusions, Python filters, Advanced Search (SQL and
# Python conditions, all and any).
EXPRESSIONS = [
    "x264", "x264,x265", "x264,res_1080p", "needs_conversion,!ignored", "!x264", "queued",
    "plex_watched,type_tv", "hdr_dv,hdr_hdr10,!res_sd", "audio_cleanup,sub_cleanup",
    "not_watched_6m,leaving_soon", "converted,!vmaf_poor", "failed_before", "missing_language,dubbed",
    "!ignored,!corrupt", _ADV_ALL, f"{_ADV_ALL},x265", f"{_ADV_ANY},!x264",
]


async def sql_counts(db_path: str, text: str = "all") -> dict:
    """The counts the SQL engine gives (count_all, or facet_counts under a filter)."""
    from backend.routes.scan import _build_enrichment_context
    expr = parse_filter(text)
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        ctx = await _build_enrichment_context(db, titles=expr.needs_titles)
        if expr.is_all:
            return (await count_all(db, ctx))[0]
        return await facet_counts(db, ctx, expr)


async def sql_savings(db_path: str) -> int:
    from backend.routes.scan import _build_enrichment_context
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        ctx = await _build_enrichment_context(db)
        return (await count_all(db, ctx))[1][1]


async def seed(db_path: str) -> None:
    from backend import demo
    await demo.seed(db_path)
    now = datetime.now(timezone.utc)
    stamp = now.isoformat()
    async with aiosqlite.connect(db_path) as db:
        async with db.execute("SELECT id, file_path FROM scan_results ORDER BY id") as cur:
            rows = await cur.fetchall()
        path = {rid: fp for rid, fp in rows}
        ids = [rid for rid, _ in rows]

        def pick(start: int, step: int = 13, n: int = 6) -> list[int]:
            return ids[start::step][:n]

        def rename(rid: int, new: str) -> tuple:
            path[rid] = new
            return ("file_path = ?", (new,), [rid])

        atmos = json.dumps([{"stream_index": 1, "language": "eng", "codec": "truehd", "channels": 8, "keep": True,
                             "profile": "Dolby TrueHD + Dolby Atmos", "title": ""}])
        french_only = json.dumps([{"stream_index": 1, "language": "fre", "codec": "ac3", "channels": 6,
                                   "keep": True, "profile": "", "title": ""}])
        subs = json.dumps([{"stream_index": 2, "language": "eng", "codec": "subrip", "forced": True, "title": "SDH",
                            "keep": True}])
        updates = [
            ("new_detected_at = ?", ((now - timedelta(hours=2)).isoformat(),), pick(0)),
            ("file_mtime = ?", (time.time() - 3600,), pick(1)),
            ("disc_type = 'bdmv'", (), pick(2, n=2)),
            ("dup_count = 2", (), pick(3, n=4)),
            ("probe_status = 'corrupt'", (), pick(4, n=2)),
            ("health_status = 'warnings', health_checked_at = ?", ((now - timedelta(days=120)).isoformat(),), pick(5)),
            ("health_status = 'healthy', health_checked_at = ?", (stamp,), pick(6)),
            ("link_count = 2", (), pick(7)),
            ("has_und_tracks_flag = 1", (), pick(8)),
            ("has_external_subs_flag = 1", (), pick(9)),
            ("is_dubbed_flag = 1", (), pick(10)),
            ("language_source = 'heuristic'", (), pick(11)),
            ("video_bit_depth = 10", (), pick(12, step=7, n=12)),
            ("video_bit_depth = 10, video_codec = 'h264'", (), pick(14, n=2)),
            ("video_interlaced = 1", (), pick(0, step=17)),
            ("video_vfr = 1", (), pick(1, step=19)),
            ("hdr_format = 'hlg'", (), pick(4, step=31, n=2)),
            ("vmaf_score = 95", (), pick(5, step=11)),
            ("vmaf_score = 89", (), pick(6, step=11)),
            ("vmaf_score = 80, vmaf_uncertain = 1", (), pick(7, step=11, n=3)),
            ("video_codec = 'av1'", (), pick(15, n=2)),
            ("video_codec = 'vp9'", (), pick(16, n=2)),
            ("video_codec = 'mjpeg'", (), pick(17, n=2)),
            ("audio_tracks_json = ?, has_lossless_audio_flag = 1", (atmos,), pick(18, n=3)),
            ("audio_tracks_json = ?, subtitle_tracks_json = '[]'", (french_only,), pick(19, n=3)),
            ("subtitle_tracks_json = ?", (subs,), pick(20, n=3)),
            ("removed_from_list = 1", (), pick(9, step=41, n=3)),
        ]
        movie = path[ids[21]].rsplit("/", 1)[0]
        updates += [
            rename(ids[22], f"{movie}/Extras/Behind the scenes.mkv"),
            rename(ids[23], path[ids[23]].replace(".mkv", ".mp4")),
            rename(ids[24], path[ids[24]].replace(".mkv", ".avi")),
            rename(ids[25], path[ids[25]].replace(".mkv", ".ts")),
            rename(ids[26], "/demo/Other/Home Video (2001) HDTV.mkv"),
        ]
        for sets, params, which in updates:
            await db.execute(f"UPDATE scan_results SET {sets} WHERE id IN ({','.join('?' * len(which))})",
                             (*params, *which))

        await db.executemany("INSERT INTO ignored_files (file_path, reason, ignored_at) VALUES (?, ?, ?)", [
            (path[ids[30]], "manual", stamp),
            ("/demo/TV/Static/", "manual", stamp),
            (path[ids[31]], "conversion_larger", stamp),
        ])
        await db.executemany(
            "INSERT INTO plex_metadata_cache (folder_path, metadata_type, metadata_value, synced_at) "
            "VALUES (?, 'watch_status', ?, ?)", [
                ("/demo/TV/Night Shift/", "watched", stamp),
                ("/demo/TV/Copper Hill/", "unwatched", stamp),
                (movie + "/", "watchlist", stamp),
            ])
        days = 86400
        await db.executemany(
            "INSERT INTO watch_activity (folder_path, server, last_viewed, added_at) VALUES (?, 'plex', ?, ?)", [
                ("/demo/TV/Deep Field/", int(time.time() - 400 * days), int(time.time() - 900 * days)),
                ("/demo/TV/Northbound/", int(time.time() - 800 * days), int(time.time() - 900 * days)),
                ("/demo/TV/Parallel Lines/", int(time.time() - days), int(time.time() - 900 * days)),
            ])
        await db.executemany(
            "INSERT INTO arr_file_status (file_path, service, monitored, cutoff_unmet, synced_at) "
            "VALUES (?, ?, ?, ?, ?)", [
                (path[ids[40]], "radarr", 1, 1, stamp),
                (path[ids[41]], "sonarr", 0, 0, stamp),
            ])
        await db.execute("INSERT INTO maintainerr_media (folder_path, collection, synced_at) "
                         "VALUES ('/demo/TV/Static/', 'Leaving soon', ?)", (stamp,))
        await db.executemany(
            "INSERT INTO jobs (file_path, job_type, status, created_at, completed_at, error_key, backup_path, "
            "space_saved, audio_tracks_to_remove) VALUES (?, 'convert', ?, ?, ?, ?, ?, ?, '[]')", [
                (path[ids[52]], "failed", stamp, stamp, "errors.vmafRejected", None, 0),
                (path[ids[51]], "completed", stamp, stamp, None, "/backups/kept.mkv", 1),
            ])
        await db.execute(
            "INSERT INTO encoding_rules (name, match_type, match_value, action, enabled, created_at) "
            "VALUES ('Skip The Glass House', 'directory', '/demo/TV/The Glass House', 'skip', 1, ?)", (stamp,))
        await db.commit()


async def mutate(db_path: str) -> None:
    """Changes like a day of use: rows edited, hidden, deleted and added; a
    file ignored, one queued, the keep languages changed, a show watched."""
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(db_path) as db:
        async with db.execute("SELECT id, file_path FROM scan_results WHERE removed_from_list = 0 ORDER BY id") as cur:
            rows = await cur.fetchall()
        ids = [r[0] for r in rows]
        await db.execute("UPDATE scan_results SET video_codec = 'hevc', needs_conversion = 0 WHERE id IN (?, ?)",
                         (ids[60], ids[61]))
        await db.execute("UPDATE scan_results SET removed_from_list = 1 WHERE id = ?", (ids[62],))
        await db.execute("DELETE FROM scan_results WHERE id = ?", (ids[63],))
        await db.execute(
            "INSERT INTO scan_results (file_path, file_size, video_codec, needs_conversion, duration, video_height, "
            "video_width, audio_tracks_json, subtitle_tracks_json, scan_timestamp, new_detected_at, "
            "video_conv_savings_bytes) SELECT file_path || '.new.mkv', file_size, video_codec, needs_conversion, "
            "duration, video_height, video_width, audio_tracks_json, subtitle_tracks_json, scan_timestamp, ?, "
            "video_conv_savings_bytes FROM scan_results WHERE id = ?", (now, ids[64]))
        await db.execute("INSERT INTO ignored_files (file_path, reason, ignored_at) VALUES (?, 'manual', ?)",
                         (rows[65][1], now))
        await db.execute("INSERT INTO jobs (file_path, job_type, status, created_at, audio_tracks_to_remove) "
                         "VALUES (?, 'convert', 'pending', ?, '[]')", (rows[66][1], now))
        await db.execute("UPDATE settings SET value = '[\"fre\"]' WHERE key = 'always_keep_languages'")
        await db.execute("INSERT INTO plex_metadata_cache (folder_path, metadata_type, metadata_value, synced_at) "
                         "VALUES ('/demo/TV/Northbound/', 'watch_status', 'watched', ?)", (now,))
        await db.commit()
```

- [ ] **Step 2: Write the failing test**

`backend/tests/test_filter_index_evaluate.py`:

```python
"""Evaluating the filter definitions over every listed row, or some ids."""
import aiosqlite
import pytest
import pytest_asyncio

from backend.filter_index.evaluate import evaluate
from backend.scan_filters import _FILTER_LIST
from backend.tests import filter_index_library as library_data


@pytest_asyncio.fixture
async def library(test_db):
    await library_data.seed(test_db)
    return test_db


async def _evaluate(db_path, filters=_FILTER_LIST, ids=None):
    from backend.routes.scan import _build_enrichment_context
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        ctx = await _build_enrichment_context(db)
        return await evaluate(db, filters, ctx, ids=ids)


@pytest.mark.asyncio
async def test_every_filter_matches_something_in_the_test_library(library):
    counts = await library_data.sql_counts(library)
    assert [fid for fid, n in counts.items() if not n] == []


@pytest.mark.asyncio
async def test_every_listed_row_against_every_filter(library):
    alive, members, savings = await _evaluate(library)
    counts = await library_data.sql_counts(library)
    assert len(alive) == counts["all"]
    assert {fid: len(rows) for fid, rows in members.items()} == {f.id: counts[f.id] for f in _FILTER_LIST}
    assert sum(savings.get(i, 0) for i in members["needs_conversion"]) == await library_data.sql_savings(library)


@pytest.mark.asyncio
async def test_some_rows_give_their_part_of_the_whole(library):
    alive, members, _ = await _evaluate(library)
    async with aiosqlite.connect(library) as db:
        async with db.execute("SELECT id FROM scan_results WHERE removed_from_list = 1") as cur:
            hidden = [r[0] for r in await cur.fetchall()]
    some = alive[::7] + hidden + [10 ** 6]  # listed, hidden, and an id that doesn't exist
    part_alive, part_members, part_savings = await _evaluate(library, ids=some)
    wanted = set(some)
    assert sorted(part_alive) == sorted(i for i in alive if i in wanted)
    for fid, rows in members.items():
        assert sorted(part_members[fid]) == [i for i in sorted(rows) if i in wanted], fid
    assert set(part_savings) <= wanted


@pytest.mark.asyncio
async def test_only_sql_filters_skip_the_python_pass(library):
    sql_only = [f for f in _FILTER_LIST if f.sql][:5]
    _, members, savings = await _evaluate(library, filters=sql_only)
    assert set(members) == {f.id for f in sql_only}
    assert savings is None
```

- [ ] **Step 3: Run it to verify it fails**

Run: `../venv/bin/python -m pytest backend/tests/test_filter_index_evaluate.py -q -p no:warnings`
Expected: FAIL — `ModuleNotFoundError: No module named 'backend.filter_index.evaluate'`

- [ ] **Step 4: Implement**

`backend/filter_index/evaluate.py`:

```python
"""Which listed rows each filter matches: the scan_filters definitions, run
over every listed row or over some ids (an incremental refresh).

SQL filters run in one pass, packed 62 to an integer column per row (bit
63 is SQLite's sign), so the json_each filters' cost is paid once, as in
count_all. Python filters run on the PY_COLUMNS rows with the enrichment
context."""
from __future__ import annotations

import asyncio
from typing import Optional

from backend.scan_filters import LISTED, PY_COLUMNS, Filter

_PER_COLUMN = 62


async def _where(db, ids: Optional[list[int]]) -> str:
    if ids is None:
        return LISTED
    await db.execute("CREATE TEMP TABLE IF NOT EXISTS _fi_scope (id INTEGER PRIMARY KEY)")
    await db.execute("DELETE FROM _fi_scope")
    await db.executemany("INSERT OR IGNORE INTO _fi_scope (id) VALUES (?)", [(i,) for i in ids])
    return f"{LISTED} AND id IN (SELECT id FROM _fi_scope)"


async def evaluate(db, filters: list[Filter], ctx: Optional[dict], ids: Optional[list[int]] = None
                   ) -> tuple[list[int], dict[str, list[int]], Optional[dict[int, int]]]:
    """(alive, members, savings) for `filters` over the listed rows, or over
    `ids` only: the listed ids among them; {filter id: the ids it matches};
    and when a Python filter ran, {id: estimated savings} for rows with any
    (else None). `ctx`: the enrichment context, for Python filters."""
    where = await _where(db, ids)
    members: dict[str, list[int]] = {f.id: [] for f in filters}
    alive: list[int] = []

    sql_filters = [f for f in filters if f.sql]
    chunks = [sql_filters[i:i + _PER_COLUMN] for i in range(0, len(sql_filters), _PER_COLUMN)]
    params: list = []
    columns = []
    for chunk in chunks:
        parts = []
        for bit, f in enumerate(chunk):
            if f.params:
                params.extend(f.params())
            parts.append(f"(CASE WHEN ({f.sql}) THEN {1 << bit} ELSE 0 END)")
        columns.append(" | ".join(parts))
    async with db.execute(f"SELECT id{''.join(f', {c}' for c in columns)} FROM scan_results WHERE {where}",
                          params) as cur:
        rows = await cur.fetchall()

    def spread() -> None:
        for row in rows:
            alive.append(row[0])
            for n, chunk in enumerate(chunks):
                mask = row[1 + n]
                while mask:
                    low = mask & -mask
                    members[chunk[low.bit_length() - 1].id].append(row[0])
                    mask ^= low
    await asyncio.to_thread(spread)

    py_filters = [f for f in filters if f.py]
    if not py_filters:
        return alive, members, None
    async with db.execute(f"SELECT id, {PY_COLUMNS} FROM scan_results WHERE {where}") as cur:
        names = [d[0] for d in cur.description]
        py_rows = [dict(zip(names, values)) for values in await cur.fetchall()]
    savings: dict[int, int] = {}

    def judge() -> None:
        for r in py_rows:
            if r["video_conv_savings_bytes"]:
                savings[r["id"]] = r["video_conv_savings_bytes"]
            for f in py_filters:
                if f.py(r, ctx):
                    members[f.id].append(r["id"])
    await asyncio.to_thread(judge)
    return alive, members, savings
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `../venv/bin/python -m pytest backend/tests/test_filter_index_evaluate.py -q -p no:warnings`
Expected: `4 passed`. If `test_every_filter_matches_something_in_the_test_library` lists a filter, add an update to `seed()` that makes it match; never loosen the assertion.

- [ ] **Step 6: Commit**

```bash
git add backend/filter_index/evaluate.py backend/tests/filter_index_library.py backend/tests/test_filter_index_evaluate.py
git commit -m "feat(filters): evaluate the filter definitions over all or some rows

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: The index and its counts

**Files:**
- Create: `backend/filter_index/index.py` (counts part; save / load in Task 6)
- Test: `backend/tests/test_filter_index_counts.py`

- [ ] **Step 1: Write the failing test**

`backend/tests/test_filter_index_counts.py`:

```python
"""The index's counts are the SQL engine's (count_all / facet_counts), and
patching changed rows and inputs gives what rebuilding gives."""
import aiosqlite
import pytest
import pytest_asyncio

from backend.filter_index.evaluate import evaluate
from backend.filter_index.index import FilterIndex, definitions_version
from backend.scan_filters import _FILTER_LIST, _group_key, parse_filter
from backend.tests import filter_index_library as library_data


@pytest_asyncio.fixture
async def library(test_db):
    await library_data.seed(test_db)
    return test_db


async def _context(db):
    from backend.routes.scan import _build_enrichment_context
    return await _build_enrichment_context(db)


async def index_of(db_path) -> FilterIndex:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        alive, members, savings = await evaluate(db, _FILTER_LIST, await _context(db))
    return FilterIndex.of(alive, members, savings, version=definitions_version(), db_id="test")


def _facets(index: FilterIndex, text: str) -> dict:
    expr = parse_filter(text)
    groups = []
    for group in expr.groups:
        bits = 0
        for f in group:
            bits |= index.bits[f.id]
        groups.append((_group_key(group[0]), bits))
    return index.facet_counts(groups, [(f.id, index.bits[f.id]) for f in expr.excluded])


async def _state(db_path):
    async with aiosqlite.connect(db_path) as db:
        async with db.execute("SELECT COALESCE(MAX(seq), 0) FROM filter_index_changes") as cur:
            seq = (await cur.fetchone())[0]
        async with db.execute("SELECT name, n FROM change_counters WHERE name LIKE 'fi:%'") as cur:
            counters = dict(await cur.fetchall())
    return seq, counters


@pytest.mark.asyncio
async def test_the_counts_are_the_sql_engines(library):
    index = await index_of(library)
    assert index.count_all() == await library_data.sql_counts(library)
    assert index.savings_of("needs_conversion") == await library_data.sql_savings(library)
    for text in library_data.EXPRESSIONS:
        if "adv:" in text:
            continue  # Advanced Search conditions aren't in the index: the service evaluates them (Task 7)
        assert _facets(index, text) == await library_data.sql_counts(library, text), text


@pytest.mark.asyncio
async def test_patching_rows_and_inputs_equals_rebuilding(library):
    index = await index_of(library)
    seq, counters = await _state(library)
    await library_data.mutate(library)
    new_seq, new_counters = await _state(library)
    async with aiosqlite.connect(library) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT DISTINCT row_id FROM filter_index_changes WHERE seq > ?", (seq,)) as cur:
            changed = [r[0] for r in await cur.fetchall()]
        moved = {name[3:] for name, n in new_counters.items() if counters[name] != n}
        dirty = [f for f in _FILTER_LIST if set(f.inputs) & moved]
        ctx = await _context(db)
        alive, members, savings = await evaluate(db, _FILTER_LIST, ctx, ids=changed)
        index.patch(changed, alive, members, savings)
        _, members, _ = await evaluate(db, dirty, ctx)
        index.replace(dirty, members)
    assert moved == {"ignored", "jobs", "settings", "plex"}
    fresh = await index_of(library)
    assert index.alive == fresh.alive
    assert index.bits == fresh.bits
    assert index.savings == fresh.savings
    assert index.count_all() == await library_data.sql_counts(library)


def test_the_definitions_version_follows_the_filters(monkeypatch):
    import dataclasses
    import backend.filter_index.index as index_module
    before = definitions_version()
    changed = [dataclasses.replace(f, sql=f.sql + " AND 1") if f.id == "x264" else f for f in _FILTER_LIST]
    monkeypatch.setattr(index_module, "_FILTER_LIST", changed)
    assert definitions_version() != before
    monkeypatch.setattr(index_module, "_FILTER_LIST", _FILTER_LIST)
    monkeypatch.setattr(index_module, "FILTERS_VERSION", index_module.FILTERS_VERSION + 1)
    assert definitions_version() != before
```

- [ ] **Step 2: Run it to verify it fails**

Run: `../venv/bin/python -m pytest backend/tests/test_filter_index_counts.py -q -p no:warnings`
Expected: FAIL — `ModuleNotFoundError: No module named 'backend.filter_index.index'`

- [ ] **Step 3: Implement**

`backend/filter_index/index.py`:

```python
"""The filter index: for every Scanner filter, the listed scan_results rows
it matches, as a bitset over row ids (bits.py). The pills' counts under any
filter are then a few ANDs and popcounts instead of a pass over the library."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Iterable, Optional

from backend.filter_index.bits import from_ids, ids_of
from backend.scan_filters import FILTERS_VERSION, _FILTER_LIST, Filter, _group_key


def definitions_version() -> str:
    """Changes when a filter does: a saved index for other definitions is rebuilt."""
    text = repr([FILTERS_VERSION] + [(f.id, f.group, f.sql, f.pre_sql, f.py is not None, f.inputs)
                                     for f in _FILTER_LIST])
    return hashlib.sha1(text.encode()).hexdigest()[:16]


@dataclass
class FilterIndex:
    version: str  # definitions_version() it was built with
    db_id: str    # the database it was built from (service._db_id)
    seq: int = 0  # the last filter_index_changes entry applied
    counters: dict[str, int] = field(default_factory=dict)  # the input counters applied ("jobs": n, ...)
    time_at: float = 0.0  # when the time-based filters were evaluated
    alive: int = 0  # the listed rows
    bits: dict[str, int] = field(default_factory=dict)  # filter id -> the rows it matches
    savings: dict[int, int] = field(default_factory=dict)  # row id -> estimated savings, when > 0

    @classmethod
    def of(cls, alive: list[int], members: dict[str, list[int]], savings: dict[int, int], **meta) -> FilterIndex:
        """An index from evaluate()'s answer for every filter over every row."""
        index = cls(**meta)
        index.alive = from_ids(alive)
        index.bits = {f.id: from_ids(members[f.id]) for f in _FILTER_LIST}
        index.savings = dict(savings)
        return index

    def replace(self, filters: Iterable[Filter], members: dict[str, list[int]]) -> None:
        """Take these filters' rows over the whole library. (Savings depend only
        on the row: the change log and patch() keep them right.)"""
        for f in filters:
            self.bits[f.id] = from_ids(members[f.id])

    def patch(self, ids: list[int], alive: list[int], members: dict[str, list[int]],
              savings: dict[int, int]) -> None:
        """Take these rows' answers for every filter: `ids` were evaluated,
        `alive` are the ones still listed, `members` and `savings` theirs."""
        keep = ~from_ids(ids)
        self.alive = (self.alive & keep) | from_ids(alive)
        for fid, bits in list(self.bits.items()):
            self.bits[fid] = (bits & keep) | from_ids(members.get(fid, ()))
        for i in ids:
            self.savings.pop(i, None)
        self.savings.update(savings)

    def count_all(self) -> dict[str, int]:
        """scan_filters.count_all's counts."""
        counts = {"all": self.alive.bit_count()}
        counts.update((fid, bits.bit_count()) for fid, bits in self.bits.items())
        return counts

    def savings_of(self, fid: str) -> int:
        """The estimated savings of the files a filter matches."""
        return sum(self.savings.get(i, 0) for i in ids_of(self.bits[fid]))

    def facet_counts(self, groups: list[tuple[str, int]], excluded: list[tuple[str, int]]) -> dict[str, int]:
        """scan_filters.facet_counts's counts: each pill's with the
        constraints its click replaces (its group's, its own exclusion) set
        aside; "matching": every constraint. `groups`: (group key, the rows
        any of its filters match); `excluded`: (filter id, its rows)."""
        constraints = [bits for _, bits in groups] + [self.alive & ~bits for _, bits in excluded]
        group_at = {key: n for n, (key, _) in enumerate(groups)}
        excluded_at = {fid: len(groups) + n for n, (fid, _) in enumerate(excluded)}
        bases: dict[frozenset, int] = {}

        def base(skip: frozenset) -> int:
            if skip not in bases:
                rows = self.alive
                for n, bits in enumerate(constraints):
                    if n not in skip:
                        rows &= bits
                bases[skip] = rows
            return bases[skip]

        counts = {}
        for f in _FILTER_LIST:
            skip = frozenset(n for n in (group_at.get(_group_key(f)), excluded_at.get(f.id)) if n is not None)
            counts[f.id] = (base(skip) & self.bits[f.id]).bit_count()
        counts["matching"] = base(frozenset()).bit_count()
        return counts
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `../venv/bin/python -m pytest backend/tests/test_filter_index_counts.py -q -p no:warnings`
Expected: `3 passed`

- [ ] **Step 5: Commit**

```bash
git add backend/filter_index/index.py backend/tests/test_filter_index_counts.py
git commit -m "feat(filters): the filter index and its counts

Bitsets per filter over row ids; count_all and facet_counts give the SQL
engine's numbers; patching changed rows and inputs equals rebuilding.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: Saving and loading the index

**Files:**
- Modify: `backend/filter_index/index.py` (add `save`, `load`)
- Test: `backend/tests/test_filter_index_file.py`

- [ ] **Step 1: Write the failing test**

`backend/tests/test_filter_index_file.py`:

```python
"""The index on disk: what's loaded is what was saved, and a damaged or
foreign file loads as nothing (the index is then rebuilt)."""
from backend.filter_index.bits import from_ids
from backend.filter_index.index import FilterIndex
from backend.scan_filters import _FILTER_LIST


def _index() -> FilterIndex:
    index = FilterIndex(version="v1", db_id="db", seq=42, counters={"jobs": 3, "ignored": 1}, time_at=1700000000.5)
    index.alive = from_ids(range(0, 300_000, 3))
    index.bits = {f.id: from_ids(range(n, 300_000, 97 + n)) for n, f in enumerate(_FILTER_LIST)}
    index.bits["x264"] = 0  # an empty one
    index.savings = {3: 1_000_000_000, 299_997: 7}
    return index


def test_saving_and_loading(tmp_path):
    path = str(tmp_path / "filter-index.bin")
    saved = _index()
    saved.save(path)
    assert FilterIndex.load(path) == saved


def test_a_damaged_or_foreign_file_loads_as_nothing(tmp_path):
    path = tmp_path / "filter-index.bin"
    assert FilterIndex.load(str(path)) is None  # none yet
    _index().save(str(path))
    data = path.read_bytes()
    for broken in (data[:-10], data[:len(data) // 2], data[:20] + b"x" + data[21:],
                   data[:-1] + bytes([data[-1] ^ 1]), b"something else entirely\n" + data):
        path.write_bytes(broken)
        assert FilterIndex.load(str(path)) is None
```

- [ ] **Step 2: Run it to verify it fails**

Run: `../venv/bin/python -m pytest backend/tests/test_filter_index_file.py -q -p no:warnings`
Expected: FAIL — `AttributeError: 'FilterIndex' object has no attribute 'save'`

- [ ] **Step 3: Implement**

In `backend/filter_index/index.py`, add to the imports:

```python
import json
import zlib
from array import array
```

after `definitions_version()`, add:

```python
# The file: this line, a JSON header line, then the bitsets and the savings.
# Written in place, not renamed into place (renaming over a file on a NAS
# share can send the old one to its recycle bin): a torn write fails the
# checksum and the index is rebuilt.
_MAGIC = b"SHRINKERR FILTER INDEX 1\n"


def _to_bytes(bits: int) -> bytes:
    return bits.to_bytes((bits.bit_length() + 7) // 8, "little")
```

and as `FilterIndex` methods:

```python
    def save(self, path: str) -> None:
        blobs = [("alive", _to_bytes(self.alive))] + [(fid, _to_bytes(bits)) for fid, bits in self.bits.items()]
        pairs = array("q")
        for row_id, saving in self.savings.items():
            pairs.extend((row_id, saving))
        payload = b"".join(blob for _, blob in blobs) + pairs.tobytes()
        header = {"version": self.version, "db_id": self.db_id, "seq": self.seq, "counters": self.counters,
                  "time_at": self.time_at, "sizes": [[name, len(blob)] for name, blob in blobs],
                  "savings": len(pairs), "length": len(payload), "crc": zlib.crc32(payload)}
        with open(path, "wb") as fh:
            fh.write(_MAGIC + json.dumps(header).encode() + b"\n" + payload)

    @classmethod
    def load(cls, path: str) -> Optional[FilterIndex]:
        """The saved index, or None (none saved, damaged, not one)."""
        try:
            with open(path, "rb") as fh:
                data = fh.read()
        except OSError:
            return None
        if not data.startswith(_MAGIC):
            return None
        try:
            end = data.index(b"\n", len(_MAGIC))
            header = json.loads(data[len(_MAGIC):end])
            payload = data[end + 1:]
            if len(payload) != header["length"] or zlib.crc32(payload) != header["crc"]:
                return None
            index = cls(version=header["version"], db_id=header["db_id"], seq=header["seq"],
                        counters=header["counters"], time_at=header["time_at"])
            at = 0
            for name, size in header["sizes"]:
                value = int.from_bytes(payload[at:at + size], "little")
                at += size
                if name == "alive":
                    index.alive = value
                else:
                    index.bits[name] = value
            pairs = array("q")
            pairs.frombytes(payload[at:at + header["savings"] * pairs.itemsize])
            index.savings = dict(zip(pairs[0::2], pairs[1::2]))
            return index
        except (ValueError, KeyError, TypeError):
            return None
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `../venv/bin/python -m pytest backend/tests/test_filter_index_file.py -q -p no:warnings`
Expected: `2 passed`

- [ ] **Step 5: Commit**

```bash
git add backend/filter_index/index.py backend/tests/test_filter_index_file.py
git commit -m "feat(filters): save and load the filter index

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: The service — build, refresh, counts, save, start-up

**Files:**
- Create: `backend/filter_index/service.py`
- Test: `backend/tests/test_filter_index_service.py`

- [ ] **Step 1: Write the failing test**

`backend/tests/test_filter_index_service.py`:

```python
"""The filter index kept up to date: built in the background, its counts the
SQL engine's after any change, rebuilt for big changes and other databases,
saved and loaded."""
import os

import aiosqlite
import pytest
import pytest_asyncio

from backend.filter_index import service
from backend.scan_filters import parse_filter
from backend.tests import filter_index_library as library_data


@pytest_asyncio.fixture
async def library(test_db):
    await library_data.seed(test_db)
    service.reset()
    yield test_db
    service.reset()


async def _built():
    assert await service.counts(parse_filter("x264")) is None  # nothing yet: starts the build
    await service.wait_rebuilt()


async def _assert_counts_match(db_path):
    for text in ["all", *library_data.EXPRESSIONS]:
        assert await service.counts(parse_filter(text)) == await library_data.sql_counts(db_path, text), text
    counts, savings = await service.stats()
    assert counts == await library_data.sql_counts(db_path)
    assert savings == await library_data.sql_savings(db_path)


@pytest.mark.asyncio
async def test_counts_once_built(library):
    await _built()
    await _assert_counts_match(library)


@pytest.mark.asyncio
async def test_counts_follow_changes(library):
    await _built()
    await library_data.mutate(library)
    await _assert_counts_match(library)
    assert service._state.index.bits == (await service.build()).bits  # what a rebuild gives


@pytest.mark.asyncio
async def test_time_based_filters_are_evaluated_again_every_few_minutes(library):
    await _built()
    right = service._state.index.bits["new"]
    service._state.index.bits["new"] = 0  # stale
    await service.refresh()
    assert service._state.index.bits["new"] == 0  # not yet time
    service._state.index.time_at -= service.TIME_REFRESH
    await service.refresh()
    assert service._state.index.bits["new"] == right


@pytest.mark.asyncio
async def test_a_big_change_rebuilds_in_the_background(library, monkeypatch):
    await _built()
    before = await service.counts(parse_filter("x264"))
    monkeypatch.setattr(service, "BIG_CHANGE", 5)
    async with aiosqlite.connect(library) as db:
        await db.execute("UPDATE scan_results SET video_codec = 'hevc' WHERE video_codec = 'h264'")
        await db.commit()
    assert await service.counts(parse_filter("x264")) == before  # the old index answers meanwhile
    await service.wait_rebuilt()
    assert await service.counts(parse_filter("x264")) == await library_data.sql_counts(library, "x264")


@pytest.mark.asyncio
async def test_another_database_rebuilds(library):
    await _built()
    async with aiosqlite.connect(library) as db:  # what restoring a backup looks like
        await db.execute("UPDATE settings SET value = 'another' WHERE key = 'filter_index_db_id'")
        await db.commit()
    await service.refresh()
    await service.wait_rebuilt()
    assert service._state.index.db_id == "another"


@pytest.mark.asyncio
async def test_saved_then_loaded_after_a_restart(library):
    await _built()
    await service.save(force=True)
    assert os.path.exists(service.index_path())
    async with aiosqlite.connect(library) as db:
        async with db.execute("SELECT COUNT(*) FROM filter_index_changes WHERE seq <= ?",
                              (service._state.index.seq,)) as cur:
            assert (await cur.fetchone())[0] == 0  # what the file holds is dropped from the log
    await library_data.mutate(library)  # changes after the save, while "stopped"
    service.reset()
    assert await service.load()
    await _assert_counts_match(library)


@pytest.mark.asyncio
async def test_an_index_for_other_filter_definitions_isnt_loaded(library, monkeypatch):
    await _built()
    await service.save(force=True)
    service.reset()
    monkeypatch.setattr(service, "definitions_version", lambda: "something else")
    assert not await service.load()
```

- [ ] **Step 2: Run it to verify it fails**

Run: `../venv/bin/python -m pytest backend/tests/test_filter_index_service.py -q -p no:warnings`
Expected: FAIL — `ImportError: cannot import name 'service'`

- [ ] **Step 3: Implement**

`backend/filter_index/service.py`:

```python
"""The Scanner's filter index, kept up to date (spec:
.superpowers/specs/2026-10-11-scanner-filter-overhaul-design.md).

- The first start builds it in the background (~13 s for 166K files); the
  counts come from the SQL engine (scan_filters.count_all / facet_counts)
  until then.
- It's saved in the data folder and loaded at start-up.
- Before answering it applies what changed: scan_results rows (triggers log
  their ids in filter_index_changes), the other tables filters read (change
  counters, inputs.py), and every 5 minutes the time. Many changed rows at
  once (a full scan) rebuild it in the background; the old one answers
  meanwhile. A background loop does the same every few seconds, so a click
  rarely waits.
"""
from __future__ import annotations

import asyncio
import os
import time
import uuid
from typing import Optional

from backend import database
from backend.filter_index.bits import from_ids
from backend.filter_index.evaluate import evaluate
from backend.filter_index.index import FilterIndex, definitions_version
from backend.scan_filters import _FILTER_LIST, Filter, FilterExpr, _group_key

TIME_REFRESH = 300   # seconds between evaluating the time-based filters again
REFRESH_EVERY = 10   # the background loop's pace
SAVE_EVERY = 60      # written to disk at most this often
BIG_CHANGE = 5000    # more changed rows than this (and a fifth of the library): rebuild instead


class _State:
    def __init__(self) -> None:
        self.index: Optional[FilterIndex] = None
        self.rebuild: Optional[asyncio.Task] = None
        self.lock = asyncio.Lock()
        self.dirty = False  # changed since it was saved
        self.last_save = 0.0
        self.ctx_key: Optional[tuple] = None
        self.ctx: Optional[dict] = None
        self.adhoc: dict = {}  # Advanced Search conditions' rows, until the index changes


_state = _State()


def reset() -> None:
    """Forget the index (tests)."""
    global _state
    if _state.rebuild is not None and not _state.rebuild.done():
        _state.rebuild.cancel()
    _state = _State()


def index_path() -> str:
    return os.path.join(os.path.dirname(database.DB_PATH), "filter-index.bin")


def _rebuilding() -> bool:
    return _state.rebuild is not None and not _state.rebuild.done()


async def _db_id(db) -> str:
    """This database's identity: an index from another (a restored backup) is rebuilt."""
    async with db.execute("SELECT value FROM settings WHERE key = 'filter_index_db_id'") as cur:
        row = await cur.fetchone()
    if row:
        return row[0]
    await db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('filter_index_db_id', ?)",
                     (uuid.uuid4().hex,))
    await db.commit()
    return await _db_id(db)


async def _snapshot(db) -> tuple[int, dict[str, int]]:
    """The last logged scan_results change, and the input counters."""
    async with db.execute("SELECT seq FROM sqlite_sequence WHERE name = 'filter_index_changes'") as cur:
        row = await cur.fetchone()
    async with db.execute("SELECT name, n FROM change_counters WHERE name LIKE 'fi:%'") as cur:
        counters = {name[3:]: n for name, n in await cur.fetchall()}
    return (row[0] if row else 0), counters


async def _context(db, counters: dict[str, int], fresh: bool = False) -> dict:
    """The enrichment context the Python filters read; kept while the tables
    it comes from don't change."""
    key = tuple(sorted(counters.items()))
    if fresh or _state.ctx is None or _state.ctx_key != key:
        from backend.routes.scan import _build_enrichment_context
        _state.ctx, _state.ctx_key = await _build_enrichment_context(db), key
    return _state.ctx


async def build() -> FilterIndex:
    """A new index over the whole library."""
    db = await database.connect_db()
    try:
        db_id = await _db_id(db)
        seq, counters = await _snapshot(db)  # before evaluating: later changes are applied after
        started = time.time()
        ctx = await _context(db, counters, fresh=True)
        alive, members, savings = await evaluate(db, _FILTER_LIST, ctx)
    finally:
        await db.close()
    return FilterIndex.of(alive, members, savings or {}, version=definitions_version(), db_id=db_id,
                          seq=seq, counters=counters, time_at=started)


def _start_rebuild() -> None:
    if _rebuilding():
        return

    async def run() -> None:
        started = time.perf_counter()
        try:
            index = await build()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[FILTERS] Building the filter index failed: {exc}", flush=True)
            return
        async with _state.lock:
            _state.index, _state.dirty = index, True
            _state.adhoc.clear()
        print(f"[FILTERS] Filter index built in {time.perf_counter() - started:.1f}s", flush=True)
    _state.rebuild = asyncio.create_task(run())


async def wait_rebuilt() -> None:
    """Until the build or rebuild that's running ends (tests, the benchmark)."""
    if _state.rebuild is not None:
        await _state.rebuild


async def refresh() -> None:
    """Apply what changed since the index last looked (see the module doc)."""
    async with _state.lock:
        index = _state.index
        if index is None or _rebuilding():
            return
        db = await database.connect_db()
        try:
            seq, counters = await _snapshot(db)
            if (seq < index.seq or any(counters.get(k, 0) < n for k, n in index.counters.items())
                    or await _db_id(db) != index.db_id):
                _start_rebuild()  # another database: a restored backup
                return
            moved = {k for k, n in counters.items() if index.counters.get(k) != n}
            now = time.time()
            if now - index.time_at >= TIME_REFRESH:
                moved.add("time")
            changed: list[int] = []
            if seq > index.seq:
                async with db.execute("SELECT DISTINCT row_id FROM filter_index_changes WHERE seq > ? AND seq <= ?",
                                      (index.seq, seq)) as cur:
                    changed = [r[0] for r in await cur.fetchall()]
            if not changed and not moved:
                return
            if len(changed) > max(BIG_CHANGE, index.alive.bit_count() // 5):
                _start_rebuild()
                return
            dirty = [f for f in _FILTER_LIST if set(f.inputs) & moved]
            ctx = await _context(db, counters) if changed or any(f.py for f in dirty) else None
            if changed:
                alive, members, savings = await evaluate(db, _FILTER_LIST, ctx, ids=changed)
                index.patch(changed, alive, members, savings)
            if dirty:
                _, members, _ = await evaluate(db, dirty, ctx)
                index.replace(dirty, members)
            index.seq, index.counters = seq, counters
            if "time" in moved:
                index.time_at = now
            _state.adhoc.clear()
            _state.dirty = True
        finally:
            await db.close()


async def current() -> Optional[FilterIndex]:
    """The index, up to date; None until it's first built (which this starts)."""
    if _state.index is None:
        _start_rebuild()
        return None
    await refresh()
    return _state.index


async def _bits_of(index: FilterIndex, f: Filter) -> int:
    """A filter's rows: the index's, or for an Advanced Search condition,
    evaluated now and (SQL ones) kept until the index changes."""
    if f.id in index.bits:
        return index.bits[f.id]
    key = (f.sql, f.params() if f.params else ()) if f.sql else None
    if key is not None and key in _state.adhoc:
        return _state.adhoc[key]
    db = await database.connect_db()
    try:
        ctx = None
        if f.py:
            from backend.routes.scan import _build_enrichment_context
            ctx = await _build_enrichment_context(db, titles=f.titles)
        _, members, _ = await evaluate(db, [f], ctx)
    finally:
        await db.close()
    bits = from_ids(members[f.id])
    if key is not None:
        _state.adhoc[key] = bits
    return bits


async def counts(expr: FilterExpr) -> Optional[dict[str, int]]:
    """The pills' counts under `expr`, as count_all (no filter) or
    facet_counts give them; None until the index is first built."""
    if await current() is None:
        return None
    async with _state.lock:
        index = _state.index
        if expr.is_all:
            return index.count_all()
        groups = []
        for group in expr.groups:
            bits = 0
            for f in group:
                bits |= await _bits_of(index, f)
            groups.append((_group_key(group[0]), bits))
        excluded = [(f.id, await _bits_of(index, f)) for f in expr.excluded]
        return index.facet_counts(groups, excluded)


async def stats() -> Optional[tuple[dict[str, int], int]]:
    """count_all's counts and the estimated savings of the files that need
    converting; None until the index is first built."""
    if await current() is None:
        return None
    async with _state.lock:
        return _state.index.count_all(), _state.index.savings_of("needs_conversion")


async def save(force: bool = False) -> None:
    """Write the index to the data folder (at most every SAVE_EVERY seconds
    unless `force`), then drop the logged row changes it holds. After a
    crash the next start applies everything after the saved position."""
    async with _state.lock:
        index = _state.index
        if index is None or not _state.dirty or (not force and time.time() - _state.last_save < SAVE_EVERY):
            return
        await asyncio.to_thread(index.save, index_path())
        _state.dirty, _state.last_save = False, time.time()
        seq = index.seq
    db = await database.connect_db()
    try:
        await db.execute("DELETE FROM filter_index_changes WHERE seq <= ?", (seq,))
        await db.commit()
    finally:
        await db.close()


async def load() -> bool:
    """Take the saved index, if it was made with these filter definitions."""
    index = await asyncio.to_thread(FilterIndex.load, index_path())
    if index is None or index.version != definitions_version():
        return False
    async with _state.lock:
        _state.index = index
    return True


async def filter_index_loop() -> None:
    """Start-up: the saved index, else one built in the background; then
    kept fresh and saved."""
    if await load():
        print("[FILTERS] Filter index loaded", flush=True)
    else:
        _start_rebuild()
    while True:
        try:
            await refresh()
            await save()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[FILTERS] Updating the filter index failed: {exc}", flush=True)
        await asyncio.sleep(REFRESH_EVERY)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `../venv/bin/python -m pytest backend/tests/test_filter_index_service.py -q -p no:warnings`
Expected: `7 passed`

- [ ] **Step 5: Mutation-check the tests**

Each of these edits to `service.py` must make at least one test fail (make it, run the file, undo it):
1. In `refresh`, `index.patch(changed, alive, members, savings)` → `pass`
2. In `refresh`, `if now - index.time_at >= TIME_REFRESH:` → `if False:`
3. In `refresh`, delete the `_start_rebuild()` call marked `# another database: a restored backup` (keep its `return`)
4. In `save`, the `DELETE FROM filter_index_changes` → `SELECT 1`
5. In `_bits_of`, return `0` for filters not in the index

- [ ] **Step 6: Commit**

```bash
git add backend/filter_index/service.py backend/tests/test_filter_index_service.py
git commit -m "feat(filters): keep the filter index up to date and answer counts

Built in the background, refreshed from the change log and counters
before answering (rebuilt for big changes or another database), saved
to the data folder and loaded at start-up.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 8: Counts endpoints from the index; start-up and shutdown

**Files:**
- Modify: `backend/routes/scan.py` (`get_scan_stats` ~line 1922, `get_filter_counts` ~line 2358)
- Modify: `backend/main.py` (lifespan: after `mqtt_task = ...`; teardown)
- Test: `backend/tests/test_filter_index_endpoints.py`

- [ ] **Step 1: Write the failing test**

`backend/tests/test_filter_index_endpoints.py`:

```python
"""/api/scan/filter-counts and /api/scan/scan-stats answer from the filter
index once it's built, and from the SQL engine until then."""
import pytest
import pytest_asyncio

from backend.filter_index import service
from backend.tests import filter_index_library as library_data


@pytest_asyncio.fixture
async def library(test_db, monkeypatch):
    import backend.routes.scan as scan_route
    monkeypatch.setattr(scan_route, "DB_PATH", test_db)
    await library_data.seed(test_db)
    service.reset()
    yield test_db
    service.reset()


@pytest.mark.asyncio
async def test_the_counts_come_from_the_index_once_built(library, monkeypatch):
    import backend.routes.scan as scan_route
    expected = {text: await library_data.sql_counts(library, text) for text in ("all", "x264,!ignored")}
    savings = await library_data.sql_savings(library)
    # Not built yet: the SQL engine answers (and the build starts).
    assert await scan_route.get_filter_counts("x264,!ignored") == {"counts": expected["x264,!ignored"]}
    await service.wait_rebuilt()

    async def sql_engine(*args, **kwargs):
        raise AssertionError("the SQL engine ran")
    monkeypatch.setattr(scan_route, "count_all", sql_engine)
    monkeypatch.setattr(scan_route, "facet_counts", sql_engine)
    for text, counts in expected.items():
        assert await scan_route.get_filter_counts(text) == {"counts": counts}
    stats = await scan_route.get_scan_stats()
    assert stats["summary"]["files_to_convert"] == expected["all"]["needs_conversion"]
    assert stats["summary"]["estimated_savings_bytes"] == savings
    assert stats["counts"]["x264"] == expected["all"]["x264"]
```

- [ ] **Step 2: Run it to verify it fails**

Run: `../venv/bin/python -m pytest backend/tests/test_filter_index_endpoints.py -q -p no:warnings`
Expected: FAIL — `AssertionError: the SQL engine ran`

- [ ] **Step 3: Answer from the index**

In `backend/routes/scan.py`, `get_filter_counts`, replace the body:

```python
    expr = parse_filter(filter)
    from backend.filter_index import service as filter_index
    fast = await filter_index.counts(expr)
    if fast is not None:
        return {"counts": fast}
    # The index's first build is still running: the SQL engine (~12 s at 166K files).
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    try:
        ctx = await _build_enrichment_context(db, titles=expr.needs_titles)
        if expr.is_all:
            counts, _ = await count_all(db, ctx)
            return {"counts": counts}
        return {"counts": await facet_counts(db, ctx, expr)}
    finally:
        await db.close()
```

In `get_scan_stats`, replace

```python
        ctx = await _build_enrichment_context(db)
        counts, (_, estimated_savings) = await count_all(db, ctx)
```

with

```python
        from backend.filter_index import service as filter_index
        fast = await filter_index.stats()
        if fast is not None:
            counts, estimated_savings = fast
        else:  # the index's first build is still running
            ctx = await _build_enrichment_context(db)
            counts, (_, estimated_savings) = await count_all(db, ctx)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `../venv/bin/python -m pytest backend/tests/test_filter_index_endpoints.py -q -p no:warnings`
Expected: `1 passed`

- [ ] **Step 5: Start it with the app, save it on shutdown**

In `backend/main.py`, lifespan, after `mqtt_task = asyncio.create_task(mqtt_loop())`:

```python
    # The Scanner's filter index: loaded or built, then kept up to date (v0.10.0).
    from backend.filter_index.service import filter_index_loop
    filter_index_task = asyncio.create_task(filter_index_loop())
```

In the teardown after `yield`, after `digest_task.cancel()`:

```python
    filter_index_task.cancel()
    try:
        from backend.filter_index.service import save as save_filter_index
        await save_filter_index(force=True)
    except Exception as exc:
        print(f"[FILTERS] Saving the filter index failed: {exc}", flush=True)
```

- [ ] **Step 6: Check it in the running app**

Before restarting, note three pill counts on the e2e Scanner page (e.g. All, x264, and x264 + 1080p). Restart the e2e preview server (`preview_start` name `shrinkerr-e2e-develop`; its DB is under `../e2e3/data`). Its log shows `[FILTERS] Filter index built in …` within seconds, and `../e2e3/data/filter-index.bin` exists after the next save (≤ 60 s) or a stop. Restart it again: `[FILTERS] Filter index loaded`. The same three counts match what you noted, and clicking pills updates counts at once.

- [ ] **Step 7: Run the full suite**

Run the full suite command. Expected: `EXIT=0`.

- [ ] **Step 8: Commit**

```bash
git add backend/routes/scan.py backend/main.py backend/tests/test_filter_index_endpoints.py
git commit -m "feat(filters): the Scanner's counts come from the filter index

/filter-counts and /scan-stats answer from the index (milliseconds), from
the SQL engine while its first build runs. The index starts with the app
and is saved on shutdown.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 9: Benchmark, changelog, push

**Files:**
- Create: `scripts/bench_filter_index.py`
- Modify: `CHANGELOG.md` ([Unreleased] → Changed)

- [ ] **Step 1: Write the benchmark**

`scripts/bench_filter_index.py`:

```python
"""How fast the Scanner's filter index is on a synthetic library (by hand,
not in CI):

    python scripts/bench_filter_index.py /tmp/shrinkerr-bench.db

The first run makes the library: the demo's files copied to ~166K. The
spec's targets: counts after a click < 150 ms, counts after a restart
< 300 ms, 1,000 changed files < 500 ms, a full build < 20 s."""
import asyncio
import os
import sqlite3
import sys
import time

DB = os.path.abspath(sys.argv[1])
os.environ["SHRINKERR_DB_PATH"] = DB
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import backend.config  # noqa: E402
from backend.config import Settings  # noqa: E402

backend.config.settings = Settings(db_path=DB, media_root=os.path.dirname(DB))
import backend.database as database  # noqa: E402

database.DB_PATH = DB
from backend import demo  # noqa: E402
from backend.filter_index import service  # noqa: E402
from backend.scan_filters import parse_filter  # noqa: E402

FILES = 166_000


async def make() -> None:
    await database.init_db()
    n = await demo.seed(DB)
    con = sqlite3.connect(DB)
    cols = [r[1] for r in con.execute("PRAGMA table_info(scan_results)") if r[1] != "id"]
    pick = ", ".join("file_path || '.copy' || ?" if c == "file_path" else c for c in cols)
    for i in range(1, FILES // n):
        con.execute(f"INSERT INTO scan_results ({', '.join(cols)}) SELECT {pick} FROM scan_results WHERE id <= ?",
                    (i, n))
    con.commit()
    con.close()


def report(label: str, started: float, target: float) -> None:
    took = time.perf_counter() - started
    print(f"{label:40} {took * 1000:9.1f} ms   target {target * 1000:7.0f} ms   {'ok' if took <= target else 'SLOW'}")


async def main() -> None:
    if not os.path.exists(DB):
        print("Making the library…")
        await make()
    await database.init_db()
    if os.path.exists(service.index_path()):
        os.remove(service.index_path())
    service.reset()
    started = time.perf_counter()
    await service.current()
    await service.wait_rebuilt()
    report("full build", started, 20)
    for text in ("x264", "x264,res_1080p,!ignored", "needs_conversion,!ignored,plex_watched"):
        started = time.perf_counter()
        await service.counts(parse_filter(text))
        report(f"counts: {text}", started, 0.15)
    await service.save(force=True)
    service.reset()
    started = time.perf_counter()
    await service.load()
    await service.counts(parse_filter("x264"))
    report("restart: load, then counts", started, 0.3)
    con = sqlite3.connect(DB)
    con.execute("UPDATE scan_results SET file_size = file_size + 1 "
                "WHERE id IN (SELECT id FROM scan_results ORDER BY id LIMIT 1000 OFFSET 5000)")
    con.commit()
    con.close()
    started = time.perf_counter()
    await service.refresh()
    report("1,000 changed files", started, 0.5)


asyncio.run(main())
```

- [ ] **Step 2: Run it**

Run: `../venv/bin/python scripts/bench_filter_index.py ../fi/bench.db` (the first run makes the library: a few minutes)
Expected: every line `ok`. Paste the output into the task report. If a line is `SLOW`, report it with the numbers rather than tuning blindly.

- [ ] **Step 3: Changelog**

In `CHANGELOG.md`, `## [Unreleased]` → `### Changed`, add as the first entry:

```markdown
- **Scanner counts are instant**: which files each filter matches is kept in an index that follows your files, the queue and your settings as they change, so a click updates every count at once instead of re-reading the library (12 seconds on a big one). It's built once in the background after the update, and saved with your data.
```

- [ ] **Step 4: Docs check**

Run: `grep -rn -i "count_all\|facet_counts\|filter counts\|pill counts" docs/ README.md | head`
If a statement there describes the counts as computed on every request, update it to the index; otherwise nothing to change.

- [ ] **Step 5: Full suite, frontend build, push**

Run the full suite command. Expected: `EXIT=0`.
Run: `cd frontend && npm run build > ../../build.txt 2>&1; echo "EXIT=$?"` Expected: `EXIT=0` (no frontend change; confirms nothing broke).

```bash
git add scripts/bench_filter_index.py CHANGELOG.md
git commit -m "chore(filters): filter index benchmark and changelog

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git push origin develop
```

---

## Self-review notes

- **Spec coverage (phase 1):** bitsets per option (Tasks 2, 5); build from the scan_filters definitions in one SQL pass plus the Python predicates (Task 4); persistence with definitions version and change sequence (Tasks 6, 7); row changes by trigger log, re-evaluated by id (Tasks 3, 7); big batches rebuild in the background while the old index answers (Task 7); context inputs with counters re-evaluating only dependent filters (Tasks 1, 3, 7); time-based filters every 5 minutes (Task 7); counts by AND / popcount with the group set aside (Task 5); Advanced Search conditions as SQL into a bitset, cached until the index changes (Task 7); not ready → the SQL engine answers (Task 8; the spec's "counting…" UI is phase 2); equivalence tests, incremental = rebuild, benchmark by hand (Tasks 4, 5, 7, 9). Restored backups (another database) rebuild (Task 7).
- **Deferred to later phases, as the spec's rollout says:** list facets and their search endpoint, id lists for rare values (phase 3); tree, file lists and Add to Queue through the index (phase 5); the new bar (phase 2).
- **Known costs:** an Advanced Search condition that's a Python check (regex, name, release group, title fields) is evaluated per request (~0.3–0.5 s at 166K files) — only SQL conditions are cached. The trigger log takes one row per scan_results write; a full scan logs every row (cleared at the next save).
