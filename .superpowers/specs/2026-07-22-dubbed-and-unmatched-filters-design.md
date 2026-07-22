# Dubbed + Not-API-Matched Filters & Incremental Metadata Refresh — Design Spec

**Version target:** one cohesive change, likely one release (schema + two filters + refresh change)
**Status:** Design — awaiting implementation plan
**Decisions locked via Q&A:**
- Dubbed status is only meaningful when native language comes from a source *independent of the audio present* → the dubbed filter considers only `language_source ∈ (api, tmdb-manual, manual)` with a real (non-`und`) native. Heuristic native is derived *from* the audio (`detect_native_language` = original-disposition track's lang, else first track's lang), so it can't yield a trustworthy dubbed signal.
- Conservative `und`-audio rule: an item is flagged dubbed only when **every** audio track has a known (non-`und`) language and **none** match native. An `und` track might be the missing original, so its presence makes the status uncertain → not flagged.
- Language matching uses `LANGUAGE_EQUIVALENTS` (e.g. `zho`≈`cmn`/`yue`, `nor`≈`nob`/`nno`).
- "Not API-matched" filter surfaces items whose native isn't from TMDB/manual, split into *not-yet-tried* vs *tried-but-no-match*.
- Metadata refresh becomes incremental via **mark-and-skip**: a failed TMDB lookup sets a persistent flag so future refreshes skip it; the old blanket `api` re-heal moves behind an explicit opt-in.

---

## Background

Two user goals against a ~130K-item library:
1. **Find dubbed content** — items missing their original-language audio (e.g. a Korean film ripped with only an English dub).
2. **Get everything matched from TMDB** — see which items are still only heuristically matched, and make `refresh-metadata` converge fast instead of re-processing the whole library (currently hours) every run.

Key facts from the code:
- `native_language` + `language_source` (`api` | `heuristic` | `manual` | `tmdb-manual`) live on each `scan_results` row.
- `detect_native_language(audio_tracks)` (the heuristic) returns the original-disposition audio track's language, else the first audio track's — so heuristic native **is** one of the present audio languages. Dubbed status is therefore undefined for heuristic rows.
- `_run_metadata_refresh` selects `WHERE language_source IN ('heuristic','api')` and, per row: heuristic → TMDB lookup (→ `api` if resolved); `api` → `_reclass_item` recompute-and-maybe-rewrite (a one-time heal from v0.9.70, wasteful to repeat).
- Filters are SQL predicates on stored flags (e.g. `unknown_language` → `has_und_tracks_flag`), so a dubbed filter needs a stored flag; matching JSON audio langs against native with equivalence isn't feasible in SQL.

## Schema — migration (add two columns to `scan_results`)

- **`tmdb_unresolved INTEGER DEFAULT 0`** — 1 when a TMDB lookup returned no match, or the file is in an `Other`-typed dir. Future refreshes skip these.
- **`is_dubbed_flag INTEGER DEFAULT 0`** — precomputed so the filter is a plain SQL predicate.

Both default 0 for existing rows; they populate correctly on the next refresh / detect / set-language / scan of each row. No backfill required (a `refresh-metadata` run recomputes them for the rows it touches).

## Feature 1 — Dubbed filter

**Helper** (in `scan.py`, near `_und_flag`):
```
_dubbed_flag(audio_tracks, native_language, language_source) -> int
```
Returns 1 iff **all** hold:
- `language_source in ("api", "tmdb-manual", "manual")`
- `native = (native_language or "").lower()` is truthy and `!= "und"`
- `audio_tracks` is non-empty
- every audio track's language is non-`und`
- no audio track's language matches `native` under equivalence

Matching: build `equiv(x) = LANGUAGE_EQUIVALENTS.get(x, {x})`; a track lang `L` matches native `N` iff `L in equiv(N)` or `N in equiv(L)`.

Computed and written wherever `has_und_tracks_flag` is written today — the scan-persist INSERT, the detect endpoint (`scan.py:~1214`), set-track-language (`~1389`), and the metadata-refresh reclass (`~3067`). Uses the classified pydantic audio tracks (`.language`).

**Filter:** key `dubbed` → `AND COALESCE(is_dubbed_flag,0)=1`, added to the backend filter switch and the FilterBar under a new "Language:" divider group.

## Feature 2 — Not-API-matched filter

**Filter:** key `not_api_matched` → `AND (language_source IS NULL OR language_source NOT IN ('api','manual','tmdb-manual'))`, in the same "Language:" group.

**Visibility of the split:** the filter's result set includes both untried (`tmdb_unresolved=0`) and tried-no-match (`tmdb_unresolved=1`) rows. Surface both counts in the existing per-filter count UI (the same place the unknown-language count is shown) so the user can see the manual-tagging backlog (tried-but-failed) distinctly from the not-yet-tried set. If a clean split needs its own predicate, expose it as an internal detail of the count endpoint rather than two separate top-level filter chips.

## Feature 3 — Incremental metadata refresh

- **Default select** becomes `WHERE language_source = 'heuristic' AND COALESCE(tmdb_unresolved,0) = 0 AND removed_from_list = 0`. Only untried, unmatched rows get a lookup.
- Per row: lookup **resolves** → flip to `api` (+ reclass, + recompute `is_dubbed_flag`). Lookup **fails** or `Other`-typed dir → set `tmdb_unresolved = 1` (skipped on future runs, shown in the tried-no-match bucket).
- The prior blanket `api` re-heal is removed from the default path and offered behind an explicit **`deep=true`** option on the refresh trigger (default off) for the rare case of a global rule change. Nothing is lost; normal refresh now converges and shrinks each run.
- Recompute `is_dubbed_flag` for every row the refresh rewrites.

## Testing

- `_dubbed_flag`: api-native + English-only audio vs `kor` native → 1; native matches an audio track (incl. equivalence `zho`/`cmn`) → 0; heuristic source → 0; `und` native → 0; any `und` audio track → 0; no audio tracks → 0; manual native honored.
- Filter predicates: `dubbed` and `not_api_matched` SQL added; a small DB fixture asserts each selects the right rows (incl. `tmdb_unresolved` split).
- Refresh: fixture with heuristic(untried), heuristic(`tmdb_unresolved=1`), and api rows → default run touches only the untried heuristic row; a resolving lookup flips it to api; a failing lookup sets `tmdb_unresolved=1`; `deep=true` also re-heals api rows.
- Migration test: columns added with defaults; idempotent.
- Full suite green in the `python:3.12-slim` container; CI build + Tests green.

## Risks & mitigations

| Risk | Mitigation |
|---|---|
| Dropping the `api` re-heal from default refresh hides drift after a global rule change | Kept behind `deep=true`; document it |
| `is_dubbed_flag` goes stale if native/audio change without hitting a persist site | It's written at every existing flag-write site; a refresh/detect/set-language recomputes it — same staleness profile as `has_und_tracks_flag` |
| `Other`-typed dirs marked `tmdb_unresolved` could hide a genuinely matchable item | They're already skipped by refresh today (spurious TMDB matches); marking only formalizes existing behavior |
| Equivalence map incomplete for some language | Falls back to exact-code match; no worse than today, and the map is easily extended |

## Rollout

Single release. Order within it: migration → `_dubbed_flag` helper + persist-site wiring → filters (backend + FilterBar) → incremental refresh + `deep` option. Ships via the normal versioned tag; CI validates.
