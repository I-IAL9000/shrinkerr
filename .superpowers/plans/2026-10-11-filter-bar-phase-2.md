# Filter Bar (Phase 2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the Scanner's ~90 filter pills with four quick pills, a menu per topic (sections of checkboxes with counts, ⊘ to exclude, zero counts greyed out) and the active filters as chips — with one rule: within a section, ticking more shows more; across sections, files must match all.

**Architecture:** The bar's layout lives in one JSON file (`frontend/src/filterCatalog.json`: quick pills, categories → sections → options) that the React components render and a backend test checks against the server's filters. Every section is exactly one server filter group (`backend/scan_filters.py`), which is what makes "ticking more in a section shows more" true; six sections need the server's groups changed. The counts come from the existing `/api/scan/filter-counts` (served by the phase 1 filter index); no new API.

**Tech Stack:** React 19 + TypeScript (Vite, `npm run build` = `tsc -b && vite build`), react-i18next (en + es), plain CSS in `frontend/src/theme.css`; Python 3.12 / pytest for the server filters and the catalog guard.

**Spec:** `.superpowers/specs/2026-10-11-scanner-filter-overhaul-design.md` — "UI", "Semantics" (regrouping, ids), "Rollout" step 2.

**Scope of this phase vs the spec's table:** eight menus — Status, Video, Audio, Subtitles, Language, Library, Watched, Direct play. Not yet (phase 3, list facets): the Title menu, the language / codec / channel lists in Audio and Subtitles, Library's Library and Folder lists. Moved forward from phase 3: the bitrate tiers (three SQL filters) so Video's Bitrate section is complete. `audio_71` ("7.1") stays visible in Audio until phase 3's Channels list replaces it.

## Conventions for every task

- Work in the `develop` worktree. Python: `../venv/bin/python` from the worktree root.
- One test file: `../venv/bin/python -m pytest backend/tests/<file> -q -p no:warnings`
- Full suite (never pipe pytest into `tail`): `../venv/bin/python -m pytest backend/tests -q -p no:warnings > ../suite.txt 2>&1; echo "EXIT=$?"; grep -E "passed|failed" ../suite.txt | tail -1`
- Frontend check: `cd frontend && npm run build > ../../build.txt 2>&1; echo "EXIT=$?"; grep -iE "error" ../../build.txt | head` (not `tsc --noEmit`; it missed errors before).
- No hex colours in `.ts`/`.tsx` (a test enforces it): use theme.css variables (`--accent`, `--accent-bg`, `--accent-text`, `--danger`, `--text-muted`, `--border`, `--bg-card`, `--bg-tertiary`…).
- Every new user-facing string in both `frontend/src/i18n/locales/en/scanner.json` and `.../es/scanner.json`; Spanish adds a `_many` plural wherever English has `_other`; placeholders match.
- Comments and docstrings: plain, short, say why; match the surrounding code.
- Commit messages end with: `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>` (or your model).

## File structure

| File | Responsibility |
|---|---|
| `backend/scan_filters.py` (modify) | the regrouped filters; the three bitrate tiers |
| `backend/tests/test_filter_regroup.py` (create) | sections widen; bitrate tiers |
| `backend/tests/test_filter_spec.py` (modify) | drop the old FilterBar-parsing guard (replaced by the catalog guard) |
| `frontend/src/filterCatalog.json` (create) | the bar's layout: quick pills, categories, sections (= server groups), options, hidden ids |
| `frontend/src/filterCatalog.ts` (create) | typed access + pure helpers: labels, chips, selection, toggling |
| `backend/tests/test_filter_catalog.py` (create) | the catalog vs the server's filters and both languages |
| `frontend/src/i18n/locales/{en,es}/scanner.json` (modify) | `filterBar` strings and new option labels |
| `frontend/src/components/FilterPanel.tsx` (create) | one category's panel: sections, option rows, keyboard |
| `frontend/src/components/FilterBar.tsx` (rewrite) | quick pills, category buttons, chips |
| `frontend/src/pages/ScannerPage.tsx` (modify) | the bar always shown; the old toggle and pill grid removed |
| `frontend/src/theme.css` (modify) | the bar's, panels' and chips' styles; phones |

---

### Task 1: Regroup the server's filters; add the bitrate tiers

**Files:**
- Modify: `backend/scan_filters.py` (`_FILTER_LIST`, ~lines 404–545)
- Modify: `backend/tests/test_filter_spec.py` (delete `test_the_filter_bar_groups_are_the_server_groups`, ~line 408 to the end of that function)
- Create: `backend/tests/test_filter_regroup.py`

- [ ] **Step 1: Write the failing test**

`backend/tests/test_filter_regroup.py`:

```python
"""Phase 2 of the filter overhaul: each section of the new filter bar is one
server group, so ticking more in a section shows more (Queue, Health,
Bitrate, Language, Added and File options used to narrow each other); and
the bitrate tiers."""
import aiosqlite
import pytest
import pytest_asyncio

import backend.routes.scan as scan_route
from backend.scan_filters import FILTERS, HIGH_BITRATE, LOW_BITRATE
from backend.tests import filter_index_library as library_data

SECTIONS = {
    "queue": ["needs_conversion", "queued", "converted", "ignored"],
    "health": ["corrupt", "health_never", "health_warnings", "health_stale"],
    "bitrate": ["bitrate_low", "bitrate_medium", "bitrate_high", "bloated"],
    "language": ["unknown_language", "missing_language", "dubbed", "not_api_matched"],
    "added": ["new", "added_7d", "added_30d", "added_90d"],
    "file": ["disc_iso", "extras", "duplicates", "hardlinked"],
}


@pytest_asyncio.fixture
async def library(test_db, monkeypatch):
    monkeypatch.setattr(scan_route, "DB_PATH", test_db)
    await library_data.seed(test_db)
    return test_db


async def paths(text: str) -> set[str]:
    return set(await scan_route._paths_matching(text))


def test_the_regrouped_sections():
    for group, ids in SECTIONS.items():
        assert [FILTERS[i].group for i in ids] == [group] * len(ids), group
    # Older ids, no longer shown, keep their meaning (their own groups).
    for fid in ("low_bitrate", "high_bitrate", "recent", "large_files"):
        assert FILTERS[fid].group is None, fid


@pytest.mark.asyncio
async def test_ticking_more_in_a_section_shows_more(library):
    for group, ids in SECTIONS.items():
        first, second = await paths(ids[0]), await paths(ids[1])
        assert await paths(f"{ids[0]},{ids[1]}") == first | second, group


@pytest.mark.asyncio
async def test_the_bitrate_tiers(library):
    async with aiosqlite.connect(library) as db:
        async with db.execute("SELECT file_path, file_size * 8.0 / duration FROM scan_results "
                              "WHERE removed_from_list = 0 AND duration > 0") as cur:
            rates = dict(await cur.fetchall())
    low, medium, high = await paths("bitrate_low"), await paths("bitrate_medium"), await paths("bitrate_high")
    assert low == {p for p, r in rates.items() if r < LOW_BITRATE}
    assert medium == {p for p, r in rates.items() if LOW_BITRATE <= r <= HIGH_BITRATE}
    assert high == {p for p, r in rates.items() if r > HIGH_BITRATE}
    assert low and medium and high  # the test library has each
    # Needs conversion + High is what the older High bitrate pill showed.
    assert await paths("needs_conversion,bitrate_high") == await paths("high_bitrate")
```

- [ ] **Step 2: Run it to verify it fails**

Run: `../venv/bin/python -m pytest backend/tests/test_filter_regroup.py -q -p no:warnings`
Expected: FAIL — `test_the_regrouped_sections` (`KeyError: 'bitrate_low'` or groups `None`), and the others.

- [ ] **Step 3: Regroup and add the tiers**

In `backend/scan_filters.py` `_FILTER_LIST`, change only the group (the second positional argument) of these entries, keeping everything else; the result must read:

```python
    # Status. Each section of the filter bar is one group (v0.10.0): ticking
    # more in a section shows more; different sections narrow.
    Filter("new", "added", sql="new_detected_at > ?", params=_new_cutoff, inputs=("time",)),
    Filter("recent", sql="file_mtime > ?", params=_recent_cutoff, inputs=("time",)),
    Filter("needs_conversion", "queue", pre_sql=_NEEDS, inputs=("ignored", "rules", "plex"),
           py=lambda r, c: bool(r.get("needs_conversion")) and not row_low_bitrate(r) and not row_ignored(r, c)),
```

```python
    Filter("bloated", "bitrate", sql=f"{_HAS_BPP} AND {_BPP} > (CASE WHEN {_HEVC} OR {_AV1} OR {_VP9} THEN 0.15 ELSE 0.25 END)"),
    # Bitrate tiers (v0.10.0): the file's overall bitrate, whatever its codec.
    Filter("bitrate_low", "bitrate", sql=f"duration > 0 AND file_size * 8.0 / duration < {LOW_BITRATE}"),
    Filter("bitrate_medium", "bitrate",
           sql=f"duration > 0 AND file_size * 8.0 / duration BETWEEN {LOW_BITRATE} AND {HIGH_BITRATE}"),
    Filter("bitrate_high", "bitrate", sql=f"duration > 0 AND file_size * 8.0 / duration > {HIGH_BITRATE}"),
```

```python
    Filter("unknown_language", "language", sql="COALESCE(has_und_tracks_flag, 0) = 1"),
    Filter("disc_iso", "file", sql="disc_type IS NOT NULL"),
    Filter("ignored", "queue", py=lambda r, c: row_ignored(r, c), inputs=("ignored", "rules", "plex")),
    Filter("duplicates", "file", sql="COALESCE(dup_count, 0) > 1"),
    Filter("corrupt", "health", sql="(COALESCE(probe_status, 'ok') != 'ok' OR COALESCE(health_status, '') = 'corrupt')"),
    Filter("converted", "queue", py=row_converted, inputs=("jobs",)),
    Filter("queued", "queue", py=lambda r, c: r["file_path"] in c["queued_paths"], inputs=("jobs",)),
    Filter("extras", "file", sql=_EXTRAS),
    # Still hardlinked elsewhere (seeding): converting frees nothing (v0.10.0)
    Filter("hardlinked", "file", sql="COALESCE(link_count, 1) > 1"),
```

Leave `low_bitrate`, `high_bitrate`, `recent`, `large_files` and `sub_cleanup` without a group. Update the comment that says "Status: each its own group, so combining them narrows." (it's replaced by the comment above). Put the three bitrate tiers right after `bloated`.

- [ ] **Step 4: Remove the old bar guard**

In `backend/tests/test_filter_spec.py`, delete the whole `test_the_filter_bar_groups_are_the_server_groups` function (it parses the old `FilterBar.tsx`, which Task 4 replaces; Task 2 adds `test_filter_catalog.py` in its place). Remove imports only it used, if any.

- [ ] **Step 5: Run the tests**

Run: `../venv/bin/python -m pytest backend/tests/test_filter_regroup.py -q -p no:warnings` — Expected: `3 passed`.
If `test_the_bitrate_tiers` finds a tier empty in the test library, add an update to `backend/tests/filter_index_library.py` `seed()` that gives one listed row that bitrate (and keep `test_every_filter_matches_something_in_the_test_library` passing); don't loosen the assertion.
Run the full suite. Expected: `EXIT=0`. A failure in a test that combined two of the regrouped filters expecting them to narrow is expected only if that test encodes the old rule — update its expectation to the new rule and say so in your report; anything else is a bug.

- [ ] **Step 6: Mutation-check**

Each must fail a test (undo after): put `needs_conversion` back in its own group (drop `"queue"`); change `bitrate_medium`'s `BETWEEN` to `> {LOW_BITRATE} AND ... < {HIGH_BITRATE}` (boundaries); give `bitrate_high` no group.

- [ ] **Step 7: Commit**

```bash
git add backend/scan_filters.py backend/tests/test_filter_regroup.py backend/tests/test_filter_spec.py
git commit -m "feat(filters): each filter bar section is one group; bitrate tiers

Queue, Health, Language, Added and File options (and Big for its
resolution with the new Low / Medium / High bitrate tiers) now widen
within their section instead of narrowing each other. The older ids keep
their meaning.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: The bar's layout, its strings, and the guard

**Files:**
- Create: `frontend/src/filterCatalog.json`
- Create: `frontend/src/filterCatalog.ts`
- Modify: `frontend/src/i18n/locales/en/scanner.json`, `frontend/src/i18n/locales/es/scanner.json`
- Create: `backend/tests/test_filter_catalog.py`

- [ ] **Step 1: Write the failing test**

`backend/tests/test_filter_catalog.py`:

```python
"""The Scanner's filter bar layout (frontend/src/filterCatalog.json) against
the server's filters: every section is one server group, so ticking more in
a section shows more and different sections narrow; every filter is shown
once or listed as hidden; every label exists in English and Spanish."""
import json
import re
from pathlib import Path

from backend.scan_filters import FILTERS

ROOT = Path(__file__).resolve().parents[2]
CATALOG = json.loads((ROOT / "frontend/src/filterCatalog.json").read_text())
LOCALES = ROOT / "frontend/src/i18n/locales"
_PLACEHOLDER = re.compile(r"\{\{\s*(\w+)")


def _options():
    for category in CATALOG["categories"]:
        for section in category["sections"]:
            for option in section["options"]:
                yield category, section, option


def _lookup(lang: str, key: str):
    ns, _, path = key.partition(":")
    node = json.loads((LOCALES / lang / f"{ns}.json").read_text())
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def test_each_section_is_one_server_group():
    for _, section, option in _options():
        f = FILTERS[option["key"]]
        assert (f.group or f"_{f.id}") == section["key"], (section["key"], option["key"])


def test_every_filter_is_shown_once_or_hidden():
    shown = [option["key"] for _, _, option in _options()]
    hidden = [h["key"] for h in CATALOG["hidden"]]
    assert len(shown) == len(set(shown))
    assert sorted(shown + hidden) == sorted(FILTERS)
    assert set(CATALOG["quick"]) <= set(shown)


def test_every_label_exists_in_english_and_spanish():
    keys = set()
    for category, section, option in _options():
        keys |= {category["labelKey"], section["labelKey"], section.get("hintKey"), option.get("labelKey")}
        assert option.get("label") or option.get("labelKey"), option["key"]
    keys |= {h["labelKey"] for h in CATALOG["hidden"]}
    keys.discard(None)
    for lang in ("en", "es"):
        missing = [k for k in sorted(keys) if not isinstance(_lookup(lang, k), str)]
        assert not missing, (lang, missing)


def test_the_bar_strings_match_in_english_and_spanish():
    def leaves(node, prefix=""):
        for k, v in node.items():
            if isinstance(v, dict):
                yield from leaves(v, f"{prefix}{k}.")
            else:
                yield f"{prefix}{k}", v
    en = dict(leaves(_lookup("en", "scanner:filterBar")))
    es = dict(leaves(_lookup("es", "scanner:filterBar")))
    expected = set(en) | {k[: -len("_other")] + "_many" for k in en if k.endswith("_other")}
    assert set(es) == expected
    for key, text in en.items():
        assert set(_PLACEHOLDER.findall(text)) == set(_PLACEHOLDER.findall(es[key])), key
```

- [ ] **Step 2: Run it to verify it fails**

Run: `../venv/bin/python -m pytest backend/tests/test_filter_catalog.py -q -p no:warnings`
Expected: FAIL at collection — `FileNotFoundError: ... filterCatalog.json`.

- [ ] **Step 3: The layout**

`frontend/src/filterCatalog.json` (a section's `key` is the server group its options share — `"_<id>"` for a filter with no group):

```json
{
  "quick": ["needs_conversion", "audio_cleanup", "sub_cleanup", "new"],
  "categories": [
    {"key": "status", "labelKey": "scanner:filterBar.categories.status", "sections": [
      {"key": "queue", "labelKey": "scanner:filterBar.sections.queue", "options": [
        {"key": "needs_conversion", "labelKey": "scanner:filters.needsConversion"},
        {"key": "queued", "labelKey": "scanner:filters.queued"},
        {"key": "converted", "labelKey": "scanner:filters.converted"},
        {"key": "ignored", "labelKey": "scanner:filters.ignored"}]},
      {"key": "outcome", "labelKey": "scanner:filterBar.sections.outcome", "options": [
        {"key": "failed_before", "labelKey": "scanner:filters.failedBefore"},
        {"key": "vmaf_rejected", "labelKey": "scanner:filters.vmafRejected"},
        {"key": "no_savings", "labelKey": "scanner:filters.noSavings"},
        {"key": "undo_possible", "labelKey": "scanner:filters.undoPossible"}]},
      {"key": "health", "labelKey": "scanner:filterBar.sections.health", "options": [
        {"key": "corrupt", "labelKey": "scanner:filters.corrupt"},
        {"key": "health_warnings", "labelKey": "scanner:filters.healthWarnings"},
        {"key": "health_never", "labelKey": "scanner:filters.healthNever"},
        {"key": "health_stale", "labelKey": "scanner:filters.healthStale"}]},
      {"key": "vmaf", "labelKey": "scanner:filterBar.sections.vmaf", "options": [
        {"key": "vmaf_excellent", "labelKey": "scanner:filters.vmafExcellent"},
        {"key": "vmaf_good", "labelKey": "scanner:filters.vmafGood"},
        {"key": "vmaf_poor", "labelKey": "scanner:filters.vmafPoor"},
        {"key": "vmaf_uncertain", "labelKey": "scanner:filters.vmafUncertain"}]}
    ]},
    {"key": "video", "labelKey": "scanner:filterBar.categories.video", "sections": [
      {"key": "codec", "labelKey": "scanner:filterBar.sections.codec", "options": [
        {"key": "x264", "label": "x264"},
        {"key": "x265", "label": "x265"},
        {"key": "av1", "label": "AV1"},
        {"key": "codec_mpeg2", "label": "MPEG-2"},
        {"key": "codec_vc1", "label": "VC-1 / WMV"},
        {"key": "codec_mpeg4", "label": "MPEG-4 / Xvid"},
        {"key": "codec_vp9", "label": "VP9"},
        {"key": "misc_codec", "labelKey": "scanner:filters.otherCodecs"}]},
      {"key": "resolution", "labelKey": "scanner:filterBar.sections.resolution", "options": [
        {"key": "res_4k", "label": "4K"},
        {"key": "res_1080p", "label": "1080p"},
        {"key": "res_720p", "label": "720p"},
        {"key": "res_sd", "label": "SD"}]},
      {"key": "bitrate", "labelKey": "scanner:filterBar.sections.bitrate", "options": [
        {"key": "bitrate_low", "labelKey": "scanner:filters.bitrateLow"},
        {"key": "bitrate_medium", "labelKey": "scanner:filters.bitrateMedium"},
        {"key": "bitrate_high", "labelKey": "scanner:filters.bitrateHigh"},
        {"key": "bloated", "labelKey": "scanner:filters.bloated"}]},
      {"key": "hdr", "labelKey": "scanner:filterBar.sections.hdr", "options": [
        {"key": "hdr_dv", "label": "Dolby Vision"},
        {"key": "hdr_hdr10", "label": "HDR10"},
        {"key": "hdr_hlg", "label": "HLG"}]},
      {"key": "picture", "labelKey": "scanner:filterBar.sections.picture", "options": [
        {"key": "bit10", "label": "10-bit"},
        {"key": "hi10p", "label": "Hi10P"},
        {"key": "interlaced", "labelKey": "scanner:filters.interlaced"},
        {"key": "vfr", "label": "VFR"}]}
    ]},
    {"key": "audio", "labelKey": "scanner:filterBar.categories.audio", "sections": [
      {"key": "audio", "labelKey": "scanner:filterBar.sections.audio", "options": [
        {"key": "audio_cleanup", "labelKey": "scanner:filters.audioCleanup"},
        {"key": "lossless_audio", "labelKey": "scanner:filters.losslessAudio"},
        {"key": "lossy_audio", "labelKey": "scanner:filters.lossyAudio"},
        {"key": "object_audio", "label": "Atmos / DTS:X"},
        {"key": "audio_71", "label": "7.1"},
        {"key": "commentary", "labelKey": "scanner:filters.commentary"}]}
    ]},
    {"key": "subtitles", "labelKey": "scanner:filterBar.categories.subtitles", "sections": [
      {"key": "subtitles", "labelKey": "scanner:filterBar.sections.kind", "options": [
        {"key": "image_subs", "labelKey": "scanner:filters.imageSubs"},
        {"key": "external_subs", "labelKey": "scanner:filters.externalSubs"},
        {"key": "forced_subs", "labelKey": "scanner:filters.forcedSubs"},
        {"key": "sdh_subs", "label": "SDH"}]},
      {"key": "_sub_cleanup", "labelKey": "scanner:filterBar.sections.subCleanup", "options": [
        {"key": "sub_cleanup", "labelKey": "scanner:filters.subCleanup"}]}
    ]},
    {"key": "language", "labelKey": "scanner:filterBar.categories.language", "sections": [
      {"key": "language", "labelKey": "scanner:filterBar.sections.language", "options": [
        {"key": "unknown_language", "labelKey": "scanner:filters.unknownLanguage"},
        {"key": "missing_language", "labelKey": "scanner:filters.missingLanguage"},
        {"key": "dubbed", "labelKey": "scanner:filters.dubbed"},
        {"key": "not_api_matched", "labelKey": "scanner:filters.notApiMatched"}]}
    ]},
    {"key": "library", "labelKey": "scanner:filterBar.categories.library", "sections": [
      {"key": "type", "labelKey": "scanner:filterBar.sections.type", "options": [
        {"key": "type_movie", "labelKey": "scanner:filters.movies"},
        {"key": "type_tv", "labelKey": "scanner:filters.tvShows"},
        {"key": "type_other", "labelKey": "scanner:filters.other"}]},
      {"key": "source", "labelKey": "scanner:filterBar.sections.source", "options": [
        {"key": "src_remux", "label": "Remux"},
        {"key": "src_bluray", "label": "Blu-ray"},
        {"key": "src_webdl", "label": "WEB-DL"},
        {"key": "src_hdtv", "label": "HDTV"},
        {"key": "src_dvd", "label": "DVD"}]},
      {"key": "added", "labelKey": "scanner:filterBar.sections.added", "options": [
        {"key": "new", "labelKey": "scanner:filters.new"},
        {"key": "added_7d", "labelKey": "scanner:filters.added7d"},
        {"key": "added_30d", "labelKey": "scanner:filters.added30d"},
        {"key": "added_90d", "labelKey": "scanner:filters.added90d"}]},
      {"key": "file", "labelKey": "scanner:filterBar.sections.file", "options": [
        {"key": "disc_iso", "labelKey": "scanner:filters.discIso"},
        {"key": "extras", "labelKey": "scanner:filters.extras"},
        {"key": "duplicates", "labelKey": "scanner:filters.duplicates"},
        {"key": "hardlinked", "labelKey": "scanner:filters.hardlinked"}]},
      {"key": "container", "labelKey": "scanner:filterBar.sections.container", "options": [
        {"key": "container_mkv", "label": "MKV"},
        {"key": "container_mp4", "label": "MP4"},
        {"key": "container_avi", "label": "AVI"},
        {"key": "container_other", "labelKey": "scanner:filters.other"}]},
      {"key": "size", "labelKey": "scanner:filterBar.sections.size", "options": [
        {"key": "size_small", "labelKey": "scanner:filters.sizeSmall"},
        {"key": "size_medium", "labelKey": "scanner:filters.sizeMedium"},
        {"key": "size_large", "labelKey": "scanner:filters.sizeLarge"}]}
    ]},
    {"key": "watched", "labelKey": "scanner:filterBar.categories.watched", "sections": [
      {"key": "plex", "labelKey": "scanner:filterBar.sections.plex", "options": [
        {"key": "plex_watched", "labelKey": "scanner:filters.watched"},
        {"key": "plex_unwatched", "labelKey": "scanner:filters.unwatched"},
        {"key": "plex_watchlist", "labelKey": "scanner:filters.watchlist"}]},
      {"key": "not_watched", "labelKey": "scanner:filterBar.sections.notWatched", "hintKey": "scanner:filters.notWatchedHint", "options": [
        {"key": "not_watched_6m", "labelKey": "scanner:filters.notWatched6m"},
        {"key": "not_watched_12m", "labelKey": "scanner:filters.notWatched12m"},
        {"key": "not_watched_24m", "labelKey": "scanner:filters.notWatched24m"}]},
      {"key": "arr", "labelKey": "scanner:filterBar.sections.arr", "options": [
        {"key": "arr_cutoff_unmet", "labelKey": "scanner:filters.arrCutoffUnmet"},
        {"key": "arr_unmonitored", "labelKey": "scanner:filters.arrUnmonitored"}]},
      {"key": "maintainerr", "labelKey": "scanner:filterBar.sections.maintainerr", "options": [
        {"key": "leaving_soon", "labelKey": "scanner:filters.leavingSoon"}]}
    ]},
    {"key": "direct_play", "labelKey": "scanner:filterBar.categories.directPlay", "sections": [
      {"key": "direct_play", "labelKey": "scanner:filterBar.sections.directPlay", "hintKey": "scanner:filters.directPlayHint", "options": [
        {"key": "dp_video", "label": "Hi10P"},
        {"key": "dp_subs", "labelKey": "scanner:filters.dpSubs"},
        {"key": "dp_audio", "labelKey": "scanner:filters.dpAudio"},
        {"key": "dp_container", "labelKey": "scanner:filters.dpContainer"}]}
    ]}
  ],
  "hidden": [
    {"key": "recent", "labelKey": "scanner:filters.recent"},
    {"key": "large_files", "labelKey": "scanner:filters.sizeLarge"},
    {"key": "low_bitrate", "labelKey": "scanner:filters.lowBitrate"},
    {"key": "high_bitrate", "labelKey": "scanner:filters.highBitrate"}
  ]
}
```

- [ ] **Step 4: The strings**

In `frontend/src/i18n/locales/en/scanner.json`, inside `"filters"`, add:

```json
    "bitrateLow": "Low · under 3 Mbps",
    "bitrateMedium": "Medium · 3–15 Mbps",
    "bitrateHigh": "High · over 15 Mbps",
    "watchlist": "Watchlist",
    "recent": "Changed today",
```

and at the top level of the file (beside `"filters"`), add:

```json
  "filterBar": {
    "categories": {
      "status": "Status", "video": "Video", "audio": "Audio", "subtitles": "Subtitles",
      "language": "Language", "library": "Library", "watched": "Watched", "directPlay": "Direct play"
    },
    "sections": {
      "queue": "Queue", "outcome": "Outcome", "health": "Health", "vmaf": "VMAF",
      "codec": "Codec", "resolution": "Resolution", "bitrate": "Bitrate", "hdr": "HDR", "picture": "Picture",
      "audio": "Audio", "kind": "Kind", "subCleanup": "Cleanup", "language": "Language",
      "type": "Type", "source": "Source", "added": "Added", "file": "File", "container": "Container", "size": "Size",
      "plex": "Plex", "notWatched": "Not watched in", "arr": "Sonarr / Radarr", "maintainerr": "Maintainerr",
      "directPlay": "Direct play"
    },
    "summary": "{{category}} · {{selected}}",
    "more": "+{{count}}",
    "chip": "{{section}}: {{label}}",
    "chipNot": "{{section}}: not {{label}}",
    "remove": "Remove {{label}}",
    "clearAll": "Clear all",
    "matching_one": "{{n}} file matches",
    "matching_other": "{{n}} files match",
    "panel": "{{category}} filters",
    "exclude": "Exclude {{label}}",
    "unexclude": "Stop excluding {{label}}",
    "excluded": "excluded",
    "rule": "Within a section, ticking more shows more; across sections, files must match all. ⊘ or Alt-click excludes."
  },
```

In `frontend/src/i18n/locales/es/scanner.json`, the same keys. Inside `"filters"`:

```json
    "bitrateLow": "Baja · menos de 3 Mbps",
    "bitrateMedium": "Media · 3–15 Mbps",
    "bitrateHigh": "Alta · más de 15 Mbps",
    "watchlist": "Lista de seguimiento",
    "recent": "Cambiado hoy",
```

and at the top level:

```json
  "filterBar": {
    "categories": {
      "status": "Estado", "video": "Vídeo", "audio": "Audio", "subtitles": "Subtítulos",
      "language": "Idioma", "library": "Biblioteca", "watched": "Visto", "directPlay": "Reproducción directa"
    },
    "sections": {
      "queue": "Cola", "outcome": "Resultado", "health": "Salud", "vmaf": "VMAF",
      "codec": "Códec", "resolution": "Resolución", "bitrate": "Tasa de bits", "hdr": "HDR", "picture": "Imagen",
      "audio": "Audio", "kind": "Tipo", "subCleanup": "Limpieza", "language": "Idioma",
      "type": "Tipo", "source": "Origen", "added": "Añadido", "file": "Archivo", "container": "Contenedor", "size": "Tamaño",
      "plex": "Plex", "notWatched": "Sin ver en", "arr": "Sonarr / Radarr", "maintainerr": "Maintainerr",
      "directPlay": "Reproducción directa"
    },
    "summary": "{{category}} · {{selected}}",
    "more": "+{{count}}",
    "chip": "{{section}}: {{label}}",
    "chipNot": "{{section}}: no {{label}}",
    "remove": "Quitar {{label}}",
    "clearAll": "Quitar todos",
    "matching_one": "{{n}} archivo coincide",
    "matching_many": "{{n}} de archivos coinciden",
    "matching_other": "{{n}} archivos coinciden",
    "panel": "Filtros de {{category}}",
    "exclude": "Excluir {{label}}",
    "unexclude": "Dejar de excluir {{label}}",
    "excluded": "excluido",
    "rule": "En una sección, marcar más muestra más; entre secciones, los archivos deben cumplirlas todas. ⊘ o Alt+clic excluye."
  },
```

Keep the existing Spanish wording style: if `es/scanner.json` already names one of these ideas differently in `"filters"` (e.g. the old group headings), use that term.

- [ ] **Step 5: Typed access and helpers**

`frontend/src/filterCatalog.ts`:

```ts
/**
 * The Scanner's filter bar (v0.10.0): quick pills, categories, their sections
 * and options, from filterCatalog.json. A backend test checks it against the
 * server's filters (backend/tests/test_filter_catalog.py): each section is one
 * server group, so ticking more in a section shows more.
 */
import catalog from "./filterCatalog.json";
import { decodeAdvanced } from "./advancedSearch";

type T = (key: string, options?: Record<string, unknown>) => string;

export interface FilterOption { key: string; label?: string; labelKey?: string }
export interface FilterSection { key: string; labelKey: string; hintKey?: string; options: FilterOption[] }
export interface FilterCategory { key: string; labelKey: string; sections: FilterSection[] }

export const QUICK_FILTERS: string[] = catalog.quick;
export const CATEGORIES = catalog.categories as FilterCategory[];

interface Place { option: FilterOption; section?: FilterSection; category?: FilterCategory }
const PLACES = new Map<string, Place>();
for (const category of CATEGORIES) {
  for (const section of category.sections) {
    for (const option of section.options) PLACES.set(option.key, { option, section, category });
  }
}
for (const option of catalog.hidden as FilterOption[]) PLACES.set(option.key, { option });

/** An option's label; an unknown id (an old bookmark) as itself. */
export function optionLabel(key: string, t: T): string {
  const option = PLACES.get(key)?.option;
  if (!option) return key.replace(/_/g, " ");
  return option.labelKey ? t(option.labelKey) : option.label ?? key;
}

/** A filter-string token as a chip: "Codec: x264", "Codec: not x264", "Advanced: 2 conditions". */
export function chipLabel(token: string, t: T): string {
  if (token.startsWith("adv:")) {
    return t("scanner:filters.advancedLabel", { count: decodeAdvanced(token)?.p.length ?? 0 });
  }
  const excluded = token.startsWith("!");
  const key = excluded ? token.slice(1) : token;
  const label = optionLabel(key, t);
  const section = PLACES.get(key)?.section;
  if (!section) return excluded ? t("scanner:filters.notLabel", { label }) : label;
  return t(excluded ? "scanner:filterBar.chipNot" : "scanner:filterBar.chip", { section: t(section.labelKey), label });
}

/** A category's options that are on, in its order: ticked or excluded. */
export function selectedIn(category: FilterCategory, active: string[]): { key: string; excluded: boolean }[] {
  const out: { key: string; excluded: boolean }[] = [];
  for (const section of category.sections) {
    for (const option of section.options) {
      if (active.includes(option.key)) out.push({ key: option.key, excluded: false });
      else if (active.includes("!" + option.key)) out.push({ key: option.key, excluded: true });
    }
  }
  return out;
}

/** Tick (exclude = false) or ⊘ (exclude = true) an option: on, or off if it
 *  already was. Ticking an excluded option includes it, and vice versa. */
export function toggleFilter(active: string[], key: string, exclude: boolean): string[] {
  const token = exclude ? "!" + key : key;
  const rest = active.filter(x => x !== "all" && x !== key && x !== "!" + key);
  const next = active.includes(token) ? rest : [...rest, token];
  return next.length ? next : ["all"];
}
```

Check that `decodeAdvanced` is exported from `frontend/src/advancedSearch.ts` (the old `FilterBar.tsx` imports it; use the same path).

- [ ] **Step 6: Run the tests and the build**

Run: `../venv/bin/python -m pytest backend/tests/test_filter_catalog.py -q -p no:warnings` — Expected: `4 passed`.
Run the frontend build — Expected: `EXIT=0` (the new module isn't used yet; that's fine).

- [ ] **Step 7: Mutation-check**

Each must fail a test (undo after): move `corrupt` into the Status → Queue section in the JSON; delete `bitrate_low` from the JSON; delete `es`'s `filterBar.matching_many`; change `es`'s `filterBar.chip` to drop `{{section}}`.

- [ ] **Step 8: Commit**

```bash
git add frontend/src/filterCatalog.json frontend/src/filterCatalog.ts frontend/src/i18n/locales/en/scanner.json frontend/src/i18n/locales/es/scanner.json backend/tests/test_filter_catalog.py
git commit -m "feat(scanner): the filter bar's layout, strings and guard

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: A category's panel

**Files:**
- Create: `frontend/src/components/FilterPanel.tsx`
- Modify: `frontend/src/theme.css` (after the `.sort-pill` rules, ~line 457)

- [ ] **Step 1: The component**

`frontend/src/components/FilterPanel.tsx`:

```tsx
import { useEffect, useRef, type CSSProperties, type KeyboardEvent } from "react";
import { useTranslation } from "react-i18next";
import { optionLabel, type FilterCategory } from "../filterCatalog";

interface FilterPanelProps {
  category: FilterCategory;
  active: string[];
  counts?: Record<string, number>;
  onToggle: (key: string, exclude: boolean) => void;
  onClose: () => void;
}

const FOCUSABLE = "input:not(:disabled), button:not(:disabled)";

/**
 * A category's options (v0.10.0): a checkbox (Alt-click excludes), the count
 * of files clicking it would give, and ⊘ to exclude. Options with no files
 * are greyed out unless they're on. Tab stays inside; Escape closes.
 */
export default function FilterPanel({ category, active, counts, onToggle, onClose }: FilterPanelProps) {
  const { t } = useTranslation(["scanner"]);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    ref.current?.querySelector<HTMLElement>(FOCUSABLE)?.focus();
  }, []);

  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    if (e.key === "Escape") {
      e.stopPropagation();
      onClose();
      return;
    }
    if (e.key !== "Tab" || !ref.current) return;
    const items = [...ref.current.querySelectorAll<HTMLElement>(FOCUSABLE)];
    if (items.length === 0) return;
    const first = items[0], last = items[items.length - 1];
    if (e.shiftKey && document.activeElement === first) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault();
      first.focus();
    }
  };

  const several = category.sections.length > 1;
  const style = { "--panel-width": `${Math.min(category.sections.length, 3) * 210 + 26}px` } as CSSProperties;
  return (
    <div ref={ref} className="filter-panel" role="dialog" style={style} onKeyDown={onKeyDown}
      aria-label={t("scanner:filterBar.panel", { category: t(category.labelKey) })}>
      <div className="filter-panel-sections">
        {category.sections.map(section => (
          <fieldset key={section.key} className="filter-section">
            <legend className={several ? "filter-section-title" : "sr-only"}
              title={section.hintKey ? t(section.hintKey) : undefined}>
              {t(section.labelKey)}
            </legend>
            {section.options.map(option => {
              const label = optionLabel(option.key, t);
              const included = active.includes(option.key);
              const excluded = active.includes("!" + option.key);
              const count = counts?.[option.key];
              const empty = count === 0 && !included && !excluded;
              return (
                <div key={option.key}
                  className={`filter-option${excluded ? " excluded" : ""}${empty ? " empty" : ""}`}>
                  <label className="filter-option-main">
                    <input type="checkbox" checked={included} disabled={empty}
                      onChange={e => onToggle(option.key, (e.nativeEvent as MouseEvent).altKey === true)} />
                    <span className="filter-option-label">{label}</span>
                    {excluded && <span className="sr-only">{t("scanner:filterBar.excluded")}</span>}
                    <span className="filter-option-count">{count == null ? "…" : count.toLocaleString()}</span>
                  </label>
                  <button type="button" className="filter-option-exclude" aria-pressed={excluded} disabled={empty}
                    aria-label={t(excluded ? "scanner:filterBar.unexclude" : "scanner:filterBar.exclude", { label })}
                    title={t(excluded ? "scanner:filterBar.unexclude" : "scanner:filterBar.exclude", { label })}
                    onClick={() => onToggle(option.key, true)}>
                    ⊘
                  </button>
                </div>
              );
            })}
          </fieldset>
        ))}
      </div>
      <div className="filter-panel-rule">{t("scanner:filterBar.rule")}</div>
    </div>
  );
}
```

- [ ] **Step 2: Its styles**

In `frontend/src/theme.css`, after the `.sort-pill.active:hover` rule, add:

```css
/* Filter bar panels (v0.10.0) */
.filter-category { position: relative; display: inline-flex; }
.filter-panel {
  position: absolute; top: calc(100% + 6px); left: 0; z-index: 100; /* above the sticky selection bar (50) */
  width: min(var(--panel-width, 240px), calc(100vw - 32px)); max-height: 70vh; overflow-y: auto;
  padding: 12px; box-sizing: border-box;
  background: var(--bg-card); border: 1px solid var(--border); border-radius: 8px;
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.3);
}
.filter-panel-sections { display: grid; grid-template-columns: repeat(auto-fill, minmax(190px, 1fr)); gap: 12px 16px; }
.filter-section { border: 0; margin: 0; padding: 0; min-width: 0; }
.filter-section-title {
  padding: 0; margin-bottom: 4px; font-size: 11px; font-weight: 600;
  text-transform: uppercase; letter-spacing: 0.4px; color: var(--text-muted);
}
.filter-option { display: flex; align-items: center; gap: 2px; border-radius: 4px; }
.filter-option:hover { background: var(--bg-tertiary); }
.filter-option-main {
  flex: 1; min-width: 0; display: flex; align-items: center; gap: 8px; padding: 4px 6px;
  font-size: 12px; color: var(--text-secondary); cursor: pointer;
}
.filter-option-main input { accent-color: var(--accent); margin: 0; }
.filter-option-label { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.filter-option-count { font-size: 11px; color: var(--text-muted); font-variant-numeric: tabular-nums; }
.filter-option.empty .filter-option-main { opacity: 0.45; cursor: default; }
.filter-option.excluded .filter-option-label { text-decoration: line-through; color: var(--danger); }
.filter-option-exclude {
  background: none; border: none; border-radius: 4px; padding: 2px 6px;
  font-size: 13px; color: var(--text-muted); cursor: pointer;
}
.filter-option-exclude:hover:not(:disabled),
.filter-option-exclude[aria-pressed="true"] { color: var(--danger); }
.filter-option-exclude:disabled { opacity: 0.3; cursor: default; }
.filter-panel-rule { margin-top: 10px; padding-top: 8px; border-top: 1px solid var(--border); font-size: 11px; color: var(--text-muted); }
/* Phones: a panel spans the bar (.filter-bar is the positioned parent). */
@media (max-width: 600px) {
  .filter-category { position: static; }
  .filter-panel { left: 0; right: 0; width: auto; }
}
```

- [ ] **Step 3: Build**

Run the frontend build. Expected: `EXIT=0`. Run `../venv/bin/python -m pytest backend/tests/test_frontend_colors.py -q -p no:warnings` — Expected: pass (no hex colours).

- [ ] **Step 4: Commit**

```bash
git add frontend/src/components/FilterPanel.tsx frontend/src/theme.css
git commit -m "feat(scanner): the filter bar's category panel

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: The new bar in the Scanner

**Files:**
- Rewrite: `frontend/src/components/FilterBar.tsx`
- Modify: `frontend/src/pages/ScannerPage.tsx` (import ~line 11; `filtersOpen` ~line 143; the filter toggle row ~lines 1538–1586; the expanded block ~lines 1637–1670)
- Modify: `frontend/src/theme.css` (after the Task 3 block)

- [ ] **Step 1: The bar**

Replace all of `frontend/src/components/FilterBar.tsx` with:

```tsx
import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import FilterPanel from "./FilterPanel";
import { CATEGORIES, QUICK_FILTERS, chipLabel, optionLabel, selectedIn, type FilterCategory } from "../filterCatalog";

interface FilterBarProps {
  activeFilters: string[];
  counts?: Record<string, number>;
  matching: number;  // files the whole filter matches
  onToggle: (key: string, exclude: boolean) => void;
  onRemove: (token: string) => void;
  onClear: () => void;
}

function shortCount(n: number): string {
  return n > 99999 ? `${(n / 1000).toFixed(0)}k` : n.toLocaleString();
}

/**
 * The Scanner's filter bar (v0.10.0): quick pills, a menu per category (see
 * FilterPanel) and the active filters as chips. Within a section, ticking
 * more shows more; across sections, files must match all (the server's
 * scan_filters.py decides; filterCatalog.json lays it out).
 */
export default function FilterBar({ activeFilters, counts, matching, onToggle, onRemove, onClear }: FilterBarProps) {
  const { t } = useTranslation(["scanner", "common"]);
  const [open, setOpen] = useState<string | null>(null);
  const barRef = useRef<HTMLDivElement>(null);
  const buttons = useRef(new Map<string, HTMLButtonElement>());
  const active = activeFilters.filter(f => f !== "all");

  const close = () => {
    const key = open;
    setOpen(null);
    if (key) buttons.current.get(key)?.focus();
  };

  useEffect(() => {
    if (!open) return;
    const away = (e: MouseEvent) => {
      if (barRef.current && !barRef.current.contains(e.target as Node)) setOpen(null);
    };
    const esc = (e: globalThis.KeyboardEvent) => { if (e.key === "Escape") close(); };
    document.addEventListener("mousedown", away);
    document.addEventListener("keydown", esc);
    return () => {
      document.removeEventListener("mousedown", away);
      document.removeEventListener("keydown", esc);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  const categoryButton = (category: FilterCategory) => {
    const selected = selectedIn(category, active);
    const hasFiles = category.sections.some(s => s.options.some(o => (counts?.[o.key] ?? 1) > 0));
    const dimmed = !hasFiles && selected.length === 0;
    const names = selected.map(s =>
      s.excluded ? t("scanner:filters.notLabel", { label: optionLabel(s.key, t) }) : optionLabel(s.key, t));
    const shown = names.slice(0, 2).join(", ")
      + (names.length > 2 ? " " + t("scanner:filterBar.more", { count: names.length - 2 }) : "");
    const title = t(category.labelKey);
    const isOpen = open === category.key;
    return (
      <div key={category.key} className="filter-category">
        <button
          ref={el => { if (el) buttons.current.set(category.key, el); }}
          className={`filter-pill filter-category-button${selected.length ? " active" : ""}${dimmed ? " dimmed" : ""}`}
          aria-haspopup="dialog" aria-expanded={isOpen}
          onClick={() => setOpen(isOpen ? null : category.key)}>
          <span>{selected.length ? t("scanner:filterBar.summary", { category: title, selected: shown }) : title}</span>
          <span aria-hidden="true" className="filter-category-caret">▾</span>
        </button>
        {isOpen && (
          <FilterPanel category={category} active={active} counts={counts} onToggle={onToggle} onClose={close} />
        )}
      </div>
    );
  };

  return (
    <div ref={barRef} className="filter-bar">
      <div className="filter-bar-row">
        {QUICK_FILTERS.map(key => {
          const included = active.includes(key);
          const excluded = active.includes("!" + key);
          const count = counts?.[key];
          const empty = count === 0 && !included && !excluded;
          return (
            <button key={key}
              className={`filter-pill${included ? " active" : ""}${excluded ? " excluded" : ""}${empty ? " dimmed" : ""}`}
              aria-pressed={included || excluded} disabled={empty}
              title={t("scanner:filters.excludeHint")}
              onClick={e => onToggle(key, e.altKey)}>
              <span className="filter-pill-label">{optionLabel(key, t)}</span>
              {excluded && <span className="sr-only">{t("scanner:filterBar.excluded")}</span>}
              {count != null && <span className="filter-pill-count">{shortCount(count)}</span>}
            </button>
          );
        })}
        <span className="filter-bar-divider" aria-hidden="true" />
        {CATEGORIES.map(categoryButton)}
      </div>
      {active.length > 0 && (
        <div className="filter-chips">
          {active.map(token => {
            const label = chipLabel(token, t);
            return (
              <span key={token} className={`filter-chip${token.startsWith("!") ? " excluded" : ""}`}>
                {label}
                <button type="button" className="filter-chip-remove"
                  aria-label={t("scanner:filterBar.remove", { label })} onClick={() => onRemove(token)}>
                  ×
                </button>
              </span>
            );
          })}
          <span className="filter-chips-matching">
            {t("scanner:filterBar.matching", { count: matching, n: shortCount(matching) })}
          </span>
          <button type="button" className="filter-chips-clear" onClick={onClear}>
            {t("scanner:filterBar.clearAll")}
          </button>
        </div>
      )}
    </div>
  );
}
```

- [ ] **Step 2: The bar's styles**

In `frontend/src/theme.css`, after the Task 3 block, add:

```css
/* Filter bar (v0.10.0) */
.filter-bar { position: relative; margin-bottom: 12px; }
.filter-bar-row { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
.filter-bar-divider { width: 1px; height: 18px; background: var(--border); margin: 0 4px; }
.filter-pill.dimmed { opacity: 0.45; }
.filter-pill:disabled { cursor: default; }
.filter-pill-count {
  padding: 1px 6px; border-radius: 8px; font-size: 10px; font-weight: bold; line-height: 1.4;
  background: color-mix(in srgb, var(--accent) 30%, transparent); color: var(--text-secondary);
}
.filter-category-caret { font-size: 9px; opacity: 0.6; }
.filter-chips { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; margin-top: 8px; }
.filter-chip {
  display: inline-flex; align-items: center; gap: 2px; padding: 3px 4px 3px 10px;
  font-size: 12px; border-radius: 14px;
  background: var(--accent-bg); color: var(--accent-text); border: 1px solid var(--accent);
}
.filter-chip.excluded { background: color-mix(in srgb, var(--danger) 10%, transparent); color: var(--danger); border-color: var(--danger); }
.filter-chip-remove { background: none; border: none; color: inherit; cursor: pointer; font-size: 14px; line-height: 1; padding: 0 4px; }
.filter-chips-matching { font-size: 12px; color: var(--text-muted); margin-left: 4px; }
.filter-chips-clear { background: none; border: none; color: var(--accent-text); cursor: pointer; font-size: 12px; padding: 2px 4px; }
```

Remove the now-unused `.filter-bar-hint` rule (only the old FilterBar used it; `grep -rn "filter-bar-hint" frontend/src` must find nothing else first).

- [ ] **Step 3: Use it in the Scanner**

In `frontend/src/pages/ScannerPage.tsx`:

1. Replace `import FilterBar, { filterLabel } from "../components/FilterBar";` with:

```tsx
import FilterBar from "../components/FilterBar";
import { toggleFilter } from "../filterCatalog";
```

2. Delete `const [filtersOpen, setFiltersOpen] = useState(false);`.

3. Replace the whole block that starts with the comment `{/* Filter toggle + active filter indicator */}` — the `<div>` holding the Filter toggle button, the Clear pill and `{!filtersOpen && scannerControls}` — with:

```tsx
          {/* The filter bar (v0.10.0): quick pills, a menu per category, the
              active filters as chips. Then the search / sort / view controls. */}
          <FilterBar
            activeFilters={filters}
            counts={filterCounts}
            matching={facetCounts?.matching ?? treeTotalFiles}
            onToggle={(key, exclude) => setFilters(prev => toggleFilter(prev, key, exclude))}
            onRemove={token => setFilters(prev => {
              const next = prev.filter(x => x !== token && x !== "all");
              return next.length ? next : ["all"];
            })}
            onClear={() => setFilters(["all"])}
          />
          <div style={{ display: "flex", gap: 6, alignItems: "center", marginBottom: 12, flexWrap: "wrap" }}>
            {scannerControls}
          </div>
```

4. Delete the whole `{filtersOpen && ( <> <FilterBar …/> <div …>{scannerControls}</div> </> )}` block and its comment (`{/* Expanded-filter layout: … */}`). The poster progress bar and the Corrupt banner stay where they are (now below the controls).

5. Update the comment above `const scannerControls = (` that describes the two positions (collapsed / expanded): it now has one position, below the filter bar.

6. Run `grep -n "filtersOpen\|filterLabel\|setFiltersOpen" frontend/src/pages/ScannerPage.tsx` — must print nothing.

- [ ] **Step 4: Build and tests**

Run the frontend build — Expected `EXIT=0`. Run `../venv/bin/python -m pytest backend/tests/test_frontend_colors.py backend/tests/test_filter_catalog.py -q -p no:warnings` — Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/FilterBar.tsx frontend/src/pages/ScannerPage.tsx frontend/src/theme.css
git commit -m "feat(scanner): a filter bar with quick pills, menus and chips

Four quick pills and a menu per topic instead of ~90 pills; options with no
files greyed out; the active filters as chips with Clear all and the
number of files that match.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Check it in the browser; changelog; push

Run by the controller (it needs the Browser pane), on the e2e preview `shrinkerr-e2e-develop` (port 6691) after restarting it so the backend change (Task 1) is loaded and the filter index rebuilds for the new definitions.

- [ ] **Step 1: Desktop (1400×900)**
  - The bar shows 4 quick pills, a divider and 8 category buttons; no "Filter" toggle; the search / sort row below.
  - Open Video: the sections Codec, Resolution, Bitrate, HDR, Picture with counts; options with 0 are greyed and their checkbox and ⊘ disabled.
  - Tick x264: the URL has `filter=x264`, a chip "Codec: x264", the button reads "Video · x264", "N files match", the tree updates. Tick x265 too: the chip set has both and N grows (same section widens). Tick 1080p: N shrinks or stays (another section narrows).
  - ⊘ on x265: its chip becomes "Codec: not x265" (red), the row strikes through; ticking x265's checkbox again includes it.
  - Alt-click a quick pill: excluded (red); click again: off.
  - Chip ×: removes that one; Clear all: none left, the chips row disappears.
  - Escape closes the panel and focus is back on its button; Tab cycles inside the open panel; a click outside closes it.
  - A category whose options all have 0 files (e.g. Watched on the e2e library, if so) is dimmed.
- [ ] **Step 2: Phone (375×812)**: the bar wraps; a panel spans the width under the bar and scrolls; chips wrap.
- [ ] **Step 3: Light theme** (Settings → theme, or `data-theme="light"` on `<html>`): text and borders readable; the excluded state readable.
- [ ] **Step 4: Spanish**: switch the UI language to Español: categories, sections, chips and "N archivos coinciden" translated.
- [ ] **Step 5: Changelog**

In `CHANGELOG.md`, `## [Unreleased]` → `### Changed`, add after the "Scanner counts are instant" entry:

```markdown
- **A simpler filter bar**: four quick filters (Needs conversion, Audio cleanup, Subtitle cleanup, New) and a menu per topic — Status, Video, Audio, Subtitles, Language, Library, Watched, Direct play — instead of 90 pills. Options with no files are greyed out, the active filters show as chips, and within a section ticking more shows more (Queue, Health, Language, Added, File and Bitrate options used to narrow each other). Video adds bitrate tiers with their numbers (under 3, 3–15, over 15 Mbps) and Watched the Plex watchlist.
```

- [ ] **Step 6: Spec note**

In `.superpowers/specs/2026-10-11-scanner-filter-overhaul-design.md` → Rollout, phase 2 now also includes the bitrate group (moved from phase 3), and phase 3 adds the Title menu and the Channels list (which then hides `audio_71`). Edit the two list items to say so.

- [ ] **Step 7: Full suite, build, commit, push**

Run the full suite (`EXIT=0`) and the frontend build (`EXIT=0`), then:

```bash
git add CHANGELOG.md .superpowers/specs/2026-10-11-scanner-filter-overhaul-design.md
git commit -m "docs: changelog and spec for the new filter bar

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git push origin develop
```

---

## Self-review notes

- **Spec coverage (phase 2):** quick pills (Task 4); category buttons with a summary label and dimmed when empty (Task 4); panels with sections, checkbox + count + ⊘, Alt-click, greyed zeros (Task 3); chips with Clear all and "N files match" (Task 4); phones (Tasks 3–4 CSS, Task 5); keyboard: menu buttons with `aria-haspopup` / `aria-expanded`, focus moves into the panel, Tab trapped, Escape returns focus (Tasks 3–4); screen readers: checkboxes whose label includes the count, `sr-only` "excluded", legends for sections (Task 3); en + es (Task 2); the one rule and the regrouping (Task 1, guarded by Task 2's test); old ids keep working (Task 1 test; hidden ids get labels for chips, Task 2); bitrate tiers with numbers (Tasks 1–2).
- **Deferred to phase 3, as planned:** list facets (languages, codecs, channels, genre, year, rating, country, network, show status, library, folder) with search; the Title menu; replacing `audio_71` with Channels. Phase 4: actors, directors, studios, Plex labels.
- **Behaviour changes users will notice:** ticking an excluded option now includes it (it used to only clear the exclusion); "All" is no longer a pill (Clear all in the chips row instead); the bar is always shown (no Filter toggle).
