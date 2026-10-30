# VMAF Target: Per-Rule Targets and a Robust Search — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rules can set or switch off the VMAF target. The search finds the
highest quality value (most compression) that holds the target, robustly.
A finished file that misses is re-encoded once.

**Architecture:**
- **Search:** `backend/vmaf_target.py` gets a bracket + bisect search with a
  half-step, a sample count set by duration, a pass rule of mean plus worst
  sample, and a per-folder memory of where searches landed.
- **Target per job:** the job's target is resolved from its rule at queue time
  and stored on the job (like `vmaf_min_score`). `rule_actions.settings_overrides`
  turns it into the `vmaf_target_enabled` / `vmaf_target_score` settings that
  `convert_file` and remote workers already read.
- **Re-encode:** `convert_file` runs the one re-encode by calling itself with a
  lower quality value, holding the first encode aside as a fallback.

**Tech Stack:** Python 3.13, FastAPI, aiosqlite, ffmpeg/libvmaf, pytest
(+pytest-asyncio); React + TypeScript + i18next (en, es).

**Spec:** `.superpowers/specs/2026-10-30-vmaf-target-robust-design.md` (read it
first; section numbers below refer to it).

## Global Constraints

- **Search range:** CQ_BEST = 14, CQ_WORST = 42 (NVENC CQ scale).
- **Samples:** 10 s each. n = clamp(round(duration / 480), 3, 6), at
  (i + 0.5) / n of the duration. Files under 120 s are never searched.
- **Pass rule:** a quality passes when mean ≥ target AND lowest sample ≥
  target − 2.
- **Half-step:** NVENC (HEVC) and libx265 (HEVC) only. Whole steps for QSV,
  VAAPI, VideoToolbox and any AV1 output.
- **Re-encode:** when the finished score is < target − 1.0 and not
  measurement-uncertain. Once only. Never after a search that ended at the
  floor without reaching the target.
- **Rule storage:** `encoding_rules.vmaf_target` and `jobs.vmaf_target` are
  REAL. NULL = Settings', 0 = Off, otherwise the score. A score must be within
  80–99 and ≥ the minimum that applies to the rule.
- **The VMAF minimum** rejects only when Settings' VMAF analysis is on (as
  today). The effective target is max(target, active minimum).
- **Strings:** every new user-visible string needs en + es entries. es
  plurals need `_many`. Server message codes go through the existing
  catalogs; the contract tests enforce this.
- **Tests:** don't import `backend.main` at test-module import time.
  Real-encode tests skip unless ffmpeg has libvmaf (`vmaf_target._tools()`
  pattern).
- **Media share:** never rename onto an existing file.
- **CHANGELOG:** one-liners under `## [Unreleased]`, with a bold lead for
  notable entries (repo CLAUDE.md).
- **Verification:** frontend changes are checked with `cd frontend && npm run build` (not `tsc --noEmit`).

## Review Focus

1. **A rule set to Off while Settings' target is on.** The job must never
   search, and must encode at the rule's quality. Test in Task 3.
2. **Target on, Settings' VMAF analysis off.** The finished file is still
   scored, and that score can trigger the re-encode. Nothing is rejected for
   being under the (hidden) minimum, and the job shows its score. Test in
   Task 5.
3. **A re-encode rejected for being larger than the original.** The held
   first encode is placed instead, through a fresh path; no rename lands on
   an existing file. Test in Task 5.
4. **An older remote worker.** It reads only `vmaf_target_enabled` /
   `vmaf_target_score`, so a rule's Off must arrive as enabled = false. Test
   in Task 3.
5. **VideoToolbox, where CQ ≥ 29 all map to q40.** The search must not
   encode the same `q:v` twice. Test in Task 1.

---

### Task 1: The search (pure)

**Files:**
- Modify: `backend/vmaf_target.py`. Rewrite `search`; add `sample_points`,
  `passes` and `fractional_steps`; change `CQ_WORST`.
- Test: `backend/tests/test_vmaf_target.py`. Replace `test_the_search`; add
  the tests below.

**Interfaces:**
- Produces:
  - `CQ_BEST = 14`, `CQ_WORST = 42`, `MAX_SAMPLE_DIP = 2.0`.
  - `sample_points(duration: float) -> list[float]`: the sample start times in seconds.
  - `passes(values: list[float], target: float) -> bool`.
  - `fractional_steps(encoder: str, output_codec: str | None) -> bool`.
  - `async search(score: Callable[[float], Awaitable[list[float]]], target: float, start: float, *, best: float = CQ_BEST, worst: float = CQ_WORST, half_steps: bool = False, setting_key: Callable[[float], object] = lambda cq: cq) -> dict`.

    It returns `{"cq": float, "vmaf": float (mean at cq), "min": float (lowest sample at cq), "reached": bool, "rounds": int, "scores": {cq: {"mean": float, "min": float}}}`.
    `score(cq)` returns the per-sample values. `setting_key` maps a CQ to the
    encoder's actual setting; CQs with the same key reuse one scoring.

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.asyncio
async def test_bracket_then_bisect():
    asked = []
    async def score(cq):                       # one VMAF point per CQ step, samples ±0.5
        asked.append(cq)
        return [111.0 - cq - 0.5, 111.0 - cq + 0.5]
    r = await vmaf_target.search(score, 93, start=20)
    assert (r["cq"], r["reached"]) == (18, True) and r["vmaf"] == 93.0 and r["min"] == 92.5
    assert asked[0] == 20 and len(asked) == len(set(asked)) and r["rounds"] == len(asked) <= 6

@pytest.mark.asyncio
async def test_the_ceiling_is_42():
    async def score(cq): return [99.0, 99.0]
    assert (await vmaf_target.search(score, 93, start=36))["cq"] == 42

@pytest.mark.asyncio
async def test_out_of_reach_uses_the_floor():
    async def score(cq): return [80.0, 80.0]
    r = await vmaf_target.search(score, 93, start=30)
    assert (r["cq"], r["reached"]) == (14, False)

@pytest.mark.asyncio
async def test_a_hard_sample_fails_the_quality():
    async def score(cq): return [96.0, 96.0, 90.5]          # mean 94.2 but one sample 2.5 under
    assert not (await vmaf_target.search(score, 93, start=30))["reached"]
    assert vmaf_target.passes([95.0, 95.0, 91.0], 93) and not vmaf_target.passes([95.0, 95.0, 90.9], 93)

@pytest.mark.asyncio
async def test_a_half_step():
    async def score(cq): return [111.0 - cq * 1.0] * 2       # passes up to CQ 18.0; 18.5 → 92.5 fails
    assert (await vmaf_target.search(score, 93, start=20, half_steps=True))["cq"] == 18.0
    async def gentle(cq): return [102.0 - cq * 0.5] * 2      # 18.0 → 93.0, 18.5 → 92.75: stays 18
    assert (await vmaf_target.search(gentle, 93, start=20, half_steps=True))["cq"] == 18.0
    async def flat(cq): return [100.6 - cq * 0.4] * 2        # 19.0 → 93.0, 19.5 → 92.8 fails; 19 kept
    assert (await vmaf_target.search(flat, 93, start=20, half_steps=True))["cq"] == 19.0

@pytest.mark.asyncio
async def test_one_scoring_per_encoder_setting():           # VideoToolbox: CQ ≥ 29 is all q40
    asked = []
    async def score(cq):
        asked.append(cq); return [99.0, 99.0]
    await vmaf_target.search(score, 93, start=30, setting_key=lambda cq: min(cq, 29))
    assert len([cq for cq in asked if cq >= 29]) == 1

def test_sample_points():
    assert len(vmaf_target.sample_points(25 * 60)) == 3 and len(vmaf_target.sample_points(120 * 60)) == 6
    assert len(vmaf_target.sample_points(50 * 60)) == 6 and len(vmaf_target.sample_points(150)) == 3
    assert vmaf_target.sample_points(1500)[0] == pytest.approx(1500 / 6 - 5)   # centred on (i+0.5)/n
    assert all(0 <= s <= 1500 - 10 for s in vmaf_target.sample_points(1500))

def test_fractional_steps():
    assert vmaf_target.fractional_steps("nvenc", None) and vmaf_target.fractional_steps("libx265", "hevc")
    assert not any(vmaf_target.fractional_steps(e, None) for e in ("qsv", "vaapi", "videotoolbox"))
    assert not vmaf_target.fractional_steps("nvenc", "av1")
```

- [ ] **Step 2: Run them and see them fail.**
  `cd dev6 && ../venv/bin/python -m pytest -q -p no:cacheprovider backend/tests/test_vmaf_target.py`.
  Expect failures: no `sample_points`, `passes` or `fractional_steps`; the
  old search signature.

- [ ] **Step 3: Implement** the interfaces above in `backend/vmaf_target.py`.

  `sample_points` centres each 10 s sample on (i + 0.5) / n of the
  duration, clamped to [0, duration − 10].

  The search algorithm, which the tests leave open:

```
key-cached at(cq) -> (mean, min)        # rounds = number of distinct keys scored
cq = clamp(start, best, worst)
if passes(at(cq)):  lo = cq; step up by 2 while next ≤ worst and passes → lo; first fail → hi (or worst+1 if none)
else:               hi = cq; step down by 2 while next ≥ best and fails → hi; first pass → lo (or none → floor)
bisect integers strictly between lo and hi
if half_steps and lo found and lo + 0.5 < hi: try lo + 0.5, keep it if it passes
no pass anywhere → {"cq": best, "reached": False}
```

- [ ] **Step 4: Run the tests and see them pass.** Same command. Expect all
  of `test_vmaf_target.py`'s pure tests to pass; the real-encode tests are
  handled in Task 2.

- [ ] **Step 5: Commit**: `git commit -m "feat(vmaf-target): bracket-and-bisect search with half steps"`.

---

### Task 2: Sampling and `find_quality`

**Files:**
- Modify: `backend/vmaf_target.py` (`find_quality`; replace `SAMPLE_AT`).
- Modify: `backend/converter.py` (the `vmaf_target` block near "VMAF target
  quality — this title's own quality").
- Test: `backend/tests/test_vmaf_target.py`. Update the `quick` fixture,
  `test_the_quality_found` and `test_a_conversion_uses_it`.

**Interfaces:**
- Consumes: Task 1's functions.
- Produces:
  - `async find_quality(input_path, encoder, duration, target, start, convert_kwargs=None, *, output_codec: str | None = None) -> dict | None`.
    It returns Task 1's dict plus `"target"`, `"samples": int` and
    `"seconds": float` (wall time of the search).
  - `convert_file(..., vmaf_target_start_cq: Optional[float] = None)` is a
    new keyword. It's the remembered start; when it's None, the job's own CQ
    is the start.
  - `encoding_stats["vmaf_target"]` becomes `{target, cq, vmaf, min, reached, rounds, samples, seconds}`.

- [ ] **Step 1: Update the tests.**
  - The `quick` fixture monkeypatches `vmaf_target.sample_points` to
    `lambda d: [4.0, 12.0]` and `SAMPLE_SECONDS` to 3.
  - `test_the_quality_found` asserts the found CQ passes and the next whole
    step fails. It reads per-CQ `{"mean","min"}` from `scores`, and checks
    `result["samples"] == 2` and `result["seconds"] > 0`.
  - `test_a_conversion_uses_it` asserts `stats["vmaf_target"]` has the keys
    above.
  - A new `test_the_remembered_start` calls
    `convert_file(..., vmaf_target_start_cq=26)`, spies `find_quality`, and
    asserts it was called with `start=26`.

- [ ] **Step 2: Run them and see them fail.** Same command, with ffmpeg
  plus libvmaf installed (true locally).

- [ ] **Step 3: Implement.**
  - `find_quality` cuts one sample per `sample_points(duration)` and scores
    all of them per CQ. It calls `search(..., half_steps=fractional_steps(encoder, output_codec), setting_key=...)`;
    `setting_key` is `encoding_estimates.quality_settings(encoder, cq)`'s
    value tuple for whole CQs, and the CQ itself otherwise.
  - It times the search and logs each round:
    `[VMAF-TARGET] CQ 32: VMAF 93.4 (lowest 92.1) on 3 samples`.
  - In `convert_file`:
    - pass `output_codec`;
    - use `vmaf_target_start_cq` as `start` when given;
    - store the new `encoding_stats["vmaf_target"]` keys;
    - extend the `vmaf_target` dict in the result with `min`, `rounds`,
      `samples`, `seconds`;
    - keep fractional `cq` / `crf` as floats in the command
      (`-cq 32.5`, `crf=34.5`).
  - The search's progress step names the round:
    `"VMAF target: CQ 32 (round 3)"`. Send it through `progress_callback`
    with `step_key="steps.vmafTargetRound"` and `step_params={"cq", "round"}`,
    and add en/es entries to `serverJobs.json` `steps`. Add a
    `websocket.STEP_KEYS` entry only if a fixed English label is used.

- [ ] **Step 4: Run the tests and see them pass.** Run the same file plus
  `backend/tests/test_remote_worker_parity.py`. Expect PASS.

- [ ] **Step 5: Commit**: `feat(vmaf-target): samples scale with the file; the search starts where it's told`.

---

### Task 3: Per-rule targets (rule → job → settings → encode / worker)

**Files:**
- Modify: `backend/database.py`. Add `vmaf_target REAL DEFAULT NULL` to
  `encoding_rules` and `jobs`, in the v0.10.0 per-rule actions loop.
- Modify: `backend/routes/rules.py`.
  - `RuleCreate` / `RuleUpdate` get `vmaf_target: Optional[float] = Field(default=None, ge=0, le=99)`.
  - Add it to the INSERT and to `update_rule`.
  - Validate it with `ApiError(code="rules.vmafTargetRange" | "rules.vmafTargetBelowMinimum")`.
- Modify: `backend/rule_resolver.py` (`_make_rule_result` keys) and
  `backend/rule_actions.py` (`job_overrides`, `settings_overrides`).
- Modify: `backend/queue.py` (`add_jobs_bulk` INSERT column) and
  `backend/routes/jobs.py`. The job dicts already spread
  `**job_overrides(rule)`; check the policies path too, around line 1347.
- Modify: `backend/routes/nodes.py`. In the assignment, settings come from
  `settings_overrides(job)` before the global ones.
- Modify: `backend/converter.py`. Effective target = max(target, active
  minimum); the minimum is active only when VMAF analysis is on and > 0.
- Modify: the en/es `serverErrors` catalog (wherever `rules.*` ApiError codes
  live), adding `rules.vmafTargetRange` and `rules.vmafTargetBelowMinimum`.
- Test: create `backend/tests/test_vmaf_target_rules.py`.

**Interfaces:**
- Produces:
  - `settings_overrides(job)` returns `{"vmaf_target_enabled": False}` when
    `job["vmaf_target"] == 0`.
  - It returns `{"vmaf_target_enabled": True, "vmaf_target_score": v}` when
    `v > 0`.
  - It returns nothing about the target when `job["vmaf_target"]` is None.

- [ ] **Step 1: Write the failing tests:**
  - `test_a_rule_off_never_searches`. Global target on; a rule with
    `vmaf_target=0` and `nvenc_cq=38`. Queue a file matching it through
    `add_jobs_from_scan`. Assert `jobs.vmaf_target == 0`, and that
    `settings_overrides(job)` gives `vmaf_target_enabled is False`.
  - `test_a_rule_target_without_the_global_setting`. Global off; the rule
    sets 95. Assert the overrides give `{enabled: True, score: 95.0}`.
  - `test_no_rule_follows_settings`. Assert that, with no rule target,
    `settings_overrides` has no `vmaf_target_*` keys.
  - `test_the_target_is_kept_at_or_above_the_minimum`. Create a rule with
    `vmaf_target=85` and `vmaf_min_score=88` through the rules route
    function; it raises an ApiError with code `rules.vmafTargetBelowMinimum`.
    With `vmaf_target=79`, the code is `rules.vmafTargetRange`.
  - `test_an_older_worker_gets_off_as_disabled`. Build a node assignment for
    a job with `vmaf_target=0` (`routes.nodes` helper as in
    `test_vmaf_target.test_remote_workers_get_the_settings`). Assert
    `assigned["vmaf_target_enabled"] is False`.
  - `test_the_minimum_raises_the_target`. In `convert_file(command_only=True)`,
    with target 85 (rule) and Settings' minimum 90 with analysis on, a spy
    on `find_quality` receives target 90.

- [ ] **Step 2: Run the tests and see them fail.**
  `backend/tests/test_vmaf_target_rules.py`. Expect FAIL: there's no column
  or field yet.

- [ ] **Step 3: Implement** the files above.

- [ ] **Step 4: Run the tests and see them pass.** Run that file, plus
  `test_vmaf_target.py`, `test_remote_worker_parity.py` and
  `test_message_codes.py`. Expect PASS.

- [ ] **Step 5: Commit**: `feat(rules): a rule sets its own VMAF target, or switches it off`.

---

### Task 4: Remembering where searches land

**Files:**
- Modify: `backend/database.py`. Add the table
  `vmaf_target_memory (folder TEXT, target REAL, encoder TEXT, output_codec TEXT, cq REAL, updated_at TEXT, PRIMARY KEY (folder, target, encoder, output_codec))`.
- Modify: `backend/vmaf_target.py`. Add
  `async remembered_cq(db, file_path, target, encoder, output_codec) -> float | None`,
  which ignores rows older than 90 days, and
  `async remember(db, file_path, target, encoder, output_codec, cq) -> None`
  (an upsert). The folder is `os.path.dirname(file_path)`.
- Modify: `backend/queue.py`.
  - Before a local `convert_file`, look up the start.
  - After the job completes with `encoding_stats["vmaf_target"]`, remember
    the CQ.
  - Do both only when the job's effective target is on.
- Modify: `backend/routes/nodes.py` and `backend/worker_mode.py`.
  - Send `vmaf_target_start_cq` with the assignment.
  - The worker passes it to `convert_file`.
  - `report_complete` remembers the CQ from the reported `encoding_stats`.
- Test: `backend/tests/test_vmaf_target_memory.py`.

**Interfaces:**
- Consumes: Task 2's `convert_file(..., vmaf_target_start_cq=...)` and the
  `encoding_stats["vmaf_target"]["cq"]`.

- [ ] **Step 1: Write the failing tests:**
  - `test_remember_and_recall`. Remember (folder "/m/Show/Season 1",
    target 93, "nvenc", "hevc", 31.5). Recall it from
    "/m/Show/Season 1/E02.mkv" → 31.5. A different target, encoder or codec
    returns None. A row dated 91 days ago returns None.
  - `test_a_local_job_starts_where_the_last_one_landed`. Monkeypatch
    `converter.convert_file` in queue to capture kwargs and return a
    completed result with `vmaf_target.cq = 30`. Run two jobs in the same
    folder through `QueueWorker._process_job`. The second gets
    `vmaf_target_start_cq == 30`.

- [ ] **Step 2: Run the tests and see them fail.**

- [ ] **Step 3: Implement.**

- [ ] **Step 4: Run the tests and see them pass.** Run them plus the queue
  and node test files that touch `convert_file` kwargs:
  `test_remote_worker_parity.py`, `test_queue.py`.

- [ ] **Step 5: Commit**: `feat(vmaf-target): searches start where the last one in the folder landed`.

---

### Task 5: Checking the finished file, and the one re-encode

**Files:**
- Modify: `backend/converter.py` (`convert_file`).
  - Capture `_call_args = dict(locals())` as the first statement; it's
    every parameter.
  - Add an internal keyword `_target_retry: Optional[dict] = None`.
  - VMAF runs when the analysis setting is on OR a target applied
    (`vmaf_target` set).
  - The minimum rejects only when the analysis setting is on.
  - Add the re-encode block after the VMAF score is known and before the
    minimum's rejection block.
- Modify: `backend/queue.py` and `backend/worker_mode.py`, only if a result
  key they read changes. Keep `vmaf_score` and `encoding_stats` as today.
- Test: create `backend/tests/test_vmaf_target_reencode.py`.

**Interfaces:**
- Consumes: Task 2's `vmaf_target` dict (`cq`, `reached`, `target`, `scores`).
- Produces:
  - `encoding_stats["vmaf_target"]["reencoded"] = {"from_cq": float, "from_vmaf": float}`
    on the result that was kept after a re-encode.
  - `encoding_stats["vmaf_target"]["first_kept"] = True` when the fallback
    placed the first encode.
  - `encoding_stats["vmaf_target"]["checked_score"]`: the finished file's
    score.
  - `_target_retry_cq(vmaf_target: dict, score: float, half_steps: bool) -> float`
    is a module function. It returns the highest searched CQ whose sample
    mean ≥ target + (target − score); else cq − max(1, ceil(target − score)).
    It's clamped to CQ_BEST and rounded to whole steps unless `half_steps`.

- [ ] **Step 1: Write the failing tests.** Run them on real clips (needs
  libvmaf and libx265, as `needs_vmaf`). Stub `find_quality` to return a
  fixed dict, and stub `_run_libvmaf_pass` so the first call scores 90.0
  and later calls 95.0 (target 93):
  - `test_a_miss_is_reencoded_once`. `convert_file` encodes twice. The
    second command has the lower CRF. The result's
    `vmaf_target.reencoded == {"from_cq": 30, "from_vmaf": 90.0}`. Only one
    output file exists, and no `.converting` or held files are left.
  - `test_within_a_point_is_kept`. A first score of 92.2 gives no
    re-encode.
  - `test_the_floor_isnt_retried`. With `reached=False` and `cq=14`, there's
    no re-encode.
  - `test_a_larger_reencode_keeps_the_first`. Stub `_run_libvmaf_pass` as
    above. Make the second encode "larger": monkeypatch the second run's
    output size check by giving `original_size` a size between the two
    encodes, using a CRF pair that differs strongly. Assert the placed file
    is the first encode (its CRF in `encoding_stats`), `first_kept` is True,
    and no file was renamed onto an existing path. Spy `os.rename` and
    `Path.rename` for an existing destination, as `test_dovi.py` does.
  - `test_analysis_off_scores_but_never_rejects`. Settings:
    `vmaf_analysis_enabled=false`, `vmaf_min_score=88`, target 93. The stub
    scores 80 then 85. The result keeps the re-encode, with
    `vmaf_score == 85` and no `vmaf_rejected`.
  - `test_analysis_on_still_rejects_below_the_minimum`. Analysis on,
    minimum 88. The stub scores 80 then 85. The result has
    `vmaf_rejected`, the original is kept, and no temp files are left.
  - `test_the_retry_cq` (pure): `_target_retry_cq({"cq": 30, "target": 93,
    "scores": {28: {"mean": 95.5, "min": 94}, 30: {"mean": 93.2, "min": 92}}}, 91.0, False) == 28`;
    with no curve, `… == 28` (30 − 2); with a half step,
    `({"cq": 30.5, …, "scores": {}}, 92.5, True) == 29.5`.

- [ ] **Step 2: Run the tests and see them fail.**

- [ ] **Step 3: Implement.** The re-encode block's order, which the tests
  leave open:

```
if vmaf_target and not _target_retry and vmaf_score is not None and not vmaf_uncertain
   and vmaf_score < target - 1.0 and not (not vmaf_target["reached"] and cq <= CQ_BEST):
    held = <unique new path beside temp_path, e.g. "<stem>.vmaf-first-<uuid8>.mkv">   # never an existing name
    rename temp_path -> held
    retry = await convert_file(**{**_call_args, "override_cq": new_cq, "override_crf": <libx265 crf for new_cq>,
                                  "_target_retry": {"from_cq": cq, "from_vmaf": vmaf_score}})
    if retry placed its output (success, not vmaf_rejected, not skipped_larger, no error):
        delete held; return retry   (its encoding_stats.vmaf_target carries "reencoded")
    first_ok = (not analysis_on or min_score <= 0 or vmaf_score >= min_score)
    if first_ok: rename held -> temp_path (temp_path is free again: the retry removed its own temp);
                 mark first_kept and continue the normal placement path below
    else: delete held; return the retry's rejection result (original kept)
```

  Within a call where `_target_retry` is set:
  - skip the VMAF-target search;
  - don't hold larger outputs for "Keep anyway" (delete them);
  - never re-encode again.

- [ ] **Step 4: Run the tests and see them pass.** Run the new file plus
  `test_vmaf_target.py`, `test_vmaf_alignment.py`, `test_dovi.py` and
  `test_finalize_safety.py`. Expect PASS.

- [ ] **Step 5: Commit**: `feat(vmaf-target): a finished file that misses the target is re-encoded once`.

---

### Task 6: Add to Queue's estimate

**Files:**
- Modify: `backend/routes/jobs.py` (`_estimate_jobs_impl`).
- Test: `backend/tests/test_vmaf_target_estimate.py`.

**Interfaces:**
- Consumes: Task 3's rule field. The rule result dict carries `vmaf_target`.
- Produces two response keys:
  - `"vmaf_target": {"targets": {"93": int, "95": int}, "fixed_by_rule": int}`
    counts only files that get a conversion (`jt` in convert / combined).
    The effective target per file is: rule value if > 0; else off if the
    rule value is 0; else Settings'. Each target is raised to the active
    minimum.
  - `"vmaf_target_search_seconds": float` is the median of
    `json_extract(encoding_stats,'$.vmaf_target.seconds')` over the last 20
    completed jobs that have it; 180.0 when there are none. It's added once
    per target file to `estimated_time_seconds` before the parallel
    division.

- [ ] **Step 1: Write the failing tests.** Use the `library` fixture
  pattern of `test_direct_play_choices.py`: four h264 files, one matched by
  a rule with target 0 and one by a rule with target 95.
  - `test_the_estimate_counts_targets`: Settings' target is on at 93. The
    response gives `targets == {"93": 2, "95": 1}` and `fixed_by_rule == 1`.
  - `test_the_search_time_is_added`: seed two completed jobs whose
    `encoding_stats` have `vmaf_target.seconds` of 100 and 200. Compared
    with the target off, the time grows by 3 × 150 / parallel.
  - `test_target_off_says_nothing`: with Settings' target off and no rules,
    `targets == {}` and `fixed_by_rule == 0`.

- [ ] **Step 2: Run the tests and see them fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run them, plus `test_direct_play_choices.py` and
  `test_savings_estimates.py`, and see them pass.**
- [ ] **Step 5: Commit**: `feat(estimate): Add to Queue counts the files the VMAF target searches`.

---

### Task 7: The UI

**Files:**
- Modify: `frontend/src/pages/SettingsPage.tsx`.
  - Rule form state: add `vmaf_target` (`""` | `"off"` | a number string).
  - Load it from rules, around line 4225.
  - Save it, around line 4793: `""` → null, `"off"` → 0, otherwise
    parseFloat. Clear it for non-encode actions.
  - Add the field next to "VMAF minimum", around line 4729: a select with
    Settings' / Off / Custom, plus a number input (80–99) when Custom.
  - Rule summary chip, around line 4194: `VMAF target 95` or
    `VMAF target off`.
  - The VMAF target hint in Settings → Video (around line 1704) says rules
    can override it.
- Modify: `frontend/src/components/EstimateModal.tsx`. In "What will
  happen", add one line per target, plus the fixed-by-rule line, from
  `estimate.vmaf_target`. The quality slider's hint, when any target
  applies: "the starting point for the VMAF target".
- Modify: `frontend/src/components/JobListItem.tsx`. After the existing
  `vmafTargetFound` / `Missed` line:
  - the lowest sample;
  - when `reencoded`: "Re-encoded at {{quality}}: the first encode
    ({{from}}) scored {{fromVmaf}}";
  - when `first_kept`: "Kept the first encode: the re-encode was rejected".
- Modify: `frontend/src/components/SetupWizard.tsx`, only if a new string
  key is needed. The setup guide's quality step already offers
  **Automatic** (VMAF target 93) as the badged, preselected "Recommended"
  option on new installs, and it stays that way. Update its copy in
  `dashboard.json` `setup.quality.automatic` (en + es):
  - `slower`: "Slower: 3 to 6 ten-second samples of each file (by length)
    are test-encoded first, and a file that misses the target is encoded
    again."
  - Add one sentence to `desc`: rules can set their own target or switch it
    off.
- Modify: `frontend/src/i18n/locales/{en,es}/settingsIntegrations.json`
  (rules form), `scannerModals.json` (estimate lines), `queue.json`
  (report) and `dashboard.json` (setup guide). es plurals need `_many`.

- [ ] **Step 1: Build:** `cd frontend && npm run build`. Expect
  `✓ built`.
- [ ] **Step 2: Preview check.**
  - Use `shrinkerr-e2e-develop` in `/Users/hal9000/Documents/Claude/.claude/launch.json`.
    Point it at this clone first; the e2e media in `scratchpad/e2e2/media`
    may need regenerating after the /tmp purge.
  - Check that creating a rule with Off saves and shows the chip.
  - Check that Custom 85 with Settings' minimum 88 shows the API error toast.
  - Check that Add to Queue with Settings' target on shows "VMAF target 93
    … (N files)".
- [ ] **Step 3: Commit**: `feat(ui): VMAF target per rule, in Add to Queue and the job report`.

---

### Task 8: Docs, changelog, full suite, push

**Files:**
- Modify: `CHANGELOG.md`. Add one Added entry with a bold lead, e.g.
  "**VMAF target per rule, and a search that holds it**: …", plus a Changed
  entry for the ceiling and the re-encode.
- Modify: `docs/encoding-guide.md`, the section that explains the VMAF
  target. Add two sentences: rules can set or switch it off; a miss is
  re-encoded once.

- [ ] **Step 1: Run the full suite**:
  `../venv/bin/python -m pytest -q -p no:cacheprovider backend/tests > ../suite.log 2>&1; echo "exit $?"`.
  Expect exit 0. Don't pipe pytest to tail.
- [ ] **Step 2: Push.** Check that no image build is running, then
  `git push -q git@github.com:I-IAL9000/shrinkerr.git HEAD:develop`, then
  `bash ../ci_wait.sh <sha7>`. Expect Tests and Build & publish images both
  success.
