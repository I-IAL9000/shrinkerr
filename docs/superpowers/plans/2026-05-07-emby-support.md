# Emby Support Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Emby integration to Shrinkerr at full feature parity with Jellyfin (library refresh after conversion, active-stream detection, watched-status sync, rule-engine inputs, metadata cache sync, path mapping, connection test).

**Architecture:** New `backend/emby.py` mirrors `backend/jellyfin.py` 1:1 — same function names, same signatures, same return shapes — with three known divergences: (a) `Authorization: MediaBrowser Token=...` header is reused (Emby accepts it), (b) library refresh uses the same `POST /Library/Refresh` endpoint Jellyfin already uses (blanket refresh, not path-targeted), (c) settings keys are prefixed `emby_` instead of `jellyfin_`. Parallel-modules pattern keeps every change auditable: any future Jellyfin update has an obvious mirror in `emby.py`.

**Tech Stack:** Python 3.13, FastAPI, aiosqlite, httpx, pytest + pytest-asyncio, React/TypeScript.

**Spec:** `docs/superpowers/specs/2026-05-07-emby-support-design.md`

---

## File Structure

**New files:**
- `backend/emby.py` (~530 lines) — mirrors `backend/jellyfin.py`
- `backend/tests/test_emby.py` — synthetic API-shape unit tests

**Modified files:**

| File | Change |
|---|---|
| `backend/models.py` | 9 new settings fields on encoding settings model |
| `backend/routes/settings.py` | Read/write/validate the 9 new keys |
| `backend/ssrf_guard.py` | Allow `emby_url` |
| `backend/routes/scan.py` (or test-key route) | Accept `"emby"` for `testApiKey` |
| `backend/queue.py` | `trigger_emby_scan` + `_should_pause_for_emby` + optional empty-trash |
| `backend/rule_resolver.py` | Emby genre/tag/library/watched resolvers |
| `backend/routes/rules.py` | `POST /api/rules/sync-emby` + extend rule-options endpoint |
| `frontend/src/api.ts` | `testApiKey` union type + `syncEmbyMetadata` export |
| `frontend/src/pages/SettingsPage.tsx` | Emby connection section + rule-condition UI |
| `frontend/src/pages/SchedulePage.tsx` | Stream-aware scheduling copy |
| `VERSION` | `0.4.0` |
| `CHANGELOG.md` | v0.4.0 entry |

Each task below is independently committable. Total expected commits: ~13.

---

## Task 1: Create `backend/emby.py` by mirroring jellyfin.py

**Files:**
- Create: `backend/emby.py`
- Reference: `backend/jellyfin.py`

- [ ] **Step 1: Copy jellyfin.py → emby.py as the starting point**

```bash
cp backend/jellyfin.py backend/emby.py
```

- [ ] **Step 2: Replace identifier prefixes inside emby.py**

Use sed (verify before/after with grep counts):

```bash
sed -i.bak 's/jellyfin/emby/g; s/Jellyfin/Emby/g; s/JELLYFIN/EMBY/g' backend/emby.py
rm backend/emby.py.bak
grep -c jellyfin backend/emby.py
# Expected: 0
grep -c emby backend/emby.py
# Expected: 30+ matches
```

- [ ] **Step 3: Update the docstring at top of file**

Edit `backend/emby.py:1` — change:

```python
"""Emby API integration — library sync, watched status, metadata, stream detection."""
```

(Should already be correct after sed, but verify.)

- [ ] **Step 4: Verify the auth header is correct for Emby**

`backend/emby.py:_headers()` should send:

```python
return {
    "Authorization": f'MediaBrowser Token="{api_key}"',
    "Content-Type": "application/json",
}
```

Both Emby and Jellyfin accept this. No code change needed.

- [ ] **Step 5: Verify `trigger_emby_scan` uses `/Library/Refresh` (blanket)**

Match the Jellyfin behavior exactly — blanket refresh, no path targeting. The post-sed file should already have this. Verify:

```bash
grep -A3 "Library/Refresh" backend/emby.py
```

Expected: a single `POST /Library/Refresh` call with no path param, mirroring `jellyfin.py:170-181`.

- [ ] **Step 6: Verify no empty-trash function exists in `emby.py` (parallel to Jellyfin)**

Confirmed: `jellyfin.py` has no `empty_jellyfin_trash` function — only Plex implements empty-trash (`plex.py:141-178`, called from `queue.py:2382`). Mirror Jellyfin's state: the `emby_empty_trash` *setting* exists for parallelism, but no `empty_emby_trash` function and no worker call. If Jellyfin's gets implemented in a future release, Emby's gets matching wiring at that time.

Verification:

```bash
grep -c "empty_jellyfin_trash\|empty_emby_trash" backend/jellyfin.py backend/emby.py
# Expected: 0 in both files
```

- [ ] **Step 7: Sanity-check import**

```bash
cd /Users/hal9000/Documents/Claude/shrinkerr && python3 -c "import ast; ast.parse(open('backend/emby.py').read()); print('syntax ok')"
```

Expected: `syntax ok`

```bash
cd /Users/hal9000/Documents/Claude/shrinkerr && python3 -c "from backend import emby; print([n for n in dir(emby) if not n.startswith('_')])"
```

Expected: a list including `test_emby_connection`, `get_emby_libraries`, `trigger_emby_scan`, `get_active_streams`, `get_watch_status_folders`, etc.

- [ ] **Step 8: Commit**

```bash
git add backend/emby.py
git commit -m "feat: add backend/emby.py mirroring jellyfin.py for Emby support"
```

---

## Task 2: Synthetic API-shape tests for emby.py

**Files:**
- Create: `backend/tests/test_emby.py`
- Reference: `backend/tests/test_routes.py` for fixture patterns

- [ ] **Step 1: Write failing test for `_translate_path` and `_reverse_translate_path`**

Create `backend/tests/test_emby.py`:

```python
"""Synthetic API-shape tests for backend/emby.py.

These mock the HTTP responses with captured-from-docs JSON examples and
assert the parsing returns the expected shape. They don't catch real-server
quirks (the user validates those live against their Emby instance) but
they protect against regressions when emby.py is modified.
"""
import pytest
from unittest.mock import patch, AsyncMock
import httpx
from backend import emby


def test_translate_path_no_mapping():
    assert emby._translate_path("/media/foo.mkv", "") == "/media/foo.mkv"


def test_translate_path_with_mapping():
    result = emby._translate_path("/media/foo.mkv", "/media=/mnt/media")
    assert result == "/mnt/media/foo.mkv"


def test_reverse_translate_path():
    result = emby._reverse_translate_path("/mnt/media/foo.mkv", "/media=/mnt/media")
    assert result == "/media/foo.mkv"
```

- [ ] **Step 2: Run tests to verify they fail (or pass — trivial first)**

```bash
cd /Users/hal9000/Documents/Claude/shrinkerr && python3 -m pytest backend/tests/test_emby.py -v
```

Expected: 3 PASSes (path translation is pure logic, copy-pasted from working jellyfin.py).

- [ ] **Step 3: Add tests for `test_emby_connection`**

Append to `backend/tests/test_emby.py`:

```python
@pytest.mark.asyncio
async def test_emby_connection_no_credentials():
    """Returns failure when URL/api_key not configured."""
    with patch("backend.emby._get_emby_settings", new=AsyncMock(return_value={})):
        result = await emby.test_emby_connection()
    assert result["success"] is False
    assert "URL and API key required" in result["error"]


@pytest.mark.asyncio
async def test_emby_connection_success():
    """Parses /System/Info + /Library/VirtualFolders correctly."""
    settings = {
        "emby_url": "http://emby.local:8096",
        "emby_api_key": "test-key",
    }
    system_info = {"ServerName": "Emby Server", "Version": "4.7.14.0"}
    libraries = [
        {"ItemId": "abc", "Name": "Movies", "CollectionType": "movies",
         "Locations": ["/media/Movies"]},
        {"ItemId": "def", "Name": "TV Shows", "CollectionType": "tvshows",
         "Locations": ["/media/TV"]},
    ]

    async def fake_get(self, url, **kwargs):
        resp = httpx.Response(200)
        if "/System/Info" in url:
            resp = httpx.Response(200, json=system_info)
        elif "/Library/VirtualFolders" in url:
            resp = httpx.Response(200, json=libraries)
        return resp

    with patch("backend.emby._get_emby_settings", new=AsyncMock(return_value=settings)):
        with patch("httpx.AsyncClient.get", new=fake_get):
            result = await emby.test_emby_connection()

    assert result["success"] is True
    assert result["server_name"] == "Emby Server"
    assert result["library_count"] == 2
    assert len(result["libraries"]) == 2
    assert result["libraries"][0]["title"] == "Movies"
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd /Users/hal9000/Documents/Claude/shrinkerr && python3 -m pytest backend/tests/test_emby.py -v
```

Expected: 5 PASSes.

- [ ] **Step 5: Add tests for `_get_user_id` admin auto-detect**

Append:

```python
@pytest.mark.asyncio
async def test_get_user_id_stored():
    """Stored user_id short-circuits the lookup."""
    result = await emby._get_user_id("http://x", "key", stored_user_id="user-123")
    assert result == "user-123"


@pytest.mark.asyncio
async def test_get_user_id_auto_admin():
    """Picks the first admin user from /Users."""
    users = [
        {"Id": "user-1", "Name": "alice", "Policy": {"IsAdministrator": False}},
        {"Id": "user-2", "Name": "bob",   "Policy": {"IsAdministrator": True}},
        {"Id": "user-3", "Name": "carol", "Policy": {"IsAdministrator": True}},
    ]

    async def fake_get(self, url, **kwargs):
        return httpx.Response(200, json=users)

    with patch("httpx.AsyncClient.get", new=fake_get):
        result = await emby._get_user_id("http://x", "key", "")
    assert result == "user-2"  # first admin
```

- [ ] **Step 6: Add test for `trigger_emby_scan`**

Append:

```python
@pytest.mark.asyncio
async def test_trigger_emby_scan_no_credentials():
    """Returns False when not configured."""
    with patch("backend.emby._get_emby_settings", new=AsyncMock(return_value={})):
        result = await emby.trigger_emby_scan("/media/foo.mkv")
    assert result is False


@pytest.mark.asyncio
async def test_trigger_emby_scan_success():
    """POSTs to /Library/Refresh and returns True on 204."""
    settings = {"emby_url": "http://x", "emby_api_key": "key"}

    async def fake_post(self, url, **kwargs):
        assert "/Library/Refresh" in url
        return httpx.Response(204)

    with patch("backend.emby._get_emby_settings", new=AsyncMock(return_value=settings)):
        with patch("httpx.AsyncClient.post", new=fake_post):
            result = await emby.trigger_emby_scan("/media/foo.mkv")
    assert result is True
```

- [ ] **Step 7: Run all emby tests**

```bash
cd /Users/hal9000/Documents/Claude/shrinkerr && python3 -m pytest backend/tests/test_emby.py -v
```

Expected: 9 PASSes.

- [ ] **Step 8: Commit**

```bash
git add backend/tests/test_emby.py
git commit -m "test: add synthetic API-shape tests for emby.py"
```

---

## Task 3: Add 9 settings keys to models + settings route

**Files:**
- Modify: `backend/models.py:174-182` (encoding settings model)
- Modify: `backend/routes/settings.py:80-88` (settings keys list)
- Reference: existing Jellyfin keys in both files

- [ ] **Step 1: Read the current Jellyfin entries to know exact format**

```bash
grep -n "jellyfin" backend/models.py backend/routes/settings.py | head -20
```

- [ ] **Step 2: Add Emby fields to `models.py` encoding settings model**

For each `jellyfin_<key>` field on the model, add a parallel `emby_<key>` field with matching type and default:

```python
emby_url: Optional[str] = None
emby_api_key: Optional[str] = None
emby_user_id: Optional[str] = None
emby_path_mapping: Optional[str] = None
emby_scan_after_conversion: bool = True
emby_empty_trash: bool = False
emby_pause_on_stream: bool = False
emby_pause_stream_threshold: int = 1
emby_pause_transcode_only: bool = False
emby_configured: bool = False  # derived; not user-set
```

- [ ] **Step 3: Add Emby keys to settings.py read/write list**

In `backend/routes/settings.py:80-88` find the SQL fragment listing Jellyfin keys and add the Emby ones in the same shape. Apply both to the read query and the validate-known-keys allowlist.

- [ ] **Step 4: Add derived `emby_configured` flag**

Wherever `jellyfin_configured` is computed (typically: `jellyfin_url AND jellyfin_api_key`), add the same for Emby:

```python
emby_configured = bool(settings.get("emby_url") and settings.get("emby_api_key"))
```

- [ ] **Step 5: Restart server, smoke-test the settings endpoint**

```bash
# Save a setting
curl -X POST http://localhost:8000/api/settings/encoding \
  -H "Content-Type: application/json" \
  -d '{"emby_url": "http://test.local:8096", "emby_api_key": "abc"}'
# Read it back
curl http://localhost:8000/api/settings/encoding | python3 -m json.tool | grep emby
```

Expected: `emby_url`, `emby_api_key`, `emby_configured: true`.

- [ ] **Step 6: Commit**

```bash
git add backend/models.py backend/routes/settings.py
git commit -m "feat: add 9 Emby settings keys to encoding settings model"
```

---

## Task 4: Allow Emby URL through SSRF guard

**Files:**
- Modify: `backend/ssrf_guard.py`
- Reference: existing `jellyfin_url` allowlist source

- [ ] **Step 1: Find where Jellyfin URL is added to the allowlist**

```bash
grep -n "jellyfin_url" backend/ssrf_guard.py
```

- [ ] **Step 2: Add `emby_url` parallel to the Jellyfin entry**

Wherever the allowlist reads `jellyfin_url` from settings, add a sibling line that also reads `emby_url` and adds its host to the allowlist.

- [ ] **Step 3: Smoke-test outbound HTTP allowlist**

If there's a programmatic way to print the current allowlist, do it. Otherwise verify by configuring `emby_url` and confirming `test_emby_connection()` doesn't get blocked at the SSRF layer.

- [ ] **Step 4: Commit**

```bash
git add backend/ssrf_guard.py
git commit -m "feat: allow emby_url through SSRF allowlist"
```

---

## Task 5: Wire `testApiKey("emby")` dispatch

**Files:**
- Modify: `backend/routes/scan.py` (or wherever `testApiKey` is dispatched — search to confirm)
- Reference: existing `"jellyfin"` branch

- [ ] **Step 1: Find the dispatch site**

```bash
grep -rn 'testApiKey\|test_api_key\|test_connection' backend/routes/ | head
```

- [ ] **Step 2: Add `"emby"` branch dispatching to `test_emby_connection`**

In the function that handles `service == "jellyfin"` calling `test_jellyfin_connection()`, add a parallel branch:

```python
elif service == "emby":
    from backend.emby import test_emby_connection
    return await test_emby_connection()
```

- [ ] **Step 3: Add a route test**

Append to `backend/tests/test_routes.py`:

```python
@pytest.mark.asyncio
async def test_test_api_key_emby_unconfigured(client):
    """When Emby isn't configured, the test endpoint returns success=False."""
    response = await client.post("/api/settings/test-api-key",
                                  json={"service": "emby"})
    assert response.status_code == 200
    data = response.json()
    assert data["success"] is False
```

(Adjust route path to match the actual endpoint — search if needed.)

- [ ] **Step 4: Run the test**

```bash
cd /Users/hal9000/Documents/Claude/shrinkerr && python3 -m pytest backend/tests/test_routes.py::test_test_api_key_emby_unconfigured -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/routes/scan.py backend/tests/test_routes.py
git commit -m "feat: dispatch testApiKey('emby') to test_emby_connection"
```

---

## Task 6: Worker triggers Emby scan after conversion

**Files:**
- Modify: `backend/queue.py`
- Reference: lines where `trigger_jellyfin_scan` is called (search to find)

- [ ] **Step 1: Find where Jellyfin scan is triggered post-conversion**

```bash
grep -n "trigger_jellyfin_scan\|jellyfin_scan_after_conversion\|current_file_path" backend/queue.py
```

The grep also confirms what variable name holds the post-rename path at the trigger site (`current_file_path`, `file_path`, etc.) — use whatever the Jellyfin trigger uses.

- [ ] **Step 2: Add `trigger_emby_scan` call alongside**

For each spot that triggers Jellyfin's scan, add a sibling block:

```python
# Trigger Emby scan (parallel to Plex/Jellyfin)
try:
    db = await self._db()
    try:
        async with db.execute(
            "SELECT value FROM settings WHERE key = 'emby_scan_after_conversion'"
        ) as cur:
            row = await cur.fetchone()
            emby_scan_enabled = row is None or (row[0] or "").lower() != "false"
    finally:
        await db.close()
    if emby_scan_enabled:
        from backend.emby import trigger_emby_scan
        await trigger_emby_scan(current_file_path)  # use whatever path-variable the Jellyfin call uses at this same site

except Exception as exc:
    print(f"[WORKER] Emby scan trigger failed (non-fatal): {exc}", flush=True)
```

(Match the exact pattern used for Jellyfin — replace `jellyfin` → `emby` everywhere.)

- [ ] **Step 3: Commit**

```bash
git add backend/queue.py
git commit -m "feat: trigger Emby library scan after each conversion"
```

---

## Task 7: Pause-on-stream gating for Emby

**Files:**
- Modify: `backend/queue.py:936-954` (`_should_pause_for_jellyfin`)
- Reference: that function's exact shape

- [ ] **Step 1: Read the Jellyfin pause-on-stream function**

```bash
sed -n '936,954p' backend/queue.py
```

- [ ] **Step 2: Add `_should_pause_for_emby` mirror**

Right after `_should_pause_for_jellyfin`, add:

```python
async def _should_pause_for_emby(self) -> tuple[bool, str]:
    """Check if Emby is streaming and pause settings dictate stopping work.
    Returns (should_pause, reason)."""
    db = await self._db()
    try:
        async with db.execute(
            "SELECT key, value FROM settings WHERE key IN "
            "('emby_pause_on_stream', 'emby_pause_stream_threshold', 'emby_pause_transcode_only')"
        ) as cur:
            settings = {r[0]: r[1] for r in await cur.fetchall()}
    finally:
        await db.close()
    if (settings.get("emby_pause_on_stream") or "").lower() != "true":
        return (False, "")
    threshold = int(settings.get("emby_pause_stream_threshold", "1") or 1)
    transcode_only = (settings.get("emby_pause_transcode_only") or "").lower() == "true"
    try:
        from backend.emby import get_active_streams
        streams = await get_active_streams()
        count = streams.get("transcoding_count" if transcode_only else "active_count", 0)
        if count >= threshold:
            return (True, f"Emby has {count} active stream(s)")
    except Exception:
        pass
    return (False, "")
```

(Adjust to match Jellyfin's function shape exactly — same signature, same return type.)

- [ ] **Step 3: Wire the new helper into the pause-check chain**

Find every call site of `_should_pause_for_jellyfin`:

```bash
grep -n "_should_pause_for_jellyfin" backend/queue.py
```

For each call site, add a parallel `_should_pause_for_emby` call that ORs into the same `should_pause` result variable. Match the exact pattern used for Jellyfin (same indentation, same error handling).

- [ ] **Step 4: Commit**

```bash
git add backend/queue.py
git commit -m "feat: pause-on-stream gating for Emby"
```

---

## Task 8: Rule-engine resolvers for Emby (genre / tag / library / watched)

**Files:**
- Modify: `backend/rule_resolver.py`
- Reference: existing Jellyfin resolver functions

- [ ] **Step 1: Find the Jellyfin resolver functions**

```bash
grep -n "jellyfin" backend/rule_resolver.py | head
```

- [ ] **Step 2: Read the existing Jellyfin resolvers verbatim**

```bash
grep -n "_resolve_jellyfin\|jellyfin" backend/rule_resolver.py
```

Capture the function names, signatures, and bodies of every `_resolve_jellyfin_*` (or however they're named — could be `_jellyfin_genre_folders`, etc.). Each Emby resolver mirrors the exact shape of its Jellyfin sibling — same signature, same error handling, same return type.

- [ ] **Step 3: Add Emby resolvers as line-by-line copies of Jellyfin's**

For each Jellyfin resolver function, write an Emby twin. Replace `from backend.jellyfin import X` → `from backend.emby import X`, and replace function-name prefix `jellyfin` → `emby`. No structural changes.

- [ ] **Step 4: Wire the new resolvers into the rule-condition dispatcher**

Find the dispatcher (typically a dict or `if/elif` chain that maps condition type strings like `"jellyfin_tag"` to resolver functions):

```bash
grep -n '"jellyfin_' backend/rule_resolver.py
```

For each `"jellyfin_*"` mapping, add a parallel `"emby_*"` mapping right next to it.

- [ ] **Step 5: Commit**

```bash
git add backend/rule_resolver.py
git commit -m "feat: rule-engine resolvers for Emby genre/tag/library/watched"
```

---

## Task 9: `POST /api/rules/sync-emby` and rule-options endpoint

**Files:**
- Modify: `backend/routes/rules.py:307-314` (`/sync-jellyfin` endpoint)
- Modify: `backend/routes/rules.py:424-445` (rule-options consumer)
- Reference: existing Jellyfin sync route

- [ ] **Step 1: Add `/sync-emby` endpoint**

After the Jellyfin sync endpoint, add:

```python
@router.post("/sync-emby")
async def sync_emby() -> dict:
    """Sync Emby metadata (genres, tags, libraries) into the local cache."""
    from backend.emby import sync_emby_metadata_cache
    result = await sync_emby_metadata_cache()
    return result
```

- [ ] **Step 2: Inspect the rule-options endpoint**

```bash
sed -n '420,450p' backend/routes/rules.py
```

Note the actual response structure. The Jellyfin section likely sets one or more keys on the response dict (e.g. `result["jellyfin_tags"] = ...` or nested under `result["jellyfin"]`).

- [ ] **Step 3: Mirror the Jellyfin block for Emby**

Right after every `jellyfin_*` assignment in the rule-options response, add an `emby_*` parallel:

```python
from backend.emby import get_available_emby_options
emby_opts = await get_available_emby_options()
# Match Jellyfin's exact assignment shape — could be result["emby"] = emby_opts
# or result["emby_tags"] = emby_opts.get("tags", []) etc. depending on what
# the inspect step revealed.
```

- [ ] **Step 4: Commit**

```bash
git add backend/routes/rules.py
git commit -m "feat: /sync-emby endpoint + Emby in rule-options"
```

---

## Task 10: Frontend api.ts — testApiKey union + syncEmbyMetadata export

**Files:**
- Modify: `frontend/src/api.ts:715` (`testApiKey` union type)
- Modify: `frontend/src/api.ts:721-722` (`syncJellyfinMetadata` export)

- [ ] **Step 1: Read current state**

```bash
sed -n '710,725p' frontend/src/api.ts
```

- [ ] **Step 2: Add `"emby"` to the testApiKey union**

Change:

```typescript
service: "plex" | "jellyfin"
```

to:

```typescript
service: "plex" | "jellyfin" | "emby"
```

- [ ] **Step 3: Add `syncEmbyMetadata` export**

Mirror `syncJellyfinMetadata`:

```typescript
export const syncEmbyMetadata = () =>
  apiFetch<{ synced: number; ... }>("/rules/sync-emby", { method: "POST" });
```

(Match exact return type from `syncJellyfinMetadata`.)

- [ ] **Step 4: Build to verify TypeScript compiles**

```bash
cd /Users/hal9000/Documents/Claude/shrinkerr/frontend && npm run build 2>&1 | tail -20
```

Expected: build succeeds with no type errors.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/api.ts
git commit -m "feat(frontend): testApiKey emby union + syncEmbyMetadata export"
```

---

## Task 11: Frontend Settings page — Emby connection section

**Files:**
- Modify: `frontend/src/pages/SettingsPage.tsx` — add Emby section after the Jellyfin one

- [ ] **Step 1: Find the Jellyfin section block**

```bash
grep -n "jellyfin" frontend/src/pages/SettingsPage.tsx | head -20
```

- [ ] **Step 2: Copy the full Jellyfin section (URL, API key, User ID, Path mapping, Test, save buttons + the pause-on-stream three-toggle block)**

After the closing `</div>` of the Jellyfin section, paste a copy of the entire Jellyfin block. Then mass-replace `jellyfin_` → `emby_` and `Jellyfin` → `Emby` in that copied block only (use careful manual edit to scope the replacement).

- [ ] **Step 3: Update the help text wording**

```
Connect your Emby server to automatically refresh your library after each conversion, create encoding rules based on Emby tags and genres, sync watched status for queue prioritization, and pause encoding during active streams.
```

- [ ] **Step 4: Update the save handler payload**

Mirror exactly what the Jellyfin save handler sends (read it via `grep -A 12 "jellyfin_url:" frontend/src/pages/SettingsPage.tsx` first to see the actual shape it uses, then write the Emby version with the same keys swapped to `emby_*`). The "Empty trash after scan" checkbox should be present in the UI for parallelism with Jellyfin even though the setting isn't wired in queue.py yet — this matches Jellyfin's UI state.

- [ ] **Step 5: Build**

```bash
cd /Users/hal9000/Documents/Claude/shrinkerr/frontend && npm run build 2>&1 | tail -10
```

Expected: success.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/pages/SettingsPage.tsx
git commit -m "feat(frontend): Emby Settings section (connection panel)"
```

---

## Task 12: Frontend Settings page — Rule-condition UI

**Files:**
- Modify: `frontend/src/pages/SettingsPage.tsx:375` (rule-condition type union)
- Modify: `frontend/src/pages/SettingsPage.tsx:3065,3180` (`condOpts` and dropdown options)

- [ ] **Step 1: Inspect current rule-condition UI for Jellyfin references**

```bash
grep -n "jellyfin_tag\|jellyfin_genre" frontend/src/pages/SettingsPage.tsx
```

- [ ] **Step 2: Add Emby condition types to the union/enum**

For each `jellyfin_tag` / `jellyfin_genre` / `jellyfin_library` / `jellyfin_watched` rule-condition type, add a parallel `emby_*` entry.

- [ ] **Step 3: Inspect the existing `condOpts` shape and Jellyfin wiring**

```bash
grep -n "condOpts\|jellyfin_tag\|jellyfin_genre" frontend/src/pages/SettingsPage.tsx | head -25
```

Look at how Jellyfin's options get loaded into `condOpts` and how the dropdown reads them. The exact key shape (`condOpts["jellyfin_tag"]` vs `condOpts.jellyfin?.tags` etc.) determines the parallel Emby code.

- [ ] **Step 4: Extend `condOpts` to provide Emby dropdown options**

Mirror the Jellyfin wiring exactly — same code path, same data shape, just `jellyfin` → `emby` substituted. The rule-options endpoint (Task 9) should now return Emby's options in whatever shape Jellyfin's are returned in.

- [ ] **Step 5: Update the condition-type dropdown labels**

Add UI labels: `"Emby tag"`, `"Emby genre"`, `"Emby library"`, `"Emby watched"` parallel to the Jellyfin ones.

- [ ] **Step 6: Build + smoke-test in browser**

```bash
cd /Users/hal9000/Documents/Claude/shrinkerr/frontend && npm run build 2>&1 | tail -10
```

Manually: open Rules → New rule → confirm Emby condition types appear in the dropdown.

- [ ] **Step 7: Commit**

```bash
git add frontend/src/pages/SettingsPage.tsx
git commit -m "feat(frontend): Emby rule-condition types in rule builder"
```

---

## Task 13: SchedulePage copy update

**Files:**
- Modify: `frontend/src/pages/SchedulePage.tsx:343,352`

- [ ] **Step 1: Find the stream-aware scheduling copy**

```bash
sed -n '340,360p' frontend/src/pages/SchedulePage.tsx
```

- [ ] **Step 2: Update the wording**

Find any reference to "Plex / Jellyfin streams" (or similar) and add Emby:

```
Plex, Jellyfin, or Emby streams
```

- [ ] **Step 3: Build**

```bash
cd /Users/hal9000/Documents/Claude/shrinkerr/frontend && npm run build 2>&1 | tail -5
```

- [ ] **Step 4: Commit**

```bash
git add frontend/src/pages/SchedulePage.tsx
git commit -m "feat(frontend): mention Emby in stream-aware scheduling copy"
```

---

## Task 14: Version bump + CHANGELOG

**Files:**
- Modify: `VERSION`
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Bump VERSION**

```bash
echo "0.4.0" > VERSION
```

- [ ] **Step 2: Add CHANGELOG entry**

Insert at the top (under the `# Changelog` header, before the first existing entry):

```markdown
## [0.4.0] — 2026-05-07

### Added
- Emby integration at full feature parity with Jellyfin: library refresh after conversion, active-stream detection (pause encoding when someone's watching), watched-status sync for queue prioritization, rule-engine inputs (genre/tag/library/watched), bulk metadata sync, path mapping, connection test. New Emby section in Settings; nine new settings keys (`emby_url`, `emby_api_key`, `emby_user_id`, `emby_path_mapping`, `emby_scan_after_conversion`, `emby_empty_trash`, `emby_pause_on_stream`, `emby_pause_stream_threshold`, `emby_pause_transcode_only`). Mirrors `backend/jellyfin.py` structure.
```

- [ ] **Step 3: Run all tests**

```bash
cd /Users/hal9000/Documents/Claude/shrinkerr && python3 -m pytest backend/tests/ -v 2>&1 | tail -20
```

Expected: all pass.

- [ ] **Step 4: Build frontend**

```bash
cd /Users/hal9000/Documents/Claude/shrinkerr/frontend && npm run build 2>&1 | tail -10
```

Expected: success.

- [ ] **Step 5: Commit, tag, push**

```bash
git add VERSION CHANGELOG.md
git commit -m "release: v0.4.0 — Emby support"
git tag v0.4.0
git push origin main
git push origin v0.4.0
```

---

## Verification (manual, post-merge)

User will set up Emby and verify:

1. Settings → Emby → fill URL + API key → **Test Connection** → toast shows server name + library count.
2. Convert one file with `emby_scan_after_conversion=true` → file appears in Emby library without manual refresh.
3. Start playback in Emby → Shrinkerr's encoding pauses (with `emby_pause_on_stream=true`).
4. Create rule "Genre: Action → libx265 cq25" with Emby genre source → applies to Emby-tagged Action folders.

Issues found become v0.4.x follow-up fix releases. Most likely failure modes documented in spec under "Known live-validation risk areas":
- Path mapping silent no-op
- Auth header rejection (fallback: switch to `X-Emby-Token` in `_headers()`)
- Admin user-policy field shape divergence

---

## Skill References

- @superpowers:test-driven-development — for the test-first approach in Tasks 2 onward
- @superpowers:verification-before-completion — verify each task's tests pass before committing
- @superpowers:subagent-driven-development — recommended execution mode for this plan
