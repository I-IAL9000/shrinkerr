"""Blu-ray (PGS) subtitle OCR in a process of its own
(image_sub_ocr._pgsrip_rip_isolated).

Run in a thread of the server it held the interpreter lock for minutes, and
every page and API call crawled while a Blu-ray's subtitles were read
(v0.10.0); a thread also can't be stopped. Here it runs apart and is killed
when it takes too long. It reads the track with backend.pgs_ocr (fast),
and with pgsrip only when that can't parse the stream.

    python -m backend.pgsrip_child <sup path> <tesseract packs, "+"-joined> [most]

`most`: read only that many subtitles, spread through the track (language
detection). The last line printed is "PGSRIP_RESULT <json>": {"srt": path
or null} or {"rip_error": true} (pgsrip couldn't decode the .sup)."""
import json
import sys

RESULT = "PGSRIP_RESULT "


def main(argv: list[str]) -> int:
    from backend.image_sub_ocr import _PgsRipError, _pgsrip_rip
    from backend.pgs_ocr import NotPgs, sup_to_srt
    sup, packs = argv[0], tuple(p for p in argv[1].split("+") if p)
    most = int(argv[2]) if len(argv) > 2 and argv[2] else None
    try:
        try:
            result = {"srt": sup_to_srt(sup, packs, most)}
        except NotPgs as exc:
            print(f"[IMG-OCR] {sup} isn't read as PGS ({exc}): pgsrip", flush=True)
            result = {"srt": _pgsrip_rip(sup, packs)}
    except _PgsRipError:
        result = {"rip_error": True}
    print(RESULT + json.dumps(result), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
