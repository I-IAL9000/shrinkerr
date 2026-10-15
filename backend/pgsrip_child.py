"""pgsrip in a process of its own (image_sub_ocr._pgsrip_rip_isolated).

pgsrip decodes PGS bitmaps in Python: run in a thread of the server it held
the interpreter lock for minutes, and every page and API call crawled while
a Blu-ray's subtitles were read (v0.10.0); a thread also can't be stopped.
Here it runs apart and is killed when it takes too long.

    python -m backend.pgsrip_child <sup path> <tesseract packs, "+"-joined>

The last line printed is "PGSRIP_RESULT <json>": {"srt": path or null} or
{"rip_error": true} (pgsrip couldn't decode the .sup)."""
import json
import sys

RESULT = "PGSRIP_RESULT "


def main(argv: list[str]) -> int:
    from backend.image_sub_ocr import _PgsRipError, _pgsrip_rip
    sup, packs = argv[0], tuple(p for p in argv[1].split("+") if p)
    try:
        result = {"srt": _pgsrip_rip(sup, packs)}
    except _PgsRipError:
        result = {"rip_error": True}
    print(RESULT + json.dumps(result), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
