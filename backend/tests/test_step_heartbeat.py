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


@pytest.mark.asyncio
async def test_a_step_can_be_renamed_while_it_runs(monkeypatch):
    """The VMAF target's step names the round being scored: the repeats say
    the latest, not the one the step began with."""
    from backend import converter
    monkeypatch.setattr(converter, "_STEP_REPEAT_S", 0.02)
    said = []

    async def progress(progress=0, fps=0, eta_seconds=None, **fields):
        said.append(fields)
    step = {"step": "Finding the quality for the VMAF target…"}

    async def search():
        await asyncio.sleep(0.05)
        step.update(step="VMAF target: CQ 32 (round 3)", step_key="steps.vmafTargetRound",
                    step_params={"cq": 32, "round": 3})
        await asyncio.sleep(0.05)
    await converter._with_step(progress, step, search())
    assert said[0] == {"step": "Finding the quality for the VMAF target…"}
    assert said[-1] == {"step": "VMAF target: CQ 32 (round 3)", "step_key": "steps.vmafTargetRound",
                        "step_params": {"cq": 32, "round": 3}}
