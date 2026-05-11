# Auto-Queue Priority Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let users control queue priority of auto-queued (Sonarr/Radarr-dropped) files so they're processed ahead of an existing library backlog — via a new `auto_queue_priority` Settings dropdown AND by making `watcher._auto_queue_new_files` run through the rules engine like every other queue-entry path.

**Architecture:** New `auto_queue_priority` setting (Normal / High / Highest) persists round-trip through `GET/PUT /api/settings/encoding`. The watcher's auto-queue handler is refactored to call `resolve_rules_for_batch(...)` once per batch, overlay rule output on top of global Settings defaults per file, short-circuit `skip`/`ignore` actions, then pass through to `add_job(priority=...)`. Priority precedence: rule's `queue_priority` > new global Settings dropdown > 0 (Normal).

**Tech Stack:** Python 3.13, FastAPI, aiosqlite, pytest + pytest-asyncio, React/TypeScript.

**Spec:** `docs/superpowers/specs/2026-05-08-auto-queue-priority-design.md`

---

## File Structure

**Modified:**

| File | Change |
|---|---|
| `backend/models.py` | Add `auto_queue_priority: Optional[Any] = None` to `SettingsUpdate` |
| `backend/routes/settings.py` | 3 additions: `_ENCODING_DEFAULTS` entry, GET response builder, PUT save handler (with clamp) |
| `backend/watcher.py` | `_auto_queue_new_files` body rewritten to call `resolve_rules_for_batch`, merge rule output with Settings, short-circuit skip/ignore, pass through priority |
| `backend/tests/test_routes.py` | One round-trip test for `auto_queue_priority` |
| `backend/tests/test_watcher.py` | NEW file — two tests for `_auto_queue_new_files` priority resolution |
| `frontend/src/pages/SettingsPage.tsx` | New dropdown in Automation section near `auto_queue_new` checkbox + save-handler key |
| `CHANGELOG.md` | v0.5.0 entry: Added (dropdown) + Fixed (rules-engine gap, full action surface enumerated) |
| `VERSION` | `0.5.0` |

**New:**
- `backend/tests/test_watcher.py`

No DB migration needed — `jobs.priority` and `encoding_rules.queue_priority` columns already exist.

Total expected commits: 7.

---

## Task 1: Add `auto_queue_priority` to settings model + persist round-trip

**Files:**
- Modify: `backend/models.py` (add field to `SettingsUpdate`)
- Modify: `backend/routes/settings.py` (3 additions)
- Test: `backend/tests/test_routes.py` (1 new round-trip test)

- [ ] **Step 1: Inspect the existing pattern for an int-with-coercion field**

```bash
grep -n "Optional\[Any\]\b\|pause_stream_threshold\|_ENCODING_DEFAULTS\b" backend/models.py backend/routes/settings.py | head -20
```

Identifies the existing `*_pause_stream_threshold: Optional[Any]` pattern that the new field should mirror (int-typed in the DB, accepts int/str/bool from JSON, surfaces as int from GET).

- [ ] **Step 2: Write the failing round-trip test**

Append to `backend/tests/test_routes.py`:

```python
@pytest.mark.asyncio
async def test_auto_queue_priority_round_trip(client):
    """PUT then GET — auto_queue_priority setting must persist (Normal/High/Highest).
    v0.5.0+."""
    # Default should be 0 (Normal) on a fresh DB
    get0 = await client.get("/api/settings/encoding")
    assert get0.status_code == 200
    assert get0.json().get("auto_queue_priority", 0) == 0

    # PUT a non-default value
    put_resp = await client.put("/api/settings/encoding",
                                  json={"auto_queue_priority": 2})
    assert put_resp.status_code == 200, put_resp.text

    # GET should return the new value as int
    get_resp = await client.get("/api/settings/encoding")
    assert get_resp.status_code == 200
    data = get_resp.json()
    assert data["auto_queue_priority"] == 2

    # Clamp out-of-range values to [0, 2]
    put_low = await client.put("/api/settings/encoding",
                                json={"auto_queue_priority": -5})
    assert put_low.status_code == 200
    assert (await client.get("/api/settings/encoding")).json()["auto_queue_priority"] == 0

    put_high = await client.put("/api/settings/encoding",
                                 json={"auto_queue_priority": 99})
    assert put_high.status_code == 200
    assert (await client.get("/api/settings/encoding")).json()["auto_queue_priority"] == 2
```

- [ ] **Step 3: Run the test to verify it fails**

```bash
cd /Users/hal9000/Documents/Claude/shrinkerr && docker run --rm -v "$(pwd):/app" -w /app python:3.11 sh -c "pip install -q -r requirements.txt && python -m pytest backend/tests/test_routes.py::test_auto_queue_priority_round_trip -v"
```

Expected: FAIL — `auto_queue_priority` won't be in the response (or the PUT won't persist it because the model rejects the field).

- [ ] **Step 4: Add the field to `SettingsUpdate` in `backend/models.py`**

Find the existing `jellyfin_pause_stream_threshold: Optional[Any] = None` line as a reference. Add right after `auto_queue_new` (find it via `grep -n auto_queue_new backend/models.py`) or near the other `*_priority` fields:

```python
auto_queue_priority: Optional[Any] = None
```

`Optional[Any]` matches the existing pattern for int-typed settings that need to accept int/str/bool from request JSON.

- [ ] **Step 5: Add the default to `_ENCODING_DEFAULTS` in `backend/routes/settings.py`**

Find the dict (search for `auto_queue_new` or `_ENCODING_DEFAULTS`) and add:

```python
"auto_queue_priority": "0",
```

Place it near the existing `auto_queue_new` default for readability.

- [ ] **Step 6: Add the GET response builder line**

Find where `auto_queue_new` is surfaced in the response builder (search for `result["auto_queue_new"]`) and add right after:

```python
try:
    _aqp = int(merged.get("auto_queue_priority", "0") or 0)
except (TypeError, ValueError):
    _aqp = 0
result["auto_queue_priority"] = max(0, min(2, _aqp))
```

The clamp guards against malformed stored values.

- [ ] **Step 7: Add the PUT save handler**

In `update_encoding_settings` (around the other `auto_queue_*` writes), add:

```python
if update.auto_queue_priority is not None:
    try:
        v = int(update.auto_queue_priority)
    except (TypeError, ValueError):
        v = 0
    updates["auto_queue_priority"] = str(max(0, min(2, v)))
```

- [ ] **Step 8: Run the test to verify it passes**

```bash
cd /Users/hal9000/Documents/Claude/shrinkerr && docker run --rm -v "$(pwd):/app" -w /app python:3.11 sh -c "pip install -q -r requirements.txt && python -m pytest backend/tests/test_routes.py::test_auto_queue_priority_round_trip -v"
```

Expected: PASS.

- [ ] **Step 9: Run the FULL test suite to confirm no regression**

```bash
cd /Users/hal9000/Documents/Claude/shrinkerr && docker run --rm -v "$(pwd):/app" -w /app python:3.11 sh -c "pip install -q -r requirements.txt && python -m pytest backend/tests/ 2>&1 | tail -3"
```

Expected: 48 passed (was 47, +1 new).

- [ ] **Step 10: Commit**

```bash
git add backend/models.py backend/routes/settings.py backend/tests/test_routes.py
git commit -m "feat: add auto_queue_priority setting (Normal/High/Highest) with round-trip persist"
```

---

## Task 2: Refactor `_auto_queue_new_files` to run through the rules engine

**Files:**
- Modify: `backend/watcher.py:319-385` (rewrite `_auto_queue_new_files` body)
- Create: `backend/tests/test_watcher.py` (new file, 2 tests)

This is the meat of the feature. We're switching the watcher's auto-queue handler from "use global Settings defaults" to "resolve rules per-file, fall back to global Settings".

- [ ] **Step 1: Read the current implementation**

```bash
sed -n '319,385p' backend/watcher.py
```

Note the exact `add_job` parameter list — encoder, audio_tracks_to_remove, original_size, nvenc_preset, nvenc_cq, audio_codec, audio_bitrate. The refactor must preserve all of these, just sourcing some from rules when available.

- [ ] **Step 2: Read the manual-queue precedent**

```bash
sed -n '860,990p' backend/routes/jobs.py
```

This is `routes/jobs.py:864` — the existing call site that does what we want. Note specifically:
  - Line ~904: `rule_results = await resolve_rules_for_batch(safe_file_paths, extra_context=extra_context)`
  - Line ~946-948: `if rule and rule.get("action") in ("skip", "ignore"): continue`
  - Line ~973: `priority = max(payload.priority, rule.get("queue_priority") or 0)` ← we use OR-cascade, NOT max(), in the watcher (see spec for rationale)

- [ ] **Step 3: Write the failing tests first**

Create `backend/tests/test_watcher.py`:

```python
"""Tests for backend/watcher.py auto-queue priority resolution.

v0.5.0+: _auto_queue_new_files now runs through the rules engine instead
of using global Settings defaults directly. Verify the priority cascade:
rule.queue_priority > settings.auto_queue_priority > 0.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from backend.watcher import FileWatcher


def _fake_scanned(path: str = "/media/test.mkv"):
    """Minimal ScannedFile-shaped object the watcher iterates over."""
    track = MagicMock()
    track.stream_index = 1
    track.keep = True
    track.locked = False
    s = MagicMock()
    s.file_path = path
    s.file_size = 1_000_000_000
    s.needs_conversion = True  # so job_type == "convert"
    s.audio_tracks = [track]
    return s


@pytest.mark.asyncio
async def test_auto_queue_priority_rule_wins_over_setting(test_db):
    """When a rule sets queue_priority=2 (Highest) and auto_queue_priority
    setting is 1 (High), the rule wins (OR-cascade, not max())."""
    # Seed the auto_queue_priority + auto_queue_new settings
    import aiosqlite
    db = await aiosqlite.connect(test_db)
    try:
        await db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES ('auto_queue_new', 'true')")
        await db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES ('auto_queue_priority', '1')")
        await db.commit()
    finally:
        await db.close()

    watcher = FileWatcher(test_db, interval_minutes=5)
    scanned = _fake_scanned("/media/test.mkv")

    rule_results = {
        "/media/test.mkv": {
            "queue_priority": 2,
            "action": "encode",
            "encoder": None, "nvenc_preset": None, "nvenc_cq": None,
            "libx265_crf": None, "libx265_preset": None,
            "target_resolution": None, "audio_codec": None,
            "audio_bitrate": None,
        },
    }

    captured = {}

    async def fake_add_job(file_path, job_type, **kwargs):
        captured["file_path"] = file_path
        captured["job_type"] = job_type
        captured.update(kwargs)

    with patch("backend.queue.JobQueue") as MockQueue:
        instance = MockQueue.return_value
        instance.add_job = AsyncMock(side_effect=fake_add_job)
        with patch("backend.rule_resolver.resolve_rules_for_batch",
                   new=AsyncMock(return_value=rule_results)):
            await watcher._auto_queue_new_files([scanned])

    assert captured.get("priority") == 2, \
        f"Expected priority=2 (rule wins), got {captured.get('priority')}"


@pytest.mark.asyncio
async def test_auto_queue_priority_setting_wins_when_no_rule(test_db):
    """When no rule matches (or rule has no queue_priority), the global
    auto_queue_priority setting wins. Setting=1 → priority=1."""
    import aiosqlite
    db = await aiosqlite.connect(test_db)
    try:
        await db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES ('auto_queue_new', 'true')")
        await db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES ('auto_queue_priority', '1')")
        await db.commit()
    finally:
        await db.close()

    watcher = FileWatcher(test_db, interval_minutes=5)
    scanned = _fake_scanned("/media/test.mkv")

    # rule_results returns None for the path (no matching rule)
    rule_results = {"/media/test.mkv": None}

    captured = {}

    async def fake_add_job(file_path, job_type, **kwargs):
        captured["file_path"] = file_path
        captured["job_type"] = job_type
        captured.update(kwargs)

    with patch("backend.queue.JobQueue") as MockQueue:
        instance = MockQueue.return_value
        instance.add_job = AsyncMock(side_effect=fake_add_job)
        with patch("backend.rule_resolver.resolve_rules_for_batch",
                   new=AsyncMock(return_value=rule_results)):
            await watcher._auto_queue_new_files([scanned])

    assert captured.get("priority") == 1, \
        f"Expected priority=1 (setting wins, no rule), got {captured.get('priority')}"


@pytest.mark.asyncio
async def test_auto_queue_skip_action_short_circuits(test_db):
    """Rule action='skip' must prevent enqueue. add_job should not be called."""
    import aiosqlite
    db = await aiosqlite.connect(test_db)
    try:
        await db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES ('auto_queue_new', 'true')")
        await db.commit()
    finally:
        await db.close()

    watcher = FileWatcher(test_db, interval_minutes=5)
    scanned = _fake_scanned("/media/skip-me.mkv")

    rule_results = {"/media/skip-me.mkv": {"action": "skip"}}

    add_job_mock = AsyncMock()

    with patch("backend.queue.JobQueue") as MockQueue:
        MockQueue.return_value.add_job = add_job_mock
        with patch("backend.rule_resolver.resolve_rules_for_batch",
                   new=AsyncMock(return_value=rule_results)):
            await watcher._auto_queue_new_files([scanned])

    add_job_mock.assert_not_called()
```

- [ ] **Step 4: Run the new tests to verify they fail**

```bash
cd /Users/hal9000/Documents/Claude/shrinkerr && docker run --rm -v "$(pwd):/app" -w /app python:3.11 sh -c "pip install -q -r requirements.txt && python -m pytest backend/tests/test_watcher.py -v"
```

Expected: 3 FAILURES — the current `_auto_queue_new_files` doesn't call the rules engine and doesn't read `auto_queue_priority`.

- [ ] **Step 5: Rewrite `_auto_queue_new_files` to use the rules engine**

Replace the entire body of `_auto_queue_new_files` (currently `backend/watcher.py:319-385`) with this implementation:

```python
async def _auto_queue_new_files(self, results: list) -> int:
    """Auto-enqueue new files that need work, if the setting is enabled.

    v0.5.0+: now goes through the rules engine (resolve_rules_for_batch)
    like every other queue-entry path. Priority precedence:
        rule.queue_priority  OR  settings.auto_queue_priority  OR  0
    Skip / ignore rule actions short-circuit enqueue.
    """
    # Check setting
    db = await aiosqlite.connect(self.db_path)
    try:
        async with db.execute(
            "SELECT value FROM settings WHERE key = 'auto_queue_new'"
        ) as cur:
            row = await cur.fetchone()
            enabled = row and row[0].lower() == "true"
    finally:
        await db.close()

    if not enabled:
        return 0

    # Load all settings (used as fallbacks when a rule doesn't override)
    db = await aiosqlite.connect(self.db_path)
    try:
        settings = {}
        async with db.execute("SELECT key, value FROM settings") as cur:
            rows = await cur.fetchall()
            for r in rows:
                settings[r[0]] = r[1]
    finally:
        await db.close()

    default_encoder = settings.get("default_encoder", "nvenc")
    default_nvenc_preset = settings.get("nvenc_preset", "p6")
    default_nvenc_cq = int(settings.get("nvenc_cq", "20"))
    default_libx265_preset = settings.get("libx265_preset", "medium")
    default_libx265_crf = int(settings.get("libx265_crf", "20"))
    default_target_res = settings.get("target_resolution", "")
    default_audio_codec = settings.get("audio_codec", "copy")
    default_audio_bitrate = int(settings.get("audio_bitrate", "128"))
    try:
        default_priority = max(0, min(2, int(settings.get("auto_queue_priority", "0") or 0)))
    except (TypeError, ValueError):
        default_priority = 0

    # Resolve rules for the whole batch at once (one DB hit instead of per-file).
    # extra_context=None because auto-queue has no nzbget category to surface.
    from backend.rule_resolver import resolve_rules_for_batch
    file_paths = [s.file_path for s in results]
    rule_results = await resolve_rules_for_batch(file_paths, extra_context=None)

    from backend.queue import JobQueue
    queue = JobQueue(self.db_path)

    queued = 0
    skipped_by_rule = 0
    for scanned in results:
        tracks_to_remove = [t.stream_index for t in scanned.audio_tracks
                            if not t.keep and not t.locked]
        has_removable = len(tracks_to_remove) > 0

        if not scanned.needs_conversion and not has_removable:
            continue

        # Skip / ignore actions from rules short-circuit enqueue.
        # Mirrors routes/jobs.py:946-948.
        rule = rule_results.get(scanned.file_path)
        if rule and rule.get("action") in ("skip", "ignore"):
            skipped_by_rule += 1
            continue

        # Determine job type
        if scanned.needs_conversion and has_removable:
            job_type = "combined"
        elif scanned.needs_conversion:
            job_type = "convert"
        else:
            job_type = "audio"

        # Resolve every kwarg via OR-cascade: rule wins, else global default.
        # NOTE: this is different from routes/jobs.py:973's `max(...)`
        # because auto-queue has no per-file user choice that should act
        # as a floor — the rule fully wins (including lowering).
        r = rule or {}
        encoder = r.get("encoder") or default_encoder
        nvenc_preset = r.get("nvenc_preset") or default_nvenc_preset
        nvenc_cq = r.get("nvenc_cq") if r.get("nvenc_cq") is not None else default_nvenc_cq
        libx265_preset = r.get("libx265_preset") or default_libx265_preset
        libx265_crf = r.get("libx265_crf") if r.get("libx265_crf") is not None else default_libx265_crf
        target_resolution = r.get("target_resolution") or default_target_res
        audio_codec = r.get("audio_codec") or default_audio_codec
        audio_bitrate = r.get("audio_bitrate") if r.get("audio_bitrate") is not None else default_audio_bitrate
        rule_pri = r.get("queue_priority")
        priority = rule_pri if rule_pri is not None else default_priority

        await queue.add_job(
            file_path=scanned.file_path,
            job_type=job_type,
            encoder=encoder,
            audio_tracks_to_remove=tracks_to_remove,
            original_size=scanned.file_size,
            nvenc_preset=nvenc_preset,
            nvenc_cq=nvenc_cq,
            libx265_preset=libx265_preset,
            libx265_crf=libx265_crf,
            target_resolution=target_resolution,
            audio_codec=audio_codec,
            audio_bitrate=audio_bitrate,
            priority=priority,
        )
        queued += 1

    if queued or skipped_by_rule:
        msg = f"[WATCHER] Auto-queued {queued} new files"
        if skipped_by_rule:
            msg += f" ({skipped_by_rule} skipped by rule)"
        print(msg, flush=True)
    return queued
```

- [ ] **Step 6: Run the new watcher tests to verify they pass**

```bash
cd /Users/hal9000/Documents/Claude/shrinkerr && docker run --rm -v "$(pwd):/app" -w /app python:3.11 sh -c "pip install -q -r requirements.txt && python -m pytest backend/tests/test_watcher.py -v"
```

Expected: 3 PASSES.

- [ ] **Step 7: Run the full test suite**

```bash
cd /Users/hal9000/Documents/Claude/shrinkerr && docker run --rm -v "$(pwd):/app" -w /app python:3.11 sh -c "pip install -q -r requirements.txt && python -m pytest backend/tests/ 2>&1 | tail -3"
```

Expected: 51 passed (48 from before + 3 new in test_watcher.py).

- [ ] **Step 8: Commit**

```bash
git add backend/watcher.py backend/tests/test_watcher.py
git commit -m "feat: route auto-queue through rules engine + apply auto_queue_priority"
```

---

## Task 3: Frontend dropdown in the Automation section

**Files:**
- Modify: `frontend/src/pages/SettingsPage.tsx` (Automation section near `auto_queue_new` checkbox)

- [ ] **Step 1: Read the Automation section to understand its layout**

```bash
sed -n '3515,3560p' frontend/src/pages/SettingsPage.tsx
```

Identify:
- The container element style (so the dropdown matches)
- The existing `auto_queue_new` checkbox markup (so the dropdown follows the same pattern)
- The Save button's `updateEncodingSettings({...})` payload (we need to add the new key there)

- [ ] **Step 2: Find the encoding-settings save handler**

```bash
grep -n "updateEncodingSettings.*auto_queue_new\|updateEncodingSettings(" frontend/src/pages/SettingsPage.tsx | head
```

There's likely a single save handler in the Automation section that calls `updateEncodingSettings`. Note its exact location.

- [ ] **Step 3: Add the dropdown right after the auto_queue_new checkbox**

Find the closing element of the `auto_queue_new` checkbox block. Add a new dropdown below it:

```tsx
<div style={{ display: "flex", alignItems: "center", gap: 8, marginTop: 10,
              opacity: encoding?.auto_queue_new ? 1 : 0.5 }}>
  <span style={labelStyle}>Auto-queue priority:</span>
  <select style={{ ...inputStyle, width: 130 }}
    value={String(encoding?.auto_queue_priority ?? 0)}
    disabled={!encoding?.auto_queue_new}
    onChange={e => setEncoding({
      ...encoding,
      auto_queue_priority: parseInt(e.target.value, 10),
    })}>
    <option value="0">Normal</option>
    <option value="1">High</option>
    <option value="2">Highest</option>
  </select>
  <span style={{ fontSize: 11, color: "var(--text-muted)", marginLeft: 6 }}>
    Newly-detected files use this priority. Rules with a Queue Priority
    action override this setting.
  </span>
</div>
```

(Style + tag conventions copied from existing `<select>` controls in the file — adjust if the codebase uses a different pattern.)

- [ ] **Step 4: Add the field to the save handler payload**

Find the `updateEncodingSettings({...})` call in this section (Step 2 located it) and add `auto_queue_priority` to the object:

```tsx
auto_queue_priority: encoding?.auto_queue_priority,
```

- [ ] **Step 5: Build to verify TypeScript compiles**

```bash
cd /Users/hal9000/Documents/Claude/shrinkerr/frontend && npm run build 2>&1 | tail -10
```

Expected: build succeeds, no errors.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/pages/SettingsPage.tsx
git commit -m "feat(frontend): auto-queue priority dropdown in Settings → Automation"
```

---

## Task 4: Version bump + CHANGELOG

**Files:**
- Modify: `VERSION`
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Bump VERSION**

```bash
echo "0.5.0" > /Users/hal9000/Documents/Claude/shrinkerr/VERSION
```

- [ ] **Step 2: Add CHANGELOG entry**

Insert at the top (below the `# Changelog` header, before the first existing entry):

```markdown
## [0.5.0] — 2026-05-08

### Added
- **Auto-queue priority** Settings dropdown (Normal / High / Highest) in the Automation section. Newly-detected Sonarr/Radarr-dropped files inherit this priority when auto-queued, jumping them ahead of the manual backlog. Resolves [GitHub feature request "Auto-Queue: Set Priority"].

### Fixed
- **`encoding_rules` now apply to auto-queued files.** Pre-fix `watcher._auto_queue_new_files` called `add_job` with global Settings defaults and silently bypassed the rules engine — the only queue-entry path that didn't. Now the watcher calls `resolve_rules_for_batch` and rule actions take effect across the full surface: `encoder`, `nvenc_preset`, `nvenc_cq`, `libx265_crf`, `libx265_preset`, `qsv_*`, `vaapi_*`, `target_resolution`, `audio_codec`, `audio_bitrate`, `queue_priority`, and `skip` / `ignore` actions. **Behavior change for users with existing rules**: rules you wrote thinking they only applied to manual queueing now apply to auto-queue too. Review your rules after upgrade if this matters. Priority precedence: rule's `queue_priority` > new Settings dropdown > 0 (Normal).
```

- [ ] **Step 3: Run all tests one more time**

```bash
cd /Users/hal9000/Documents/Claude/shrinkerr && docker run --rm -v "$(pwd):/app" -w /app python:3.11 sh -c "pip install -q -r requirements.txt && python -m pytest backend/tests/ 2>&1 | tail -3"
```

Expected: all pass (count = previous total + 4 new tests in this release).

- [ ] **Step 4: Build frontend**

```bash
cd /Users/hal9000/Documents/Claude/shrinkerr/frontend && npm run build 2>&1 | tail -5
```

Expected: success.

- [ ] **Step 5: Commit, tag, push**

```bash
cd /Users/hal9000/Documents/Claude/shrinkerr
git add VERSION CHANGELOG.md
git commit -m "release: v0.5.0 — auto-queue priority + rules engine integration"
git tag v0.5.0
git push origin main
git push origin v0.5.0
```

---

## Verification (manual, post-merge)

User will set `auto_queue_priority` to **High** and verify:

1. **Settings round-trip** — Settings → Automation → "Auto-queue priority: High" → Save → reload page → still says High.
2. **Auto-queued file gets High priority** — Sonarr drops a new h264 file → watcher picks it up → check the Queue page Pending tab → the new file should have a "High" priority indicator and sort ABOVE Normal-priority backlog jobs.
3. **Rule wins over Settings** — create a rule "Directory contains TV → Queue Priority: Highest". Drop a new TV file. Verify the new job has "Highest" priority (rule won over Settings "High").
4. **Skip action short-circuits** — create a rule "Directory contains BackupOnly → Action: skip". Drop a file into that folder. Verify it does NOT appear in the Pending tab.

Issues found during live validation become v0.5.x follow-up releases.

---

## Skill References

- @superpowers:test-driven-development — TDD-first approach for Tasks 1 & 2 (write failing test, then implement)
- @superpowers:verification-before-completion — Run tests + build after every change, never skip the suite
- @superpowers:subagent-driven-development — Recommended execution mode for this plan (fresh subagent per task + two-stage review)
