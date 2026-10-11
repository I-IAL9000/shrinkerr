# Scanner filter overhaul — design

Date: 2026-10-11. Status: approved in conversation (layout, grouping,
behaviour, index, data); this document is for review before planning.

## Problem

The Scanner has about 95 filter pills in one wrapping block (see the
screenshot of 10 Oct). In a large library:

- **Too many to scan.** Hard to find a filter; most of them show 0 and add noise.
- **Slow.** Every click recomputes every count from scratch. Measured on a
  synthetic 165,741-file library (`bench/make_db.py`):
  - `count_all` (first load): 12.96 s;
  - `facet_counts` (after any click): 11.9–12.7 s.
  - The track-JSON filters dominate. Run one by one, "no compatible audio"
    (direct play) takes 2.6 s and image-only subtitles 1.6 s; the 73 SQL
    filters add up to 16.5 s.
- **Missing filters.** Rating, year, genre and the other title fields are only
  in Advanced Search; there are no language, codec, actor, studio or folder
  lists.

## Goals

1. A bar you can read at a glance: a few quick pills, nine category
   buttons, the active filters as chips.
2. Counts that update in well under a second after any click, and appear
   immediately after a restart.
3. New lists: audio and subtitle language, audio codec and channels,
   genre, year, rating, country, studio, network, show status, actor,
   director, Plex label, library, folder; a bitrate group with numbers.

Non-goals: changing what existing filters mean (except the new bitrate
tiers); replacing Advanced Search; user-defined quick pills (later).

## UI

**Quick pills** (always shown): Needs conversion · Audio cleanup ·
Subtitle cleanup · New. Each is the same filter as its option in a panel,
so ticking it in either place ticks both.

**Category buttons.** Each opens a panel; each panel is made of sections,
and each section is one list.

| Button | Sections (options) |
|---|---|
| Status | Queue (needs conversion, queued, converted, ignored) · Outcome (failed before, VMAF-rejected, no savings, Undo possible) · Health (corrupt, warnings, never checked, checked 90+ days ago) · VMAF (excellent, good, poor, uncertain) |
| Video | Codec (x264, x265, AV1, MPEG-2, VC-1 / WMV, MPEG-4 / Xvid, VP9, other) · Resolution (4K, 1080p, 720p, SD) · Bitrate (Low < 3 Mbps, Medium 3–15 Mbps, High > 15 Mbps, Big for its resolution) · HDR (Dolby Vision, HDR10, HLG) · Picture (10-bit, Hi10P, interlaced, VFR) |
| Audio | Language (list) · Codec (list) · Channels (mono, stereo, 5.1, 7.1+, by the file's widest track) · Audio (audio cleanup, lossless, lossy, Atmos / DTS:X, commentary) |
| Subtitles | Language (list) · Kind (image, external, forced, SDH) · Subtitle cleanup |
| Language | unknown language, missing my language, dubbed, not matched to TMDB |
| Title | Genre · Year (decades, then years) · Rating (≥ 8, 7–8, 6–7, 5–6, < 5, no rating) · Country · Studio · Network · Show status · Actor · Director · Plex label |
| Library | Library (media folders) · Folder (searchable) · Type (movies, TV, other) · Source (Remux, Blu-ray, WEB-DL, HDTV, DVD) · Added (new, 7, 30, 90 days) · File (disc / ISO, extras, duplicates, hardlinked) · Container (MKV, MP4, AVI, other) · Size (< 5 GB, 5–10 GB, > 10 GB) |
| Watched | Plex (watched, unwatched, watchlist) · Not watched in (6 months, 1 year, 2 years) · Sonarr / Radarr (below cutoff, unmonitored) · Maintainerr (leaving soon) |
| Direct play | Hi10P, image-only subtitles, no compatible audio, old container |

- **Buttons.** A button's label shows what's selected in it ("Video ·
  x264, 1080p"). It's greyed out when no option in it has files, unless one
  of its options is selected.
- **Options.**
  - A row is a checkbox, a name, a count and an exclude control (⊘); Alt-click also excludes.
  - A count of 0 is greyed out and can't be ticked (it stays enabled while selected or excluded).
- **Long lists** (languages, codecs, genres, years, countries, studios,
  networks, actors, directors, labels, folders):
  - a search box, then the top values by count (Unknown first for languages);
  - "Show all" for lists of up to 200 values; longer lists (actors, folders) work by search.
- **Active filters** are chips under the bar ("Audio language: Icelandic ×",
  "Not ignored ×"), with Clear all and the "N files match" total.
- **Phones.** A panel opens full-width under the bar.
- **Keyboard and screen readers.** Buttons are menu buttons; the panel
  traps focus and closes with Escape; options are checkboxes; counts are
  read with their option.
- English and Spanish strings for everything.

## Semantics

**One rule: within a section, ticking more widens; across sections,
narrows.** Every section is one filter group: a file matches if it matches
any ticked option in the section, and it must match every section that has
something ticked. ⊘ excludes, whatever the section. A count is what you'd
get with that option added (its own section's current choice set aside).
The filter string stays in the page address (`?filter=`).

**Regrouped options.** Today some of these options are each their own group
(ticking two narrows). They move into their section's group:

| Section | Options |
|---|---|
| Queue (new group `queue`) | `needs_conversion`, `queued`, `converted`, `ignored` |
| Health | `corrupt` joins `health_*` |
| Bitrate (new group `bitrate`) | `bloated` joins the new tiers |
| Language | `unknown_language` joins `dubbed`, `not_api_matched`, `missing_language` |
| Added | `new` joins `added_*` |
| File (new group `file`) | `disc_iso`, `extras`, `duplicates`, `hardlinked` |

Policies act on saved views (`policies.py` → `_paths_matching`), so a
saved view that ticked two options in one row above would start matching
more files. Saved views and policies are new in 0.10.0, which hasn't been
released, so only test installs can have one. Old bookmarks just list a
little more. No migration is needed; the changelog entry for the bar says
"options in one section now widen".

**Ids.** Fixed options keep their ids. Old ids stay valid in saved views,
policies and bookmarks. Hidden options are no longer shown, but they still
parse with their old group and meaning:

- `low_bitrate`, `high_bitrate`: their meaning is unchanged (they still
  require "needs converting").
- `audio_71`: shown as Channels 7.1+ instead.
- `recent`, `large_files`: already hidden today.

New ids:

- **Bitrate:** `bitrate_low` (< 3 Mbps), `bitrate_medium` (3–15 Mbps),
  `bitrate_high` (> 15 Mbps).
  - They use the file's overall bitrate and the existing `LOW_BITRATE` /
    `HIGH_BITRATE` constants.
  - Labels show the numbers: "Low · under 3 Mbps".
  - Needs conversion + High covers what the High bitrate pill shows today.
- **Lists:** `<facet>:<value>`, with the value URL-encoded in the filter
  string. Facets:
  - `alang`, `slang`: any track in that language. Codes follow ISO 639-2,
    with `fre`/`fra`-style pairs folded into one; `und` means unknown.
  - `acodec`: any audio track with that codec.
  - `achan`: `1`, `2`, `6`, `8`, set by the widest audio track; 7 channels
    counts as 6, more than 8 as 8.
  - `genre`, `year` (`1990s` or `1994`), `rating` (band), `country`;
  - `studio`, `network`, `showstatus`, `actor`, `director`, `plabel`;
  - `library` (media folder id), `folder` (title folder path).
- Every facet is defined once in `scan_filters` as a SQL condition with a
  parameter, so `parse_filter` (tree, lists, Add to Queue, saved views,
  rules, policies) and the index share it.

## Data

- **Title credits.** A new table `title_credits(folder_path, kind, name)`,
  where kind is `actor`, `director` or `studio`.
  - Filled from TMDB in the background:
    - films: `/movie/{id}?append_to_response=external_ids,credits` (the call
      that today fetches only `/external_ids`);
    - TV: `/tv/{id}?append_to_response=external_ids,aggregate_credits`.
  - What's kept: the 10 top-billed actors; directors for films and creators
    for TV (both shown as Director); production companies as studios.
  - It extends the existing details backfill (`_backfill_details`): rate
    limited, retried on failure, and only new titles after the first run.
  - A manual Fix match refetches.
- **Plex labels.** The Plex metadata sync stores every label (today only
  the ones rules use).
- **Existing data used as is:** `poster_cache` (year, rating, genres,
  country, network, status), `media_dirs`, the track JSON.

## Filter index

- **What it is:** a materialised membership of every option.
  - Each fixed option and each list value is a bitset over `scan_results`
    row ids: bit n is row n (a Python `int`; `int.bit_count()` counts it).
    Using the ids directly needs no position map; the cost is bitsets as
    long as the largest id rather than the row count.
  - Values with very few files (most actors, folders) are kept as id lists,
    and turned into bitsets only when selected or counted.
- **Build** (background, after start-up, first time only): the same
  `scan_filters` definitions, run as one SQL pass over all rows for the
  fixed options, one pass per list facet for its values, and the existing
  Python predicates for the context filters.
- **Persistence:** `<data>/filter-index.bin` holds the bitsets, the
  definitions version and the change sequence it reflects.
  - At start-up it's loaded in milliseconds, then brought up to date.
  - A newer definitions version means a full rebuild.
- **Row changes:**
  - A trigger on `scan_results` bumps a `row_version` (insert, delete, or
    an update that changes a filter-relevant column), and records deletes.
  - The refresher re-evaluates only changed rows — the same SQL, `WHERE id
    IN (…)` — and patches their bits.
  - Deleted or unlisted rows have their bits cleared.
  - A batch larger than about 20% of the library rebuilds in the
    background instead, and the previous index answers meanwhile.
- **Context changes:**
  - Filters that read other tables or settings depend on named inputs, each
    with a change counter (triggers in `change_counters`): jobs (queue,
    outcome, Undo), the ignore list, settings (keep languages, backup days),
    watch history, Sonarr / Radarr status, Maintainerr, poster_cache,
    title credits, Plex metadata.
  - When an input's counter moves, only the filters that depend on it are
    recomputed.
  - Age-based filters (new, added in N days, not watched in, checked 90+
    days ago) are recomputed every 5 minutes.
- **Counts:**
  - From the filter expression: each group's bitset (OR of its options),
    and the exclusions.
  - For each group, the AND of all the other constraints; an option's count
    is `popcount(that & option)`.
  - About 200 options on 166K files takes a few milliseconds.
  - An Advanced Search condition in the filter (`adv:…`) has no stored
    bitset. It runs once as SQL into a bitset, cached until the index next
    changes, and is then combined like any group.
- **Lists in the UI:**
  - The counts response holds every fixed option, plus each list facet's
    top 50 values under the current filter, plus any selected value.
  - `GET /api/scan/facet-values?facet=&q=&filter=` searches one facet.
- **Tree, file lists and Add to Queue on a filter** take their ids from the
  index, falling back to SQL until the index is ready. The slow filters
  then list as fast as the simple ones.
- **Not ready yet:** counts show "counting…" and the file list still works
  through SQL.
- **Memory:** about 20 KB per bitset per 166K row ids. A few hundred
  bitsets plus id lists come to roughly 15–30 MB.

**Performance targets** (server side, on the synthetic 166K-file library;
then checked on a real server):

- counts after a click: < 150 ms;
- first counts after a restart: < 300 ms;
- 1,000 changed rows applied: < 500 ms;
- full build: < 20 s, in the background.

## Testing

- **Equivalence.** Index counts equal `count_all` / `facet_counts` (the
  current definitions) for every option and a set of expressions on a seeded
  database. Incrementally updated equals rebuilt after random inserts,
  updates, deletes and context changes.
- **List facets** match direct SQL over the same rows.
- **Regrouping.** Two options of one regrouped section now widen. Hidden
  old ids parse as before.
- **Performance.** `bench/` scripts (synthetic 166K library) report the
  targets above; run them by hand, not in CI.
- **UI.** Browser checks of the bar, panels, greyed zeros, search, chips,
  phone width and keyboard; `npm run build`.

## Rollout

Each phase gets its own implementation plan and ships on `develop` on its own:

1. The index and the counts endpoints, behind today's pills. The speed-up
   comes first.
2. The new bar: quick pills, category buttons, panels, chips, greyed zeros;
   the regrouping; the bitrate group (moved here from step 3). Eight menus:
   Title comes with the lists in step 3.
3. The list facets from existing data (languages, codecs, channels, genre,
   year, rating, country, network, show status, library, folder) and the
   Title menu; Channels replaces the 7.1 option (`audio_71`, then hidden).
4. Title credits (actor, director, studio) and all Plex labels.
5. Tree, lists and Add to Queue through the index.
