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
