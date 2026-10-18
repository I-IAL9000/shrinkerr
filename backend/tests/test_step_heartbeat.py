"""A long step before the encode (reading image subtitles, the VMAF target
search, language detection) said its name once: the Queue shows a job's
step from its live progress, so a page opened later showed a bare spinner
for minutes. The step is repeated while it runs (v0.10.0)."""
import asyncio

import pytest


@pytest.mark.asyncio
async def test_the_step_is_repeated_while_it_runs(monkeypatch):
    from backend import converter
    monkeypatch.setattr(converter, "_STEP_REPEAT_S", 0.02)
    said = []

    async def progress(progress=0, fps=0, eta_seconds=None, step=None):
        said.append(step)

    async def ocr():
        await asyncio.sleep(0.11)
        return ["subs"]
    assert await converter._with_step(progress, "Reading image subtitles (OCR)…", ocr()) == ["subs"]
    count = len(said)
    assert count >= 4 and set(said) == {"Reading image subtitles (OCR)…"}
    await asyncio.sleep(0.06)
    assert len(said) == count  # stops with the step
    assert await converter._with_step(None, "x", ocr()) == ["subs"]  # no callback: just the step
