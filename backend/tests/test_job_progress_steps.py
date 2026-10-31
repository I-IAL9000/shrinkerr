"""A job's progress reaches the Queue at most every 0.5 s — but a new step
always does (v0.10.0). The quality cross-check's start came within 0.5 s of
the VMAF analysis' 99% and was dropped: the card said "VMAF analysis 99%"
for the four minutes the cross-check ran."""
import pytest

from backend.websocket import ConnectionManager


@pytest.mark.asyncio
async def test_a_new_step_is_never_held_back():
    manager = ConnectionManager()
    sent = []

    async def capture(msg):
        sent.append(msg)
    manager.broadcast = capture
    job = dict(job_id=1, file_name="f.mkv", fps=None, eta=None, jobs_completed=0, jobs_total=1, total_saved=0)
    await manager.send_job_progress(progress=98.0, step="VMAF analysis", **job)
    await manager.send_job_progress(progress=98.5, step="VMAF analysis", **job)  # held back
    await manager.send_job_progress(progress=0, step="Quality cross-check", **job)
    await manager.send_job_progress(progress=1, step="Quality cross-check", **job)  # held back
    assert [(m["step"], m["progress"]) for m in sent] == [("VMAF analysis", 98.0), ("Quality cross-check", 0)]


@pytest.mark.asyncio
async def test_the_settings_an_encode_runs_with_reach_the_queue():
    manager = ConnectionManager()
    sent = []

    async def capture(msg):
        sent.append(msg)
    manager.broadcast = capture
    job = dict(job_id=2, file_name="f.mkv", fps=None, eta=None, jobs_completed=0, jobs_total=1, total_saved=0)
    await manager.send_job_progress(progress=1, step="converting", encoder_settings={"nvenc_cq": 29}, **job)
    await manager.send_job_progress(progress=1, step="VMAF analysis", **job)
    assert sent[0]["encoder_settings"] == {"nvenc_cq": 29} and "encoder_settings" not in sent[1]
