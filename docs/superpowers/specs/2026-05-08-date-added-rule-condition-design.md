# Date Added — Rule Condition Type — Design Spec

**Status**: Approved
**Date**: 2026-05-08
**Scope**: Add a `date_added` condition type to the encoding-rules engine so users can write rules that target recently-added files (e.g. "files added in the last 24 hours → Queue Priority: Highest").

## Goal

Compose with v0.5.0's auto-queue priority work to give users fine-grained control over rule firing based on file age. The original GitHub feature request had two alternatives — global auto-queue priority (shipped as v0.5.0) and a date-added rule condition (this spec). Both pay off the same user goal: prioritise newly-arrived files over an existing manual library backlog, but with different flexibility.

Example user rule after this ships:

> **Conditions**: Date Added newer than 24 hours  
> **Action**: Encode  
> **Queue Priority**: Highest

This rule fires only for files the watcher first saw in the last 24 hours; the rest of the backlog continues at Normal priority.

## Non-Goals

- Absolute-date comparisons ("added after 2026-01-01"). Rolling-window comparisons cover the realistic use cases; absolute dates would require a date picker UI and have niche value.
- `file_mtime` as a data source. We use `scan_results.new_detected_at` exclusively — semantically "Shrinkerr first noticed this file at time T", resilient to filesystem touch / chmod / remux operations.
- A "Date Modified" sibling condition. If a real use case for mtime emerges later, it's an additive change; no architectural blocker.

## Existing Infrastructure

| Piece | Where | Status |
|---|---|---|
| 14 condition types | `backend/rule_resolver.py:201-355` (inline `if ctype == "..."` blocks) | Established pattern; each ~10 LOC |
| `scan_results.new_detected_at` column | `backend/database.py:88` (in CREATE TABLE) | ISO-8601 string, populated by `_write_batch_sync_inner` in `scan.py:46` |
| `scan_data` SELECT | `backend/rule_resolver.py:447-449` | Currently loads `file_path, file_size, video_codec, video_height, audio_tracks_json` |
| `CONDITION_TYPES` frontend dict | `frontend/src/pages/SettingsPage.tsx:360-379` | 17 entries; each maps to a backend `ctype` |
| `valueType` enum on conditions | `frontend/src/pages/SettingsPage.tsx:360` | Currently `"select" \| "text" \| "number"` |
| Rule resolution called by | `routes/jobs.py:203/864/1534`, `watcher.py:_auto_queue_new_files` (v0.5.0+) | All paths apply the rules engine |

## Architecture

### Value format

Conditions serialize `value` as a string. For `date_added`, the format is:

```
<integer><unit>
```

Where unit is one of:
- `h` — hours
- `d` — days (multiplier: 24)
- `w` — weeks (multiplier: 168)

Examples: `"24h"`, `"7d"`, `"4w"`. The frontend renders a number input + units dropdown that composes into this string; the backend parses it with a small regex helper.

### Backend (`backend/rule_resolver.py`)

**1. Extend scan_data SELECT.** Current SELECT at line 448 adds one column:

```python
"SELECT file_path, file_size, video_codec, video_height, audio_tracks_json, "
"new_detected_at FROM scan_results WHERE file_path IN ({...})"
```

**2. Add `_parse_age_hours` module-level helper:**

```python
_AGE_UNITS = {"h": 1, "d": 24, "w": 168}

def _parse_age_hours(s: str) -> Optional[int]:
    """Parse a date_added value '24h'/'7d'/'4w' into hours.
    Returns None on malformed input (rule then short-circuits to False)."""
    s = (s or "").strip().lower()
    m = re.match(r"^(\d+)\s*([hdw])$", s)
    if not m:
        return None
    return int(m.group(1)) * _AGE_UNITS[m.group(2)]
```

**3. Add `date_added` handler in `_check_condition`** right after the `file_size` block (around line 263 — keeps related "comparison-style" conditions adjacent):

```python
if ctype == "date_added":
    detected_at = scan_row.get("new_detected_at")
    if not detected_at:
        # NULL = file existed before watcher first observed it.
        # Treat as ancient: newer-than returns False, older-than returns True.
        return op == "greater_than"
    age_hours = _parse_age_hours(value)
    if age_hours is None:
        return False  # malformed value
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

### Operator semantics

| Backend `op` value | Frontend label | What it matches |
|---|---|---|
| `less_than` | "newer than" | `file_age < N` — file detected recently |
| `greater_than` | "older than" | `file_age > N` — file detected long ago |

Backend keeps `less_than`/`greater_than` for consistency with the existing `file_size` operator vocabulary. Frontend labels them "newer than"/"older than" because that's how users read time comparisons.

### Frontend (`frontend/src/pages/SettingsPage.tsx`)

**1. Add `valueType: "duration"`** to the existing `CONDITION_TYPES` value-type enum (line 360):

```typescript
const CONDITION_TYPES: Record<string, {
  label: string; group: string;
  operators: { value: string; label: string }[];
  valueType: "select" | "text" | "number" | "duration";
}> = { ... }
```

**2. Add the `date_added` entry** in the dict, in the "File" group:

```typescript
date_added: {
  label: "Date Added",
  group: "File",
  operators: [
    { value: "less_than", label: "newer than" },
    { value: "greater_than", label: "older than" },
  ],
  valueType: "duration",
},
```

**3. Add a value-editor branch** for `valueType === "duration"` in the condition-editor rendering (currently a series of `cond.type === "..."` blocks around `SettingsPage.tsx:3160`):

```tsx
{cond.type === "date_added" && (() => {
  const m = (cond.value || "").match(/^(\d+)([hdw])$/);
  const num = m ? m[1] : "";
  const unit = m ? m[2] : "h";
  const setBoth = (n: string, u: string) =>
    updateCondition(idx, "value", `${n || "0"}${u}`);
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

The `updateCondition(idx, key, value)` helper already exists for the other value editors; the duration editor uses it to set the combined `${num}${unit}` string into `cond.value`.

## API Differences from Existing Conditions

None — `date_added` uses the same JSON shape as every other condition (`type`/`operator`/`value` strings), the same `_check_condition` dispatch, and the same persistence path. The only novelty is the **string format** of `value` (`"24h"` instead of e.g. `"24"`).

## Testing

### Backend

Add to `backend/tests/test_rule_resolver.py` (create if it doesn't exist):

- `test_date_added_newer_than_within_window` — `less_than 24h` with `new_detected_at = now - 2h` → True
- `test_date_added_newer_than_outside_window` — `less_than 24h` with `new_detected_at = now - 48h` → False
- `test_date_added_older_than_within_window` — `greater_than 7d` with `new_detected_at = now - 14d` → True
- `test_date_added_null_treated_as_ancient_for_older_than` — `greater_than 7d` with `new_detected_at = NULL` → True
- `test_date_added_null_returns_false_for_newer_than` — `less_than 7d` with `new_detected_at = NULL` → False
- `test_date_added_malformed_value_returns_false` — `value = "foo"` → False
- `test_date_added_units_conversion` — `less_than 1d` parses identically to `less_than 24h` (asserts parser correctness via the resolver's external behavior)

### Integration

Extend `backend/tests/test_watcher.py`:

- `test_auto_queue_date_added_rule_fires_for_new_file` — auto-queue a fake `ScannedFile`; mock `resolve_rules_for_batch` to return a rule with `conditions: [{type: "date_added", operator: "less_than", value: "24h"}]` and `queue_priority: 2`; assert the resulting `add_job` call gets `priority=2`.

(The condition matching itself isn't exercised here — that's covered by the unit tests above. This test just verifies the watcher correctly applies a rule that uses the new condition.)

### Frontend

No automated tests (matches codebase convention for SettingsPage.tsx).

### Live Validation

User creates a rule "Date Added newer than 24 hours → Queue Priority: Highest", drops a Sonarr file, confirms the resulting job appears with Highest priority. Drops an older file (manually queued from backlog), confirms it stays Normal.

## Rollout

Single release: v0.5.1 (patch — additive feature, no behavior change, no schema migration).

Implementation order:

1. Backend helper + condition handler + SELECT addition + unit tests
2. Frontend CONDITION_TYPES entry + duration value editor
3. Integration test in test_watcher.py
4. Version bump + CHANGELOG

## File Inventory

**Modified:**
- `backend/rule_resolver.py` — `_parse_age_hours` helper, `date_added` `ctype` branch, scan_data SELECT
- `backend/tests/test_rule_resolver.py` — NEW or extended with 7 unit tests
- `backend/tests/test_watcher.py` — 1 integration test
- `frontend/src/pages/SettingsPage.tsx` — `valueType: "duration"` added to enum, `date_added` CONDITION_TYPES entry, value editor branch
- `CHANGELOG.md` — v0.5.1 Added entry
- `VERSION` — `0.5.1`

**New:**
- `backend/tests/test_rule_resolver.py` (if no existing test file for rule_resolver)

**Database:** No schema changes — `new_detected_at` column already exists.

## Open Questions

None — design fully specified.
