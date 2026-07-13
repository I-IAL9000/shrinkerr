"""v0.9.0 OCR tooling spike — run INSIDE the container against a real
file with an und PGS or VobSub subtitle:

    docker cp scripts/ocr_spike.py shrinkerr:/tmp/
    docker exec shrinkerr python3 /tmp/ocr_spike.py "/media/.../file.mkv" <stream_index>

Reports, step by step, whether each stage works: binaries present,
pgsrip/pytesseract import, mkvextract to .sup, and a pgsrip end-to-end
rip — so we know which tooling to build the real module around.
"""
import sys
import subprocess
import tempfile
import os
import shutil


def main():
    if len(sys.argv) < 3:
        print("usage: ocr_spike.py <file> <stream_index>")
        return 1
    fp, idx = sys.argv[1], sys.argv[2]
    print(f"file: {fp}\nstream: {idx}")

    print("\n[1] binaries present?")
    for b in ("mkvextract", "tesseract", "ffmpeg"):
        print(f"  {b}: {shutil.which(b)}")
    try:
        langs = subprocess.run(
            ["tesseract", "--list-langs"], capture_output=True, text=True
        ).stdout
        print("  tesseract langs:", langs.replace(chr(10), " "))
    except Exception as e:
        print("  tesseract --list-langs failed:", e)

    print("\n[2] pgsrip import?")
    try:
        import pgsrip
        print("  pgsrip OK", getattr(pgsrip, "__version__", "?"))
    except Exception as e:
        print("  pgsrip import failed:", e)
    print("[2b] pytesseract import?")
    try:
        import pytesseract  # noqa: F401
        print("  pytesseract OK")
    except Exception as e:
        print("  pytesseract import failed:", e)

    print("\n[3] mkvextract the sub track to .sup?")
    tmp = tempfile.mkdtemp(prefix="ocrspike_")
    sup = os.path.join(tmp, "sub.sup")
    r = subprocess.run(
        ["mkvextract", "tracks", fp, f"{idx}:{sup}"],
        capture_output=True, text=True,
    )
    print(
        f"  rc={r.returncode}; sup exists={os.path.exists(sup)}; "
        f"size={os.path.getsize(sup) if os.path.exists(sup) else 0}"
    )
    if r.returncode != 0:
        print("  stderr:", r.stderr[-400:])

    print("\n[4] pgsrip on the extracted .sup (writable temp, so it can write srt)?")
    if not os.path.exists(sup):
        print("  no .sup from step [3]; skipping")
    else:
        try:
            from pgsrip import pgsrip as _pg, Sup, Options
            from babelfish import Language
            media = Sup(sup)
            ret = _pg.rip(media, Options(languages={Language("eng")}, overwrite=True))
            print("  pgsrip.rip returned:", repr(ret))
            # List everything pgsrip wrote into the temp dir.
            print("  temp dir now contains:", os.listdir(tmp))
            srt = os.path.splitext(sup)[0] + ".srt"
            if os.path.exists(srt):
                with open(srt, "rb") as fh:
                    data = fh.read()
                print(f"  SRT produced: {os.path.getsize(srt)} bytes")
                print(f"  first 500 bytes: {data[:500]!r}")
            else:
                print(f"  no srt at {srt}")
        except Exception as e:
            print("  pgsrip .sup path failed:", repr(e))

    print("\n[5] direct OSD script detection on a rendered frame (fallback path)?")
    # Validate the manual path too: can tesseract OSD read a PGS frame?
    # pgsrip's PgsSubtitle can render images; try to get one and OSD it.
    try:
        from pgsrip.pgs import PgsReader
        import pytesseract
        from PIL import Image  # noqa: F401
        with open(sup, "rb") as fh:
            pgs_bytes = fh.read()
        ds_list = list(PgsReader.decode(pgs_bytes))
        print(f"  decoded {len(ds_list)} PGS segments/displaysets")
        # Find a displayset with an image and OSD it.
        shown = 0
        for ds in ds_list:
            img = getattr(ds, "image", None)
            if img is not None and shown < 1:
                osd = pytesseract.image_to_osd(img)
                print("  OSD on first image:\n   " + osd.replace(chr(10), chr(10) + "   "))
                shown += 1
        if shown == 0:
            print("  (no renderable image found via this API — will adjust)")
    except Exception as e:
        print("  manual OSD path probe failed (informational):", repr(e))

    print("\nDONE — paste this whole output back.")
    shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
