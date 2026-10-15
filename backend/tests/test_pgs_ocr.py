"""Blu-ray (PGS) subtitles read fast (backend/pgs_ocr.py, v0.10.0): pgsrip
decoded Dead Tone's full-frame bitmaps pixel by pixel in Python — 10+
minutes for 20 minutes of film — then gave tesseract a 31890x46820 image
it refused. These build real PGS streams and read them with a stand-in
tesseract."""
import os
import struct
import sys

import pytest

from backend import pgs_ocr

W, H = 64, 16


def _rle(rows: list[list[int]]) -> bytes:
    """PGS run-length encoding of rows of palette indexes."""
    out = bytearray()
    for row in rows:
        x = 0
        while x < len(row):
            c, n = row[x], 1
            while x + n < len(row) and row[x + n] == c and n < 16383:
                n += 1
            if c == 0:
                out += bytes((0, n)) if n < 64 else bytes((0, 0x40 | n >> 8, n & 0xFF))
            elif n == 1:
                out.append(c)
            else:
                out += bytes((0, 0x80 | n, c)) if n < 64 else bytes((0, 0xC0 | n >> 8, n & 0xFF, c))
            x += n
        out += b"\x00\x00"
    return bytes(out)


def _bitmap(x0: int, x1: int) -> bytes:
    """A W x H image: a bar of colour 1 from x0 to x1 on rows 5-9, else transparent."""
    return _rle([[1 if 5 <= y <= 9 and x0 <= x < x1 else 0 for x in range(W)] for y in range(H)])


def _seg(kind: int, pts: int, body: bytes) -> bytes:
    return struct.pack(">2sIIBH", b"PG", pts, 0, kind, len(body)) + body


def _shown(pts: int, rle: bytes | None, state: int, alpha: int = 255) -> bytes:
    """A display set showing `rle` (None: a clear) — epoch start 0x80,
    acquisition point 0x40 (the same again), normal 0x00."""
    objs = 1 if rle is not None else 0
    pcs = struct.pack(">HHBHBBBB", 1920, 1080, 0x10, pts & 0xFFFF, state, 0, 0, objs)
    if objs:
        pcs += struct.pack(">HBBHH", 1, 0, 0, 100, 900)
    out = _seg(0x16, pts, pcs)
    if rle is not None:
        out += _seg(0x14, pts, bytes((0, 0)) + bytes((0, 16, 128, 128, 0)) + bytes((1, 235, 128, 128, alpha)))
        out += _seg(0x15, pts, struct.pack(">HBB", 1, 0, 0xC0) + (len(rle) + 4).to_bytes(3, "big")
                    + struct.pack(">HH", W, H) + rle)
    return out + _seg(0x80, pts, b"")


SECOND = 90000
STREAM = b"".join([
    _shown(1 * SECOND, _bitmap(4, 30), 0x80), _shown(2 * SECOND, _bitmap(4, 30), 0x40),
    _shown(3 * SECOND, _bitmap(4, 30), 0x40), _shown(4 * SECOND, None, 0x00),   # "A", 1 s to 4 s
    _shown(6 * SECOND, _bitmap(20, 60), 0x80), _shown(8 * SECOND, None, 0x00),  # "B", 6 s to 8 s
])


@pytest.fixture
def tesseract(tmp_path, monkeypatch):
    """A tesseract that reads every image as "line <n>" (n: its place in the
    listing), pages split by form feeds, as the real one prints them."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "tesseract").write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        "if sys.argv[1] == '--list-langs':\n"
        "    print('List of available languages (1):'); print('eng'); sys.exit()\n"
        "paths = open(sys.argv[1]).read().split() if sys.argv[1].endswith('.txt') else [sys.argv[1]]\n"
        "for p in paths:\n"
        "    assert open(p, 'rb').read(2) == b'P5'\n"
        "sys.stdout.write('\\f'.join(f'line {p.rsplit(\"/\", 1)[-1][:5]}' for p in paths))\n")
    (bindir / "tesseract").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")


def test_a_subtitle_shown_again_is_one():
    subs, images = pgs_ocr.subtitles(STREAM)
    assert [(s.start // SECOND, s.end // SECOND) for s in subs] == [(1, 4), (6, 8)]
    assert len(images) == 2  # each distinct image once


def test_the_image_is_the_text_cropped_dark_on_white():
    rle, w, h, palette = next(iter(pgs_ocr.subtitles(STREAM)[1].values()))
    pgm = pgs_ocr._image(rle, w, h, palette)
    header, pixels = pgm.split(b"\n255\n", 1)
    assert header == b"P5\n%d %d" % (26 + 20, 5 + 20)  # the bar's 26 x 5, and 10 px of white around
    assert pixels[0] == 255 and pixels[10 * 46 + 10] == 255 - 235  # white margin, dark text
    assert pgs_ocr._image(rle, w, h, {1: (235, 0)}) is None  # nothing shows


def test_a_few_spread_through_the_track():
    many = b"".join(_shown((10 + 2 * n) * SECOND, _bitmap(n % 40, n % 40 + 20), 0x80)
                    + _shown((11 + 2 * n) * SECOND, None, 0x00) for n in range(100))
    subs, _ = pgs_ocr.subtitles(many, most=10)
    assert len(subs) == 10 and subs[0].start == 10 * SECOND and subs[-1].start == 208 * SECOND


def test_the_srt(tmp_path, tesseract):
    sup = tmp_path / "sub.sup"
    sup.write_bytes(STREAM)
    srt = pgs_ocr.sup_to_srt(str(sup), ("eng",))
    assert open(srt).read() == ("1\n00:00:01,000 --> 00:00:04,000\nline 00000\n\n"
                                "2\n00:00:06,000 --> 00:00:08,000\nline 00001\n\n")


def test_what_isnt_pgs(tmp_path):
    with pytest.raises(pgs_ocr.NotPgs):
        pgs_ocr.subtitles(b"not a pgs stream")


def test_the_ocr_process_reads_it(tmp_path, tesseract):
    """Through the OCR process detection and SRT conversion use (no pgsrip)."""
    from backend.image_sub_ocr import _pgsrip_rip_isolated
    sup = tmp_path / "sub.sup"
    sup.write_bytes(STREAM)
    srt = _pgsrip_rip_isolated(str(sup), ("eng",), timeout=60, most=1)
    assert srt and open(srt).read().count(" --> ") == 1
