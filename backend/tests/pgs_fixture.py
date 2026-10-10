"""Real image subtitles for the OCR tests: write_sup() renders each cue's
text with Pillow into a PGS (Blu-ray) display set; make_video() muxes it
into an MKV beside a short test video, optionally converted to VobSub (DVD)
by ffmpeg."""
import struct
import subprocess


def _segment(kind: int, pts: int, payload: bytes) -> bytes:
    return b"PG" + struct.pack(">IIBH", pts, 0, kind, len(payload)) + payload


def _rle(pixels, width: int, height: int) -> bytes:
    """PGS run-length coding of a 0 (transparent) / 1 (text) bitmap."""
    out = bytearray()
    for y in range(height):
        x = 0
        while x < width:
            color, n = pixels[x, y], 1
            while x + n < width and pixels[x + n, y] == color and n < 16383:
                n += 1
            if color == 0:
                out += bytes([0, n]) if n < 64 else bytes([0, 0x40 | (n >> 8), n & 0xFF])
            elif n < 3:
                out += bytes([color]) * n
            else:
                out += bytes([0, 0x80 | n, color]) if n < 64 else bytes([0, 0xC0 | (n >> 8), n & 0xFF, color])
            x += n
        out += b"\x00\x00"
    return bytes(out)


def write_sup(path, cues: list[tuple[float, float, str]], size=(1920, 1080)) -> None:
    """A .sup with one display set per (start s, end s, text) cue, and one
    clearing it."""
    from PIL import Image, ImageDraw, ImageFont
    width, height = size
    font = ImageFont.load_default(size=64)
    out = bytearray()
    for n, (start, end, text) in enumerate(cues):
        left, top, right, bottom = ImageDraw.Draw(Image.new("L", (1, 1))).textbbox((0, 0), text, font=font)
        w, h = right - left + 40, bottom - top + 40
        img = Image.new("L", (w, h), 0)
        ImageDraw.Draw(img).text((20 - left, 20 - top), text, fill=255, font=font)
        img = img.point(lambda v: 1 if v > 127 else 0)
        x, y = (width - w) // 2, height - h - 80
        shown, cleared = int(start * 90000), int(end * 90000)
        window = struct.pack(">BBHHHH", 1, 0, x, y, w, h)
        rle = _rle(img.load(), w, h)
        out += _segment(0x16, shown, struct.pack(">HHBHBBBB", width, height, 0x10, 2 * n, 0x80, 0, 0, 1)
                        + struct.pack(">HBBHH", 0, 0, 0, x, y))
        out += _segment(0x17, shown, window)
        out += _segment(0x14, shown, bytes([0, 0, 0, 16, 128, 128, 0, 1, 235, 128, 128, 255]))
        out += _segment(0x15, shown, struct.pack(">HBB", 0, 0, 0xC0) + (len(rle) + 4).to_bytes(3, "big")
                        + struct.pack(">HH", w, h) + rle)
        out += _segment(0x80, shown, b"")
        out += _segment(0x16, cleared, struct.pack(">HHBHBBBB", width, height, 0x10, 2 * n + 1, 0, 0, 0, 0))
        out += _segment(0x17, cleared, window)
        out += _segment(0x80, cleared, b"")
    with open(path, "wb") as fh:
        fh.write(out)


def make_video(path, subs: list[tuple[str, list[tuple[float, float, str]]]], seconds: int = 8,
               vobsub: bool = False, extra_args: list[str] | None = None) -> None:
    """An MKV with a test video and one image subtitle per (language, cues)."""
    sups = []
    for i, (_, cues) in enumerate(subs):
        sups.append(f"{path}.{i}.sup")
        write_sup(sups[-1], cues)
    # -copyts: ffmpeg would start each .sup input at 0, moving its cues.
    cmd = ["ffmpeg", "-v", "error", "-y", "-copyts", "-f", "lavfi", "-i", f"testsrc2=s=320x240:r=24:d={seconds}"]
    for sup in sups:
        cmd += ["-i", sup]
    cmd += ["-map", "0:v"]
    for i, (lang, _) in enumerate(subs):
        cmd += ["-map", f"{i + 1}:s", f"-metadata:s:s:{i}", f"language={lang}"]
    cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-c:s", "dvdsub" if vobsub else "copy",
            *(extra_args or []), str(path)]
    subprocess.run(cmd, check=True)
