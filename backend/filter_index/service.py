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
- A build that fails is tried again after a pause (1 min, doubling to 10).
  Meanwhile, and on any error, the SQL engine answers.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import time
import traceback
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
BACKOFF_FIRST = 60   # seconds before trying a failed build again; doubles each time
BACKOFF_MAX = 600
ERROR_LOG_EVERY = 60  # an error is logged at most this often


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
        self.stale = False    # a rebuild must replace it (refresh decided so)
        self.loading = False  # the saved index is being read (start-up)
        self.failed_at = 0.0  # when the last build failed (time.monotonic)
        self.backoff = 0.0    # the pause after it; 0 after a build succeeds
        self.error_logged_at = float("-inf")


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
    """This database's identity: an index built from another database is
    rebuilt. (A restored backup of this one keeps it: refresh sees the
    change log go back instead.)"""
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
    # ~0.5 s for 166K files: off the event loop.
    return await asyncio.to_thread(FilterIndex.of, alive, members, savings or {}, version=definitions_version(),
                                   db_id=db_id, seq=seq, counters=counters, time_at=started)


def _start_rebuild() -> None:
    """Build in the background, unless a build is running or the last one
    failed less than the backoff ago."""
    if _rebuilding() or time.monotonic() - _state.failed_at < _state.backoff:
        return

    async def run() -> None:
        started = time.perf_counter()
        try:
            index = await build()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not _state.backoff:  # the first failure in a row: the traceback, once
                print(f"[FILTERS] Building the filter index failed:\n{traceback.format_exc()}", end="", flush=True)
            _state.backoff = min(_state.backoff * 2, BACKOFF_MAX) if _state.backoff else BACKOFF_FIRST
            _state.failed_at = time.monotonic()
            print(f"[FILTERS] Building the filter index failed: {exc}; trying again in {_state.backoff:.0f}s",
                  flush=True)
            return
        async with _state.lock:
            _state.index, _state.dirty, _state.stale, _state.backoff = index, True, False, 0.0
            _state.adhoc = {}
        print(f"[FILTERS] Filter index built in {time.perf_counter() - started:.1f}s", flush=True)
        try:  # the Scanner fetches its counts again
            from backend.websocket import ws_manager
            await ws_manager.send_scan_results_changed(added=0, removed=0)
        except Exception as exc:
            print(f"[FILTERS] WS broadcast failed (non-fatal): {exc}", flush=True)
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
        if _state.stale:
            _start_rebuild()  # the last one failed: again once the backoff is over
            return
        db = await database.connect_db()
        try:
            seq, counters = await _snapshot(db)
            if (seq < index.seq or any(counters.get(k, 0) < n for k, n in index.counters.items())
                    or await _db_id(db) != index.db_id):
                # A restored backup (the change log or a counter went back) or another database.
                _state.stale = True
                _start_rebuild()
                return
            moved = {k for k, n in counters.items() if index.counters.get(k) != n}
            now = time.time()
            if now - index.time_at >= TIME_REFRESH or now < index.time_at:  # or the clock went back
                moved.add("time")
            changed: list[int] = []
            if seq > index.seq:
                async with db.execute("SELECT DISTINCT row_id FROM filter_index_changes WHERE seq > ? AND seq <= ?",
                                      (index.seq, seq)) as cur:
                    changed = [r[0] for r in await cur.fetchall()]
            if not changed and not moved:
                return
            if len(changed) > max(BIG_CHANGE, index.alive.bit_count() // 5):
                _state.stale = True
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
            _state.adhoc = {}
            _state.dirty = True
        finally:
            await db.close()


def _log_error(what: str, exc: Exception) -> None:
    """At most every ERROR_LOG_EVERY seconds: a broken database would log on every click."""
    now = time.monotonic()
    if now - _state.error_logged_at >= ERROR_LOG_EVERY:
        _state.error_logged_at = now
        print(f"[FILTERS] {what} failed: {exc}", flush=True)


async def current() -> Optional[FilterIndex]:
    """The index, up to date. None (the SQL engine answers) while the saved
    one loads, until it's first built (which this starts), and when a
    rebuild must replace it and the last try failed."""
    if _state.loading:
        return None
    if _state.index is None:
        _start_rebuild()
        return None
    await refresh()
    if _state.stale and (_state.backoff or not _rebuilding()):
        return None  # the old index answers only while its first rebuild runs
    return _state.index


async def _adhoc_bits(f: Filter) -> int:
    """An Advanced Search condition's rows, evaluated now; SQL ones kept
    until the index changes. (A change replaces the cache, so rows evaluated
    across one land in the old cache.)"""
    key = (f.sql, f.params() if f.params else ()) if f.sql else None
    cache = _state.adhoc
    if key is not None and key in cache:
        return cache[key]
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
        cache[key] = bits
    return bits


async def counts(expr: FilterExpr) -> Optional[dict[str, int]]:
    """The pills' counts under `expr`, as count_all (no filter) or
    facet_counts give them; None when the SQL engine should answer
    (current()) or on an error."""
    try:
        index = await current()
        if index is None:
            return None
        # Advanced Search conditions first, outside the lock: a refresh
        # shouldn't wait on a Python condition's evaluation.
        adhoc: dict[str, int] = {}
        for f in [f for group in expr.groups for f in group] + expr.excluded:
            if f.id not in index.bits:
                adhoc[f.id] = await _adhoc_bits(f)
        async with _state.lock:
            index = _state.index
            if expr.is_all:
                return index.count_all()

            def bits_of(f: Filter) -> int:
                return adhoc[f.id] if f.id in adhoc else index.bits[f.id]
            groups = []
            for group in expr.groups:
                bits = 0
                for f in group:
                    bits |= bits_of(f)
                groups.append((_group_key(group[0]), bits))
            excluded = [(f.id, bits_of(f)) for f in expr.excluded]
            return index.facet_counts(groups, excluded)
    except Exception as exc:
        _log_error("Counting with the filter index", exc)
        return None


async def stats() -> Optional[tuple[dict[str, int], int]]:
    """count_all's counts and the estimated savings of the files that need
    converting; None as for counts()."""
    try:
        if await current() is None:
            return None
        async with _state.lock:
            return _state.index.count_all(), _state.index.savings_of("needs_conversion")
    except Exception as exc:
        _log_error("Counting with the filter index", exc)
        return None


async def save(force: bool = False) -> None:
    """Write the index to the data folder (at most every SAVE_EVERY seconds
    unless `force`), then drop the logged row changes it holds. After a
    crash the next start applies everything after the saved position."""
    async with _state.lock:
        index = _state.index
        if index is None or not _state.dirty or (not force and time.time() - _state.last_save < SAVE_EVERY):
            return
        if _state.stale or _rebuilding():
            # Its position can be past the rows a rebuild still needs (a
            # restored backup), and saved it would no longer look stale.
            return
        # Cancelled mid-write (the app stopping): finish the write before
        # letting go of the lock, or the shutdown save would write over it.
        write = asyncio.ensure_future(asyncio.to_thread(index.save, index_path()))
        try:
            await asyncio.shield(write)
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await write
            raise
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
    kept fresh and saved. Cancelled (the app stopping), it cancels a rebuild."""
    try:
        _state.loading = True  # counts() doesn't start a build meanwhile
        try:
            loaded = await load()
        finally:
            _state.loading = False
        if loaded:
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
                _log_error("Updating the filter index", exc)
            await asyncio.sleep(REFRESH_EVERY)
    finally:
        if _rebuilding():
            rebuild = _state.rebuild
            rebuild.cancel()
            # Until it has closed its connection: an open one keeps the process from exiting.
            await asyncio.gather(rebuild, return_exceptions=True)
