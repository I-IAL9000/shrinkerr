"""Blu-ray (PGS) subtitles to text, fast (v0.10.0).

pgsrip decoded every bitmap pixel by pixel in Python: a Blu-ray track of
full-frame 1920x1080 images repeated as "acquisition points" took it 10+
minutes for 20 minutes of film. Here the run-length data is decoded run by
run, the palette applied with bytes.translate, each image cropped to its
text, repeats skipped (a subtitle is OCR'd once however often it's shown),
and the distinct images read by tesseract in a few batches at once: the
same 20 minutes in a few seconds.

Runs in the OCR process (backend.pgsrip_child), never in the server."""
from __future__ import annotations

import hashlib
import os
import struct
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

_SEGMENT = struct.Struct(">2sIIBH")  # "PG", pts, dts, segment type, size
_PDS, _ODS, _PCS, _END = 0x14, 0x15, 0x16, 0x80


class NotPgs(ValueError):
    """The file isn't a PGS stream this reader understands."""


@dataclass
class Subtitle:
    start: int  # 90 kHz
    end: int | None = None
    keys: tuple = ()  # its images
    text: str = ""


@dataclass
class _Shown:
    pts: int
    objects: list = field(default_factory=list)  # (key, rle, width, height, palette)


def _display_sets(data: bytes) -> list[_Shown]:
    """Each display set: when, and the images it shows (with the palette)."""
    at, out = 0, []
    palette: dict[int, tuple[int, int]] = {}
    objects: dict[int, list] = {}
    pts, shown = 0, []
    while at + _SEGMENT.size <= len(data):
        magic, seg_pts, _dts, kind, size = _SEGMENT.unpack_from(data, at)
        if magic != b"PG":
            raise NotPgs(f"no PGS segment at byte {at}")
        body = data[at + _SEGMENT.size: at + _SEGMENT.size + size]
        at += _SEGMENT.size + size
        if kind == _PCS:
            if len(body) < 11:
                raise NotPgs("short composition segment")
            pts = seg_pts
            if body[7] == 0x80:  # epoch start: what came before is gone
                palette, objects = {}, {}
            shown = [struct.unpack_from(">H", body, 11 + 8 * k)[0] for k in range(body[10])
                     if 11 + 8 * k + 2 <= len(body)]
        elif kind == _PDS:
            for k in range(2, len(body) - 4, 5):
                idx, y, _cr, _cb, alpha = body[k:k + 5]
                palette[idx] = (y, alpha)
        elif kind == _ODS and len(body) >= 4:
            oid, flag = struct.unpack_from(">H", body, 0)[0], body[3]
            if flag & 0x80 and len(body) >= 11:  # an image's first part
                width, height = struct.unpack_from(">HH", body, 7)
                objects[oid] = [bytearray(body[11:]), width, height]
            elif oid in objects:
                objects[oid][0] += body[4:]
        elif kind == _END:
            visible = any(alpha for _y, alpha in palette.values())
            pal = tuple(sorted(palette.items()))
            out.append(_Shown(pts, [(hashlib.md5(bytes(objects[o][0]) + repr(pal).encode()).digest(),
                                     bytes(objects[o][0]), objects[o][1], objects[o][2], dict(palette))
                                    for o in shown if o in objects] if visible else []))
    if not out:
        raise NotPgs("no display sets")
    return out


def _decode(rle: bytes, width: int, height: int) -> bytearray:
    """The image's palette indexes, run by run (PGS run-length encoding)."""
    out = bytearray(width * height)
    i, n, pos, row = 0, len(rle), 0, 0
    end = len(out)
    while i < n and pos <= end:
        b = rle[i]
        i += 1
        if b:
            if pos < end:
                out[pos] = b
            pos += 1
            continue
        if i >= n:
            break
        b2 = rle[i]
        i += 1
        if not b2:  # end of line
            row += 1
            pos = row * width
            continue
        kind = b2 & 0xC0
        if kind == 0x00:
            pos += b2 & 0x3F
            continue
        if kind == 0x40:
            pos += ((b2 & 0x3F) << 8) | (rle[i] if i < n else 0)
            i += 1
            continue
        if kind == 0x80:
            run, color = b2 & 0x3F, (rle[i] if i < n else 0)
            i += 1
        else:
            run, color = ((b2 & 0x3F) << 8) | (rle[i] if i < n else 0), (rle[i + 1] if i + 1 < n else 0)
            i += 2
        run = max(0, min(run, end - pos))
        out[pos:pos + run] = bytes((color,)) * run
        pos += run
    return out


def _image(rle: bytes, width: int, height: int, palette: dict, pad: int = 10) -> bytes | None:
    """The subtitle as dark text on white, cropped to it, as a PGM image
    tesseract reads (None: nothing shows). No imaging library: bytes only."""
    px = bytes(_decode(rle, width, height))
    ink, shown = bytearray([255] * 256), bytearray(256)
    for idx, (y, alpha) in palette.items():
        ink[idx] = 255 - (y * alpha) // 255
        shown[idx] = 255 if alpha else 0
    mask, page = px.translate(shown), px.translate(ink)
    top = bottom = None
    left, right = width, 0
    for y in range(height):
        row = mask[y * width:(y + 1) * width]
        stripped = row.lstrip(b"\x00")
        if not stripped:
            continue
        top = y if top is None else top
        bottom = y
        left = min(left, width - len(stripped))
        right = max(right, len(row.rstrip(b"\x00")))
    if top is None:
        return None
    w = right - left + 2 * pad
    blank = b"\xff" * w
    rows = [blank] * pad + [b"\xff" * pad + page[y * width + left:y * width + right] + b"\xff" * pad
                            for y in range(top, bottom + 1)] + [blank] * pad
    return b"P5\n%d %d\n255\n" % (w, len(rows)) + b"".join(rows)


def subtitles(data: bytes, most: int | None = None) -> tuple[list[Subtitle], dict]:
    """The stream's subtitles — a run of display sets showing the same images
    is one — `most` of them spread through it when given; and their images
    (key -> rle, width, height, palette) for read_text."""
    subs: list[Subtitle] = []
    images: dict = {}
    for shown in _display_sets(data):
        keys = tuple(key for key, *_ in shown.objects)
        if subs and subs[-1].end is None and subs[-1].keys == keys:
            continue  # the same subtitle, shown again (acquisition point)
        if subs and subs[-1].end is None:
            subs[-1].end = shown.pts
        if keys:
            subs.append(Subtitle(shown.pts, keys=keys))
            for key, rle, width, height, palette in shown.objects:
                images.setdefault(key, (rle, width, height, palette))
    if most is not None and len(subs) > most:
        subs = [subs[round(k * (len(subs) - 1) / (most - 1))] for k in range(most)] if most > 1 else subs[:1]
    return subs, images


def read_text(subs: list[Subtitle], images: dict, packs: tuple[str, ...], workers: int | None = None) -> None:
    """OCR each subtitle's images (each distinct one once) into its text."""
    keys = list(dict.fromkeys(key for sub in subs for key in sub.keys))
    work = tempfile.mkdtemp(prefix="shrinkerr_pgs_")
    try:
        files: dict = {}
        for n, key in enumerate(keys):
            img = _image(*images[key])
            if img is not None:
                files[key] = os.path.join(work, f"{n:05d}.pgm")
                with open(files[key], "wb") as fh:
                    fh.write(img)
        todo = list(files.items())
        workers = workers or max(1, min(4, (os.cpu_count() or 2) // 2))
        chunks = [todo[k::workers] for k in range(workers) if todo[k::workers]]

        def ocr(chunk) -> dict:
            listing = os.path.join(work, f"list-{os.path.basename(chunk[0][1])}.txt")
            with open(listing, "w") as fh:
                fh.write("\n".join(path for _, path in chunk))
            env = {**os.environ, "OMP_THREAD_LIMIT": "1"}
            out = subprocess.run(["tesseract", listing, "stdout", "-l", "+".join(packs), "--psm", "6"],
                                 capture_output=True, text=True, env=env).stdout.split("\f")
            if out and not out[-1].strip() and len(out) == len(chunk) + 1:
                out = out[:-1]
            if len(out) != len(chunk):  # pages don't line up: one image at a time
                out = [subprocess.run(["tesseract", path, "stdout", "-l", "+".join(packs), "--psm", "6"],
                                      capture_output=True, text=True, env=env).stdout for _, path in chunk]
            return {key: text.strip() for (key, _), text in zip(chunk, out)}
        texts: dict = {}
        with ThreadPoolExecutor(len(chunks) or 1) as pool:
            for part in pool.map(ocr, chunks):
                texts.update(part)
        for sub in subs:
            sub.text = "\n".join(t for t in (texts.get(key, "") for key in sub.keys) if t)
    finally:
        import shutil
        shutil.rmtree(work, ignore_errors=True)


def _stamp(pts: int) -> str:
    ms = pts // 90
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def sup_to_srt(sup_path: str, packs: tuple[str, ...], most: int | None = None) -> str | None:
    """Write the .sup's subtitles as an .srt beside it (its path), or None
    when nothing was read. Raises NotPgs for a stream it can't read."""
    with open(sup_path, "rb") as fh:
        data = fh.read()
    subs, images = subtitles(data, most)
    read_text(subs, images, packs)
    entries = [s for s in subs if s.text]
    if not entries:
        return None
    srt = os.path.splitext(sup_path)[0] + ".srt"
    with open(srt, "w", encoding="utf-8") as fh:
        for n, s in enumerate(entries, 1):
            end = s.end if s.end is not None and s.end > s.start else s.start + 3 * 90000
            fh.write(f"{n}\n{_stamp(s.start)} --> {_stamp(end)}\n{s.text}\n\n")
    return srt
