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

Five new keys, mirroring Jellyfin's prefix-by-server pattern:

```
emby_url                    e.g. http://192.168.0.103:8096
emby_api_key                Emby API key
emby_user_id                optional; auto-detected if empty
emby_path_mapping           e.g. /media=/mnt/media
emby_scan_after_conversion  bool, default true
```

Plus a derived `emby_configured` flag (set when URL + API key are both present), surfaced to the frontend for the "Connected" green dot.

## Integration Points

### `backend/queue.py`

After each successful conversion, the worker currently calls `trigger_plex_scan()` and `trigger_jellyfin_scan()`. Add a third call to `trigger_emby_scan()`, gated on `emby_scan_after_conversion`. Each is independently enabled — users can run any combination of the three media servers.

### `backend/rule_resolver.py`

The rule engine resolves rules like "encode all 'Action' genre with NVENC" by asking the configured media server(s) for matching folder lists. Add Emby resolvers parallel to the Jellyfin ones:
- Genre filter
- Tag filter
- Library filter
- Watched-status filter

Rules already handle multiple media servers (genre rule can match Plex AND Jellyfin folders). Emby joins as a third union source.

### `backend/ssrf_guard.py`

The outbound-HTTP allowlist passes through configured Plex and Jellyfin hosts to prevent SSRF. Add `emby_url` from settings to the same allowlist source.

### `backend/routes/scan.py` and `backend/models.py`

The connection-test endpoint accepts a `service` parameter (`"plex"`, `"jellyfin"`). Add `"emby"` as a third accepted value, dispatching to `test_emby_connection()`.

### Active-stream aggregation

The pause-during-streaming logic ORs Plex's `is_streaming()` with Jellyfin's. Add Emby as a third OR'd source so any active stream on any configured server pauses encoding.

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

### 2. Auth header parsing

Both use `X-Emby-Token` (the legacy name from when Jellyfin was forked). The header value is the API key from settings. Identical wire format; same code in both modules.

## Frontend

### Settings section (`SettingsPage.tsx`)

A new section under the existing Plex / Jellyfin sections, structurally identical to the Jellyfin block:

- URL field (placeholder `http://192.168.0.103:8096`)
- API Key field (password-typed)
- User ID field with auto-detect note
- Path mapping field (format `/container=/emby`)
- Test Connection button → `testApiKey("emby")` → toast with server name + library count
- Connected indicator (green dot when `encoding.emby_configured`)
- "Refresh library after conversion" checkbox → `emby_scan_after_conversion`
- Save button → `updateEncodingSettings({ emby_url, emby_api_key, emby_user_id, emby_path_mapping, emby_scan_after_conversion })`

Help text:
> Connect your Emby server to automatically refresh your library after each conversion, create encoding rules based on Emby tags and genres, sync watched status for queue prioritization, and pause encoding during active streams.

~70 lines of JSX, mirroring the Jellyfin block with `jellyfin_` → `emby_` and label changes.

### `frontend/src/api.ts`

Already exports `testApiKey(service)` — accepts `"emby"` as a third value via the backend dispatch. No new API export needed beyond what's already there.

## Testing Strategy

### Synthetic API-shape tests

For each `emby.py` function that parses an Emby API response, add a small test mocking the HTTP response with a captured-from-docs JSON example, asserting the parsing returns the expected shape. Doesn't catch real-server quirks but protects against regressions.

### Live validation by user

User will set up Emby after merge and verify:
- (a) Test Connection returns server name + library count
- (b) Convert one file with `emby_scan_after_conversion=true` → file appears in Emby's library without manual refresh
- (c) Start playback in Emby → Shrinkerr's encoding pauses (with pause-during-streaming enabled)
- (d) Create a rule "Genre: Action → libx265 cq25" → applies to Emby-tagged Action folders

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
- `backend/queue.py` — add `trigger_emby_scan()` call alongside Plex/Jellyfin
- `backend/rule_resolver.py` — add Emby filters
- `backend/ssrf_guard.py` — allow Emby URL
- `backend/routes/scan.py` (or wherever `testApiKey` dispatches) — accept `"emby"`
- `backend/routes/settings.py` (or `models.py`) — encoding settings schema additions
- `frontend/src/pages/SettingsPage.tsx` — new Emby section + save handler
- `CHANGELOG.md` — v0.4.0 entry
- `VERSION` — 0.4.0

**Database:** No schema changes — Emby reuses the existing `plex_metadata_cache` table or an analogous existing structure (decision deferred to writing-plans phase).
