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

    print("\n[4] pgsrip end-to-end (mkv -> srt text)?")
    try:
        from pgsrip import pgsrip as _pg, Mkv, Options
        from babelfish import Language
        media = Mkv(fp)
        ok = _pg.rip(media, Options(languages={Language("eng")}, overwrite=True))
        print("  pgsrip rip ok:", ok)
        # If it produced an .srt next to the file, show a sample.
        base = os.path.splitext(fp)[0]
        for cand in (base + ".en.srt", base + ".eng.srt", base + ".srt"):
            if os.path.exists(cand):
                with open(cand, "rb") as fh:
                    sample = fh.read(400)
                print(f"  produced {cand}; sample: {sample!r}")
                break
    except Exception as e:
        print("  pgsrip path failed (may need different API/args):", repr(e))

    print("\nDONE — paste this whole output back.")
    shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
