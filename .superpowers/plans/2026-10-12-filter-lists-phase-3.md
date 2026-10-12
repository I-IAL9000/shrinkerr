# Filter lists (phase 3) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The Scanner's filter menus get list sections — audio and subtitle language, audio codec, channels, and a Title menu (genre, year, rating, country, network, show status) plus Library (media folder) and Folder (searchable) — each value with its count under the current filter, as in `.superpowers/specs/2026-10-11-scanner-filter-overhaul-design.md` (rollout step 3).

**Architecture:** A list value is a filter token `<facet>:<value>` (value URL-encoded), defined once in `backend/scan_filters.py` as a `Facet` that knows how to read the value(s) of a row (SQL pairs or a Python function) and how to match one value (a SQL condition, or the same Python function). `parse_filter` turns tokens into ordinary `Filter`s (group = the facet key: OR within a list, AND across), so the tree, file lists, Add to Queue, saved views and policies need no change. The filter index stores every value of every indexed facet as a bitset under the key `"<facet>:<value>"` next to the fixed filters' bitsets, patches them like the others, and answers each facet's values with counts (the top 50 per facet in the counts response; all of one facet on request). Folders aren't indexed (tens of thousands of values): they're searched.

**Tech Stack:** Python 3.12, FastAPI, aiosqlite, pytest (`../venv/bin/python -m pytest`); React 18 + TypeScript + Vite (`npm run build`), i18next (en, es).

**Working directory:** the `develop` worktree `/private/tmp/claude-501/-Users-hal9000-Documents-Claude/8e0cfa28-8360-455a-8f76-ad7e08f1f7f1/scratchpad/wt-develop`. Run backend tests as `../venv/bin/python -m pytest <files> -q -p no:warnings > ../t.txt 2>&1; echo EXIT=$?` and read `../t.txt` (never pipe pytest into `tail`). Frontend: `cd frontend && npm run build`. Never `git stash`. Don't import `backend.main` at test-module import time.

---

## Facts the tasks rely on

- `backend/scan_filters.py`: `Filter(id, group, sql, params, py, pre_sql, titles, inputs)` (frozen dataclass; `params` is a zero-argument callable returning a tuple). `parse_filter(text)` → `FilterExpr` (`groups`, `excluded`, `select_sql`, `where_sql`, `post_filter(row, ctx)`, `needs_ctx`, `needs_titles`). `_group_key(f)` = `f.group or f"_{f.id}"`. `_tracks(column, condition)` builds `EXISTS (... json_each(<column>) t WHERE <condition>)`. `title_folder(fp)` and `row_title(row, ctx)` read the Poster grid's metadata from `ctx["title_meta"]` (`folder -> (year, rating, genres, network, status)` today) and the media servers' genres from `ctx["server_genres"]`. Advanced Search filter ids look like `adv0` (no `:`).
- `backend/routes/scan.py`: `_title_metadata(db)` builds `title_meta`; `_build_enrichment_context(db, titles=False)` builds `ctx` (add title metadata with `titles=True`). `GET /api/scan/filter-counts?filter=` returns `{"counts": {...}}` from the index or the SQL engine.
- `backend/filter_index/`: `evaluate.evaluate(db, filters, ctx, ids=None)` → `(alive, members, savings)`; `index.FilterIndex` (`bits: dict[str, int]`, `of`, `patch`, `replace`, `count_all`, `facet_counts`, `save`/`load` — `save` writes every key of `bits`); `inputs.TABLES` (input → tables whose changes bump `change_counters` `fi:<input>`); `service` (`build`, `_apply_changes`, `_context`, `_adhoc_bits`, `counts`, `current`, `_state.lock`).
- `poster_cache` columns: `folder_path, year, rating, genres (comma-joined TMDB names), country (", "-joined names, at most 2), network (one name), status (TMDB status)`.
- Language codes: `backend/disc_metadata.py` has `_ISO639_1_TO_2` (2-letter → 3-letter B form) and `_ISO639_2_T_TO_B` / `_normalize_iso639_2` (T form → B form: `fra`→`fre`, `deu`→`ger`, `zho`→`chi`).
- Frontend: `frontend/src/filterCatalog.json` (quick, categories → sections → options, hidden), `filterCatalog.ts` (`optionLabel`, `chipLabel`, `selectedIn`, `toggleFilter`, `normalizeFilters`, `ALIASES`), `components/FilterBar.tsx`, `components/FilterPanel.tsx`, `pages/ScannerPage.tsx` (fetches `getFilterCounts(filter)` into `facetCounts`), `api.ts`. Language names: `t("settingsMedia:languages.<code>", { defaultValue: code })`.
- Guard tests: `backend/tests/test_filter_catalog.py` (catalog ↔ server filters, strings in en+es), `backend/tests/test_button_styles.py`.

## File structure

- Modify `backend/scan_filters.py` — language/channel/rating helpers, `Facet`, `FACETS`, `INDEXED_FACETS`, `FACETS_VERSION`, `facet_filter`, `facet_token`; `row_title` gains `countries` and `network_name`; `parse_filter` reads facet tokens.
- Modify `backend/routes/scan.py` — `_title_metadata` adds country; `_build_enrichment_context` adds `media_dir_ids`; `filter-counts` returns lists; new `GET /api/scan/facet-values`.
- Modify `backend/filter_index/evaluate.py`, `index.py`, `inputs.py`, `service.py` — facets in the index.
- Create `backend/tests/test_filter_facets.py`, `backend/tests/test_filter_index_facets.py`; modify `backend/tests/filter_index_library.py` (title metadata in the seed), `backend/tests/test_filter_catalog.py`.
- Create `frontend/src/facetLabels.ts`, `frontend/src/components/FilterListSection.tsx`; modify `filterCatalog.json`, `filterCatalog.ts`, `api.ts`, `FilterBar.tsx`, `FilterPanel.tsx`, `ScannerPage.tsx`, `theme.css`, `i18n/locales/{en,es}/scanner.json`.

---

### Task 1: List facets in `scan_filters` (definitions and parsing)

**Files:**
- Modify: `backend/scan_filters.py`
- Modify: `backend/routes/scan.py` (`_title_metadata`, `_build_enrichment_context`)
- Test: `backend/tests/test_filter_facets.py`

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_filter_facets.py`:

```python
"""List facets (v0.10.0, filter overhaul phase 3): a value of a list is a
filter token "<facet>:<value>" that parse_filter turns into a filter, so the
tree, lists, Add to Queue, saved views and policies apply it like any other.
Within a list, values widen; across lists, they narrow."""
import json
from urllib.parse import quote

import aiosqlite
import pytest
import pytest_asyncio

from backend.scan_filters import (FACETS, PY_COLUMNS, channel_band, facet_token, lang_aliases, lang_key,
                                  parse_filter, rating_band)


def test_language_keys_fold_codes():
    assert [lang_key(c) for c in ("fra", "fr", "FRE", " fre ", "deu", "en", "zho")] == \
        ["fre", "fre", "fre", "fre", "ger", "eng", "chi"]
    assert [lang_key(c) for c in ("", None, "und", "UND")] == ["und"] * 4
    assert {"fre", "fra", "fr"} <= set(lang_aliases("fre"))
    assert set(lang_aliases("und")) == {"", "und"}


def test_channel_and_rating_bands():
    assert [channel_band(n) for n in (1, 2, 3, 6, 7, 8, 12)] == ["1", "2", "6", "6", "6", "8", "8"]
    assert [channel_band(n) for n in (0, None, "x")] == [None, None, None]
    assert [rating_band(r) for r in (8.4, 8.0, 7.2, 6.0, 5.5, 3.1, None, 0)] == \
        ["8", "8", "7", "6", "5", "lt5", "none", "none"]


def test_tokens_quote_their_value():
    assert facet_token("network", "Paramount+, HD") == "network:" + quote("Paramount+, HD", safe="")
    assert set(FACETS) == {"alang", "slang", "acodec", "achan", "genre", "year", "rating", "country",
                           "network", "showstatus", "library", "folder"}


def tr(lang, codec="aac", channels=2):
    return {"stream_index": 1, "language": lang, "codec": codec, "channels": channels}


ROWS = {
    # path: (audio tracks, subtitle tracks)
    "/m/Films/Amelie (2001) [tt0211915]/Amelie.mkv": ([tr("fra", "ac3", 6)], [tr("eng")]),
    "/m/Films/Heat (1995) [tt0113277]/Heat.mkv": ([tr("eng", "truehd", 8), tr("fre", "ac3", 6)], []),
    "/m/Films/Mute (2018) [tt1464335]/Mute.mkv": ([tr("", "aac", 1)], [tr("ger")]),
    "/m/TV/The Wire (2002) [tvdb-79126]/Season 1/The Wire S01E01.mkv": ([tr("en", "eac3", 2)], []),
    "/m/Other, Stuff/Home Video.mkv": ([], []),
}
POSTERS = [
    # folder, year, rating, genres, country, network, status
    ("/m/Films/Amelie (2001) [tt0211915]", "2001", 8.3, "Comedy, Romance", "France", None, None),
    ("/m/Films/Heat (1995) [tt0113277]", "1995", 8.3, "Crime, Drama", "United States", None, None),
    ("/m/Films/Mute (2018) [tt1464335]", "2018", 5.4, "Science Fiction", "United Kingdom, Germany", None, None),
    ("/m/TV/The Wire (2002) [tvdb-79126]", "2002", 9.3, "Crime, Drama", "United States", "HBO", "Ended"),
]


@pytest_asyncio.fixture
async def library(test_db):
    async with aiosqlite.connect(test_db) as db:
        for path, (audio, subs) in ROWS.items():
            await db.execute(
                "INSERT INTO scan_results (file_path, file_size, scan_timestamp, audio_tracks_json, "
                "subtitle_tracks_json) VALUES (?, 1, '2026-10-12', ?, ?)", (path, json.dumps(audio), json.dumps(subs)))
        await db.executemany(
            "INSERT INTO poster_cache (folder_path, year, rating, genres, country, network, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)", POSTERS)
        await db.execute("INSERT INTO media_dirs (path, label) VALUES ('/m/Films', 'Films'), ('/m/TV', 'TV'), "
                         "('/m', 'Everything')")
        await db.commit()
        async with db.execute("SELECT id, path FROM media_dirs") as cur:
            dirs = {path: str(i) for i, path in await cur.fetchall()}
    return test_db, dirs


async def matching(db_path, text):
    """The files a filter lists, as the tree endpoint finds them."""
    from backend.routes.scan import _build_enrichment_context
    expr = parse_filter(text)
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        ctx = await _build_enrichment_context(db, titles=expr.needs_titles) if expr.needs_ctx else None
        async with db.execute(
            f"SELECT {PY_COLUMNS}{expr.select_sql} FROM scan_results WHERE removed_from_list = 0{expr.where_sql}",
            [*expr.select_params, *expr.where_params]) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
    return sorted(r["file_path"].rsplit("/", 1)[-1] for r in rows if not expr.needs_ctx or expr.post_filter(r, ctx))


@pytest.mark.asyncio
async def test_track_lists(library):
    db, _ = library
    assert await matching(db, "alang:fre") == ["Amelie.mkv", "Heat.mkv"]          # "fra" and "fre"
    assert await matching(db, "alang:eng") == ["Heat.mkv", "The Wire S01E01.mkv"]  # "eng" and "en"
    assert await matching(db, "alang:und") == ["Mute.mkv"]
    assert await matching(db, "slang:ger") == ["Mute.mkv"]
    assert await matching(db, "acodec:truehd") == ["Heat.mkv"]
    assert await matching(db, "achan:8") == ["Heat.mkv"]                           # its widest track
    assert await matching(db, "achan:6") == ["Amelie.mkv"]
    assert await matching(db, "achan:1,achan:2") == ["Mute.mkv", "The Wire S01E01.mkv"]  # a list widens
    assert await matching(db, "alang:fre,acodec:truehd") == ["Heat.mkv"]                 # lists narrow
    assert await matching(db, "alang:fre,!acodec:truehd") == ["Amelie.mkv"]


@pytest.mark.asyncio
async def test_title_lists(library):
    db, _ = library
    assert await matching(db, "genre:drama") == ["Heat.mkv", "The Wire S01E01.mkv"]
    assert await matching(db, "year:1990s") == ["Heat.mkv"]
    assert await matching(db, "year:2001,year:2018") == ["Amelie.mkv", "Mute.mkv"]
    assert await matching(db, "rating:8") == ["Amelie.mkv", "Heat.mkv", "The Wire S01E01.mkv"]
    assert await matching(db, "rating:none") == ["Home Video.mkv"]
    assert await matching(db, facet_token("country", "United Kingdom")) == ["Mute.mkv"]
    assert await matching(db, "network:HBO") == ["The Wire S01E01.mkv"]
    assert await matching(db, "showstatus:ended") == ["The Wire S01E01.mkv"]


@pytest.mark.asyncio
async def test_library_and_folder(library):
    db, dirs = library
    assert await matching(db, f"library:{dirs['/m/TV']}") == ["The Wire S01E01.mkv"]
    assert await matching(db, f"library:{dirs['/m']}") == ["Home Video.mkv"]  # the most specific folder wins
    assert await matching(db, facet_token("folder", "/m/Other, Stuff")) == ["Home Video.mkv"]  # a comma in it
    assert await matching(db, facet_token("folder", "/m/Films/Heat (1995) [tt0113277]")) == ["Heat.mkv"]


@pytest.mark.asyncio
async def test_unknown_lists_and_empty_values_are_ignored(library):
    db, _ = library
    assert len(await matching(db, "nosuchlist:x")) == len(ROWS)
    assert len(await matching(db, "alang:")) == len(ROWS)
```

- [ ] **Step 2: Run them to see them fail**

Run: `../venv/bin/python -m pytest backend/tests/test_filter_facets.py -q -p no:warnings > ../t.txt 2>&1; echo EXIT=$?`
Expected: EXIT=1 (ImportError: cannot import name 'FACETS').

- [ ] **Step 3: Extend the title metadata and the context**

In `backend/routes/scan.py`, `_title_metadata`: select `country` too and keep it as the sixth item:

```python
        async with db.execute("SELECT folder_path, year, rating, genres, network, status, country FROM poster_cache") as cur:
            for r in await cur.fetchall():
                meta[r["folder_path"]] = (r["year"], r["rating"], r["genres"], r["network"], r["status"], r["country"])
```

(and its docstring: "year, rating, genres, network, status, country").

In `_build_enrichment_context`, right before the line `    # When each title was last watched, for "Not watched in N months" (v0.10.0)`, add:

```python
    # The media folders, most specific first, for the Library list (v0.10.0).
    async with db.execute("SELECT id, path FROM media_dirs") as cur:
        ctx["media_dir_ids"] = sorted(((path.rstrip("/") + "/", str(dir_id)) for dir_id, path in await cur.fetchall()),
                                      key=lambda pair: -len(pair[0]))
```

In `backend/scan_filters.py`, `row_title`: unpack six items and keep the raw network and the countries:

```python
    year, rating, genres, network, status, country = (ctx.get("title_meta") or {}).get(folder) or (None,) * 6
```

and add two keys to the dict it builds:

```python
        "network_name": (network or "").strip(),
        "countries": [c.strip() for c in (country or "").split(",") if c.strip()],
```

- [ ] **Step 4: Add the facets**

In `backend/scan_filters.py`, add `from urllib.parse import quote, unquote` to the imports, and `Any, Iterable` to the `typing` import. After the `FILTERS` / `PY_COLUMNS` definitions (before `# ── Advanced Search conditions`), add:

```python
# ── List facets (v0.10.0, filter overhaul phase 3) ───────────────────────
# A list's value is the token "<facet>:<value>" (the value URL-encoded: a
# network or a folder can hold a comma). parse_filter makes it a Filter whose
# group is the facet: values of one list widen, lists narrow. The filter
# index keeps every value of an indexed facet as a bitset "<facet>:<value>".

FACETS_VERSION = 1  # bump when a facet's normalize / values changes


def lang_key(code: Any) -> str:
    """A track's language as one key: ISO 639-2 B form ("fre", not "fra" or
    "fr"); none, empty or "und": "und"."""
    from backend.disc_metadata import _ISO639_1_TO_2, _normalize_iso639_2
    c = str(code or "").strip().lower()
    if not c or c == "und":
        return "und"
    if len(c) == 2:
        return _ISO639_1_TO_2.get(c) or c
    if len(c) == 3:
        return _normalize_iso639_2(c)
    return c


def lang_aliases(key: str) -> list[str]:
    """Every code lang_key() folds into `key`: what a track can say for it."""
    from backend.disc_metadata import _ISO639_1_TO_2, _ISO639_2_T_TO_B
    if key == "und":
        return ["", "und"]
    out = {key}
    out |= {t for t, b in _ISO639_2_T_TO_B.items() if b == key}
    out |= {two for two, three in _ISO639_1_TO_2.items() if three == key}
    return sorted(out)


_CHANNEL_BANDS = {"1": (1, 1), "2": (2, 2), "6": (3, 7), "8": (8, 1_000)}


def channel_band(n: Any) -> Optional[str]:
    """A file's widest audio track as mono "1", stereo "2", 5.1 "6" (3-7
    channels) or 7.1+ "8"; None without channels."""
    try:
        n = int(n)
    except (TypeError, ValueError):
        return None
    return next((band for band, (lo, hi) in _CHANNEL_BANDS.items() if lo <= n <= hi), None)


def rating_band(rating: Any) -> str:
    """"8" (8 and up), "7", "6", "5" (from that to the next), "lt5", "none"."""
    try:
        r = float(rating)
    except (TypeError, ValueError):
        return "none"
    if r <= 0:
        return "none"
    return "8" if r >= 8 else "7" if r >= 7 else "6" if r >= 6 else "5" if r >= 5 else "lt5"


def _lower_or_none(raw: Any) -> Optional[str]:
    text = str(raw or "").strip().lower()
    return text or None


@dataclass(frozen=True)
class Facet:
    key: str
    # SQL facets: "SELECT <row id>, <raw value>" rows, one per row and value;
    # {where} selects the rows (scan_results columns, unqualified).
    pairs_sql: Optional[str] = None
    normalize: Callable[[Any], Optional[str]] = _lower_or_none  # raw value -> value (None: none)
    cond: Optional[Callable[[str], tuple[str, tuple]]] = None  # value -> (SQL over scan_results, params)
    # Python facets: (row, ctx) -> the row's values (rows have PY_COLUMNS).
    values: Optional[Callable[[dict, dict], Iterable[str]]] = None
    inputs: tuple[str, ...] = ()
    titles: bool = False  # values() reads title metadata (ctx["title_meta"])
    indexed: bool = True  # False: searched, not kept in the filter index (folders)


def _track_pairs(column: str, field: str) -> str:
    return (f"SELECT s.id, json_extract(t.value, '$.{field}') FROM "
            f"(SELECT id, {column} FROM scan_results WHERE {{where}}) s, "
            f"json_each(CASE WHEN json_valid(s.{column}) THEN s.{column} ELSE '[]' END) t")


_MAX_CHANNELS = ("(SELECT MAX(CAST(json_extract(t.value, '$.channels') AS INTEGER)) FROM json_each("
                 "CASE WHEN json_valid(audio_tracks_json) THEN audio_tracks_json ELSE '[]' END) t)")
_ACHAN_PAIRS = ("SELECT s.id, MAX(CAST(json_extract(t.value, '$.channels') AS INTEGER)) FROM "
                "(SELECT id, audio_tracks_json FROM scan_results WHERE {where}) s, "
                "json_each(CASE WHEN json_valid(s.audio_tracks_json) THEN s.audio_tracks_json ELSE '[]' END) t "
                "GROUP BY s.id")


def _lang_cond(column: str) -> Callable[[str], tuple[str, tuple]]:
    def cond(value: str) -> tuple[str, tuple]:
        codes = lang_aliases(value)
        return (_tracks(column, "lower(trim(COALESCE(json_extract(t.value, '$.language'), ''))) "
                                f"IN ({','.join('?' * len(codes))})"), tuple(codes))
    return cond


def _codec_cond(value: str) -> tuple[str, tuple]:
    return _tracks("audio_tracks_json", "lower(trim(COALESCE(json_extract(t.value, '$.codec'), ''))) = ?"), (value,)


def _achan_cond(value: str) -> tuple[str, tuple]:
    lo, hi = _CHANNEL_BANDS.get(value, (-1, -1))
    return f"COALESCE({_MAX_CHANNELS}, 0) BETWEEN ? AND ?", (lo, hi)


def _folder_cond(value: str) -> tuple[str, tuple]:
    prefix = value.rstrip("/") + "/"
    return "substr(file_path, 1, ?) = ?", (len(prefix), prefix)


def _year_values(row: dict, ctx: dict) -> list[str]:
    year = row_title(row, ctx)["year"]
    return [f"{year // 10 * 10}s", str(year)] if year else []


def _library_values(row: dict, ctx: dict) -> list[str]:
    fp = row["file_path"]
    return next(([dir_id] for prefix, dir_id in ctx.get("media_dir_ids") or () if fp.startswith(prefix)), [])


_TITLE_INPUTS = ("titles",)
_FACET_LIST = [
    Facet("alang", pairs_sql=_track_pairs("audio_tracks_json", "language"), normalize=lang_key,
          cond=_lang_cond("audio_tracks_json")),
    Facet("slang", pairs_sql=_track_pairs("subtitle_tracks_json", "language"), normalize=lang_key,
          cond=_lang_cond("subtitle_tracks_json")),
    Facet("acodec", pairs_sql=_track_pairs("audio_tracks_json", "codec"), cond=_codec_cond),
    Facet("achan", pairs_sql=_ACHAN_PAIRS, normalize=channel_band, cond=_achan_cond),
    Facet("genre", values=lambda r, c: row_title(r, c)["genres"], inputs=("titles", "plex"), titles=True),
    Facet("year", values=_year_values, inputs=_TITLE_INPUTS, titles=True),
    Facet("rating", values=lambda r, c: [rating_band(row_title(r, c)["rating"])], inputs=_TITLE_INPUTS, titles=True),
    Facet("country", values=lambda r, c: row_title(r, c)["countries"], inputs=_TITLE_INPUTS, titles=True),
    Facet("network", values=lambda r, c: [n for n in [row_title(r, c)["network_name"]] if n],
          inputs=_TITLE_INPUTS, titles=True),
    Facet("showstatus", values=lambda r, c: [s for s in [row_title(r, c)["status"]] if s],
          inputs=_TITLE_INPUTS, titles=True),
    Facet("library", values=_library_values, inputs=("media_dirs",)),
    Facet("folder", cond=_folder_cond, indexed=False),
]
FACETS: dict[str, Facet] = {f.key: f for f in _FACET_LIST}
INDEXED_FACETS: list[Facet] = [f for f in _FACET_LIST if f.indexed]


def facet_token(key: str, value: str) -> str:
    """The filter token for a list's value."""
    return f"{key}:{quote(value, safe='')}"


def facet_filter(key: str, value: str) -> Optional[Filter]:
    """The filter a list's value is (its group: the list); None for an
    unknown list or an empty value."""
    facet = FACETS.get(key)
    if facet is None or not value:
        return None
    fid = f"{key}:{value}"
    if facet.cond:
        sql, params = facet.cond(value)
        return Filter(fid, key, sql=sql, params=(lambda p=params: p), inputs=facet.inputs)
    values = facet.values
    return Filter(fid, key, py=lambda r, c, v=value: v in set(values(r, c)), titles=facet.titles,
                  inputs=facet.inputs)
```

(`_tracks` and `row_title` are defined earlier in the module; `Optional`, `Callable`, `dataclass` are already imported.)

- [ ] **Step 5: Parse the tokens**

In `parse_filter`, replace

```python
        neg = token.startswith("!")
        f = FILTERS.get(token[1:] if neg else token)
```

with

```python
        neg = token.startswith("!")
        name = token[1:] if neg else token
        f = FILTERS.get(name)
        if f is None and ":" in name:  # a list's value (v0.10.0)
            key, _, raw = name.partition(":")
            f = facet_filter(key, unquote(raw))
```

- [ ] **Step 6: Run the tests**

Run: `../venv/bin/python -m pytest backend/tests/test_filter_facets.py backend/tests/test_filter_spec.py backend/tests/test_filter_regroup.py -q -p no:warnings > ../t.txt 2>&1; echo EXIT=$?`
Expected: EXIT=0. If `test_title_lists` fails on `genre:drama`, check that `row_title` lowercases genres (it does: `found = {g.strip().lower() ...}`).

- [ ] **Step 7: Commit**

```bash
git add backend/scan_filters.py backend/routes/scan.py backend/tests/test_filter_facets.py
git commit -m "feat(filters): list facets: languages, codecs, channels, title, library, folder

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: List values in the filter index

**Files:**
- Modify: `backend/filter_index/evaluate.py`, `backend/filter_index/index.py`
- Modify: `backend/tests/filter_index_library.py` (title metadata in the seed)
- Test: `backend/tests/test_filter_index_facets.py`

- [ ] **Step 1: Seed title metadata in the test library**

In `backend/tests/filter_index_library.py`, `seed()`, before the final `await db.commit()`, add:

```python
        # Title metadata for the Title lists (v0.10.0): a few titles' folders.
        from backend.scan_filters import title_folder
        folders = sorted({title_folder(path[i]) for i in ids[::9]})[:8]
        meta = [("1994", 8.4, "Crime, Drama", "United States", "HBO", "Ended"),
                ("2003", 6.1, "Comedy", "United Kingdom, Ireland", "BBC One", "Returning Series"),
                ("2019", 4.2, "Science Fiction", "Germany", "", ""),
                ("1987", None, "Drama", "France", "", "")]
        await db.executemany(
            "INSERT OR REPLACE INTO poster_cache (folder_path, year, rating, genres, country, network, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)", [(f, *meta[n % len(meta)]) for n, f in enumerate(folders)])
```

and add facet expressions to `EXPRESSIONS`: `"alang:fre"`, `"alang:eng,alang:fre"`, `"acodec:truehd,!x264"`, `"achan:8,res_1080p"`, `"genre:drama,year:1990s"`, `"rating:8,type_tv"`, `"network:HBO"`, `"!slang:eng"`, `"library:1"`, `"folder:%2Fdemo%2FTV"`.

- [ ] **Step 2: Write the failing tests**

Create `backend/tests/test_filter_index_facets.py`:

```python
"""The filter index keeps every value of the indexed lists as a bitset
"<facet>:<value>", patched like the other filters, and answers each list's
values with their counts under a filter: what the SQL engine would list."""
import aiosqlite
import pytest

from backend.filter_index.bits import ids_of
from backend.filter_index.evaluate import evaluate
from backend.filter_index.index import FilterIndex
from backend.scan_filters import _FILTER_LIST, INDEXED_FACETS, PY_COLUMNS, parse_filter
from backend.tests.filter_index_library import EXPRESSIONS, mutate, seed


async def _index(db_path) -> FilterIndex:
    from backend.routes.scan import _build_enrichment_context
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        ctx = await _build_enrichment_context(db, titles=True)
        alive, members, savings = await evaluate(db, _FILTER_LIST, ctx, facets=INDEXED_FACETS)
    return FilterIndex.of(alive, members, savings or {}, version="v", db_id="d")


async def _rows(db_path, text) -> set[int]:
    """The ids parse_filter lists (the tree's way)."""
    from backend.routes.scan import _build_enrichment_context
    expr = parse_filter(text)
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        ctx = await _build_enrichment_context(db, titles=True)
        async with db.execute(f"SELECT id, {PY_COLUMNS}{expr.select_sql} FROM scan_results "
                              f"WHERE removed_from_list = 0{expr.where_sql}",
                              [*expr.select_params, *expr.where_params]) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
    return {r["id"] for r in rows if expr.post_filter(r, ctx)}


@pytest.mark.asyncio
async def test_every_value_is_the_rows_its_token_lists(test_db):
    await seed(test_db)
    index = await _index(test_db)
    values = [k for k in index.bits if ":" in k]
    assert {k.split(":")[0] for k in values} == {f.key for f in INDEXED_FACETS}
    for key in values:
        from backend.scan_filters import facet_token
        facet, _, value = key.partition(":")
        assert set(ids_of(index.bits[key])) == await _rows(test_db, facet_token(facet, value)), key


@pytest.mark.asyncio
async def test_a_lists_values_and_counts_under_a_filter(test_db):
    await seed(test_db)
    index = await _index(test_db)
    lists = index.facet_values([], [], selected=frozenset({"alang:xxx"}))
    alang = dict(lists["alang"])
    assert alang["eng"] == len(await _rows(test_db, "alang:eng")) and alang["xxx"] == 0  # selected: kept at 0
    # Under a filter: what adding the value would give, its own list set aside.
    expr = parse_filter("alang:fre,res_1080p")
    groups = [(g[0].group or f"_{g[0].id}", 0) for g in expr.groups]
    bits = {"alang": index.bits["alang:fre"], "resolution": index.bits["res_1080p"]}
    lists = index.facet_values([(k, bits[k]) for k, _ in groups], [])
    assert dict(lists["alang"])["eng"] == len(await _rows(test_db, "alang:eng,res_1080p"))
    assert dict(lists["acodec"]).get("truehd", 0) == len(await _rows(test_db, "alang:fre,res_1080p,acodec:truehd"))
    assert all(n > 0 for values in lists.values() for _, n in values)  # zeros aren't listed unless selected


@pytest.mark.asyncio
async def test_patching_rows_adds_and_drops_values(test_db):
    await seed(test_db)
    index = await _index(test_db)
    async with aiosqlite.connect(test_db) as db:
        await db.execute("UPDATE scan_results SET audio_tracks_json = "
                         "'[{\"language\": \"isl\", \"codec\": \"aac\", \"channels\": 2}]' "
                         "WHERE id = (SELECT MIN(id) FROM scan_results)")
        await db.commit()
        async with db.execute("SELECT MIN(id) FROM scan_results") as cur:
            row_id = (await cur.fetchone())[0]
    from backend.routes.scan import _build_enrichment_context
    async with aiosqlite.connect(test_db) as db:
        db.row_factory = aiosqlite.Row
        ctx = await _build_enrichment_context(db, titles=True)
        alive, members, savings = await evaluate(db, _FILTER_LIST, ctx, ids=[row_id], facets=INDEXED_FACETS)
    index.patch([row_id], alive, members, savings or {})
    assert ids_of(index.bits["alang:isl"]) == [row_id]
    rebuilt = await _index(test_db)
    assert {k: v for k, v in index.bits.items() if ":" in k} == {k: v for k, v in rebuilt.bits.items() if ":" in k}


@pytest.mark.asyncio
async def test_count_all_is_the_fixed_filters_only(test_db):
    await seed(test_db)
    index = await _index(test_db)
    assert not [k for k in index.count_all() if ":" in k]


@pytest.mark.asyncio
async def test_facet_expressions_count_like_the_sql_engine(test_db):
    """The index's counts under list values equal the SQL engine's."""
    from backend.tests.filter_index_library import sql_counts
    await seed(test_db)
    await mutate(test_db)
    index = await _index(test_db)
    from backend.filter_index import service
    for text in [e for e in EXPRESSIONS if ":" in e and not e.startswith("adv:")]:
        expr = parse_filter(text)
        groups = []
        for group in expr.groups:
            bits = 0
            for f in group:
                bits |= index.bits.get(f.id) if f.id in index.bits else await service._adhoc_bits(f)
            groups.append((group[0].group or f"_{group[0].id}", bits))
        excluded = [(f.id, index.bits[f.id] if f.id in index.bits else await service._adhoc_bits(f))
                    for f in expr.excluded]
        assert index.facet_counts(groups, excluded) == await sql_counts(test_db, text), text
```

- [ ] **Step 3: Run them to see them fail**

Run: `../venv/bin/python -m pytest backend/tests/test_filter_index_facets.py -q -p no:warnings > ../t.txt 2>&1; echo EXIT=$?`
Expected: EXIT=1 (`evaluate() got an unexpected keyword argument 'facets'`).

- [ ] **Step 4: Evaluate facets**

In `backend/filter_index/evaluate.py`: import `Facet` (`from backend.scan_filters import LISTED, PY_COLUMNS, Facet, Filter`), give `evaluate` a `facets: list[Facet] = ()` keyword (after `ids`), document it in the docstring ("and for `facets`, every value as `<facet>:<value>`"), and:

After the SQL filters' `spread` (before `py_filters = ...`), add:

```python
    for facet in [f for f in facets if f.pairs_sql]:
        async with db.execute(facet.pairs_sql.format(where=where), where_params) as cur:
            pairs = await cur.fetchall()

        def spread_pairs(pairs=pairs, facet=facet) -> None:
            seen = set()
            for row_id, raw in pairs:
                value = facet.normalize(raw)
                if value and (row_id, value) not in seen:
                    seen.add((row_id, value))
                    members.setdefault(f"{facet.key}:{value}", []).append(row_id)
        await asyncio.to_thread(spread_pairs)
```

Replace

```python
    py_filters = [f for f in filters if f.py]
    if not py_filters:
        return alive, members, None
```

with

```python
    py_filters = [f for f in filters if f.py]
    py_facets = [f for f in facets if f.values]
    if not py_filters and not py_facets:
        return alive, members, None
```

and in `judge()`'s loop, after the `for f in py_filters:` block, add:

```python
            for facet in py_facets:
                for value in set(facet.values(r, ctx)):
                    members.setdefault(f"{facet.key}:{value}", []).append(r["id"])
```

- [ ] **Step 5: Keep, patch and count the values**

In `backend/filter_index/index.py`:

Imports: `from collections import defaultdict` and, from `backend.scan_filters`, also `FACETS_VERSION, INDEXED_FACETS`.

`definitions_version()`: include the facets:

```python
    text = repr([FILTERS_VERSION, FACETS_VERSION, LISTED, PY_COLUMNS]
                + [(f.id, f.group, f.sql, f.pre_sql, f.py is not None, f.inputs) for f in _FILTER_LIST]
                + [(f.key, f.pairs_sql, f.values is not None, f.inputs) for f in INDEXED_FACETS])
```

Add a module constant `TOP_VALUES = 50  # a list's values in the counts response`.

`of()`: after `index.bits = {...}` add

```python
        index.bits.update((key, from_ids(rows)) for key, rows in members.items() if ":" in key)
```

`patch()`: after the `for fid, bits in list(self.bits.items()):` loop, add

```python
        for fid in members.keys() - self.bits.keys():  # a list's value first seen in these rows
            self.bits[fid] = from_ids(members[fid])
        for fid in [k for k, bits in self.bits.items() if ":" in k and not bits]:
            del self.bits[fid]  # a value no row has any more
```

Add after `replace()`:

```python
    def replace_facets(self, keys: Iterable[str], members: dict[str, list[int]]) -> None:
        """Take these lists' values over the whole library (an input of theirs changed)."""
        prefixes = tuple(f"{key}:" for key in keys)
        for fid in [k for k in self.bits if k.startswith(prefixes)]:
            del self.bits[fid]
        self.bits.update((k, from_ids(rows)) for k, rows in members.items() if k.startswith(prefixes) and rows)
```

`count_all()`: only the fixed filters:

```python
        counts = {"all": self.alive.bit_count()}
        counts.update((f.id, self.bits[f.id].bit_count()) for f in _FILTER_LIST)
        return counts
```

Factor the constraint set-up of `facet_counts` into a helper and add `facet_values`:

```python
    def _bases(self, groups: list[tuple[str, int]], excluded: list[tuple[str, int]]):
        """(group key -> constraint number, filter id -> constraint number,
        base(skip): the rows meeting every constraint but those in `skip`)."""
        constraints = [bits for _, bits in groups] + [self.alive & ~bits for _, bits in excluded]
        group_at = {key: n for n, (key, _) in enumerate(groups)}
        excluded_at = {fid: len(groups) + n for n, (fid, _) in enumerate(excluded)}
        cache: dict[frozenset, int] = {}

        def base(skip: frozenset) -> int:
            if skip not in cache:
                rows = self.alive
                for n, bits in enumerate(constraints):
                    if n not in skip:
                        rows &= bits
                cache[skip] = rows
            return cache[skip]
        return group_at, excluded_at, base

    def facet_counts(self, groups: list[tuple[str, int]], excluded: list[tuple[str, int]]) -> dict[str, int]:
        """(docstring as before)"""
        group_at, excluded_at, base = self._bases(groups, excluded)
        counts = {}
        for f in _FILTER_LIST:
            skip = frozenset(n for n in (group_at.get(_group_key(f)), excluded_at.get(f.id)) if n is not None)
            counts[f.id] = (base(skip) & self.bits[f.id]).bit_count()
        counts["matching"] = base(frozenset()).bit_count()
        return counts

    def facet_values(self, groups: list[tuple[str, int]], excluded: list[tuple[str, int]],
                     selected: frozenset = frozenset(), facet: Optional[str] = None,
                     top: Optional[int] = TOP_VALUES) -> dict[str, list[tuple[str, int]]]:
        """Each indexed list's values (or only `facet`'s) with what adding
        each would give: the files meeting every constraint but its own
        list's and its own exclusion, that have it. The most first (then by
        value), `top` of them (None: all), without zeros; values in
        `selected` ("<facet>:<value>") always, at 0 when no file has them."""
        group_at, excluded_at, base = self._bases(groups, excluded)
        by_facet: dict[str, list[tuple[str, int]]] = defaultdict(list)
        for fid, bits in self.bits.items():
            key, sep, value = fid.partition(":")
            if sep and (facet is None or key == facet):
                skip = frozenset(n for n in (group_at.get(key), excluded_at.get(fid)) if n is not None)
                by_facet[key].append((value, (base(skip) & bits).bit_count()))
        out = {}
        for key in [f.key for f in INDEXED_FACETS if facet is None or f.key == facet]:
            rows = sorted(by_facet.get(key, []), key=lambda vc: (-vc[1], vc[0]))
            kept = [vc for vc in (rows if top is None else rows[:top]) if vc[1] > 0]
            have = {v for v, _ in kept}
            counts = dict(rows)
            for token in sorted(selected):
                k, _, v = token.partition(":")
                if k == key and v not in have:
                    kept.append((v, counts.get(v, 0)))
            out[key] = kept
        return out
```

(Keep `facet_counts`'s existing docstring.)

- [ ] **Step 6: Run the tests**

Run: `../venv/bin/python -m pytest backend/tests/test_filter_index_facets.py backend/tests/test_filter_index_counts.py backend/tests/test_filter_index_evaluate.py backend/tests/test_filter_index_file.py -q -p no:warnings > ../t.txt 2>&1; echo EXIT=$?`
Expected: EXIT=0. `test_filter_index_file.py` must still pass: `save`/`load` write and read every `bits` key, list values included.

- [ ] **Step 7: Commit**

```bash
git add backend/filter_index/evaluate.py backend/filter_index/index.py backend/tests/filter_index_library.py backend/tests/test_filter_index_facets.py
git commit -m "feat(filters): the filter index keeps the lists' values and counts them

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: The service and the endpoints

**Files:**
- Modify: `backend/filter_index/inputs.py`, `backend/filter_index/service.py`, `backend/routes/scan.py`
- Test: `backend/tests/test_filter_index_facets.py` (append), `backend/tests/test_filter_index_inputs.py` (if it lists the inputs)

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_filter_index_facets.py`:

```python
@pytest.fixture
def service_env(test_db, tmp_path, monkeypatch):
    from backend.filter_index import service
    service.reset()
    monkeypatch.setattr(service, "index_path", lambda: str(tmp_path / "filter-index.bin"))
    yield service
    service.reset()


@pytest.mark.asyncio
async def test_the_counts_endpoint_returns_the_lists(service_env, test_db):
    from backend.routes.scan import get_facet_values, get_filter_counts
    await seed(test_db)
    service_env._state.index = await service_env.build()
    body = await get_filter_counts("alang:fre")
    assert body["counts"]["matching"] == len(await _rows(test_db, "alang:fre"))
    langs = {v["value"]: v["count"] for v in body["facets"]["alang"]}
    assert langs["fre"] == len(await _rows(test_db, "alang:fre"))  # its own list set aside
    assert len(body["facets"]["alang"]) <= 50
    library = body["facets"]["library"]
    assert all(v.get("label") for v in library)  # media folders carry their label
    every = (await get_facet_values("genre", "all", ""))["values"]
    assert every and [v["count"] for v in every] == sorted((v["count"] for v in every), reverse=True)
    folders = (await get_facet_values("folder", "all", "night"))["values"]
    assert folders and all("night" in v["value"].lower() for v in folders)
    narrowed = (await get_facet_values("folder", "res_4k", "night"))["values"]
    assert sum(v["count"] for v in narrowed) <= sum(v["count"] for v in folders)


@pytest.mark.asyncio
async def test_a_title_change_reaches_the_lists(service_env, test_db):
    await seed(test_db)
    service_env._state.index = await service_env.build()
    async with aiosqlite.connect(test_db) as db:
        await db.execute("UPDATE poster_cache SET network = 'Channel 4' WHERE network = 'HBO'")
        await db.commit()
    counts, lists = await service_env.answer(parse_filter("all"))
    networks = {v for v, _ in lists["network"]}
    assert "Channel 4" in networks and "HBO" not in networks


@pytest.mark.asyncio
async def test_lists_wait_for_the_index(service_env, test_db, monkeypatch):
    from backend.routes.scan import get_filter_counts
    await seed(test_db)
    monkeypatch.setattr(service_env, "_start_rebuild", lambda: None)  # not built yet
    body = await get_filter_counts("all", facets_only=True)
    assert body == {"counts": None, "facets": None}
```

- [ ] **Step 2: Run them to see them fail**

Run: `../venv/bin/python -m pytest backend/tests/test_filter_index_facets.py -q -p no:warnings > ../t.txt 2>&1; echo EXIT=$?`
Expected: EXIT=1 (`cannot import name 'get_facet_values'`).

- [ ] **Step 3: Title metadata is an input**

In `backend/filter_index/inputs.py`, `TABLES`, add:

```python
    "titles": [("poster_cache", "year, rating, genres, country, network, status, folder_path", None)],  # the Title lists
```

If `backend/tests/test_filter_index_inputs.py` asserts the set of inputs, add `"titles"` there.

- [ ] **Step 4: The service**

In `backend/filter_index/service.py`:

Imports: `INDEXED_FACETS, Facet, title_folder` from `backend.scan_filters`; `ids_of` from `backend.filter_index.bits`; `TOP_VALUES` from `backend.filter_index.index`.

`_context()`: build the context with titles (the Title lists read them):

```python
        _state.ctx, _state.ctx_key = await _build_enrichment_context(db, titles=True), key
```

`build()`: `alive, members, savings = await evaluate(db, _FILTER_LIST, ctx, facets=INDEXED_FACETS)`.

`_apply_changes()`: compute dirty lists next to the dirty filters, evaluate them with the changed rows, and replace them:

```python
        dirty = [f for f in _FILTER_LIST if set(f.inputs) & moved]
        dirty_facets = [f for f in INDEXED_FACETS if set(f.inputs) & moved]
        need_ctx = changed or any(f.py for f in dirty) or any(f.values for f in dirty_facets)
        ctx = await _context(db, counters) if need_ctx else None
        if changed:
            alive, members, savings = await evaluate(db, _FILTER_LIST, ctx, ids=changed, facets=INDEXED_FACETS)
            index.patch(changed, alive, members, savings)
        if dirty or dirty_facets:
            _, members, _ = await evaluate(db, dirty, ctx, facets=dirty_facets)
            if dirty:
                index.replace(dirty, members)
            if dirty_facets:
                index.replace_facets([f.key for f in dirty_facets], members)
```

(replacing the existing `dirty = ...`, `ctx = ...`, `if changed:` and `if dirty:` lines).

Replace `counts()` with two helpers, `answer()`, and `counts()` as `answer()`'s first half:

```python
async def _adhoc_for(expr: FilterExpr, index: FilterIndex) -> dict[str, int]:
    """The rows of `expr`'s filters the index doesn't keep (Advanced Search
    conditions, folders), evaluated now — outside the lock: a refresh
    shouldn't wait on a Python condition."""
    adhoc: dict[str, int] = {}
    for f in [f for group in expr.groups for f in group] + expr.excluded:
        if f.id not in index.bits and f.id not in adhoc:
            adhoc[f.id] = await _adhoc_bits(f)
    return adhoc


def _constraints(index: FilterIndex, expr: FilterExpr, adhoc: dict[str, int], skip_group: Optional[str] = None
                 ) -> tuple[list[tuple[str, int]], list[tuple[str, int]]]:
    """(groups, excluded) as FilterIndex.facet_counts takes them; without
    the group `skip_group`."""
    def bits_of(f: Filter) -> int:
        return adhoc[f.id] if f.id in adhoc else index.bits[f.id]
    groups = []
    for group in expr.groups:
        key = _group_key(group[0])
        if key == skip_group:
            continue
        bits = 0
        for f in group:
            bits |= bits_of(f)
        groups.append((key, bits))
    return groups, [(f.id, bits_of(f)) for f in expr.excluded]


async def answer(expr: FilterExpr, facet: Optional[str] = None, top: Optional[int] = TOP_VALUES
                 ) -> Optional[tuple[dict[str, int], dict[str, list[tuple[str, int]]]]]:
    """(the pills' counts under `expr`, as count_all with no filter or
    facet_counts; each indexed list's values with counts, or only `facet`'s,
    `top` of them); None when the SQL engine should answer (current()) or
    on an error."""
    try:
        index = await current()
        if index is None:
            return None
        adhoc = await _adhoc_for(expr, index)
        selected = frozenset(f.id for f in [f for g in expr.groups for f in g] + expr.excluded if ":" in f.id)
        async with _state.lock:
            index = _state.index
            groups, excluded = _constraints(index, expr, adhoc)
            counts = index.count_all() if expr.is_all else index.facet_counts(groups, excluded)
            return counts, index.facet_values(groups, excluded, selected=selected, facet=facet, top=top)
    except Exception as exc:
        _log_error("Counting with the filter index", exc)
        return None


async def counts(expr: FilterExpr) -> Optional[dict[str, int]]:
    """The pills' counts under `expr` (answer()'s first half)."""
    result = await answer(expr)
    return result[0] if result else None
```

Add the folder search (folders aren't indexed: tens of thousands of values):

```python
_folders: dict = {"seq": None, "rows": {}}  # title folder -> its listed ids, while the index's seq holds


async def folder_values(expr: FilterExpr, q: str, limit: int = 50) -> Optional[list[tuple[str, int]]]:
    """Title folders whose path contains `q` (any case), with how many files
    each would list under `expr` (its own Folder choice set aside): the most
    first, `limit` of them; None as for answer()."""
    try:
        index = await current()
        if index is None:
            return None
        if _folders["seq"] != index.seq or not _folders["rows"]:
            db = await database.connect_db()
            try:
                async with db.execute("SELECT id, file_path FROM scan_results WHERE removed_from_list = 0") as cur:
                    pairs = await cur.fetchall()
            finally:
                await db.close()

            def by_folder() -> dict[str, list[int]]:
                out: dict[str, list[int]] = {}
                for row_id, fp in pairs:
                    out.setdefault(title_folder(fp), []).append(row_id)
                return out
            _folders["rows"], _folders["seq"] = await asyncio.to_thread(by_folder), index.seq
        adhoc = await _adhoc_for(expr, index)
        async with _state.lock:
            idx = _state.index
            groups, excluded = _constraints(idx, expr, adhoc, skip_group="folder")
            _, _, base = idx._bases(groups, excluded)
            matching = set(ids_of(base(frozenset())))
        needle = q.strip().casefold()

        def search() -> list[tuple[str, int]]:
            hits = []
            for folder, ids in _folders["rows"].items():
                if needle in folder.casefold():
                    n = sum(1 for i in ids if i in matching)
                    if n:
                        hits.append((folder, n))
            hits.sort(key=lambda fc: (-fc[1], fc[0]))
            return hits[:limit]
        return await asyncio.to_thread(search)
    except Exception as exc:
        _log_error("Searching folders", exc)
        return None
```

Add `FilterIndex` to the `backend.filter_index.index` import.

- [ ] **Step 5: The endpoints**

In `backend/routes/scan.py`, replace `get_filter_counts` and add `get_facet_values` after it:

```python
async def _library_labels() -> dict[str, str]:
    db = await aiosqlite.connect(DB_PATH)
    try:
        async with db.execute("SELECT id, path, label FROM media_dirs") as cur:
            return {str(i): (label or path) for i, path, label in await cur.fetchall()}
    finally:
        await db.close()


def _values_json(facet: str, values: list[tuple[str, int]], labels: dict[str, str]) -> list[dict]:
    out = []
    for value, count in values:
        item = {"value": value, "count": count}
        if facet == "library":
            item["label"] = labels.get(value, value)
        out.append(item)
    return out


@router.get("/filter-counts")
async def get_filter_counts(filter: str = "all", facets_only: bool = False):
    """The pills' counts under the active filter: what clicking each would
    give (v0.10.0). With no filter, the library-wide counts. "facets": each
    list's top values under it, from the filter index (None until it's
    ready). `facets_only`: don't fall back to the SQL engine for the counts."""
    expr = parse_filter(filter)
    from backend.filter_index import service as filter_index
    fast = await filter_index.answer(expr)
    if fast is not None:
        counts, lists = fast
        labels = await _library_labels()
        return {"counts": counts, "facets": {k: _values_json(k, v, labels) for k, v in lists.items()}}
    if facets_only:
        return {"counts": None, "facets": None}
    # The index's first build is still running: the SQL engine (~12 s at 166K files).
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    try:
        ctx = await _build_enrichment_context(db, titles=expr.needs_titles)
        if expr.is_all:
            counts, _ = await count_all(db, ctx)
            return {"counts": counts, "facets": None}
        return {"counts": await facet_counts(db, ctx, expr), "facets": None}
    finally:
        await db.close()


@router.get("/facet-values")
async def get_facet_values(facet: str, filter: str = "all", q: str = ""):
    """One list's values under the active filter, the most first: every
    value of an indexed list ("Show all", the search box), or the title
    folders whose name contains `q`. "values": None until the filter index
    is ready."""
    from backend.filter_index import service as filter_index
    from backend.scan_filters import FACETS
    if facet not in FACETS:
        raise ApiError(404, "No such list.", code="scan.noSuchList")
    expr = parse_filter(filter)
    if facet == "folder":
        hits = await filter_index.folder_values(expr, q)
        return {"values": None if hits is None else [{"value": f, "count": n} for f, n in hits]}
    result = await filter_index.answer(expr, facet=facet, top=None)
    if result is None:
        return {"values": None}
    labels = await _library_labels() if facet == "library" else {}
    return {"values": _values_json(facet, result[1].get(facet, []), labels)}
```

Add the server message `scan.noSuchList` to the server catalogs the ApiError contract tests check (search the repo for an existing code such as `scan.notInList` and add `scan.noSuchList` next to it in the same files, in English and Spanish: "No such list." / "No existe esa lista.").

- [ ] **Step 6: Run the tests**

Run: `../venv/bin/python -m pytest backend/tests/test_filter_index_facets.py backend/tests/test_filter_index_service.py backend/tests/test_filter_index_endpoints.py backend/tests/test_filter_index_inputs.py backend/tests/test_filter_index_triggers.py -q -p no:warnings > ../t.txt 2>&1; echo EXIT=$?`
Expected: EXIT=0. `test_filter_index_endpoints.py` may assert `{"counts": ...}` exactly: update those assertions to read `body["counts"]` (the response now also has `"facets"`).

Then the whole suite: `../venv/bin/python -m pytest backend/tests -q -p no:warnings > ../t.txt 2>&1; echo EXIT=$?` → EXIT=0.

- [ ] **Step 7: Commit**

```bash
git add backend/filter_index/inputs.py backend/filter_index/service.py backend/routes/scan.py backend/tests/
git commit -m "feat(filters): the counts carry each list's values; a list's values and folder search

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: The catalog, labels and data in the frontend

**Files:**
- Modify: `frontend/src/filterCatalog.json`, `frontend/src/filterCatalog.ts`, `frontend/src/api.ts`, `frontend/src/pages/ScannerPage.tsx`, `frontend/src/i18n/locales/en/scanner.json`, `frontend/src/i18n/locales/es/scanner.json`
- Create: `frontend/src/facetLabels.ts`
- Modify (test): `backend/tests/test_filter_catalog.py`

- [ ] **Step 1: The guard test learns about lists and aliases**

In `backend/tests/test_filter_catalog.py`:

- `_options()` yields only sections with `"options"`; add `_lists()` yielding `(category, section)` for sections with `"facet"`.
- New tests:

```python
def test_list_sections_are_the_servers_lists():
    from backend.scan_filters import FACETS
    lists = [section for _, section in _lists()]
    assert sorted(s["facet"] for s in lists) == sorted(FACETS)  # every list shown once
    for s in lists:
        assert s["key"] == s["facet"]  # the section is the list's group
    groups = {f.group for f in FILTERS.values() if f.group}
    assert not groups & set(FACETS)  # no fixed filter shares a list's group


def test_hidden_aliases_point_at_something_shown():
    shown = {o["key"] for _, _, o in _options()} | {s["facet"] for _, s in _lists()}
    for hidden in CATALOG["hidden"]:
        alias = hidden.get("alias")
        if alias:
            assert alias.split(":")[0] in shown, alias
```

- `test_no_hidden_filter_shares_a_sections_group`: skip hidden entries with an `"alias"` (they're read as their alias in the bar).
- `test_every_label_exists_in_english_and_spanish`: also collect `section.get("chipLabelKey")` for every section.
- Add a test that the facet value strings exist in both languages:

```python
def test_list_value_labels_exist_in_english_and_spanish():
    keys = [f"scanner:facets.channels.{v}" for v in ("1", "2", "6", "8")]
    keys += [f"scanner:facets.rating.{v}" for v in ("8", "7", "6", "5", "lt5", "none")]
    keys += [f"scanner:facets.status.{v}" for v in ("returning", "ended", "canceled", "in_production", "planned", "pilot")]
    keys += ["scanner:facets.unknownLanguage", "scanner:facets.search", "scanner:facets.searchFolders",
             "scanner:facets.showAll", "scanner:facets.showFewer", "scanner:facets.noMatches"]
    for lang in ("en", "es"):
        for key in keys:
            assert _lookup(lang, key), (lang, key)
```

(`_lookup` is the module's existing helper that resolves a `ns:dotted.key` in a locale; if it's named differently, use that one.)

- [ ] **Step 2: Run it to see it fail**

Run: `../venv/bin/python -m pytest backend/tests/test_filter_catalog.py -q -p no:warnings > ../t.txt 2>&1; echo EXIT=$?`
Expected: EXIT=1 (no list sections yet).

- [ ] **Step 3: The catalog**

In `frontend/src/filterCatalog.json`:

- Audio: sections become `alang`, `acodec`, `achan`, then the existing `audio` section without `audio_71`:

```json
{"key": "alang", "labelKey": "scanner:filterBar.sections.language", "chipLabelKey": "scanner:filterBar.sections.audioLanguage", "facet": "alang"},
{"key": "acodec", "labelKey": "scanner:filterBar.sections.codec", "chipLabelKey": "scanner:filterBar.sections.audioCodec", "facet": "acodec"},
{"key": "achan", "labelKey": "scanner:filterBar.sections.channels", "facet": "achan"},
```

- Subtitles: first section `{"key": "slang", "labelKey": "scanner:filterBar.sections.language", "chipLabelKey": "scanner:filterBar.sections.subtitleLanguage", "facet": "slang"}`.
- A new category after `language`:

```json
{"key": "title", "labelKey": "scanner:filterBar.categories.title", "sections": [
  {"key": "genre", "labelKey": "scanner:filterBar.sections.genre", "facet": "genre"},
  {"key": "year", "labelKey": "scanner:filterBar.sections.year", "facet": "year"},
  {"key": "rating", "labelKey": "scanner:filterBar.sections.rating", "facet": "rating", "prefix": true},
  {"key": "country", "labelKey": "scanner:filterBar.sections.country", "facet": "country"},
  {"key": "network", "labelKey": "scanner:filterBar.sections.network", "facet": "network"},
  {"key": "showstatus", "labelKey": "scanner:filterBar.sections.showStatus", "facet": "showstatus", "prefix": true}
]}
```

- Library: first two sections `{"key": "library", "labelKey": "scanner:filterBar.sections.library", "facet": "library"}` and `{"key": "folder", "labelKey": "scanner:filterBar.sections.folder", "facet": "folder"}`.
- `hidden`: add `{"key": "audio_71", "label": "7.1", "alias": "achan:8"}`; give `large_files` `"alias": "size_large"`.

- [ ] **Step 4: The strings**

`frontend/src/i18n/locales/en/scanner.json` → `filterBar.categories.title`: "Title"; `filterBar.sections`: `language` "Language", `codec` (exists: keep), `channels` "Channels", `audioLanguage` "Audio language", `audioCodec` "Audio codec", `subtitleLanguage` "Subtitle language", `genre` "Genre", `year` "Year", `rating` "Rating", `country` "Country", `network` "Network", `showStatus` "Show status", `library` "Library", `folder` "Folder". A new top-level `facets` object:

```json
"facets": {
  "unknownLanguage": "Unknown",
  "channels": {"1": "Mono", "2": "Stereo", "6": "5.1", "8": "7.1+"},
  "rating": {"8": "8 and up", "7": "7–8", "6": "6–7", "5": "5–6", "lt5": "Under 5", "none": "No rating"},
  "status": {"returning": "Returning", "ended": "Ended", "canceled": "Canceled", "in_production": "In production", "planned": "Planned", "pilot": "Pilot"},
  "search": "Search…",
  "searchFolders": "Search folders…",
  "showAll": "Show all",
  "showFewer": "Show fewer",
  "noMatches": "No matches"
}
```

Spanish (`es/scanner.json`): "Título"; sections `language` "Idioma", `channels` "Canales", `audioLanguage` "Idioma del audio", `audioCodec` "Códec de audio", `subtitleLanguage` "Idioma de los subtítulos", `genre` "Género", `year` "Año", `rating` "Valoración", `country` "País", `network` "Cadena", `showStatus` "Estado de la serie", `library` "Biblioteca", `folder` "Carpeta"; `facets`: "Desconocido"; channels "Mono", "Estéreo", "5.1", "7.1+"; rating "8 o más", "7–8", "6–7", "5–6", "Menos de 5", "Sin valoración"; status "En emisión", "Finalizada", "Cancelada", "En producción", "Planeada", "Piloto"; "Buscar…", "Buscar carpetas…", "Mostrar todo", "Mostrar menos", "Sin resultados".

- [ ] **Step 5: Labels**

Create `frontend/src/facetLabels.ts`:

```ts
// A list value's label in the filter bar (v0.10.0): a language's name, a
// codec's usual spelling, a band's words; years, countries and networks as
// they are; a media folder's label from the server; a folder's last part.
type T = (key: string, options?: Record<string, unknown>) => string;

const CODECS: Record<string, string> = {
  aac: "AAC", ac3: "AC-3", eac3: "E-AC-3", dts: "DTS", truehd: "TrueHD", flac: "FLAC", opus: "Opus",
  mp3: "MP3", mp2: "MP2", vorbis: "Vorbis", alac: "ALAC",
};

export function facetValueLabel(facet: string, value: string, t: T, label?: string): string {
  switch (facet) {
    case "alang":
    case "slang":
      return value === "und" ? t("scanner:facets.unknownLanguage") : t(`settingsMedia:languages.${value}`, { defaultValue: value });
    case "acodec":
      return CODECS[value] ?? (value.startsWith("pcm") ? "PCM" : value.toUpperCase());
    case "achan":
      return t(`scanner:facets.channels.${value}`, { defaultValue: value });
    case "genre":
      return value.replace(/(^|\s|-)\p{L}/gu, c => c.toUpperCase());
    case "rating":
      return t(`scanner:facets.rating.${value}`, { defaultValue: value });
    case "showstatus":
      return t(`scanner:facets.status.${value}`, { defaultValue: value });
    case "library":
      return label ?? value;
    case "folder": {
      const parts = value.split("/").filter(Boolean);
      const last = parts.pop() ?? value;
      // "Season 2" says little alone: with its show.
      return /^(season|specials)\b/i.test(last) && parts.length ? `${parts.pop()} · ${last}` : last;
    }
    default:
      return value;
  }
}
```

- [ ] **Step 6: Catalog helpers**

In `frontend/src/filterCatalog.ts`:

- `FilterSection`: `options?: FilterOption[]; facet?: string; chipLabelKey?: string; prefix?: boolean`.
- Build `PLACES` from sections that have `options` (`for (const option of section.options ?? [])`), and a `LISTS = new Map<string, { section: FilterSection; category: FilterCategory }>()` from sections that have `facet`.
- `ALIASES` from the catalog: `const ALIASES: Record<string, string> = Object.fromEntries((catalog.hidden as (FilterOption & { alias?: string })[]).filter(h => h.alias).map(h => [h.key, h.alias as string]));` (replacing the hard-coded object).
- Export:

```ts
/** A list value's token parts, or null: "alang:fre" -> { facet: "alang", value: "fre" }. */
export function parseListToken(key: string): { facet: string; value: string } | null {
  const at = key.indexOf(":");
  if (at < 0 || key.startsWith("adv:")) return null;
  const facet = key.slice(0, at);
  if (!LISTS.has(facet)) return null;
  try {
    return { facet, value: decodeURIComponent(key.slice(at + 1)) };
  } catch {
    return { facet, value: key.slice(at + 1) };
  }
}

export function listToken(facet: string, value: string): string {
  return `${facet}:${encodeURIComponent(value)}`;
}

export function listSection(facet: string): FilterSection | undefined {
  return LISTS.get(facet)?.section;
}
```

- `optionLabel(key, t, label?)`: first `const list = parseListToken(key); if (list) return facetValueLabel(list.facet, list.value, t, label);`.
- `chipLabel(token, t, label?)`: after the `adv:` case, for list tokens use the section's `chipLabelKey ?? labelKey` as the section label and `facetValueLabel(...)` as the label (same "Section: Label" / "Section: not Label" strings; no "contains" shortcut for lists).
- `selectedIn(category, active)`: for sections with `facet`, push every active token starting with `${facet}:` (not excluded) or `!${facet}:` (excluded, key without the `!`), in the order they appear in `active`.

Import `facetValueLabel` from `./facetLabels`.

- [ ] **Step 7: Data**

In `frontend/src/api.ts`:

```ts
export interface FacetValue { value: string; count: number; label?: string }
export type FacetLists = Record<string, FacetValue[]>;
export const getFilterCounts = (filter: string, signal?: AbortSignal, facetsOnly = false) =>
  apiFetch<{ counts: Record<string, number> | null; facets: FacetLists | null }>(
    `/scan/filter-counts?filter=${encodeURIComponent(filter)}${facetsOnly ? "&facets_only=true" : ""}`, { signal });
export const getFacetValues = (facet: string, filter: string, q = "", signal?: AbortSignal) =>
  apiFetch<{ values: FacetValue[] | null }>(
    `/scan/facet-values?facet=${encodeURIComponent(facet)}&filter=${encodeURIComponent(filter)}&q=${encodeURIComponent(q)}`, { signal });
```

In `frontend/src/pages/ScannerPage.tsx`:

- `facetCounts` state becomes `{ filter: string; counts: Record<string, number>; lists: FacetLists | null } | null`. The effect fetches for every filter, `"all"` included (`getFilterCounts(filter, ctrl.signal, filter === "all")`), and stores `{ filter, counts: r.counts ?? {}, lists: r.facets }`; for `"all"` the pills' counts keep coming from `serverStats` as today (`facets` stays null for "all": keep the existing `facets` derivation but read `facetCounts.counts` only when `filter !== "all"`). The catch keeps the previous lists when they were for this filter (as it does for counts).
- Pass `lists={facetCounts?.filter === filter ? facetCounts.lists : null}` and `filter={filter}` to `FilterBar`.
- The address: write `?filter=${filter.replace(/%/g, "%25").replace(/[&#+]/g, c => encodeURIComponent(c))}` so a list value's own encoding survives (`URLSearchParams.get` decodes once).

- [ ] **Step 8: Build and run the guard test**

Run: `cd frontend && npm run build; cd ..` → exit 0 (fix any type errors in `FilterBar`/`FilterPanel` from the optional `options`: use `section.options ?? []` where they iterate options; the list sections render in Task 5).
Run: `../venv/bin/python -m pytest backend/tests/test_filter_catalog.py -q -p no:warnings > ../t.txt 2>&1; echo EXIT=$?` → EXIT=0.

- [ ] **Step 9: Commit**

```bash
git add frontend/src backend/tests/test_filter_catalog.py
git commit -m "feat(scanner): the catalog's lists, the Title menu, labels and the lists' data

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: List sections in the menus

**Files:**
- Create: `frontend/src/components/FilterListSection.tsx`
- Modify: `frontend/src/components/FilterPanel.tsx`, `frontend/src/components/FilterBar.tsx`, `frontend/src/theme.css`

- [ ] **Step 1: The list section**

Create `frontend/src/components/FilterListSection.tsx`:

```tsx
import { useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { getFacetValues, type FacetValue } from "../api";
import { listToken, parseListToken, type FilterSection } from "../filterCatalog";
import { facetValueLabel } from "../facetLabels";
import { fmtNum } from "../fmt";

const SHOWN = 8;  // values before "Show all"

/**
 * A list in a filter menu (v0.10.0): a search box, the values ticked or
 * excluded, then the most common values under the current filter, each a
 * checkbox (Alt-click excludes), its count and ⊘. "Show all" and the search
 * read every value from the server; folders are searched, not listed.
 */
export default function FilterListSection({ section, values, active, filter, onToggle }: {
  section: FilterSection;
  values: FacetValue[] | null | undefined;  // the top values (null: counting)
  active: string[];
  filter: string;
  onToggle: (key: string, exclude: boolean) => void;
}) {
  const { t } = useTranslation(["scanner", "settingsMedia"]);
  const facet = section.facet as string;
  const isFolder = facet === "folder";
  const [query, setQuery] = useState("");
  const [all, setAll] = useState<FacetValue[] | null>(null);
  const [expanded, setExpanded] = useState(false);
  const [searched, setSearched] = useState<FacetValue[] | null>(null);
  const timer = useRef<number>();

  // Every value, for "Show all" and the search box (not folders).
  useEffect(() => {
    if (isFolder || (!expanded && !query)) return;
    const ctrl = new AbortController();
    getFacetValues(facet, filter, "", ctrl.signal).then(r => setAll(r.values)).catch(() => {});
    return () => ctrl.abort();
  }, [facet, filter, isFolder, expanded, query !== ""]);

  // Folders: the server searches.
  useEffect(() => {
    if (!isFolder) return;
    window.clearTimeout(timer.current);
    if (!query.trim()) { setSearched(null); return; }
    const ctrl = new AbortController();
    timer.current = window.setTimeout(() => {
      getFacetValues(facet, filter, query.trim(), ctrl.signal).then(r => setSearched(r.values)).catch(() => {});
    }, 250);
    return () => { window.clearTimeout(timer.current); ctrl.abort(); };
  }, [facet, filter, isFolder, query]);

  const selected = active.flatMap(token => {
    const excluded = token.startsWith("!");
    const parsed = parseListToken(excluded ? token.slice(1) : token);
    return parsed && parsed.facet === facet ? [{ value: parsed.value, excluded }] : [];
  });
  const label = (v: FacetValue) => facetValueLabel(facet, v.value, t, v.label);
  const pool = isFolder ? (searched ?? []) : (query || expanded) && all ? all : (values ?? []);
  const needle = query.trim().toLocaleLowerCase();
  const shown = useMemo(() => {
    let rows = pool.filter(v => !selected.some(s => s.value === v.value));
    if (needle && !isFolder) rows = rows.filter(v => label(v).toLocaleLowerCase().includes(needle));
    if (facet === "alang" || facet === "slang") {
      rows = [...rows.filter(v => v.value === "und"), ...rows.filter(v => v.value !== "und")];
    }
    return expanded || needle ? rows : rows.slice(0, SHOWN);
  }, [pool, needle, expanded, selected.map(s => s.value).join("|")]);

  const countOf = (value: string) => (all ?? values ?? searched ?? []).find(v => v.value === value)?.count;
  const row = (value: string, count: number | undefined, excluded: boolean, included: boolean, display: string) => {
    const key = listToken(facet, value);
    const empty = count === 0 && !included && !excluded;
    return (
      <div key={key} className={`filter-option${excluded ? " excluded" : ""}${empty ? " empty" : ""}`}>
        <label className="filter-option-main">
          <input type="checkbox" checked={included} disabled={empty}
            onChange={e => onToggle(key, (e.nativeEvent as MouseEvent).altKey === true)} />
          <span className="filter-option-label" title={isFolder ? value : undefined}>{display}</span>
          {excluded && <span className="sr-only">{t("scanner:filterBar.excluded")}</span>}
          <span className="filter-option-count">{count == null ? "…" : fmtNum(count)}</span>
        </label>
        <button type="button" className="filter-option-exclude" aria-pressed={excluded} disabled={empty}
          aria-label={t("scanner:filterBar.exclude", { label: display })} title={t("scanner:filterBar.exclude", { label: display })}
          onClick={() => onToggle(key, true)}>⊘</button>
      </div>
    );
  };

  return (
    <div className="filter-list">
      <input type="search" className="filter-list-search" value={query} onChange={e => setQuery(e.target.value)}
        placeholder={t(isFolder ? "scanner:facets.searchFolders" : "scanner:facets.search")}
        aria-label={t(isFolder ? "scanner:facets.searchFolders" : "scanner:facets.search")} />
      {selected.map(s => row(s.value, countOf(s.value), s.excluded, !s.excluded,
        facetValueLabel(facet, s.value, t, (all ?? values ?? []).find(v => v.value === s.value)?.label)))}
      {values == null && !isFolder ? <p className="filter-section-hint">{t("scanner:filterBar.counting")}</p> : null}
      {shown.map(v => row(v.value, v.count, false, false, label(v)))}
      {needle && shown.length === 0 && (isFolder ? searched : all) && (
        <p className="filter-section-hint">{t("scanner:facets.noMatches")}</p>
      )}
      {!isFolder && !needle && (values?.length ?? 0) > SHOWN && (
        <button type="button" className="filter-list-more" onClick={() => setExpanded(x => !x)}>
          {t(expanded ? "scanner:facets.showFewer" : "scanner:facets.showAll")}
        </button>
      )}
    </div>
  );
}
```

- [ ] **Step 2: Panels render lists**

In `frontend/src/components/FilterPanel.tsx`:

- Props gain `lists?: FacetLists | null` and `filter: string`.
- In the sections map: if `section.facet`, render inside the `<fieldset>` (after the legend and hint) `<FilterListSection section={section} values={lists ? lists[section.facet] ?? [] : null} active={active} filter={filter} onToggle={onToggle} />` instead of the options; otherwise the options as today (`(section.options ?? []).map(...)`).
- `FOCUSABLE` already includes `input:not(:disabled)`, so the search boxes join the Tab trap.

In `frontend/src/components/FilterBar.tsx`:

- Props gain `lists?: FacetLists | null` and `filter: string`; pass both to `FilterPanel`.
- `hasFiles` for a category: a list section has files when its values list is non-empty: `category.sections.some(s => s.facet ? (lists?.[s.facet]?.length ?? 1) > 0 : (s.options ?? []).some(o => (counts?.[o.key] ?? 1) > 0))`.
- Chips and summaries: pass the library label when known: `const libraryLabel = (token: string) => { const p = parseListToken(token.replace(/^!/, "")); return p?.facet === "library" ? lists?.library?.find(v => v.value === p.value)?.label : undefined; };` and call `chipLabel(token, t, libraryLabel(token))` / `optionLabel(s.key, t, libraryLabel(s.key))`. The summary's section prefix rule (`section?.prefix`) must find list sections too: look the section up with `listSection(parseListToken(s.key)?.facet ?? "")` when `parseListToken(s.key)` is non-null.

- [ ] **Step 3: Styles**

In `frontend/src/theme.css`, after `.filter-option-exclude:disabled`:

```css
/* Lists (v0.10.0): a search box over the values, and "Show all". */
.filter-list { display: flex; flex-direction: column; gap: 2px; }
.filter-list-search { margin: 2px 0 6px; padding: 5px 8px; font-size: 12px; border: 1px solid var(--border);
  border-radius: var(--radius-sm); background: var(--bg-primary); color: var(--text-secondary); }
.filter-list .filter-option-label { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.filter-list-more { align-self: flex-start; margin-top: 4px; padding: 2px 0; border: 0; background: none;
  color: var(--accent-text); font-size: 12px; cursor: pointer; }
.filter-list-more:hover { text-decoration: underline; }
```

Long lists scroll inside the panel: give `.filter-list` `max-height: 260px; overflow-y: auto;` when expanded — add the class `expanded` on the container when `expanded` is true and `.filter-list.expanded { max-height: 260px; overflow-y: auto; }`.

- [ ] **Step 4: Build, lint, and check in the browser**

Run: `cd frontend && npm run build && npx eslint src/components/FilterListSection.tsx src/components/FilterPanel.tsx src/components/FilterBar.tsx src/facetLabels.ts src/filterCatalog.ts; cd ..` → no errors.
Run the button guard: `../venv/bin/python -m pytest backend/tests/test_button_styles.py backend/tests/test_filter_catalog.py -q -p no:warnings > ../t.txt 2>&1; echo EXIT=$?` → EXIT=0.

Browser (the demo server `shrinkerr-demo-develop`, port 6692, serves `frontend/dist`; restart it after backend changes): open the Scanner and check that:
- Audio shows Language (English, French …, Unknown first when present), Codec and Channels with counts; ticking French adds a chip "Audio language: French", and the other values' counts change to what adding them would give.
- Title shows Genre, Year (decades and years), Rating, Country, Network, Show status (the demo has no posters: lists may be empty — greyed button; check with the e2e server, or by inserting `poster_cache` rows in the demo's database copy).
- Library shows the media folders by label; Folder finds a show by typing part of its name.
- "Show all" lists every value; the search box narrows; Escape and Tab still work; on a phone width (375px) the lists fit.
- A value with a comma or "+" (a folder) survives a reload (the address keeps it).

- [ ] **Step 5: Commit**

```bash
git add frontend/src
git commit -m "feat(scanner): list sections in the filter menus: search, top values, show all

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: Changelog, test plan, and wrap-up

**Files:**
- Modify: `CHANGELOG.md`, `.superpowers/specs/2026-10-11-scanner-filter-overhaul-design.md` (rollout note: step 3 done)

- [ ] **Step 1: Changelog**

In `CHANGELOG.md`, [Unreleased] → Changed, extend the "A simpler filter bar" entry: "Lists with counts: audio and subtitle language, audio codec and channels, and a Title menu (genre, year, rating, country, network, show status); Library adds the media folders and a folder search."

- [ ] **Step 2: Full suite and build**

Run: `../venv/bin/python -m pytest backend/tests -q -p no:warnings > ../t.txt 2>&1; echo EXIT=$?` → EXIT=0. `cd frontend && npm run build` → exit 0.

- [ ] **Step 3: Commit**

```bash
git add CHANGELOG.md .superpowers/specs/2026-10-11-scanner-filter-overhaul-design.md
git commit -m "docs: changelog for the filter lists

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

## Self-review notes

- Spec coverage (step 3): languages (audio, subtitle), codecs, channels (audio_71 hidden, aliased to `achan:8`), genre, year (decades + years), rating bands, country, network, show status, library, folder; long lists (search, top values, show all; folders by search); counts response with top values per list + selected; `GET /api/scan/facet-values`; `<facet>:<value>` tokens URL-encoded; the index keeps list values and patches them; title metadata is an input (`titles`). Actor, director, studio and all Plex labels are step 4.
- Types: `Facet`, `FACETS`, `INDEXED_FACETS`, `facet_filter`, `facet_token`, `lang_key`, `lang_aliases`, `channel_band`, `rating_band` (Task 1) are used with those names in Tasks 2–3; `FilterIndex.facet_values` / `replace_facets` / `_bases` (Task 2) in Task 3; `service.answer` / `folder_values` (Task 3) in the endpoints; `FacetValue`, `FacetLists`, `getFacetValues`, `parseListToken`, `listToken`, `listSection`, `facetValueLabel` (Task 4) in Task 5.
