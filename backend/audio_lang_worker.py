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
