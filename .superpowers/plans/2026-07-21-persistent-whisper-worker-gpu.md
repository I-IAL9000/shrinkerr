# Persistent Whisper Worker + GPU Audio Detection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Load the Whisper model once (persistent killable worker) instead of per-clip, enable cuDNN/GPU on the nvenc image, and expose medium/large-v3 — so low-resource languages (Icelandic) detect reliably and a full re-run is practical.

**Architecture:** A single long-lived worker subprocess loads the model once and answers clip-language requests over a stdin/stdout line protocol. An async supervisor in the event-loop process serializes access with a lock, enforces the existing per-request timeout by killing (and lazily respawning) the worker, releases the worker after an idle period, and respawns it when the model setting changes. Killability (v0.9.21) is unchanged: the model just lives in a killable child. GPU support is a base-image switch (adds cuDNN) validated on the NUC.

**Tech Stack:** Python 3.12, asyncio subprocess, faster-whisper 1.0.3 / CTranslate2, pytest (+pytest-asyncio), Docker (nvenc image on `nvcr.io/nvidia/cuda`), React/TS frontend.

**Spec:** `.superpowers/specs/2026-07-21-persistent-whisper-worker-gpu-design.md`

**Verification baseline (matches CI/NVENC, local Python can't build av/pydantic-core):**
```bash
docker run --rm -v "$PWD":/app -w /app python:3.12-slim bash -c \
  "pip install -q -r requirements.txt >/dev/null 2>&1 && python -m pytest backend/tests/<file> -q 2>&1 | tail -6"
```

---

## File Structure

- **Modify** `backend/audio_lang_worker.py` — rewrite from one-shot to a persistent stdin read-loop.
- **Modify** `backend/language_detection.py` — add `_WhisperWorker` supervisor + `_parse_whisper_result` + `_idle_seconds`; rewire `_detect_clip_language` to delegate to a module singleton; route `_get_whisper_model` / worker diagnostics to stderr; add device-report log.
- **Modify** `backend/tests/test_language_detection.py` — add supervisor + worker + parser tests (real subprocesses via an injectable command; no model weights).
- **Modify** `Dockerfile.nvenc` — switch base to the cuDNN runtime variant (Release B).
- **Modify** `frontend/src/pages/SettingsPage.tsx` — add `medium` / `large-v3` options (Release B).
- **Modify** `VERSION`, `CHANGELOG.md` — one release bump per release.

---

# RELEASE A — Persistent worker (CPU-testable, benefits everyone)

## Task A1: Persistent worker process

**Files:**
- Modify: `backend/audio_lang_worker.py` (full rewrite)

- [ ] **Step 1: Rewrite the worker as a persistent read-loop**

Replace the entire contents of `backend/audio_lang_worker.py` with:

```python
"""Persistent, killable subprocess for whisper spoken-language ID.

    python -m backend.audio_lang_worker

Reads one clip path per line on stdin; for each, emits exactly one line on
stdout:

    RESULT\t<iso639-1>\t<confidence>

The model is loaded ONCE (on the first request) and reused across clips — the
parent spawns this once per scan instead of once per clip, so the model isn't
reloaded thousands of times (the dominant cost of the old per-clip design).
Still isolated in its own process so a wedged transcribe can be KILLED by the
parent to free CPU/GPU (the v0.9.21 property); the parent respawns it on the
next request.

STDOUT carries ONLY RESULT lines. All diagnostics (model-load, errors) go to
STDERR, which the parent inherits so they reach the container log. Fail-open:
any per-clip error still emits a RESULT line (empty language) so the parent
treats it as "undetected" rather than desyncing or hanging.
"""
import sys


def main() -> None:
    # Import inside main so `-c` fakes in tests never import the heavy module.
    from backend.language_detection import _run_whisper_lang
    for line in sys.stdin:
        clip_path = line.strip()
        if not clip_path:
            continue
        try:
            lang, conf = _run_whisper_lang(clip_path)
        except Exception as exc:  # noqa: BLE001 — fail-open, never desync
            sys.stderr.write(f"audio_lang_worker error: {exc}\n")
            sys.stderr.flush()
            lang, conf = None, 0.0
        sys.stdout.write(f"RESULT\t{lang or ''}\t{conf}\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Verify it imports/compiles**

Run: `python -m py_compile backend/audio_lang_worker.py`
Expected: no output, exit 0.

- [ ] **Step 3: Commit**

```bash
git add backend/audio_lang_worker.py
git commit -m "refactor(lang): persistent stdin read-loop whisper worker"
```

---

## Task A2: Supervisor + result parser + rewired `_detect_clip_language`

**Files:**
- Modify: `backend/language_detection.py` (add helpers/class near the existing whisper code, ~lines 305-389)
- Test: `backend/tests/test_language_detection.py`

Context: today `_detect_clip_language` spawns `python -m backend.audio_lang_worker <clip>` per call and reads a `RESULT` line. We keep the same signature and fail-open/timeout contract, but delegate to a persistent supervisor.

- [ ] **Step 1: Write failing tests for the result parser**

Add to `backend/tests/test_language_detection.py`:

```python
def test_parse_whisper_result_valid():
    from backend.language_detection import _parse_whisper_result
    assert _parse_whisper_result("RESULT\tde\t0.92\n") == ("de", 0.92)


def test_parse_whisper_result_empty_lang_and_bad_conf():
    from backend.language_detection import _parse_whisper_result
    assert _parse_whisper_result("RESULT\t\t\n") == (None, 0.0)
    assert _parse_whisper_result("RESULT\ten\tNaNlike\n") == ("en", 0.0)


def test_parse_whisper_result_non_result_line():
    from backend.language_detection import _parse_whisper_result
    assert _parse_whisper_result("[LANG-DETECT] model loaded\n") == (None, 0.0)
```

- [ ] **Step 2: Run — expect failure**

Run (container baseline, file `test_language_detection.py`):
Expected: FAIL — `cannot import name '_parse_whisper_result'`.

- [ ] **Step 3: Add the parser + idle-seconds helper**

In `backend/language_detection.py`, after `_run_whisper_lang` (currently ends ~line 352), add:

```python
def _parse_whisper_result(line: str) -> tuple[str | None, float]:
    """Parse a worker `RESULT\t<lang>\t<conf>` line. Non-RESULT lines (stray
    model-load / library chatter that slipped onto stdout) parse to (None, 0.0)
    so the caller skips them."""
    if not line.startswith("RESULT\t"):
        return (None, 0.0)
    parts = line.rstrip("\n").split("\t")
    lang = parts[1] if len(parts) > 1 else ""
    try:
        conf = float(parts[2]) if len(parts) > 2 and parts[2] else 0.0
    except ValueError:
        conf = 0.0
    return (lang or None, conf)


def _idle_seconds() -> float:
    """How long the persistent whisper worker may sit idle before it's shut
    down to free RAM/VRAM (the P2200 is shared with Plex transcoding). Env-
    tunable; default 300s. Stays hot during an active scan (each clip resets
    the timer)."""
    try:
        return float(os.environ.get("SHRINKERR_WHISPER_IDLE_SECONDS", "300"))
    except ValueError:
        return 300.0
```

- [ ] **Step 4: Run parser tests — expect pass**

Run the container baseline on `test_language_detection.py`.
Expected: the 3 new parser tests PASS.

- [ ] **Step 5: Write failing tests for the supervisor (real subprocesses, no model)**

The supervisor takes an injectable command so tests drive it with tiny `python -c` fakes instead of the real model-loading worker. Add:

```python
import sys as _sys

def _fake_worker(body: str) -> list[str]:
    """A `python -c` command acting as a whisper worker for tests."""
    return [_sys.executable, "-c", body]

# Echoes a fixed RESULT for every stdin line.
_ECHO = ("import sys\n"
         "for l in sys.stdin:\n"
         "    if l.strip():\n"
         "        sys.stdout.write('RESULT\\tde\\t0.9\\n'); sys.stdout.flush()\n")

# Emits a chatter line BEFORE the RESULT (tests skip-until-RESULT).
_NOISY = ("import sys\n"
          "for l in sys.stdin:\n"
          "    if l.strip():\n"
          "        sys.stdout.write('loading model...\\n'); sys.stdout.flush()\n"
          "        sys.stdout.write('RESULT\\tis\\t0.8\\n'); sys.stdout.flush()\n")

# Never responds (tests timeout -> kill).
_HANG = "import time\ntime.sleep(999)\n"

# Reads one line then exits (tests EOF -> respawn -> fail-open / success).
_DIE_ONCE = ("import sys\n"
             "sys.stdin.readline()\n")  # exit after first line, no output


@pytest.mark.asyncio
async def test_worker_echo_roundtrip():
    from backend.language_detection import _WhisperWorker
    w = _WhisperWorker(cmd=_fake_worker(_ECHO))
    try:
        assert await w.detect("/tmp/a.wav") == ("de", 0.9)
        assert await w.detect("/tmp/b.wav") == ("de", 0.9)
        # Same worker served both — not a fresh process per clip.
        assert w._proc is not None
    finally:
        await w.shutdown()


@pytest.mark.asyncio
async def test_worker_skips_non_result_lines():
    from backend.language_detection import _WhisperWorker
    w = _WhisperWorker(cmd=_fake_worker(_NOISY))
    try:
        assert await w.detect("/tmp/a.wav") == ("is", 0.8)
    finally:
        await w.shutdown()


@pytest.mark.asyncio
async def test_worker_timeout_kills_and_raises():
    import asyncio
    from backend.language_detection import _WhisperWorker
    w = _WhisperWorker(cmd=_fake_worker(_HANG))
    try:
        with pytest.raises(asyncio.TimeoutError):
            await w.detect("/tmp/a.wav", timeout=0.5)
        # Worker was killed on timeout.
        assert w._proc is None
    finally:
        await w.shutdown()


@pytest.mark.asyncio
async def test_worker_respawns_after_crash_then_fails_open():
    from backend.language_detection import _WhisperWorker
    # Worker dies (no output) on each request; after one respawn retry with no
    # result, detect fails open to (None, 0.0) rather than hanging.
    w = _WhisperWorker(cmd=_fake_worker(_DIE_ONCE))
    try:
        assert await w.detect("/tmp/a.wav", timeout=2) == (None, 0.0)
    finally:
        await w.shutdown()


@pytest.mark.asyncio
async def test_worker_respawns_on_model_change(monkeypatch):
    from backend import language_detection as ld
    w = ld._WhisperWorker(cmd=_fake_worker(_ECHO))
    try:
        monkeypatch.setattr(ld, "_configured_whisper_model", lambda: "tiny")
        await w.detect("/tmp/a.wav")
        pid1 = w._proc.pid
        monkeypatch.setattr(ld, "_configured_whisper_model", lambda: "large-v3")
        await w.detect("/tmp/b.wav")
        pid2 = w._proc.pid
        assert pid1 != pid2  # setting change forced a respawn
    finally:
        await w.shutdown()


@pytest.mark.asyncio
async def test_worker_idle_shutdown(monkeypatch):
    import asyncio
    from backend import language_detection as ld
    monkeypatch.setenv("SHRINKERR_WHISPER_IDLE_SECONDS", "0.2")
    w = ld._WhisperWorker(cmd=_fake_worker(_ECHO))
    try:
        await w.detect("/tmp/a.wav")
        assert w._proc is not None
        await asyncio.sleep(0.5)  # exceed idle window
        assert w._proc is None    # released while idle
    finally:
        await w.shutdown()
```

- [ ] **Step 6: Run — expect failure**

Run the container baseline on `test_language_detection.py`.
Expected: FAIL — `cannot import name '_WhisperWorker'`.

- [ ] **Step 7: Implement the supervisor**

In `backend/language_detection.py`, add the class after the helpers from Step 3:

```python
class _WhisperWorker:
    """Supervises a single persistent, killable whisper worker subprocess.

    Access is serialized (detection is single-instance — no parallel
    inference). The per-request timeout still KILLS a wedged worker to free the
    CPU/GPU (v0.9.21); the worker is lazily respawned on the next request. The
    worker is released after an idle period to free RAM/VRAM, and respawned
    when the configured model changes."""

    def __init__(self, cmd: list[str] | None = None):
        self._cmd = cmd or [sys.executable, "-m", "backend.audio_lang_worker"]
        self._proc: asyncio.subprocess.Process | None = None
        self._started_model: str | None = None
        self._lock = asyncio.Lock()
        self._idle_task: asyncio.Task | None = None

    async def detect(self, clip_path: str, timeout: int = 120) -> tuple[str | None, float]:
        async with self._lock:
            try:
                return await self._request(clip_path, timeout)
            except asyncio.TimeoutError:
                # Wedged transcribe — kill to free the CPU/GPU, abandon track.
                print(f"[LANG-DETECT] whisper worker killed after {timeout}s: {clip_path}", flush=True)
                await self._kill()
                raise
            finally:
                self._reset_idle()

    async def _request(self, clip_path: str, timeout: int) -> tuple[str | None, float]:
        # Two attempts: a crashed/EOF worker is respawned once. A timeout is
        # NOT retried (it propagates so the caller abandons the track).
        for _attempt in (1, 2):
            await self._ensure_started()
            try:
                self._proc.stdin.write((clip_path + "\n").encode())
                await self._proc.stdin.drain()
                line = await asyncio.wait_for(self._read_result(), timeout=timeout)
            except asyncio.TimeoutError:
                raise
            except (BrokenPipeError, ConnectionResetError, OSError):
                await self._kill()
                continue
            if line is None:            # EOF — worker exited
                await self._kill()
                continue
            return _parse_whisper_result(line)
        return (None, 0.0)              # died twice — fail-open

    async def _read_result(self) -> str | None:
        """Read stdout lines until a RESULT line; skip stray chatter. Returns
        the RESULT line, or None on EOF. Bounded by the caller's wait_for."""
        while True:
            raw = await self._proc.stdout.readline()
            if not raw:
                return None
            s = raw.decode(errors="replace")
            if s.startswith("RESULT\t"):
                return s
            # else: model-load / library chatter that reached stdout — skip.

    async def _ensure_started(self) -> None:
        want = _configured_whisper_model()
        if self._proc is not None and self._proc.returncode is None:
            if self._started_model == want:
                return
            await self._kill()          # model setting changed — respawn
        self._proc = await asyncio.create_subprocess_exec(
            *self._cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=None,                # inherit — diagnostics reach the log
        )
        self._started_model = want

    async def _kill(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
        except Exception:
            pass

    def _reset_idle(self) -> None:
        if self._idle_task is not None:
            self._idle_task.cancel()
        self._idle_task = asyncio.create_task(self._idle_shutdown())

    async def _idle_shutdown(self) -> None:
        try:
            await asyncio.sleep(_idle_seconds())
            async with self._lock:
                await self._kill()
        except asyncio.CancelledError:
            return

    async def shutdown(self) -> None:
        if self._idle_task is not None:
            self._idle_task.cancel()
            self._idle_task = None
        await self._kill()


_whisper_worker = _WhisperWorker()
```

- [ ] **Step 8: Rewire `_detect_clip_language` to delegate**

Replace the body of the existing `_detect_clip_language` (currently ~lines 355-389, which spawns a per-clip subprocess) with a delegation to the singleton. Keep the docstring's intent but update it:

```python
async def _detect_clip_language(clip_path: str, timeout: int = 120) -> tuple[str | None, float]:
    """Language-ID a clip via the persistent, killable whisper worker
    (v0.9.80+: model loaded once, not per clip). Raises asyncio.TimeoutError on
    a wedged transcribe (the worker is killed; caller abandons the track);
    otherwise returns (ISO 639-1 | None, confidence). Fail-open."""
    return await _whisper_worker.detect(clip_path, timeout=timeout)
```

- [ ] **Step 9: Route worker-side diagnostics to stderr**

In `_get_whisper_model` (and `_run_whisper_lang` if it prints), change the diagnostic `print(...)` calls to write to **stderr** so they never pollute the worker's stdout RESULT stream (the parent inherits stderr → they still reach the container log). Specifically, for each existing `print(f"[LANG-DETECT] Whisper model ... loaded", flush=True)` and the load-failure print, add `file=sys.stderr`:

```python
        print(f"[LANG-DETECT] Whisper model '{want}' loaded on {_model_device(_WHISPER_MODEL)}",
              file=sys.stderr, flush=True)
```
and
```python
        print(f"[LANG-DETECT] Whisper model '{want}' load failed, audio detection disabled: {exc}",
              file=sys.stderr, flush=True)
```

- [ ] **Step 10: Run supervisor tests — expect pass**

Run the container baseline on `test_language_detection.py`.
Expected: all supervisor + parser tests PASS; pre-existing tests still PASS (they patch `_detect_clip_language`, which keeps its signature).

- [ ] **Step 11: Commit**

```bash
git add backend/language_detection.py backend/tests/test_language_detection.py
git commit -m "feat(lang): persistent killable whisper worker (load model once)"
```

---

## Task A3: Device-report log helper

**Files:**
- Modify: `backend/language_detection.py`
- Test: `backend/tests/test_language_detection.py`

- [ ] **Step 1: Write a failing test for the device helper**

```python
def test_model_device_reads_ct2_device():
    from backend.language_detection import _model_device
    class _M:  # mimics a faster-whisper model wrapping a CT2 model
        class model:
            device = "cuda"
    assert _model_device(_M()) == "cuda"

    class _Bare:
        pass
    assert _model_device(_Bare()) == "unknown"
```

- [ ] **Step 2: Run — expect failure**

Expected: FAIL — `cannot import name '_model_device'`.

- [ ] **Step 3: Implement `_model_device`**

Add near `_get_whisper_model` in `backend/language_detection.py`:

```python
def _model_device(model) -> str:
    """Best-effort read of the device a loaded faster-whisper model runs on
    ('cuda' / 'cpu'), for a startup log line so GPU use is verifiable. Returns
    'unknown' if the attribute isn't present."""
    try:
        return getattr(getattr(model, "model", None), "device", None) or "unknown"
    except Exception:
        return "unknown"
```

(The Step-9 log line in Task A2 already calls `_model_device`; this task defines it and tests it. Implement A3 Step 3 before running A2 Step 10 if executing in order — or fold the helper in during A2. Order note: define `_model_device` no later than the A2 Step-9 edit so the module imports.)

- [ ] **Step 4: Run — expect pass**

Run the container baseline on `test_language_detection.py`.
Expected: PASS.

- [ ] **Step 5: Full suite + commit**

```bash
docker run --rm -v "$PWD":/app -w /app python:3.12-slim bash -c \
  "pip install -q -r requirements.txt >/dev/null 2>&1 && python -m pytest backend/tests -q 2>&1 | tail -4"
```
Expected: all pass.

```bash
git add backend/language_detection.py backend/tests/test_language_detection.py
git commit -m "feat(lang): report whisper device (cuda/cpu) at load"
```

---

## Task A4: Release A ship

- [ ] **Step 1: Bump VERSION + CHANGELOG**

```bash
printf '0.9.80\n' > VERSION
```
Add under the top of `CHANGELOG.md` (one-liner per repo convention):
```markdown
## [0.9.80] — 2026-07-21

### Changed
- **Audio language detection loads the Whisper model once instead of per clip.** A single persistent, killable worker now serves all clips (released after idle, respawned on timeout/crash/model change), so detection is much faster over large libraries and larger models become practical. Killability (v0.9.21) is unchanged.
```

- [ ] **Step 2: Commit, tag, push**

```bash
git add VERSION CHANGELOG.md
git commit -m "release: v0.9.80 — persistent whisper worker"
git tag v0.9.80
git push origin main && git push origin v0.9.80
```

- [ ] **Step 3: Confirm CI green (authenticated)**

```bash
source ~/.zshrc
curl -s -H "Authorization: Bearer $GITHUB_TOKEN" \
  "https://api.github.com/repos/I-IAL9000/shrinkerr/actions/runs?per_page=20" | python3 -c "
import sys,json
d=json.load(sys.stdin)
for r in d['workflow_runs']:
    if r['head_branch']=='v0.9.80' and r['name'] in ('Build & publish images','Tests'):
        print(r['name'], r['status'], r['conclusion'])"
```
Expected: build + Tests both `completed success` (poll until done).

---

# RELEASE B — cuDNN (GPU) + larger-model options (needs NUC validation)

## Task B1: cuDNN in the nvenc image

**Files:**
- Modify: `Dockerfile.nvenc`

- [ ] **Step 1: Determine CTranslate2's cuDNN requirement**

Run in the current CPU baseline to see the resolved version:
```bash
docker run --rm -v "$PWD":/app -w /app python:3.12-slim bash -c \
  "pip install -q -r requirements.txt >/dev/null 2>&1 && pip show ctranslate2 | grep -i version"
```
Decision rule:
- CTranslate2 **≥ 4.5.0** supports **cuDNN 9** → use the `cudnn-runtime` image as-is (it ships cuDNN 9).
- CTranslate2 **< 4.5.0** needs **cuDNN 8** → either bump the pin (`ctranslate2>=4.5` in `requirements.txt`, if compatible with `faster-whisper==1.0.3`) or install cuDNN 8 explicitly in the image.

- [ ] **Step 2: Switch the base image**

In `Dockerfile.nvenc`, change the runtime base (currently
`FROM nvcr.io/nvidia/cuda:12.6.3-runtime-ubuntu24.04`) to the cuDNN variant:
```dockerfile
FROM nvcr.io/nvidia/cuda:12.6.3-cudnn-runtime-ubuntu24.04
```
(Update the adjacent comment block noting cuDNN is now included for CTranslate2 GPU support.) If Step 1 required a pin bump, also edit `requirements.txt` accordingly and note it in the commit.

- [ ] **Step 3: Build the nvenc image locally (amd64)**

Run:
```bash
docker build -f Dockerfile.nvenc -t shrinkerr:nvenc-test .
```
Expected: builds successfully (image layers pull cuDNN).

- [ ] **Step 4 (MANUAL, on the NUC — I cannot run GPU here): GPU smoke test**

On the NUC, run the built image with the GPU and trigger one audio detection, then check the log:
- Expected log line: `[LANG-DETECT] Whisper model '<model>' loaded on cuda`
- Expected: a known clip detects the correct language.

If the log says `cpu` or the worker errors on cuDNN, revisit Step 1's decision rule (cuDNN 8 vs 9). This step **gates the release**.

- [ ] **Step 5: Commit**

```bash
git add Dockerfile.nvenc requirements.txt
git commit -m "feat(nvenc): cuDNN runtime base so whisper uses the GPU"
```

---

## Task B2: Expose medium / large-v3 in the model dropdown

**Files:**
- Modify: `frontend/src/pages/SettingsPage.tsx` (~line 1824)

- [ ] **Step 1: Add the two options**

In the `lang_detect_whisper_model` `<select>`, after the `small` option, add:
```tsx
                  <option value="medium">Medium — higher accuracy, ~1.5 GB, GPU recommended</option>
                  <option value="large-v3">Large-v3 — best accuracy (incl. low-resource langs), ~3 GB, GPU strongly recommended</option>
```

- [ ] **Step 2: Update the help text**

Amend the description div (currently ~line 1832) to note that `medium`/`large-v3` are impractical without a GPU:
```tsx
                  faster-whisper model for spoken-language ID. Larger is more accurate on non-English speech at the cost of download size, RAM, and speed. Medium/Large-v3 are practical only with an NVIDIA GPU (the :nvenc image). Downloads on first use; takes effect on the next detection (no restart).
```

- [ ] **Step 3: Verify the frontend build (tsc -b, NOT --noEmit)**

Run:
```bash
cd frontend && npm run build
```
Expected: builds clean (no TS errors).

- [ ] **Step 4: Commit**

```bash
git add frontend/src/pages/SettingsPage.tsx
git commit -m "feat(ui): expose medium/large-v3 whisper models"
```

---

## Task B3: Release B ship

- [ ] **Step 1: Bump VERSION + CHANGELOG**

```bash
printf '0.9.81\n' > VERSION
```
Add under the top of `CHANGELOG.md`:
```markdown
## [0.9.81] — 2026-07-21

### Added
- **GPU-accelerated audio detection on the :nvenc image, plus medium/large-v3 models.** The nvenc image now ships cuDNN so faster-whisper runs on the GPU (`device="auto"`, falls back to CPU), and the Settings dropdown exposes medium and large-v3 — making reliable detection of low-resource languages (e.g. Icelandic) practical. Default model unchanged (tiny).
```

- [ ] **Step 2: Commit, tag, push**

```bash
git add VERSION CHANGELOG.md
git commit -m "release: v0.9.81 — GPU audio detection + larger models"
git tag v0.9.81
git push origin main && git push origin v0.9.81
```

- [ ] **Step 3: Confirm CI green** (same authenticated poll as Task A4 Step 3, branch `v0.9.81`).

- [ ] **Step 4 (MANUAL, on the NUC): validate end-to-end** — pull the new nvenc image, set the model to `large-v3` in Settings, re-detect a few known-Icelandic items, confirm they detect correctly and quickly (GPU).

---

## Self-Review (completed during authoring)

- **Spec coverage:** Component 1 → A1/A2/A3; Component 2 → B1; Component 3 → B2. Idle-release, kill-respawn, model-change respawn, device log, default-stays-tiny all have tasks/steps.
- **Placeholder scan:** none — every code step shows complete code; the one hardware-dependent unknown (cuDNN 8 vs 9) is a concrete decision rule with exact commands, not a TODO.
- **Type/name consistency:** `_WhisperWorker`, `_parse_whisper_result`, `_idle_seconds`, `_model_device`, `_whisper_worker` singleton, and the preserved `_detect_clip_language(clip_path, timeout=120)` signature are used consistently across tasks and match the existing call site in `detect_audio_language`.
- **Ordering note:** define `_model_device` (A3 Step 3) no later than the A2 Step-9 edit, since the A2 log line references it.
