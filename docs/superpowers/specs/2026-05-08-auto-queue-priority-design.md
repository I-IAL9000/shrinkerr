# Auto-Queue Priority — Design Spec

**Status**: Approved
**Date**: 2026-05-08
**Scope**: Let users control the queue priority of auto-queued files (Sonarr/Radarr-dropped new arrivals) so they're processed ahead of the existing library backlog.

## Goal

A user with thousands of older files in the queue who's working through a backlog wants newly-arrived files (auto-queued by the file watcher when Sonarr/Radarr drops them) to be processed **next**, not at the end of the queue.

Two ways to achieve this, both shipped together:

1. **Settings dropdown** `auto_queue_priority` (Normal / High / Highest) — the simple knob. Every auto-queued file gets this priority.
2. **Auto-queue runs through the rules engine** (default-on, no opt-in toggle) — closes a pre-existing gap where `watcher._auto_queue_new_files` was the only queue-entry path that *didn't* call `resolve_rules_for_batch`. Existing rules with `queue_priority` actions (already supported in the rules UI) now apply to auto-queued files. Gives finer-grained control: "TV genre Action → Priority: Highest", "Movies > 10GB → Priority: Normal", etc.

## Non-Goals

- New rule condition types (e.g. "file age" / "date added"). The GitHub issue's "alternatively" suggestion is out of scope — the existing rule condition vocabulary (directory, genre, tag, library, watched, codec, size, etc.) is already rich enough to express what most users need.
- Per-instance priorities (e.g. "Sonarr-dropped files get one priority, Radarr-dropped files get another"). Auto-queue treats all newly-detected files the same.
- Changes to the existing priority semantics (Normal / High / Highest) or the dispatch ordering. The queue worker's `ORDER BY priority DESC, queue_order ASC` is unchanged.

## Existing Infrastructure (already-built)

| Piece | Where | Status |
|---|---|---|
| `jobs.priority` column | `backend/database.py:240` | INTEGER, default 0. Values 0/1/2 = Normal/High/Highest. |
| `encoding_rules.queue_priority` column | `backend/database.py:245` | INTEGER, NULL default. |
| `add_job(priority=...)` parameter | `backend/queue.py:155` | Default 0. |
| Dispatch order | `backend/queue.py:446` | `ORDER BY priority DESC, queue_order ASC`. |
| Rules-engine `queue_priority` action | `backend/rule_resolver.py:25` | `"queue_priority": rule.get("queue_priority")` returned in resolved-rule dict. |
| Rules UI "Queue Priority" dropdown | `frontend/src/pages/SettingsPage.tsx:3421` | Normal/High/Highest select on rule editor. |
| `resolve_rules_for_batch` called by other queue paths | `backend/routes/jobs.py:203, 864, 1534` | add-bulk, add-from-scan, estimate all run rules. |

## Gap This Closes

`watcher._auto_queue_new_files` at `backend/watcher.py:319-385` calls `queue.add_job(...)` directly with global Settings defaults — `encoder=settings.get("default_encoder", "nvenc")`, `nvenc_preset=settings.get("nvenc_preset", "p6")`, etc. — and never invokes `resolve_rules_for_batch`. So today even a rule like "Action TV shows → encoder libx265, queue_priority High" doesn't fire for auto-queued files. This is a bug-shaped gap, not just a missing feature.

## Architecture

### New setting

`auto_queue_priority` — encoding-settings INTEGER. Values:

```
0 = Normal   (default; existing behavior)
1 = High
2 = Highest
```

Stored as a string in the `settings` table like all other encoding settings (`"0" | "1" | "2"`); parsed to int on read. Surfaces in `GET /api/settings/encoding` and `PUT /api/settings/encoding` round-trip.

### Auto-queue resolution flow (new)

For each batch of new files the watcher detects:

```
1. Resolve rules:  rule_results = await resolve_rules_for_batch(
                       [f.file_path for f in scanned_files],
                       extra_context=None,  # auto-queue has no nzbget category
                   )
2. For each file:
   matched_rule = rule_results.get(file.file_path)
3. Short-circuit if rule action is skip or ignore:
   if matched_rule and matched_rule.get("action") in ("skip", "ignore"):
       continue   # don't enqueue this file
4. Pick encoder:   matched_rule.encoder         OR settings.default_encoder
5. Pick preset:    matched_rule.nvenc_preset    OR settings.nvenc_preset
6. Pick cq:        matched_rule.nvenc_cq        OR settings.nvenc_cq
   (same for libx265_crf, libx265_preset, target_resolution, audio_codec,
    audio_bitrate — these are the kwargs `add_job` actually accepts;
    qsv_*/vaapi_* settings are NOT passed per-job, they're worker-side
    only)
7. Pick priority:  matched_rule.queue_priority  OR settings.auto_queue_priority OR 0
8. add_job(file_path, ..., priority=picked_priority)
```

**Notes:**

- `resolve_rules_for_batch` is `async` and **must be awaited**. `_auto_queue_new_files` is already an `async def` so this is straightforward.
- `extra_context=None` is passed because auto-queue has no nzbget category to surface; matches what the watcher knows. The existing call site at `routes/jobs.py:904` uses `extra_context` only when the request payload carries `nzbget_category`.
- Action short-circuit mirrors the precedent at `routes/jobs.py:946-948`. The full list of actions that suppress enqueue: `"skip"`, `"ignore"`. Any other action (or no rule match) proceeds to enqueue.

### Priority precedence — OR-cascade (not `max()`)

The spec's precedence is **OR-cascade** — first non-None value wins. Specifically:

```python
priority = (
    (matched_rule or {}).get("queue_priority")   # 0/1/2 from rule, or None
    if matched_rule else None
)
if priority is None:
    priority = int(settings.get("auto_queue_priority", "0") or 0)
```

This **differs** from the manual-queueing path at `routes/jobs.py:973`, which uses `max(payload.priority, rule.get("queue_priority") or 0)` — i.e. for manual queueing the rule can only *raise* the user's per-job priority, never lower it.

**Why different**: manual queueing has an explicit per-job `payload.priority` chosen by the user clicking "Add to queue with Priority X" — the rule shouldn't be able to override that downward. Auto-queue has no per-file user choice — only the global Settings fallback — so the rule fully wins (including lowering), since the rule is the per-file customization mechanism by definition.

Documented here so the implementer (a) doesn't copy the `max(...)` semantics from `routes/jobs.py:973` without thinking, and (b) the future reader understands the divergence is intentional.

### Settings UI

A new dropdown in the **Automation** section of Settings, next to the existing "Auto-queue new files" checkbox. Labels:

```
Auto-queue priority:  [ Normal ▼ ]
                        High
                        Highest

Newly-detected files get this priority when auto-queued. Rules with a Queue
Priority action override this setting.
```

Visually disabled when "Auto-queue new files" is unchecked (the dropdown is meaningless without auto-queue enabled).

## Integration Points (files to modify)

| File | Change |
|---|---|
| `backend/models.py` | Add `auto_queue_priority: Optional[Any] = None` to `SettingsUpdate` (matches the existing `*_pause_stream_threshold` pattern for int-with-string-coercion fields). |
| `backend/routes/settings.py` | Three additions: `_ENCODING_DEFAULTS` entry (`"auto_queue_priority": "0"`), GET response builder (`result["auto_queue_priority"] = int(merged.get("auto_queue_priority", "0") or 0)`), PUT save handler (`updates["auto_queue_priority"] = str(int(update.auto_queue_priority))` with clamp to 0..2). |
| `backend/watcher.py` | `_auto_queue_new_files` calls `resolve_rules_for_batch(file_paths)` once for the whole batch, then iterates resolved rules per file. Build the `add_job` kwargs dict by overlaying rule output on top of global Settings defaults. Read `auto_queue_priority` from settings as the fallback. |
| `frontend/src/api.ts` | Settings type already untyped (`apiFetch<any>`); no change needed there. The dropdown reads `encoding?.auto_queue_priority`. |
| `frontend/src/pages/SettingsPage.tsx` | New dropdown in the Automation section. Mirrors an existing `<select>` control's styling. Bound to `encoding.auto_queue_priority`. Save handler includes the new key in the `updateEncodingSettings({...})` payload. |
| `CHANGELOG.md` | One entry under Added (the dropdown) and one under Fixed (the rules-engine gap). |
| `VERSION` | bump to `0.5.0` — minor version because the behavior change for users with existing `queue_priority` rules is user-visible (those rules now apply to auto-queue, where previously they were silently skipped). |

## Behavior Change Notes for Existing Users

Three scenarios, in order of likely impact:

1. **Auto-queue off**: zero change. The dropdown is rendered but disabled.
2. **Auto-queue on, no rules with `queue_priority`**: behavior identical, except the new dropdown is now visible. Default value (Normal) matches today's behavior.
3. **Auto-queue on, rules with ANY action defined**: those rules **now apply** to auto-queued files — not just `queue_priority`. Previously the entire rules engine was silently bypassed for this code path. Specifically: encoder, nvenc_preset, nvenc_cq, libx265_crf, libx265_preset, target_resolution, audio_codec, audio_bitrate, queue_priority, AND skip / ignore actions all now apply to auto-queued files. If a user had a "Movies → encoder libx265, preset slow" rule, newly-dropped movies now encode with libx265 + slow preset instead of falling back to the global default_encoder. Called out in the v0.5.0 CHANGELOG under **Fixed** as a behavioral change. Users who have rules they thought only applied to manual queueing should review them after upgrade.

## Testing

### Backend

- One round-trip test in `backend/tests/test_routes.py` mirroring the Jellyfin/Emby round-trip pattern: PUT `auto_queue_priority: 2`, GET, assert the value comes back as 2.
- One unit-style test in `backend/tests/test_watcher.py` (new file) that mocks `resolve_rules_for_batch` to return a rule with `queue_priority: 2`, sets `auto_queue_priority: 1` in settings, calls `_auto_queue_new_files` with a fake `ScannedFile`, and asserts the resulting `add_job` call received `priority=2` (rule wins over global setting). One companion test with no rule, asserting `priority=1` (global setting wins over default 0).

### Frontend

No automated tests (matches existing codebase convention for `SettingsPage.tsx` changes — user validates live).

### Live Validation

User triggers a Sonarr/Radarr download into a watched folder. Confirms:

1. The job appears in the Pending tab with the priority indicator matching the resolved priority (rule's `queue_priority` if any, else Settings dropdown value).
2. If the queue currently has Normal-priority backlog jobs, the new auto-queued file appears ABOVE them in the dispatch order (queries `/api/jobs?status=pending&limit=1` should return the new file first when priority is High/Highest).

## Rollout

Single release: v0.5.0. No experimental gate.

Implementation order (independently shippable but bundled into one release):

1. Settings model + persist (backend)
2. Settings UI dropdown (frontend)
3. Rules engine integration in `_auto_queue_new_files`
4. Tests
5. CHANGELOG + VERSION bump

## Open Questions

None — design fully specified.

## File Inventory

**Modified:**
- `backend/models.py` (1 field)
- `backend/routes/settings.py` (3 additions: defaults / response / save)
- `backend/watcher.py` (`_auto_queue_new_files` body rewritten to use rules engine; ~40 LOC change)
- `backend/tests/test_routes.py` (1 round-trip test)
- `backend/tests/test_watcher.py` (new, ~50 LOC)
- `frontend/src/pages/SettingsPage.tsx` (dropdown in Automation section + save-handler key, ~15 LOC)
- `CHANGELOG.md` (one Added + one Fixed entry)
- `VERSION` → `0.5.0`

**Database:** No schema changes — `jobs.priority` and `encoding_rules.queue_priority` columns already exist from prior releases.

**New:**
- `backend/tests/test_watcher.py` (only if no existing watcher tests; can fold into a different file if conventions differ — check during implementation).
