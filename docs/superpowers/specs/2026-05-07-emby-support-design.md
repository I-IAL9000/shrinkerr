# Emby Support — Design Spec

**Status**: Approved
**Date**: 2026-05-07
**Scope**: Add Emby integration to Shrinkerr at full feature parity with Plex and Jellyfin

## Goal

Users running Emby (instead of, or alongside, Plex/Jellyfin) should get the same seven integration features Shrinkerr already offers Plex and Jellyfin users:

1. Library scan trigger after each conversion
2. Active-stream detection (pause encoding when someone's watching)
3. Watched-status folder lists (queue prioritization)
4. Folder filtering by genre / tag / library / collection (rule-engine inputs)
5. Bulk metadata cache sync
6. Path mapping (container path ↔ media-server path)
7. Connection test in Settings

Emby is a sibling project to Jellyfin (Jellyfin forked from Emby in 2018); the HTTP APIs are ~90% identical for the endpoints Shrinkerr uses, so the integration mirrors Jellyfin's structure rather than introducing a new pattern.

## Non-Goals

- Emby Premiere paid features (Cinema Mode, etc.)
- Live TV / DVR endpoints (Plex/Jellyfin don't handle these either)
- Emby 3.x back-compatibility (target 4.7+ stable)
- Shared abstraction between Jellyfin and Emby (kept as parallel modules to keep diffs auditable)

## Architecture

### New module: `backend/emby.py`

Mirrors `backend/jellyfin.py` function-for-function (~530 lines). Same function names, same signatures, same return shapes — wherever Jellyfin and Emby diverge in API specifics, the differences are isolated inside `emby.py`. From every caller's perspective, `emby.X(...)` is interchangeable with `jellyfin.X(...)`.

**Public API surface:**

| Function | Purpose |
|---|---|
| `_get_emby_settings()` | Read URL / API key / user_id / path_mapping from settings |
| `_translate_path(file_path, mapping)` | Container → Emby path |
| `_reverse_translate_path(emby_path, mapping)` | Emby → container path |
| `test_emby_connection()` | Hit `/System/Info`; return server name + library count |
| `get_emby_libraries(url, api_key)` | List libraries via `/Library/MediaFolders` |
| `_get_user_id(url, api_key, stored_user_id)` | Auto-detect admin user via `/Users` |
| `trigger_emby_scan(file_path)` | POST `/Library/Media/Updated` with `{Updates: [{Path: ...}]}` |
| `get_active_streams()` | Read `/Sessions`, filter to `NowPlayingItem present AND PlayState.IsPaused == false` |
| `get_watch_status_folders()` | Watched/unwatched folder lists for queue prioritization |
| `get_folders_by_genre/tag/library(name)` | Rule-engine inputs |
| `get_available_emby_options()` | UI hint: list of available genres/tags/libraries |
| `sync_emby_metadata_cache()` | Bulk sync into local `plex_metadata_cache`-style table |

### Settings keys

Nine new keys, mirroring Jellyfin's prefix-by-server pattern (cross-checked against `backend/routes/settings.py:80-88` and `backend/models.py:174-182`):

```
emby_url                       e.g. http://192.168.0.103:8096
emby_api_key                   Emby API key
emby_user_id                   optional; auto-detected if empty
emby_path_mapping              e.g. /media=/mnt/media
emby_scan_after_conversion     bool, default true — refresh library after each conversion
emby_empty_trash               bool, default false — empty Emby trash after scan
emby_pause_on_stream           bool, default false — pause encoding when streams active
emby_pause_stream_threshold    int, default 1 — minimum concurrent streams to trigger pause
emby_pause_transcode_only      bool, default false — only pause for transcoding streams (not direct play)
```

Plus a derived `emby_configured` flag (set when URL + API key are both present), surfaced to the frontend for the "Connected" green dot.

Pause-on-stream is gated per-server: each of Plex/Jellyfin/Emby has its own three-key trio (`*_pause_on_stream`, `*_pause_stream_threshold`, `*_pause_transcode_only`). The worker-side gating logic in `queue.py:936-954` (`_should_pause_for_jellyfin` and parallel) gets a third sibling for Emby.

## Integration Points

### `backend/queue.py`

Three changes:
1. After each successful conversion, the worker calls `trigger_plex_scan()` and `trigger_jellyfin_scan()`. Add `trigger_emby_scan()`, gated on `emby_scan_after_conversion`.
2. Pause-on-stream gating: add a `_should_pause_for_emby()` parallel to `_should_pause_for_jellyfin()` (`queue.py:936-954`), reading `emby_pause_on_stream`, `emby_pause_stream_threshold`, `emby_pause_transcode_only`. The worker's pause check ORs the three per-server gates.
3. Optional `emby_empty_trash` post-scan call.

### `backend/rule_resolver.py`

The rule engine resolves rules like "encode all 'Action' genre with NVENC" by asking the configured media server(s) for matching folder lists. Add Emby resolvers parallel to the Jellyfin ones:
- Genre filter
- Tag filter
- Library filter
- Watched-status filter

Rules already handle multiple media servers (genre rule can match Plex AND Jellyfin folders). Emby joins as a third union source.

### `backend/routes/rules.py`

Two changes (cross-checked against the file):
1. Add `POST /api/rules/sync-emby` mirroring `/sync-jellyfin` (`routes/rules.py:307-314`) — triggers `sync_emby_metadata_cache()`.
2. Extend `get_available_jellyfin_options` consumer at `routes/rules.py:424-445` to also expose Emby's available genres/tags/libraries to the rule-builder UI.

### `backend/ssrf_guard.py`

The outbound-HTTP allowlist passes through configured Plex and Jellyfin hosts to prevent SSRF. Add `emby_url` from settings to the same allowlist source.

### `backend/routes/scan.py` and `backend/models.py`

The connection-test endpoint accepts a `service` parameter (`"plex"`, `"jellyfin"`). Add `"emby"` as a third accepted value, dispatching to `test_emby_connection()`. Settings model (`models.py:174-182`) gets the nine new fields listed above.

## API Differences from Jellyfin

Most endpoints are drop-in identical:
- `/System/Info` — server identification
- `/Library/MediaFolders` — list libraries
- `/Sessions` — list active sessions/streams
- `/Users` — list users (for admin auto-detect)
- `/Users/{id}/Items` with `IsPlayed`, `Genres`, `Tags`, `ParentId` filters

The two real divergences:

### 1. Path-targeted library refresh

- Jellyfin: `POST /Library/Refresh` with `path` param scans only that folder
- Emby: `POST /Library/Media/Updated` with body `{"Updates": [{"Path": "..."}]}` does the equivalent

`emby.py` handles this divergence inside `trigger_emby_scan()`; callers see the same interface.

### 2. Auth header

`backend/jellyfin.py:36` actually sends `Authorization: MediaBrowser Token="..."` — that's what Jellyfin standardised on after the fork. Emby accepts both this `Authorization: MediaBrowser Token=...` form AND its own `X-Emby-Token: <key>` / `X-MediaBrowser-Token: <key>` header.

Plan: try the `Authorization: MediaBrowser Token=...` form first (same code as Jellyfin). If any endpoint rejects it on the user's Emby server during live validation, fall back to `X-Emby-Token: <key>`. The `_headers()` helper in `emby.py` is the single point that needs to change if the fallback is required.

## Frontend

### Settings section (`SettingsPage.tsx`) — connection block

A new section under the existing Plex / Jellyfin sections, structurally identical to the Jellyfin connection block:

- URL field (placeholder `http://192.168.0.103:8096`)
- API Key field (password-typed)
- User ID field with auto-detect note
- Path mapping field (format `/container=/emby`)
- Test Connection button → `testApiKey("emby")` → toast with server name + library count
- Connected indicator (green dot when `encoding.emby_configured`)
- "Refresh library after conversion" checkbox → `emby_scan_after_conversion`
- "Empty trash after scan" checkbox → `emby_empty_trash`
- Pause-on-stream block: enable toggle, threshold input, transcode-only toggle (mirrors Jellyfin's three pause keys)
- Save button → `updateEncodingSettings({ emby_url, emby_api_key, emby_user_id, emby_path_mapping, emby_scan_after_conversion, emby_empty_trash, emby_pause_on_stream, emby_pause_stream_threshold, emby_pause_transcode_only })`

Help text:
> Connect your Emby server to automatically refresh your library after each conversion, create encoding rules based on Emby tags and genres, sync watched status for queue prioritization, and pause encoding during active streams.

### `SettingsPage.tsx` — rule-condition UI

Separate from the connection block, the rule-builder UI (around `SettingsPage.tsx:375` and `:3065`, `:3180`) needs Emby additions:
- New `emby_tag` / `emby_genre` / `emby_library` / `emby_watched` rule-condition types
- Dropdown options sourced from `get_available_emby_options()` via `routes/rules.py`
- `condOpts` shape extended for the new condition types

This is a separate concern from the connection panel; easy to miss because it lives elsewhere in the same file.

### `frontend/src/pages/SchedulePage.tsx`

Stream-aware scheduling copy at `SchedulePage.tsx:343,352` references "Plex / Jellyfin streams". Add Emby to the wording so users know the schedule respects all three.

### `frontend/src/api.ts`

Two changes:
1. The `testApiKey(service)` union type at `api.ts:715` needs `"emby"` added.
2. Add `syncEmbyMetadata` export at `api.ts:721-722` parallel to `syncJellyfinMetadata`.

### Total frontend surface

- `SettingsPage.tsx`: ~70 lines of JSX for the connection panel + ~20-30 lines of additions across the rule-condition UI
- `api.ts`: 2 lines (union type + export)
- `SchedulePage.tsx`: copy update (~2 lines)

## Testing Strategy

### Synthetic API-shape tests

For each `emby.py` function that parses an Emby API response, add a small test mocking the HTTP response with a captured-from-docs JSON example, asserting the parsing returns the expected shape. Doesn't catch real-server quirks but protects against regressions.

### Live validation by user

User will set up Emby after merge and verify:
- (a) Test Connection returns server name + library count
- (b) Convert one file with `emby_scan_after_conversion=true` → file appears in Emby's library without manual refresh
- (c) Start playback in Emby → Shrinkerr's encoding pauses (with pause-during-streaming enabled)
- (d) Create a rule "Genre: Action → libx265 cq25" → applies to Emby-tagged Action folders

Known live-validation risk areas (what's most likely to break):

1. **Path-targeted scan silently no-ops on misconfigured `emby_path_mapping`** — `/Library/Media/Updated` requires the Path field to match an Emby-known library root *after* path mapping. If the mapping is wrong, Emby silently ignores the call. The connection-test response should include the discovered library roots so users can verify their mapping translates correctly.
2. **Auth header rejection** — Emby accepts the `Authorization: MediaBrowser Token=...` form Jellyfin uses, but if any endpoint rejects it on a particular Emby version, fall back to `X-Emby-Token: <key>` in the `_headers()` helper.
3. **Admin user auto-detection** — `/Users` returns all users; we pick the first with `Policy.IsAdministrator==true`. Emby's user-policy field shape may differ subtly from Jellyfin's; the synthetic test should use a captured-from-real-Emby `/Users` response so we catch it pre-merge.

Issues found during live validation become follow-up fix releases.

## Rollout

Single release (planned: v0.4.0). No experimental badge. The synthetic tests cover regression risk; the user's live validation covers real-world correctness.

Implementation order (each step independently shippable, but all targeted at v0.4.0):
1. `emby.py` + settings keys + connection test
2. Scan-after-conversion (highest-value feature)
3. Active-stream detection
4. Rule-engine resolvers (genre/tag/library/watched)

## Open Questions

None outstanding — design fully specified.

## File Inventory

**New:**
- `backend/emby.py` (~530 lines, mirrors `jellyfin.py`)
- Tests for the synthetic API-shape coverage (location TBD by writing-plans)

**Modified:**
- `backend/queue.py` — `trigger_emby_scan()` call + `_should_pause_for_emby()` + optional `emby_empty_trash`
- `backend/rule_resolver.py` — Emby genre/tag/library/watched filters
- `backend/routes/rules.py` — `POST /api/rules/sync-emby` + extend rule-options endpoint
- `backend/ssrf_guard.py` — allow Emby URL
- `backend/routes/scan.py` (or wherever `testApiKey` dispatches) — accept `"emby"`
- `backend/routes/settings.py` — accept the 9 new Emby settings keys
- `backend/models.py` — encoding settings schema additions (9 fields)
- `frontend/src/api.ts` — `testApiKey` union type + `syncEmbyMetadata` export
- `frontend/src/pages/SettingsPage.tsx` — Emby connection section AND rule-condition UI additions (two separate spots)
- `frontend/src/pages/SchedulePage.tsx` — stream-aware scheduling copy update
- `CHANGELOG.md` — v0.4.0 entry
- `VERSION` — 0.4.0

**Database:** No schema changes. Emby reuses the existing `plex_metadata_cache` table the same way Jellyfin does (`backend/jellyfin.py:458,463`), with `metadata_type` values `'emby_tag'`, `'emby_genre'`, `'emby_library'`, `'emby_watched'` etc. The table name is historical — it's a generic metadata cache for any media server.
