"""Killable subprocess wrapper for whisper spoken-language ID.

    python -m backend.audio_lang_worker <clip.wav>

Emits one line on stdout:  ``RESULT\t<iso639-1>\t<confidence>``

Isolated in its own process so a wedged transcribe can be KILLED to free its
CPU, instead of leaking an un-cancellable thread inside the event-loop process
— which pegged every core and made the whole app unresponsive (v0.9.21).
Fail-open: any error still prints a RESULT line (empty language) so the parent
treats it as "undetected" rather than hanging.
"""
import sys


def main(clip_path: str) -> None:
    # Reuse the exact in-process detection logic (model load + language ID);
    # here it just runs in a throwaway, killable process.
    from backend.language_detection import _run_whisper_lang
    lang, conf = _run_whisper_lang(clip_path)
    sys.stdout.write(f"RESULT\t{lang or ''}\t{conf}\n")
    sys.stdout.flush()


if __name__ == "__main__":
    try:
        main(sys.argv[1])
    except Exception as exc:  # noqa: BLE001 — fail-open, never hang the parent
        sys.stderr.write(f"audio_lang_worker error: {exc}\n")
        sys.stdout.write("RESULT\t\t0.0\n")
        sys.stdout.flush()
        sys.exit(1)
