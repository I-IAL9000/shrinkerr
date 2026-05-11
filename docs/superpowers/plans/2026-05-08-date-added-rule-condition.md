# Date Added Rule Condition Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `date_added` condition type to the encoding-rules engine so users can write rules like "Date Added newer than 24 hours → Queue Priority: Highest", letting them target specifically-fresh files in the existing rules UI.

**Architecture:** New inline `ctype == "date_added"` branch in `rule_resolver._check_condition`. Reads `scan_results.new_detected_at` (already populated by the watcher). Value format `<int><unit>` (e.g. `"24h"`, `"7d"`, `"4w"`) parsed by a small helper. Frontend adds a `CONDITION_TYPES` entry with a new `valueType: "duration"` that renders as number + units dropdown.

**Tech Stack:** Python 3.13, FastAPI, aiosqlite, pytest + pytest-asyncio, React/TypeScript.

**Spec:** `docs/superpowers/specs/2026-05-08-date-added-rule-condition-design.md`

---

## File Structure

| File | Change |
|---|---|
| `backend/rule_resolver.py` | `_parse_age_hours` helper, scan_data SELECT extension, `date_added` ctype handler |
| `backend/tests/test_rule_resolver.py` | **NEW** — 7 unit tests covering parser + handler |
| `backend/tests/test_watcher.py` | 1 integration test extending existing file |
| `frontend/src/pages/SettingsPage.tsx` | `valueType: "duration"` added to enum, `date_added` CONDITION_TYPES entry, duration value editor |
| `CHANGELOG.md` | v0.5.1 Added entry |
| `VERSION` | `0.5.1` |

**Database:** No schema changes — `new_detected_at` already exists.

Total expected commits: 4 (helper+handler+tests, frontend, integration test, release).

---

## Task 1: Backend helper + handler + unit tests

**Files:**
- Modify: `backend/rule_resolver.py`
- Create: `backend/tests/test_rule_resolver.py`

- [ ] **Step 1: Inspect the existing `_check_condition` structure and the `if not value` top-level guard**

```bash
sed -n '193,210p' backend/rule_resolver.py
```

Confirms the guard at line 197 (`if not value: return False`) fires before any `ctype` dispatch, so the handler itself doesn't need to re-check for empty value.

- [ ] **Step 2: Inspect the scan_data SELECT to know exactly which line to extend**

```bash
grep -n "scan_data\[\|SELECT file_path.*FROM scan_results" backend/rule_resolver.py
```

Note the line number of the SELECT statement that loads scan_data — Step 7 modifies that exact line.

- [ ] **Step 3: Write failing tests first (new file)**

Create `backend/tests/test_rule_resolver.py`:

```python
"""Unit tests for backend/rule_resolver.py — focused on the date_added
condition type added in v0.5.1.

These tests exercise the parser + handler directly via _check_condition.
Integration with the rules engine (resolve_rules_for_batch) is covered
by test_watcher.py."""
from datetime import datetime, timedelta, timezone
import pytest
from backend.rule_resolver import _parse_age_hours, _check_condition


def _row(detected_at):
    """scan_row-shaped dict the handler reads."""
    return {
        "file_path": "/media/x.mkv",
        "file_size": 1_000_000_000,
        "video_codec": "h264",
        "video_height": 1080,
        "audio_tracks_json": None,
        "new_detected_at": detected_at,
    }


def _cond(op, value):
    return {"type": "date_added", "operator": op, "value": value}


def _iso_n_hours_ago(n):
    """Return ISO-8601 string for now − n hours, UTC."""
    return (datetime.now(timezone.utc) - timedelta(hours=n)).isoformat()


def test_parse_age_hours_units():
    assert _parse_age_hours("1h") == 1
    assert _parse_age_hours("24h") == 24
    assert _parse_age_hours("1d") == 24
    assert _parse_age_hours("7d") == 168
    assert _parse_age_hours("1w") == 168
    assert _parse_age_hours("4w") == 672


def test_parse_age_hours_rejects_zero():
    """0h/0d/0w are semantically nonsense (always-false/always-true
    tautologies), so the parser rejects them like malformed input."""
    assert _parse_age_hours("0h") is None
    assert _parse_age_hours("0d") is None
    assert _parse_age_hours("0w") is None


def test_parse_age_hours_rejects_malformed():
    assert _parse_age_hours("") is None
    assert _parse_age_hours("foo") is None
    assert _parse_age_hours("24") is None      # missing unit
    assert _parse_age_hours("h24") is None     # wrong order
    assert _parse_age_hours("24x") is None     # bad unit
    assert _parse_age_hours("-5h") is None     # signed
    assert _parse_age_hours("1.5h") is None    # decimal


def test_date_added_newer_than_within_window():
    """less_than 24h with detected_at = 2h ago → True (newer than 24h)."""
    row = _row(_iso_n_hours_ago(2))
    assert _check_condition(_cond("less_than", "24h"), "/media/x.mkv", row, [], None) is True


def test_date_added_newer_than_outside_window():
    """less_than 24h with detected_at = 48h ago → False."""
    row = _row(_iso_n_hours_ago(48))
    assert _check_condition(_cond("less_than", "24h"), "/media/x.mkv", row, [], None) is False


def test_date_added_older_than_within_window():
    """greater_than 7d with detected_at = 14d ago → True (older than 7d)."""
    row = _row(_iso_n_hours_ago(14 * 24))
    assert _check_condition(_cond("greater_than", "7d"), "/media/x.mkv", row, [], None) is True


def test_date_added_null_treated_as_ancient_for_older_than():
    """NULL detected_at + greater_than → True (treat as ancient).
    Matches pre-watcher rows, scanner-added rows, bypass paths."""
    row = _row(None)
    assert _check_condition(_cond("greater_than", "7d"), "/media/x.mkv", row, [], None) is True


def test_date_added_null_returns_false_for_newer_than():
    """NULL detected_at + less_than → False (no fresh-arrival evidence)."""
    row = _row(None)
    assert _check_condition(_cond("less_than", "7d"), "/media/x.mkv", row, [], None) is False


def test_date_added_malformed_value_returns_false():
    """Top-level guard at rule_resolver.py:197 catches empty value.
    The handler itself catches malformed-but-non-empty via _parse_age_hours."""
    row = _row(_iso_n_hours_ago(2))
    assert _check_condition(_cond("less_than", "foo"), "/media/x.mkv", row, [], None) is False


def test_date_added_zero_value_returns_false():
    """'0h'/'0d' produce surprising tautologies; parser rejects them."""
    row = _row(_iso_n_hours_ago(2))
    assert _check_condition(_cond("less_than", "0h"), "/media/x.mkv", row, [], None) is False
    assert _check_condition(_cond("less_than", "0d"), "/media/x.mkv", row, [], None) is False


def test_date_added_units_conversion():
    """less_than 1d must behave identically to less_than 24h."""
    row = _row(_iso_n_hours_ago(12))
    assert _check_condition(_cond("less_than", "1d"), "/media/x.mkv", row, [], None) is True
    assert _check_condition(_cond("less_than", "24h"), "/media/x.mkv", row, [], None) is True
    row_old = _row(_iso_n_hours_ago(36))
    assert _check_condition(_cond("less_than", "1d"), "/media/x.mkv", row_old, [], None) is False
    assert _check_condition(_cond("less_than", "24h"), "/media/x.mkv", row_old, [], None) is False
```

- [ ] **Step 4: Run the tests to verify they fail**

```bash
cd /Users/hal9000/Documents/Claude/shrinkerr && docker run --rm -v "$(pwd):/app" -w /app python:3.11 sh -c "pip install -q -r requirements.txt && python -m pytest backend/tests/test_rule_resolver.py -v"
```

Expected: ALL 10 tests FAIL (with `ImportError` on `_parse_age_hours` — the helper doesn't exist yet).

- [ ] **Step 5: Add the `_parse_age_hours` helper to `backend/rule_resolver.py`**

Locate the imports / module-level constants block (top of file). Add after the existing imports (and after any existing constants like `_AGE_UNITS` would not pre-exist — this is the first):

```python
_AGE_UNITS = {"h": 1, "d": 24, "w": 168}

def _parse_age_hours(s: str) -> Optional[int]:
    """Parse a date_added value '24h'/'7d'/'4w' into hours.
    Returns None on malformed input OR zero (rule then short-circuits
    to False). Rejecting zero is intentional — '0h' is semantically
    nonsense for a comparison ('newer than 0 hours ago' is always
    false; 'older than 0 hours' is always true). Forcing it to return
    False matches the spec's existing 'malformed value → False'
    semantics rather than silently producing surprising tautologies.
    v0.5.1+."""
    s = (s or "").strip().lower()
    m = re.match(r"^(\d+)\s*([hdw])$", s)
    if not m:
        return None
    hours = int(m.group(1)) * _AGE_UNITS[m.group(2)]
    if hours <= 0:
        return None
    return hours
```

(`re` is already imported at the top of `backend/rule_resolver.py:5`; the helper uses the module-level import directly.)

- [ ] **Step 6: Add the `date_added` handler to `_check_condition`**

Locate the `file_size` block:

```bash
grep -n 'ctype == "file_size"' backend/rule_resolver.py
```

Right after the closing `return False` of the `file_size` block (around line 262), insert the new handler:

```python
    if ctype == "date_added":
        # Operator semantics: comparison is against file AGE
        # (now − detected_at), not raw timestamps. less_than 24h means
        # "age < 24h" = "newer than 24h". The frontend labels less_than
        # as "newer than" and greater_than as "older than" to match how
        # users read time. Future maintainers seeing `less_than` here
        # should know it maps inversely to a user-facing "newer than"
        # (older file = larger age value). v0.5.1+.
        detected_at = scan_row.get("new_detected_at")
        if not detected_at:
            # NULL = file has no watcher-recorded first-seen timestamp.
            # Three populations land here: pre-watcher library rows,
            # files added via the manual scanner (which only sets
            # new_detected_at when mark_new=True; ad-hoc scans don't),
            # and files imported via paths that bypass both. Treat all
            # as ancient: older-than returns True (matches "all backlog
            # files are older than 30 days"), newer-than returns False
            # (user wants fresh arrivals — NULL = no fresh evidence).
            return op == "greater_than"
        age_hours = _parse_age_hours(value)
        if age_hours is None:
            return False  # malformed value or zero
        try:
            from datetime import datetime, timezone
            dt = datetime.fromisoformat(detected_at.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            file_age_hours = (datetime.now(timezone.utc) - dt).total_seconds() / 3600
        except (ValueError, TypeError):
            return False
        if op == "less_than":     # "newer than N units ago"
            return file_age_hours < age_hours
        if op == "greater_than":  # "older than N units ago"
            return file_age_hours > age_hours
        return False
```

- [ ] **Step 7: Extend the scan_data SELECT to include `new_detected_at`**

Find the SELECT statement (Step 2 located it; should be around line 448):

```python
"SELECT file_path, file_size, video_codec, video_height, audio_tracks_json "
"FROM scan_results WHERE file_path IN ({placeholders})",
```

Change the column list to include `new_detected_at`:

```python
"SELECT file_path, file_size, video_codec, video_height, audio_tracks_json, "
"new_detected_at FROM scan_results WHERE file_path IN ({placeholders})",
```

- [ ] **Step 8: Run the tests to verify they pass**

```bash
cd /Users/hal9000/Documents/Claude/shrinkerr && docker run --rm -v "$(pwd):/app" -w /app python:3.11 sh -c "pip install -q -r requirements.txt && python -m pytest backend/tests/test_rule_resolver.py -v"
```

Expected: 10 PASSES.

- [ ] **Step 9: Run the full test suite to confirm no regression**

```bash
cd /Users/hal9000/Documents/Claude/shrinkerr && docker run --rm -v "$(pwd):/app" -w /app python:3.11 sh -c "pip install -q -r requirements.txt && python -m pytest backend/tests/ 2>&1 | tail -3"
```

Expected: 62 passed (52 from before + 10 new).

- [ ] **Step 10: Commit**

```bash
git add backend/rule_resolver.py backend/tests/test_rule_resolver.py
git commit -m "feat: add date_added rule condition (newer-than / older-than relative comparisons)"
```

---

## Task 2: Frontend CONDITION_TYPES entry + duration value editor

**Files:**
- Modify: `frontend/src/pages/SettingsPage.tsx`

- [ ] **Step 1: Inspect the existing CONDITION_TYPES dict + valueType enum**

```bash
sed -n '360,380p' frontend/src/pages/SettingsPage.tsx
```

Note the existing valueType union (`"select" | "text" | "number"`) and the dict structure.

- [ ] **Step 2: Extend the valueType union and add the `date_added` entry**

In `CONDITION_TYPES` declaration (line 360):

Change:
```typescript
const CONDITION_TYPES: Record<string, { label: string; group: string; operators: { value: string; label: string }[]; valueType: "select" | "text" | "number" }> = {
```

To:
```typescript
const CONDITION_TYPES: Record<string, { label: string; group: string; operators: { value: string; label: string }[]; valueType: "select" | "text" | "number" | "duration" }> = {
```

Add the new entry to the dict body, in the "File" group (alphabetical/logical order is fine — place it after `file_size` for grouping):

```typescript
date_added: { label: "Date Added", group: "File", operators: [{ value: "less_than", label: "newer than" }, { value: "greater_than", label: "older than" }], valueType: "duration" },
```

- [ ] **Step 3: Locate the condition value-editor block**

```bash
grep -n 'cond.type === "directory"\|cond.type === "file_size"' frontend/src/pages/SettingsPage.tsx | head
```

The value editor lives inside a series of `{cond.type === "..." && (...)}` blocks starting around line 3166. Find the last `cond.type === "..."` block before the closing `})()` of the value-editor IIFE.

- [ ] **Step 4: Add the duration value editor**

Right before the closing `})()`, add (note: the helper is `updateConditionValue`, not `updateCondition`):

```tsx
{cond.type === "date_added" && (() => {
  const m = (cond.value || "").match(/^(\d+)([hdw])$/);
  const num = m ? m[1] : "";
  const unit = m ? m[2] : "h";
  const setBoth = (n: string, u: string) =>
    updateConditionValue(condIdx, `${n || "0"}${u}`);
  return (
    <div style={{ display: "flex", gap: 6, flex: 1 }}>
      <input style={{ ...inputStyle, width: 80 }}
             type="number" min="1" placeholder="24"
             value={num}
             onChange={e => setBoth(e.target.value, unit)} />
      <select style={{ ...inputStyle, width: 110 }}
              value={unit}
              onChange={e => setBoth(num, e.target.value)}>
        <option value="h">hours</option>
        <option value="d">days</option>
        <option value="w">weeks</option>
      </select>
    </div>
  );
})()}
```

(Variable name is `condIdx` not `idx` — verified against the surrounding code at line 3156.)

- [ ] **Step 5: Build to verify TypeScript compiles**

```bash
cd /Users/hal9000/Documents/Claude/shrinkerr/frontend && npm run build 2>&1 | tail -10
```

Expected: success, no type errors. The `"duration"` literal type addition + the dict entry should compile cleanly.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/pages/SettingsPage.tsx
git commit -m "feat(frontend): Date Added rule condition with number+units value editor"
```

---

## Task 3: Integration test in test_watcher.py

**Files:**
- Modify: `backend/tests/test_watcher.py`

- [ ] **Step 1: Read the existing test file structure**

```bash
cat backend/tests/test_watcher.py
```

Note the `_fake_scanned` helper and the `with patch("backend.queue.JobQueue")` / `with patch("backend.rule_resolver.resolve_rules_for_batch")` pattern used by the existing 4 tests.

- [ ] **Step 2: Add the integration test**

Append to `backend/tests/test_watcher.py`:

```python
@pytest.mark.asyncio
async def test_auto_queue_date_added_rule_fires_with_priority(test_db):
    """Integration: a rule with date_added condition (matched upstream)
    correctly contributes queue_priority to the auto-queued job.
    Condition matching itself is unit-tested in test_rule_resolver.py;
    this test verifies the watcher applies a date_added-rule's
    queue_priority value to add_job. v0.5.1+."""
    import aiosqlite
    db = await aiosqlite.connect(test_db)
    try:
        await db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES ('auto_queue_new', 'true')")
        await db.commit()
    finally:
        await db.close()

    watcher = FileWatcher(test_db, interval_minutes=5)
    scanned = _fake_scanned("/media/fresh.mkv")

    # resolve_rules_for_batch is mocked — assume the date_added condition
    # matched and the rule resolved to queue_priority=2. The watcher
    # doesn't care HOW the rule matched, only WHAT the resolved rule says.
    rule_results = {
        "/media/fresh.mkv": {
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
        captured.update(kwargs)

    with patch("backend.queue.JobQueue") as MockQueue:
        MockQueue.return_value.add_job = AsyncMock(side_effect=fake_add_job)
        with patch("backend.rule_resolver.resolve_rules_for_batch",
                   new=AsyncMock(return_value=rule_results)):
            await watcher._auto_queue_new_files([scanned])

    assert captured.get("priority") == 2, \
        f"Expected priority=2 from date_added rule, got {captured.get('priority')}"
```

- [ ] **Step 3: Run the test to verify it passes**

```bash
cd /Users/hal9000/Documents/Claude/shrinkerr && docker run --rm -v "$(pwd):/app" -w /app python:3.11 sh -c "pip install -q -r requirements.txt && python -m pytest backend/tests/test_watcher.py -v"
```

Expected: 5 PASSES (was 4, +1 new).

- [ ] **Step 4: Run the full test suite**

```bash
cd /Users/hal9000/Documents/Claude/shrinkerr && docker run --rm -v "$(pwd):/app" -w /app python:3.11 sh -c "pip install -q -r requirements.txt && python -m pytest backend/tests/ 2>&1 | tail -3"
```

Expected: 63 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/tests/test_watcher.py
git commit -m "test: integration coverage for date_added rule firing in auto-queue"
```

---

## Task 4: Version bump + CHANGELOG + release

**Files:**
- Modify: `VERSION`
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Bump VERSION**

```bash
echo "0.5.1" > /Users/hal9000/Documents/Claude/shrinkerr/VERSION
```

- [ ] **Step 2: Add CHANGELOG entry**

Insert at the top (below the `# Changelog` header, before the first existing entry):

```markdown
## [0.5.1] — 2026-05-08

### Added
- **`date_added` rule condition** for time-based rule firing. Lets users write rules like "Date Added newer than 24 hours → Queue Priority: Highest" to specifically target newly-arrived files. Operators: "newer than" / "older than". Value editor: number input + units dropdown (hours / days / weeks). Data source: `scan_results.new_detected_at` (the watcher's first-seen timestamp — resilient to filesystem touch / chmod / remux operations, matches the "NEW" badge semantics). Files with no recorded detection time (pre-watcher library, scanner-added rows, bypass paths) are treated as ancient: "older than" matches, "newer than" does not. Composes with v0.5.0's auto-queue priority work — the alternative pattern the original GitHub feature request suggested is now usable.
```

- [ ] **Step 3: Run the full test suite one more time**

```bash
cd /Users/hal9000/Documents/Claude/shrinkerr && docker run --rm -v "$(pwd):/app" -w /app python:3.11 sh -c "pip install -q -r requirements.txt && python -m pytest backend/tests/ 2>&1 | tail -3"
```

Expected: 63 passed.

- [ ] **Step 4: Build frontend**

```bash
cd /Users/hal9000/Documents/Claude/shrinkerr/frontend && npm run build 2>&1 | tail -5
```

Expected: success.

- [ ] **Step 5: Commit, tag, push**

```bash
cd /Users/hal9000/Documents/Claude/shrinkerr
git add VERSION CHANGELOG.md
git commit -m "release: v0.5.1 — date_added rule condition"
git tag v0.5.1
git push origin main
git push origin v0.5.1
```

---

## Verification (manual, post-merge)

User creates a rule:
- **Conditions**: Date Added newer than 24 hours
- **Action**: Encode, Queue Priority: Highest

Then:

1. **Drop a fresh file** via Sonarr → file gets auto-queued → appears in Pending tab with **Highest** priority (rule fired).
2. **Manually queue an older file** from the library backlog → appears with **Normal** priority (rule did NOT fire — the file's `new_detected_at` is either NULL or far in the past).
3. **Edit the rule** in Settings → change units to "days" → save → reload page → still says "days". Values round-trip through `<num><unit>` string format.
4. **Try to enter 0 hours** in the rule editor → number input shows 0, but on save the rule fires for nothing (the `_parse_age_hours` rejection means the condition always returns False — expected). User should not be able to save 0 in practice; the `min="1"` on the input enforces this at the UI level.

Issues found during live validation become v0.5.x follow-ups.

---

## Skill References

- @superpowers:test-driven-development — Task 1 follows strict TDD (write 10 failing tests, then implement helper + handler)
- @superpowers:verification-before-completion — every task runs the full test suite + the targeted tests; never skip
- @superpowers:subagent-driven-development — recommended execution mode for this 4-task plan
