# Dubbed + Not-API-Matched Filters & Incremental Refresh — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a "dubbed" filter (items missing their original-language audio), a "not API-matched" filter (native not from TMDB/manual, split tried/untried), and make `refresh-metadata` incremental so it converges instead of reprocessing the whole library.

**Architecture:** Two new `scan_results` columns — `is_dubbed_flag` (precomputed, so the dubbed filter/count are plain SQL) and `tmdb_unresolved` (mark-and-skip for refresh). `is_dubbed` is a pure function of (audio langs, native, source); computed inline where source is unambiguous (scan→0, refresh→api) and via a read-back recompute at per-file sites (detect, set-language) to avoid mirroring the SQL CASE. Refresh's default select narrows to untried heuristic rows; failed lookups set `tmdb_unresolved`.

**Tech Stack:** Python 3.12, aiosqlite, FastAPI, React/TS, pytest.

**Spec:** `.superpowers/specs/2026-07-22-dubbed-and-unmatched-filters-design.md`

**Container test baseline:**
```bash
docker run --rm -v "$PWD":/app -w /app python:3.12-slim bash -c \
  "pip install -q -r requirements.txt >/dev/null 2>&1 && python -m pytest backend/tests/<file> -q 2>&1 | tail -8"
```

---

## File Structure
- **Modify** `backend/database.py` — migration: add `is_dubbed_flag`, `tmdb_unresolved`.
- **Modify** `backend/scanner.py` — `_is_dubbed()` pure helper (near `LANGUAGE_EQUIVALENTS`, ~line 529).
- **Modify** `backend/routes/scan.py` — `recompute_is_dubbed_flag()`; wire the flag at scan/detect/set-language/refresh; add filter predicates + `_matches_single_filter` cases + count SUMs; incremental refresh + `deep` option.
- **Modify** `frontend/src/components/FilterBar.tsx` — "Language:" group with `dubbed` + `not_api_matched` chips.
- **Modify** `frontend/src/api.ts` + wherever counts are typed — surface new counts (if typed).
- **Tests:** `backend/tests/test_scanner.py` (`_is_dubbed`), `backend/tests/test_scan_filters.py` or existing filter test file (predicates, refresh).

---

## Task 1: Migration — two columns

**Files:** Modify `backend/database.py` (~line 339, after the flags-migration block)

- [ ] **Step 1: Add the columns (idempotent, matches existing pattern)**

After the `for col, coltype in [...]` scan_results flags block (ends ~line 339), add:
```python
        # Migration: dubbed filter + incremental-refresh mark-and-skip (v0.9.85)
        for col, coltype in [
            ("is_dubbed_flag", "INTEGER DEFAULT 0"),
            ("tmdb_unresolved", "INTEGER DEFAULT 0"),
        ]:
            try:
                await db.execute(f"ALTER TABLE scan_results ADD COLUMN {col} {coltype}")
            except Exception:
                pass  # column already exists
```

- [ ] **Step 2: Verify migration is idempotent**

Run: existing migration test (`backend/tests/test_disc_metadata.py` has a migration test) in the container; expect pass. If none targets this, add a minimal test that opens a fresh DB twice and asserts both columns exist with default 0.

- [ ] **Step 3: Commit**
```bash
git add backend/database.py
git commit -m "feat(db): add is_dubbed_flag + tmdb_unresolved columns"
```

---

## Task 2: `_is_dubbed` pure helper

**Files:** Modify `backend/scanner.py`; Test `backend/tests/test_scanner.py`

- [ ] **Step 1: Write failing tests**

Add to `backend/tests/test_scanner.py`:
```python
def test_is_dubbed_core():
    from backend.scanner import _is_dubbed
    # api native kor, only english audio -> dubbed
    assert _is_dubbed(["eng"], "kor", "api") == 1
    # a track matches native -> not dubbed
    assert _is_dubbed(["kor", "eng"], "kor", "api") == 0
    # equivalence: native zho matches cmn audio -> not dubbed
    assert _is_dubbed(["cmn"], "zho", "api") == 0
    # manual + tmdb-manual sources count
    assert _is_dubbed(["eng"], "kor", "manual") == 1
    assert _is_dubbed(["eng"], "kor", "tmdb-manual") == 1
    # heuristic source is never dubbed (native derived from audio)
    assert _is_dubbed(["eng"], "kor", "heuristic") == 0
    # und native -> not evaluable
    assert _is_dubbed(["eng"], "und", "api") == 0
    assert _is_dubbed(["eng"], "", "api") == 0
    # any und audio track -> uncertain, not flagged
    assert _is_dubbed(["eng", "und"], "kor", "api") == 0
    # no audio at all -> not dubbed
    assert _is_dubbed([], "kor", "api") == 0
    # case-insensitive
    assert _is_dubbed(["ENG"], "KOR", "api") == 1
```

- [ ] **Step 2: Run — expect ImportError/failure.**

- [ ] **Step 3: Implement (near `LANGUAGE_EQUIVALENTS`, ~line 529)**
```python
_DUBBED_NATIVE_SOURCES = ("api", "tmdb-manual", "manual")


def _is_dubbed(audio_langs: list[str], native_language: str, language_source: str) -> int:
    """1 if this item is dubbed: its native (original) language is known from a
    source independent of the audio present, it has audio, every audio track
    has a known language, and none match native (using LANGUAGE_EQUIVALENTS).
    0 otherwise (incl. when the status is uncertain). See the design spec."""
    if (language_source or "") not in _DUBBED_NATIVE_SOURCES:
        return 0
    native = (native_language or "").lower()
    if not native or native == "und":
        return 0
    langs = [(l or "und").lower() for l in audio_langs]
    if not langs:
        return 0
    if any(l == "und" for l in langs):
        return 0
    native_equiv = LANGUAGE_EQUIVALENTS.get(native, {native})
    for l in langs:
        if l in native_equiv or native in LANGUAGE_EQUIVALENTS.get(l, {l}):
            return 0  # an audio track matches native
    return 1
```

- [ ] **Step 4: Run tests — expect pass. Commit**
```bash
git add backend/scanner.py backend/tests/test_scanner.py
git commit -m "feat(scanner): _is_dubbed helper (native-vs-audio with equivalence)"
```

---

## Task 3: Persist `is_dubbed_flag` at the write sites

**Files:** Modify `backend/routes/scan.py`

- [ ] **Step 1: Add the read-back recompute helper (near `_und_flag`, ~line 869)**
```python
async def recompute_is_dubbed_flag(db, file_path: str) -> None:
    """Recompute is_dubbed_flag for one row from its CURRENT persisted state
    (audio_tracks_json, native_language, language_source). Used by the per-file
    write paths (detect, set-language) so we read the real stored result rather
    than mirroring the SQL CASE that preserves authoritative native/source."""
    from backend.scanner import _is_dubbed
    async with db.execute(
        "SELECT audio_tracks_json, native_language, language_source "
        "FROM scan_results WHERE file_path = ?", (file_path,)
    ) as cur:
        row = await cur.fetchone()
    if not row:
        return
    try:
        audio = json.loads(row[0]) if row[0] else []
    except (ValueError, TypeError):
        audio = []
    langs = [(t.get("language") or "und") for t in audio]
    flag = _is_dubbed(langs, row[1], row[2])
    await db.execute(
        "UPDATE scan_results SET is_dubbed_flag = ? WHERE file_path = ?",
        (flag, file_path))
```

- [ ] **Step 2: Scan-persist INSERT — store 0 (scan native is always heuristic).**

In the scan upsert (~line 95), add `is_dubbed_flag` to the INSERT column list and `VALUES` (literal `0`), and to the `ON CONFLICT DO UPDATE SET` clause set `is_dubbed_flag=0`. (Scan sets `language_source='heuristic'` → `_is_dubbed`=0 by definition; refresh/set-language recompute it when the row becomes api/manual.) Add a code comment saying so.

- [ ] **Step 3: Detect endpoint — recompute after the UPDATE+commit (~line 1222).**

Immediately after the existing `await db.commit()` that follows the detect UPDATE (line ~1223), add:
```python
        await recompute_is_dubbed_flag(db, req.file_path)
        await db.commit()
```
(Detect can resolve und audio on an api-native row, flipping dubbed status; the read-back sees the real preserved source/native.)

- [ ] **Step 4: set-track-language — recompute after its UPDATE+commit (~line 1389).**

After that handler's `await db.commit()`, add the same two lines (`recompute_is_dubbed_flag(db, req.file_path)` + commit) before `db.close()`.

- [ ] **Step 5: Metadata refresh — compute inline (source is definitively 'api').**

In `_write_lang_batch` (~line 3063), when `item.get("native") is not None` (the row is flipping to api), also set `is_dubbed_flag`. Extend the `a_json` branch (which already has the audio JSON + und flag) to include the dubbed flag: the refresh's `_reclass_item` should compute `is_dubbed` from the reclassified audio langs + the api native and put it in the item dict as `item["dubbed"]`; then in the UPDATE builder add `"is_dubbed_flag = ?"` with `item["dubbed"]` whenever `a_json` is present. Where `_reclass_item` builds its dict, compute:
```python
        # v0.9.85: recompute dubbed status against the (api) native.
        item["dubbed"] = _is_dubbed(
            [(t.get("language") or "und") for t in audio_list], native, "api")
```
(Use the same `native` and reclassified `audio_list` `_reclass_item` already has; import `_is_dubbed` from scanner.)

- [ ] **Step 6: Verify + commit**

Run the full suite in the container (nothing should break; behavior added). Commit:
```bash
git add backend/routes/scan.py
git commit -m "feat(scan): maintain is_dubbed_flag at scan/detect/set-lang/refresh"
```

---

## Task 4: Backend filters + counts

**Files:** Modify `backend/routes/scan.py`

- [ ] **Step 1: Add SQL predicates in the filter builder (~line 2555, near `unknown_language`).**
```python
    elif f == "dubbed":
        sql = "AND COALESCE(is_dubbed_flag, 0) = 1"
    elif f == "not_api_matched":
        sql = "AND (language_source IS NULL OR language_source NOT IN ('api','manual','tmdb-manual'))"
```
(No `needs_python` — both are pure column predicates.)

- [ ] **Step 2: Add `_matches_single_filter` cases (~line 2341) for the tree view.**

In `_matches_single_filter(enriched, filter_name)`, add:
```python
    if filter_name == "dubbed":
        return bool(enriched.get("is_dubbed_flag"))
    if filter_name == "not_api_matched":
        return (enriched.get("language_source") or "") not in ("api", "manual", "tmdb-manual")
```
Ensure `is_dubbed_flag` and `language_source` are included in the enriched dict the tree builds (add to its SELECT/projection if absent — check `get_scan_tree` ~line 2690 column list).

- [ ] **Step 3: Add count SUMs in the counts endpoint (~line 1593 SELECT).**

Add to the SELECT:
```sql
                SUM(COALESCE(is_dubbed_flag, 0)) as dubbed,
                SUM(CASE WHEN language_source IS NULL OR language_source NOT IN ('api','manual','tmdb-manual') THEN 1 ELSE 0 END) as not_api_matched,
                SUM(CASE WHEN (language_source IS NULL OR language_source NOT IN ('api','manual','tmdb-manual')) AND COALESCE(tmdb_unresolved,0) = 1 THEN 1 ELSE 0 END) as not_api_matched_no_tmdb,
```
Map them into the returned counts dict alongside `unknown_language` etc. (`not_api_matched` total and `not_api_matched_no_tmdb` = the tried-but-failed subset; untried = total − subset).

- [ ] **Step 4: Verify + commit**

Add `backend/tests/test_scan_filters.py` (or extend the existing filter test): seed a temp DB with rows exercising each predicate (api+dubbed, api+not-dubbed, heuristic, manual, tmdb_unresolved=1) and assert the filter SQL and `_matches_single_filter` select the right ones, and the count SUMs match. Run in container; commit:
```bash
git add backend/routes/scan.py backend/tests/test_scan_filters.py
git commit -m "feat(scan): dubbed + not_api_matched filters and counts"
```

---

## Task 5: FilterBar UI

**Files:** Modify `frontend/src/components/FilterBar.tsx`

- [ ] **Step 1: Add a "Language:" divider group with the two chips.**

After an existing group (e.g. after the `_audio` group), add:
```tsx
  { key: "_lang", label: "Language:", group: "divider" },
  { key: "dubbed", label: "Dubbed" },
  { key: "not_api_matched", label: "Not API-matched" },
```
If the chips render counts from the counts endpoint, wire `dubbed` and `not_api_matched` to the new count fields (mirror how `unknown_language` count is shown). For the not-API-matched chip, if the UI supports a secondary/subtitle count, show the tried-no-match subset (`not_api_matched_no_tmdb`) as a hint; otherwise leave the total.

- [ ] **Step 2: Verify the frontend build (tsc -b, NOT --noEmit).**
```bash
cd frontend && npm run build
```
Expect clean.

- [ ] **Step 3: Commit**
```bash
git add frontend/src/components/FilterBar.tsx frontend/src/api.ts
git commit -m "feat(ui): Dubbed + Not API-matched filter chips"
```

---

## Task 6: Incremental metadata refresh + `deep` option

**Files:** Modify `backend/routes/scan.py`

- [ ] **Step 1: Narrow the default refresh select (~line 3109).**

Change the SELECT from `WHERE language_source IN ('heuristic','api') AND removed_from_list = 0` to a `deep`-aware form:
```python
            if deep:
                where = "WHERE language_source IN ('heuristic','api') AND removed_from_list = 0"
            else:
                where = ("WHERE language_source = 'heuristic' "
                         "AND COALESCE(tmdb_unresolved,0) = 0 AND removed_from_list = 0")
```
`deep` comes from the refresh trigger (add an optional `deep: bool = False` query param / request field to the refresh endpoint and thread it into `_run_metadata_refresh`).

- [ ] **Step 2: Mark unresolved on failed lookup / Other-dir (~line 3137-3164).**

- In the `is_other_typed_dir` skip branch, before `continue`, record the row id to set `tmdb_unresolved=1`.
- In the heuristic branch's `else` (lookup returned no `api_lang`), set `tmdb_unresolved=1` for that row.
Thread these into the pending-update batch (add an `item["tmdb_unresolved"] = 1`) and extend `_write_lang_batch`'s UPDATE builder to set `tmdb_unresolved = ?` when present. (Rows that resolve to api leave it 0.)

- [ ] **Step 3: Recompute is_dubbed for rows the refresh rewrites** — already covered by Task 3 Step 5 for the api-native path.

- [ ] **Step 4: Tests.**

Extend the refresh test (or add one): fixture rows — heuristic-untried (resolves), heuristic-untried (fails → `tmdb_unresolved=1`), heuristic already `tmdb_unresolved=1` (skipped), api row (skipped unless `deep`). Mock `lookup_original_language` to resolve/fail per path. Assert default run touches only untried heuristic rows and sets the flag on failure; `deep=True` also processes api rows. Run in container.

- [ ] **Step 5: Commit**
```bash
git add backend/routes/scan.py backend/tests/test_scan_filters.py
git commit -m "feat(scan): incremental refresh (mark-and-skip) + deep option"
```

---

## Task 7: Ship v0.9.85

- [ ] **Step 1: Full suite green in container.**
- [ ] **Step 2: VERSION + CHANGELOG (one-liner per repo convention).**
```markdown
## [0.9.85] — 2026-07-22

### Added
- **"Dubbed" and "Not API-matched" filters, and incremental metadata refresh.** Dubbed flags items whose TMDB/manual original language has no matching audio track (heuristic-native items excluded, since their native is derived from the audio). Not-API-matched shows items still awaiting a TMDB match. Refresh now processes only untried unmatched items and marks TMDB no-matches so it converges instead of reprocessing the whole library; a `deep` option restores the full re-heal.
```
- [ ] **Step 3: Commit, tag, push, poll CI (authenticated) until build + Tests green.**

---

## Self-Review (completed during authoring)
- **Spec coverage:** migration (T1), `_is_dubbed` (T2), flag maintenance at all 4 write sites (T3), both filters + counts (T4), UI (T5), incremental refresh + `deep` (T6). All spec sections mapped.
- **Placeholder scan:** none — concrete code/edits at each step. The two edits given as prose (scan upsert column-list insertion, refresh `_reclass_item`/`_write_lang_batch` threading) reference exact functions/lines and describe the precise change; they touch existing multi-line SQL better edited in-place than pasted whole.
- **Consistency:** `_is_dubbed(audio_langs, native, source)` signature is used identically in T2/T3/T6; `is_dubbed_flag`, `tmdb_unresolved`, `not_api_matched`, `dubbed`, `not_api_matched_no_tmdb` names are consistent across backend, counts, and UI.
- **Correctness note:** per-file sites (detect, set-language) use read-back recompute to avoid mirroring the SQL CASE; refresh computes inline because source is unambiguously `api`; scan writes 0 because its source is always heuristic.
