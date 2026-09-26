"""v0.9.134: nodes swap a job's encoder for one they can run.

The local node used to run a job's encoder verbatim, so an NVENC-tagged job
on a host without NVENC (a Mac, a CPU box, an Intel QSV box) failed with
ffmpeg exit 8. Local and remote nodes now share `resolve_node_encoder`.
"""
import pytest

from backend.encoder_caps import resolve_node_encoder
from backend.queue import JobQueue


@pytest.mark.parametrize("job_enc,caps,expected", [
    ("nvenc", ["libx265", "nvenc"], "nvenc"),
    ("hevc_nvenc", ["libx265", "nvenc"], "nvenc"),
    ("x265", ["libx265", "nvenc"], "libx265"),
    ("videotoolbox", ["libx265", "videotoolbox"], "videotoolbox"),
    ("qsv", ["libx265", "qsv"], "qsv"),
])
def test_native_encoder_is_kept(job_enc, caps, expected):
    assert resolve_node_encoder(job_enc, caps, translate=True) == expected
    assert resolve_node_encoder(job_enc, caps, translate=False) == expected


@pytest.mark.parametrize("caps,expected", [
    (["libx265", "videotoolbox"], "videotoolbox"),
    (["libx265", "qsv", "vaapi"], "qsv"),
    (["libx265", "vaapi"], "vaapi"),
    (["libx265"], "libx265"),
])
def test_unsupported_encoder_translates_to_best_hardware(caps, expected):
    assert resolve_node_encoder("nvenc", caps, translate=True) == expected


def test_libx265_job_is_never_upgraded_to_hardware():
    # libx265 is a deliberate quality choice; a node that can run it keeps it.
    assert resolve_node_encoder("libx265", ["libx265", "nvenc"], translate=True) == "libx265"


def test_translation_disabled_refuses():
    assert resolve_node_encoder("nvenc", ["libx265", "videotoolbox"], translate=False) is None


def test_untagged_job_uses_best_encoder_even_without_translation():
    assert resolve_node_encoder(None, ["libx265", "videotoolbox"], translate=False) == "videotoolbox"
    assert resolve_node_encoder("", ["libx265", "nvenc"], translate=True) == "nvenc"


def test_unknown_capabilities_trust_the_job():
    # Capabilities not detected yet (or unreadable): don't guess — run the
    # job as tagged, like before, rather than silently dropping to CPU.
    assert resolve_node_encoder("nvenc", [], translate=True) == "nvenc"
    assert resolve_node_encoder(None, [], translate=True) == "nvenc"


@pytest.mark.asyncio
async def test_local_queue_skips_foreign_encoders_when_translation_off(test_db):
    q = JobQueue(test_db)
    nv = await q.add_job("/m/a.mkv", "convert", encoder="nvenc")
    vt = await q.add_job("/m/b.mkv", "convert", encoder="videotoolbox")
    caps = ["libx265", "videotoolbox"]

    job = await q.get_next_job(capabilities=caps, translate=False)
    assert job["id"] == vt  # nvenc job left pending for a capable node

    job = await q.get_next_job(capabilities=caps, translate=True)
    assert job["id"] == nv  # translation on: FIFO, gets swapped at run time
