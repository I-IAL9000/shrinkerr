# Persistent Whisper Worker + GPU Audio Detection Design Spec

**Version target:** two releases — (1) persistent worker, CPU-testable; (2) cuDNN base switch + larger-model options, needs NUC validation
**Status:** Design — awaiting implementation plan
**Decisions locked via Q&A:** single persistent worker (detection is serialized — no pool); kill-and-respawn preserves v0.9.21 killability; worker releases model when idle (frees VRAM for Plex on the shared P2200); default model stays `tiny` (bigger models opt-in); GPU path validated by manual NUC smoke test (CI is CPU-only)

---

## Background

Audio language detection uses faster-whisper. Two problems surfaced against a
real ~18K-track library:

1. **Accuracy on low-resource languages.** The `small` model frequently
   mis-detects Icelandic (confused with Faroese/Danish/Norwegian) — both as
   `und` and as confident-wrong. Reliable Icelandic ID needs `large-v3`, which
   is impractical on CPU.
2. **Speed.** The 18K run took >24h on `small`. The dominant cost is **not
   inference** — it's that the killable worker
   ([backend/audio_lang_worker.py](../../backend/audio_lang_worker.py)) is
   spawned as a **fresh process per clip**, and `_run_whisper_lang` →
   `_get_whisper_model()` **reloads the model from disk in every one** (the
   cache is a process-global, empty in each new subprocess).
   `detect_audio_language` fires 1–4 clips per track, so the model is loaded
   thousands of times. This reload cost makes `large-v3` a trap: it would make
   detection *slower*, even on the GPU, because load dominates.

The current per-clip design exists deliberately: v0.9.21 moved transcription
into a killable subprocess because a wedged in-process `transcribe` leaked an
un-cancellable thread that pegged every core and made the whole app
unresponsive. **Any redesign must keep that "kill a wedged transcribe → free
the CPU" property.**

The NVENC image is built on `nvcr.io/nvidia/cuda:12.6.3-runtime-ubuntu24.04`,
which provides the CUDA *runtime* but **not cuDNN**. CTranslate2 (faster-whisper's
engine) needs cuDNN for CUDA, so Whisper currently runs on **CPU** even on hosts
with a GPU. The user's NUC9 + Quadro P2200 is exactly the target hardware
(Pascal, 5 GB VRAM — fits `large-v3` int8; `int8` is the right compute type for
Pascal, which lacks fast FP16). The P2200 also does Plex hardware transcoding,
so the worker must not hold VRAM when idle.

## Goal

Load the Whisper model once, run it on the GPU where available, and let users
pick `large-v3` — so Icelandic (and other low-resource languages) detect
reliably and a full re-run is practical (~hours, not days). Fail-open and
killability unchanged; CPU-only and non-GPU hosts unaffected.

---

## Component 1 — Persistent killable Whisper worker

Replace the per-clip spawn with **one long-lived worker process** managed by an
async supervisor in the event-loop (parent) process.

### Worker process (`backend/audio_lang_worker.py`, rewritten)
- On start: load the model once via the existing `_get_whisper_model()`.
- Loop: read one line (a clip path) from stdin; run `_run_whisper_lang`; write
  `RESULT\t<iso639-1>\t<conf>\n` to stdout and flush. Blank/EOF stdin → exit.
- Fail-open per request: any exception on a clip → emit `RESULT\t\t0.0\n` (never
  hang the parent), keep serving. A model-load failure at startup → exit
  non-zero (supervisor treats the request as undetected, same as today).

### Supervisor (in `backend/language_detection.py`)
A module-level singleton holding: the `asyncio.subprocess.Process` handle, an
`asyncio.Lock` (serializes access — matches "single model instance, no parallel
inference"), and an idle timer.

- **`_detect_clip_language(clip_path, timeout=120)`** (signature unchanged):
  1. Acquire the lock.
  2. Ensure a live worker (lazy start / respawn if dead).
  3. Write `clip_path\n`; read one `RESULT` line with `asyncio.wait_for(timeout)`.
  4. **On timeout:** kill the worker (frees CPU/GPU — same as today), mark it
     dead, `raise asyncio.TimeoutError` (caller already abandons the track).
  5. **On closed pipe / crash before a result:** respawn once and retry this
     clip; if it dies again, return `(None, 0.0)` (fail-open).
  6. Reset the idle timer; release the lock.
- **Idle release:** if no request for `IDLE_SHUTDOWN_SECONDS` (default 300,
  env-tunable `SHRINKERR_WHISPER_IDLE_SECONDS`), terminate the worker to free
  RAM/VRAM. Next request respawns (model reloads once). Stays hot during an
  active scan because each clip resets the timer.
- **Live model-setting change:** if `_configured_whisper_model()` changed since
  the worker started, the supervisor kills + respawns so the new model takes
  effect (preserves today's no-restart behavior; the *worker* owns the model
  now, so the reload happens there).

### Preserved contracts
- `detect_audio_language` and `_detect_clip_language` keep their signatures,
  return types, and fail-open behavior → **scan.py and existing tests are
  untouched**.
- Killability is identical from the CPU's perspective: a wedged transcribe is
  still a child process the parent kills on timeout. The only change is the
  model lives in a persistent (but still killable) child.

### Why not alternatives
- *Threadpool in-process:* rejected — this is exactly what v0.9.21 removed (a
  wedged thread is un-cancellable and starves the loop).
- *Worker pool:* unnecessary — detection is serialized; one worker suffices and
  avoids N× the VRAM.

---

## Component 2 — GPU (cuDNN) for the NVENC image

- Switch `Dockerfile.nvenc` base to the **`cudnn-runtime`** variant
  (`nvcr.io/nvidia/cuda:12.6.3-cudnn-runtime-ubuntu24.04`) so CTranslate2 can
  use CUDA. No change to the portable `Dockerfile` (CPU-only, deliberately no
  CUDA).
- `compute_type` stays `int8` (correct for Pascal); `device="auto"` already
  falls back to CPU if the GPU/cuDNN is absent — so non-GPU hosts are
  unaffected.
- Add a startup log line reporting the **resolved device** (e.g.
  `Whisper model 'large-v3' loaded on cuda`) so GPU use is verifiable. The
  device is read from the loaded CTranslate2 model, not assumed.

### Integration risk (must be validated on the NUC — I cannot test GPU here)
CTranslate2's required **cuDNN major version** must match what the
`cudnn-runtime` image ships. faster-whisper is pinned at `1.0.3`
([requirements.txt:15](../../requirements.txt)); its CTranslate2 dependency
resolves to a version built against a specific cuDNN (8 vs 9). CUDA 12.x
`cudnn-runtime` images ship cuDNN 9. If the resolved CTranslate2 needs cuDNN 8,
the plan must either pin CTranslate2 to a cuDNN-9-compatible version or install
cuDNN 8 explicitly. **The plan will include a hardware smoke test:** build the
nvenc image, run one detection, confirm the startup log says `cuda` and a known
clip detects correctly. This gates the release.

---

## Component 3 — Expose medium / large-v3

- Add `medium` and `large-v3` options to the model `<select>` in
  [SettingsPage.tsx](../../frontend/src/pages/SettingsPage.tsx) (~line 1824),
  with accurate size/speed/GPU guidance in the help text.
- Default stays `tiny`. No backend change — the value passes straight through
  ([settings.py:876](../../backend/routes/settings.py)); there is no allowlist.
- Verify the frontend with `npm run build` (tsc -b), not `tsc --noEmit`.

---

## Testing & verification

- **Unit tests** (CPU-container-runnable, model mocked — no weights):
  supervisor respawn-on-timeout, idle shutdown, crash-respawn, serialized
  access under the lock, live model-setting change → respawn, fail-open on
  worker death. Worker protocol round-trip with a fake model.
- **Full suite** green in the `python:3.12-slim` container, then **CI build +
  Tests** green (both releases).
- **GPU smoke test on the NUC** (documented, manual): build nvenc image →
  startup log shows `cuda` → one real detection returns the right language.

## Risks & mitigations

| Risk | Mitigation |
|---|---|
| cuDNN/CTranslate2 version mismatch (faster-whisper GPU pitfall) | Verify CTranslate2's cuDNN requirement; pin or install to match; hardware smoke test gates release |
| Reintroducing the v0.9.21 hang | Keep kill-on-timeout; model lives in a killable child that we respawn |
| Idle-shutdown race (request arrives mid-shutdown) | Supervisor lock + respawn-if-dead check on every request |
| large-v3 VRAM vs Plex on the shared P2200 | Release-when-idle worker; ~1.5 GB held only during active scans |
| Bigger model as a footgun for CPU-only users | Default stays `tiny`; dropdown help text flags GPU need for large-v3 |

## Rollout

- **Release A — persistent worker.** CPU-testable, benefits every user, low
  risk. Ship and confirm green first.
- **Release B — cuDNN base switch + medium/large-v3 dropdown.** Needs NUC
  validation; isolates the GPU risk so the user confirms on hardware before
  relying on it.
