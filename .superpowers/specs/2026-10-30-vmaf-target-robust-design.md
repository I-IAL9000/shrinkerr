# VMAF target: per-rule targets and a search that holds the target

Status: design, 30 Oct 2026. Builds on the v0.10.0 VMAF target
(`backend/vmaf_target.py`, `converter.convert_file`'s `vmaf_target` block).

## Intent

The user wants to convert with Settings → Video → VMAF target at **93**: each
title is compressed as much as it can be while still scoring 93. They don't
want it to miss in either direction:

- **too low**: the finished file scores under 93;
- **too high**: not enough compression, the file scores well over 93 when
  it could have been smaller.

Their library also has kinds of content they want handled differently.
Films might get a higher target, and reality TV should keep a fixed, more
aggressive quality with no target at all. So **rules** must be able to set
their own target or opt out.

## What happens today (the starting point)

- The VMAF target is one global setting: `vmaf_target_enabled`, plus
  `vmaf_target_score` (default 93).
- When it's on, every conversion (except discs, and files under 120 s)
  searches for its quality before the encode:
  - It takes 3 samples of 10 s, at 20%, 50% and 80% of the file (stream copy).
  - It binary-searches CQ 14–34 on NVENC's CQ scale, starting at the job's
    own CQ, for the highest CQ whose *mean* sample VMAF reaches the target.
    That's at most 5 rounds.
  - Other encoders get the CQ mapped through
    `encoding_estimates.quality_settings`.
- The job's quality from a rule, the default, or the Add to Queue slider is
  only the search's starting point. A rule's other settings (encoder,
  preset, resolution, audio, originals, VMAF minimum) still apply.
- Add to Queue's estimate doesn't mention the target. Its savings and time
  use the configured CQ, with no search time.

Weak spots this design fixes:

1. **The ceiling of CQ 34 is below what the user runs now (CQ 36).** A title
   that would hold 93 at CQ 38 is never compressed past 34.
2. **Three 10 s samples, averaged.** A hard scene can be missed, or averaged
   away by easy ones.
3. **Whole CQ steps** (about 1–2 VMAF points each) overshoot the target.
4. **Nothing checks the finished file against the target.**

## Goals

- **Rules can set their target.** A rule picks the global setting, **Off**
  (encode at the rule's own quality, no search), or **its own score**.
- **The search finds the highest quality value (most compression) that
  holds the target**, robust to hard scenes, with no ceiling below what users
  actually run.
- **A finished file that clearly misses the target is re-encoded once.**
- **Add to Queue says the target is on**, and its time estimate includes
  the search.
- **Searches get faster within a show or season** by remembering where the
  last one landed.

## Non-goals

- A per-batch target in Add to Queue. Rules and the global setting decide.
- Changing how the post-encode VMAF is measured (one 30 s window at 33%;
  fixed for frame alignment in 80c2f97).
- Discs, and files under 120 s: unchanged, no search.
- Learning a per-title savings model from the searches.

## Anti-goals (would make it a failure even if it works)

- **Loops**: re-encoding a file more than once for the target.
- **A rule's Off being overridden**: a rule set to Off must never run the
  search, even when the global target is on.
- **Silently ignoring a rule's quality** when its target is Off.
- **Search time made invisible**: the user should see that a job is searching
  and how long searches take.

## Behaviour

### 1. Where a job's target comes from

The rule wins over the global setting:

| Rule's VMAF target | Global target on (score G) | Global target off |
|---|---|---|
| Settings' (default) | search for G | no search: the job's quality |
| Off | no search: the rule's quality | no search: the rule's quality |
| Custom score R | search for R | search for R |

- Files matching no rule follow the global setting.
- The resolved target is stored on the job when it's queued, the way
  `vmaf_min_score` is (`rule_resolver` → job column → `rule_actions.settings_overrides`).
  It travels to remote workers with the job's other settings
  (`routes/nodes.py` assignment, `worker_mode`).
- Storage (my call): `encoding_rules.vmaf_target` and `jobs.vmaf_target`,
  REAL. NULL means the global setting, 0 means Off, any other value is the
  target score (allowed range 80–99).

### 2. The search

- **Range: CQ 14–42** on NVENC's scale (was 14–34), mapped per encoder as
  today. AV1 output keeps whatever mapping the job already uses.
- **Samples scale with the file:** n = clamp(round(duration / 480 s), 3, 6),
  10 s each, spread evenly at (i + 0.5) / n of the duration. That's one per
  ~8 minutes: 3 for a 25-min episode, 6 for a film. My call.
- **A quality passes when both hold:**
  - the mean of the sample scores ≥ target, and
  - the lowest sample score ≥ target − 2 (my call).

  This stops one hard scene being averaged away.
- **Bracket, then bisect:**
  - Start at the remembered CQ for this folder (section 4), else the job's own CQ.
  - If it passes, step up by 2 until a quality fails or the ceiling is hit.
    If it fails, step down by 2 until one passes or the floor is hit.
  - Then bisect between the last pass and the first fail.
  - Expect about 3 rounds when the start is close, up to about 6 when it isn't.
- **Half-step refinement:** for NVENC (`-cq` is a float) and libx265 (CRF is
  a float), try the found CQ + 0.5 and keep it if it passes.
  - QSV, VAAPI and VideoToolbox stay on whole steps.
  - Job records and reports show values like "CQ 32.5".
- **No quality in range passes:** encode at the floor (CQ 14), as today. The
  report says the target wasn't reached.
- **VideoToolbox:** `encoding_estimates.vt_quality_for_cq` has measured
  points only for CQ 15–29. Every CQ ≥ 29 maps to `-q:v 40`, so the wider
  ceiling doesn't help it.
  - The search caches scores per *actual encoder setting*, so CQs that map to
    the same `q:v` aren't encoded twice.
  - The q40 cap stays (no unmeasured mapping points). My call.
- **AV1 output:** whole steps only.
- **Monotonicity is assumed** (score falls as CQ rises). Scores are cached
  per CQ within a search, so the same CQ is never encoded twice.

### 3. Checking the finished file

- When the target applied, the finished encode is VMAF-checked even if
  Settings' post-encode VMAF analysis is off. The check is the guarantee.
  My call.
- If the score is **below target − 1** (my call: within 1 point is
  measurement noise) and not flagged measurement-uncertain:
  - Re-encode once at a lower quality value. Go one step down per point of
    shortfall, using the search's own score curve where it has one, and at
    least one step.
  - Keep the second encode whatever it scores, and record both scores.
- No second retry, and no re-encode for scores above the target.
- No re-encode when the search already ended at the floor without reaching
  the target. There's nowhere lower to go.
- If the re-encode fails, keep the first encode (the job doesn't fail over
  this) and say so in the report.

### 3a. With the VMAF minimum (the reject threshold)

The minimum stays the hard floor it is today. It works only when
Settings' post-encode VMAF analysis is on, with Settings' value or a rule's.
An encode below it never replaces the original. All of this is my call.

- **A first encode below the target − 1 gets the one re-encode first,**
  whether or not it is also below the minimum. The minimum and "larger than
  the original" are then judged on the result:
  - If the second encode passes both, it's kept, whatever its score against
    the target.
  - If the second encode is rejected (below the minimum, or larger than the
    original), the first is kept when it passes both.
  - Otherwise the original is kept, as today.
- **A rule's target can't be below the minimum that applies to it** (its
  own, else Settings'). The rules editor won't save one, and says why. If
  Settings' minimum later rises above a rule's target, jobs use the higher
  of the two. Old rules aren't affected (their target is NULL).
- **The target's own check (section 3) never switches on the minimum.**
  When VMAF analysis is off, the forced check:
  - feeds only the re-encode decision and the job's displayed VMAF score;
  - doesn't reject anything (the minimum is hidden in Settings then).

### 4. Remembering where searches land

- After a search, store the chosen CQ keyed by **the file's folder**, the
  target and the encoder family. For TV the folder is the season, for a
  film its folder. My call.
- The next search in that folder starts there.
- This only changes the starting point. The search still runs in full.

### 5. Add to Queue (estimate modal)

"What will happen" gains lines for files that get a conversion:

- "VMAF target 93: each file's quality is found with test encodes first
  (N files)". There's one line per distinct target.
- "Fixed quality, VMAF target off by rule: M files", when a rule opts out.

The estimate figures:

- **Savings** for target files use the starting CQ (remembered or the job's),
  and are labelled as an estimate at the starting quality.
- **Time** adds the search per target file: the median of recorded search
  durations (the last 20; stored in the job's `encoding_stats`), else 3
  minutes.

The quality slider is unchanged. With the target on it's the starting
point, and its label or hint says so.

### 6. Rules editor

- A "VMAF target" field with three options: the Settings' value (showing it,
  e.g. "93" or "off"), Off (this rule's quality), and Custom (a score,
  80–99).
- When a rule's target is a score or comes from Settings, its quality field
  is the starting point. When it's Off, its quality is what's used. The
  hints say which.

### 7. Job report and queue

- The search step already shows ("Finding the quality for the VMAF target…").
  Add the round to it: "VMAF target: CQ 32 (round 3)".
- The report shows:
  - the chosen CQ and the sample scores (as today, plus the lowest sample);
  - whether the target was reached;
  - after a re-encode, both scores and both qualities.

## Constraints

- Every user-visible string needs en + es catalog entries. The contract
  tests check server message codes; es plurals need `_many`.
- New DB columns are additive (`ALTER TABLE … ADD COLUMN`) with NULL
  defaults, so existing rules keep following Settings.
- Remote workers older than this build don't know `vmaf_target`. A
  worker-side default of "follow the global settings sent with the job" is
  acceptable. Mention it in Upgrade notes only if behaviour changes for
  existing users; with NULL defaults it shouldn't.
- Tests: the search logic (`search` with a scoring function) is pure and
  unit-testable. Encode-level tests need ffmpeg with libvmaf, which CI may
  lack, so skip them there (they run locally).

## Settled from the code (builder check)

- **Older remote workers** get the resolved target through the existing
  `vmaf_target_enabled` / `vmaf_target_score` job fields. A rule's Off works
  on them too: enabled = false.
- **Search memory for remote jobs** is looked up and recorded on the server:
  - the starting CQ is sent with the job;
  - the found CQ is read back from the job's `encoding_stats`.
- **A rule set to Off with no quality of its own** uses the content-type or
  global CQ, as a job without a rule quality does today.
- **An explicit Add to Queue slider value** still overrides a rule's quality,
  as now. With the target on, it's only the starting point. A remembered CQ
  takes priority over it as the start.

## Left to the builder

- **UI:** layout and exact wording of the rules field and the modal lines.
- **Re-encode step:** how "step down per point of shortfall" maps onto the
  stored score curve, within the rules above.
- **Logs:** log lines and step labels.
- **Search memory:** whether it's a new table or a settings-style key-value
  store. Expire entries older than about 90 days.
