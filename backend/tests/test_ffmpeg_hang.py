"""v0.10.0 (H1): the ffmpeg timeout never fired on a hung ffmpeg.

Encode and remux read ffmpeg's stderr to EOF and only then waited with a
timeout. An ffmpeg stuck reading a stalled NAS mount keeps stderr open and
prints nothing, so the read blocked forever and the worker slot was lost
until a restart.
"""
import os
import shutil
import sys
import time

import aiosqlite
import pytest

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")


@pytest.mark.asyncio
async def test_a_hung_remux_times_out(test_db, tmp_path, monkeypatch):
    from backend.tests.test_remux_guards import _clip
    src = _clip(tmp_path)  # made with the real ffmpeg, before the fake is on PATH
    before = src.read_bytes()
    async with aiosqlite.connect(test_db) as db:
        await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('ffmpeg_timeout', '2')")
        await db.commit()

    from backend.audio import remux_audio
    # From here on `ffmpeg` holds stderr open and never finishes.
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "ffmpeg").write_text(f"#!{sys.executable}\nimport time\ntime.sleep(60)\n")
    (bindir / "ffmpeg").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    started = time.monotonic()
    result = await remux_audio(str(src), [1], duration=3.0)

    assert time.monotonic() - started < 20
    assert result["success"] is False and result["error_key"] == "errors.ffmpegTimedOut"
    assert src.read_bytes() == before


@pytest.mark.asyncio
async def test_silence_counts_as_a_stall(monkeypatch):
    import asyncio
    import backend.converter as converter
    monkeypatch.setattr(converter, "_FFMPEG_STALL_SECONDS", 0.5)
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "import time; time.sleep(30)", stderr=asyncio.subprocess.PIPE)
    try:
        started = time.monotonic()
        with pytest.raises(asyncio.TimeoutError):
            await converter._read_ffmpeg_output(proc.stderr, time.monotonic() + 3600)
        assert time.monotonic() - started < 5
    finally:
        proc.kill()
        await proc.wait()


@pytest.mark.asyncio
async def test_a_test_encode_survives_a_chatty_encoder(test_db, tmp_path, monkeypatch):
    """M2: Settings' test encode read progress from stdout and left stderr
    unread until the end: an encoder that writes more than a pipe's worth to
    stderr blocked there, progress stopped, and the test encode hung."""
    import asyncio
    import subprocess
    import backend.test_encode as test_encode
    from backend.test_encode import run_test_encode
    monkeypatch.setattr(test_encode, "TEMP_DIR", tmp_path / "test-encode")
    src = tmp_path / "Clip.mkv"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=25:duration=15",
                    "-c:v", "libx264", "-preset", "ultrafast", str(src)], check=True)
    real = shutil.which("ffmpeg")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "ffmpeg").write_text(
        f"#!{sys.executable}\n"
        "import os, sys\n"
        "args = sys.argv[1:]\n"
        "if args[-1].endswith('.converting.mkv'):  # the encode step: flood stderr, then finish\n"
        "    sys.stderr.write('x' * 300000); sys.stderr.flush()\n"
        "    print('progress=end', flush=True)\n"
        "    open(args[-1], 'wb').write(b'0' * 1000)\n"
        "    sys.exit(0)\n"
        f"os.execv({real!r}, [{real!r}] + args)\n")
    (bindir / "ffmpeg").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")

    result = await asyncio.wait_for(run_test_encode(str(src), encoder="libx265", sample_seconds=5), timeout=60)

    assert result.get("encoded_size") == 1000, result
