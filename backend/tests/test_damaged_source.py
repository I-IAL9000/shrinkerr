"""A conversion that fails on a damaged source (broken frames ffmpeg can't
decode) says so and marks the file corrupt in the Scanner, so a replacement
can be requested — not a bare "ffmpeg exited with code 244" (v0.10.0)."""
import os
import shutil
import subprocess
import sys

import aiosqlite
import pytest

from backend.converter import source_damaged

DAMAGED = "\n".join(
    [f"[hevc @ 0x5] Could not find ref with POC {n}\n[hevc @ 0x5] Error constructing the frame RPS." for n in range(6)]
    + ["[hevc_nvenc @ 0x5] Failed locking bitstream buffer: out of memory (10):", "Conversion failed!"])


def test_a_damaged_source_is_told_from_other_failures():
    assert source_damaged(DAMAGED)
    assert source_damaged("\n".join(["[h264 @ 0x5] error while decoding MB 12 34, bytestream -7",
                                     "[h264 @ 0x5] concealing 900 DC, 900 AC, 900 MV errors in P frame"] * 3))
    # A stray decode warning in an otherwise unrelated failure isn't damage.
    assert not source_damaged("[hevc @ 0x5] Could not find ref with POC 3\n"
                              "[out#0/matroska @ 0x5] Error writing trailer: No space left on device")
    assert not source_damaged("")


def _fake_ffmpeg(tmp_path, stderr: str, code: int) -> None:
    """`ffmpeg` that fails the encode step with this output; the rest is the real one."""
    real = shutil.which("ffmpeg")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "ffmpeg").write_text(
        f"#!{sys.executable}\n"
        "import os, sys\n"
        "args = sys.argv[1:]\n"
        "if args[-1].endswith('.converting.mkv'):\n"
        f"    sys.stderr.write({stderr!r} + '\\n'); sys.stderr.flush()\n"
        f"    sys.exit({code})\n"
        f"os.execv({real!r}, [{real!r}] + args)\n")
    (bindir / "ffmpeg").chmod(0o755)
    os.environ["PATH"] = f"{bindir}{os.pathsep}{os.environ['PATH']}"


@pytest.mark.asyncio
@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
@pytest.mark.parametrize("stderr, code, damaged", [(DAMAGED, 244, True),
                                                  ("Error writing trailer: No space left on device", 1, False)],
                         ids=["damaged", "disk-full"])
async def test_a_failed_conversion_of_a_damaged_source(test_db, tmp_path, monkeypatch, stderr, code, damaged):
    from backend.converter import convert_file
    src = tmp_path / "Show - S05E16 - 1080p WEBRip x264.mkv"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=25:duration=2",
                    "-c:v", "libx264", "-preset", "ultrafast", str(src)], check=True)
    before = src.read_bytes()
    async with aiosqlite.connect(test_db) as db:
        await db.execute("INSERT INTO scan_results (file_path, file_size, video_codec, needs_conversion, probe_status, "
                         "scan_timestamp) VALUES (?, ?, 'h264', 1, 'ok', 't')", (str(src), len(before)))
        await db.commit()
    monkeypatch.setenv("PATH", os.environ["PATH"])  # restored after the test
    _fake_ffmpeg(tmp_path, stderr, code)

    result = await convert_file(str(src), "libx265", 2.0, override_libx265_preset="ultrafast")

    assert result["success"] is False and src.read_bytes() == before
    async with aiosqlite.connect(test_db) as db:
        async with db.execute("SELECT health_status, probe_status FROM scan_results WHERE file_path = ?",
                              (str(src),)) as cur:
            row = await cur.fetchone()
    if damaged:
        assert result["error_key"] == "errors.sourceDamaged"
        assert result["error"].startswith("The source file looks damaged")
        assert "ffmpeg exited with code 244" in result["error"]  # the detail stays
        assert row == ("corrupt", "corrupt")  # the Scanner's Corrupt filter and Re-download button
    else:
        assert result["error_key"] == "errors.ffmpegExited" and row == (None, "ok")


@pytest.mark.asyncio
@pytest.mark.parametrize("log, key, marked", [(DAMAGED.splitlines(), "errors.sourceDamaged", "corrupt"),
                                              ([], "errors.outputTruncated", None)],
                         ids=["decode-errors", "no-evidence"])
async def test_a_short_output(test_db, tmp_path, monkeypatch, log, key, marked):
    """ffmpeg stopping early on broken frames (exit 0, a short output) is
    the file's damage: marked corrupt. Without decoder errors it may be a
    read cut short, so it isn't marked — the message says it's probably
    damaged, and the failed job offers a replacement either way."""
    import backend.converter as converter
    src = tmp_path / "Movie (2001).mkv"
    src.write_bytes(b"x")
    async with aiosqlite.connect(test_db) as db:
        await db.execute("INSERT INTO scan_results (file_path, file_size, scan_timestamp) VALUES (?, 1, 't')",
                         (str(src),))
        await db.commit()

    async def length(path, **kwargs):
        return 6000.0 if path == str(src) else 540.0
    monkeypatch.setattr(converter, "_probe_video_duration", length)
    monkeypatch.setattr(converter, "_probe_output_duration", length)

    got = await converter._truncation_failure(str(src), str(tmp_path / "out.mkv"), 6000.0, None, log)

    assert got["error_key"] == key and "540s of 6000s" in got["error"]
    if key == "errors.outputTruncated":
        assert "probably damaged" in got["error"]
    async with aiosqlite.connect(test_db) as db:
        async with db.execute("SELECT health_status FROM scan_results WHERE file_path = ?", (str(src),)) as cur:
            assert (await cur.fetchone())[0] == marked


@pytest.mark.asyncio
async def test_earlier_failures_of_a_damaged_source_are_told_apart_at_start(test_db):
    """A conversion that failed on a damaged source before the update kept
    "ffmpeg exited with code 244" and had no Request replacement: start-up
    finds it by its ffmpeg log, once."""
    from backend.database import init_db
    async with aiosqlite.connect(test_db) as db:
        await db.execute("DELETE FROM settings WHERE key = '_v0_10_0_backfill_damaged_sources'")
        for path, log in (("/m/Damaged.mkv", DAMAGED), ("/m/DiskFull.mkv", "No space left on device")):
            await db.execute("INSERT INTO scan_results (file_path, file_size, probe_status, scan_timestamp) "
                             "VALUES (?, 1, 'ok', 't')", (path,))
            await db.execute(
                "INSERT INTO jobs (file_path, job_type, status, created_at, error_key, error_log, ffmpeg_log, "
                "audio_tracks_to_remove) VALUES (?, 'convert', 'failed', 't', 'errors.ffmpegExited', "
                "'ffmpeg exited with code 244', ?, '[]')", (path, log))
        await db.commit()

    await init_db()

    async with aiosqlite.connect(test_db) as db:
        async with db.execute("SELECT j.file_path, j.error_key, j.error_log, s.health_status FROM jobs j "
                              "JOIN scan_results s USING (file_path) ORDER BY 1") as cur:
            rows = await cur.fetchall()
    assert rows[0][:2] == ("/m/Damaged.mkv", "errors.sourceDamaged") and rows[0][3] == "corrupt"
    assert rows[0][2].startswith("The source file looks damaged") and rows[0][2].endswith("code 244")
    assert rows[1] == ("/m/DiskFull.mkv", "errors.ffmpegExited", "ffmpeg exited with code 244", None)
