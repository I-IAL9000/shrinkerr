"""The VMAF target's check of the finished file (v0.10.0): a file that
misses the target by more than a point is encoded once more, a step lower
per point short. The re-encode is kept whatever it scores; when it's
rejected (larger, under the minimum) or fails, the first encode is kept if
it passes both. The minimum rejects only with VMAF analysis on."""
import asyncio
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import aiosqlite
import pytest

import backend.converter as converter
from backend import vmaf_target


def _tools() -> bool:
    if not shutil.which("ffmpeg"):
        return False
    filters = subprocess.run(["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True).stdout
    encoders = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
    return "libvmaf" in filters and "libx265" in encoders


needs_vmaf = pytest.mark.skipif(not _tools(), reason="needs ffmpeg with libvmaf and libx265")

# Where the (stubbed) search lands: CQ 30, libx265's CRF 32.
FOUND = {"cq": 30, "vmaf": 93.5, "min": 93.0, "reached": True, "rounds": 3,
         "scores": {30: {"mean": 93.5, "min": 93.0}}, "target": 93.0, "samples": 3, "seconds": 1.0}


def _clip(path: Path, crf: int) -> Path:
    """A short, noisy clip: its size follows the quality closely."""
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    "testsrc2=s=320x240:r=24:d=3,noise=alls=25:allf=t", "-c:v", "libx264", "-crf", str(crf),
                    "-preset", "ultrafast", str(path)], check=True)
    return path


def _left(folder: Path) -> list[str]:
    """Every file under `folder`, relative to it."""
    return sorted(str(p.relative_to(folder)) for p in folder.rglob("*") if p.is_file())


@pytest.fixture
def run(test_db, tmp_path, monkeypatch):
    """convert_file on a clip with the VMAF target (93) on. The search is
    stubbed to land at `found`; the checks of the finished files score
    `scores` in turn (a number, or a libvmaf pass's result dict). Records
    each encode's CRF and any rename onto an existing path."""
    seen = SimpleNamespace(crfs=[], onto_existing=[], src=None, before=None, probe=None)
    real_build, real_rename, real_replace = converter._build_ffmpeg_cmd_impl, os.rename, os.replace

    def build(*a, **kw):
        seen.crfs.append(kw["crf"])
        return real_build(*a, **kw)
    monkeypatch.setattr(converter, "_build_ffmpeg_cmd_impl", build)

    def rename(src, dst, *a, **kw):  # an SMB share's recycle bin keeps a file renamed over (Path.rename too)
        if os.path.lexists(dst):
            seen.onto_existing.append(str(dst))
        return real_rename(src, dst, *a, **kw)

    def replace(src, dst, *a, **kw):
        if os.path.lexists(dst):
            seen.onto_existing.append(str(dst))
        return real_replace(src, dst, *a, **kw)
    monkeypatch.setattr(os, "rename", rename)
    monkeypatch.setattr(os, "replace", replace)

    async def go(*scores, found=None, source_crf=12, with_probe=False, **settings):
        async with aiosqlite.connect(test_db) as db:
            for k, v in {"vmaf_target_enabled": "true", "vmaf_target_score": "93", **settings}.items():
                await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (k, str(v)))
            await db.commit()
        landed = {**FOUND, **(found or {})}

        async def search(*a, **kw):
            return dict(landed)
        monkeypatch.setattr(vmaf_target, "find_quality", search)
        queue = list(scores)

        async def score(**kw):
            s = queue.pop(0)
            s = s if isinstance(s, dict) else {"score": s, "min": s, "max": s}
            return {"harmonic_mean": s.get("score"), "error": None, "stderr_tail": [], "seek": kw["seek"], **s}
        monkeypatch.setattr(converter, "_run_libvmaf_pass", score)
        seen.src = _clip(tmp_path / "media" / "Film (2020)" / "Film (2020) x264.mkv", source_crf)
        seen.before = seen.src.read_bytes()
        if with_probe:  # the caller's probe of the source, as the queue passes it
            from backend.scanner import probe_file
            seen.probe = await probe_file(str(seen.src))
            go.kwargs["pre_probe"] = seen.probe
        result = await converter.convert_file(str(seen.src), "libx265", 3.0, override_libx265_preset="ultrafast",
                                              **go.kwargs)
        assert queue == [], "every check scored"
        return result
    go.kwargs = {}
    go.seen = seen
    return go


def _intercept_retry(monkeypatch, outcome):
    """convert_file with its re-encode (the call carrying _target_retry)
    answered by `outcome(**kwargs)`. The recursive call finds the module's
    convert_file when it runs, so this catches it."""
    real, calls = converter.convert_file, []

    async def wrapper(*a, **kw):
        if kw.get("_target_retry"):
            calls.append(kw)
            return await outcome(**kw)
        return await real(*a, **kw)
    monkeypatch.setattr(converter, "convert_file", wrapper)
    return calls


def test_the_retry_cq():
    curve = {"cq": 30, "target": 93, "scores": {28: {"mean": 95.5, "min": 94}, 30: {"mean": 93.2, "min": 92}}}
    assert converter._target_retry_cq(curve, 91.0, False) == 28  # the curve: 28 scored 93 + 2 points short
    assert converter._target_retry_cq({**curve, "scores": {}}, 91.0, False) == 28  # no curve: 30 − 2
    assert converter._target_retry_cq({**curve, "cq": 30.5, "scores": {}}, 92.5, True) == 29.5  # at least a step
    # the curve below the target plus the shortfall: a step per point short
    assert converter._target_retry_cq({**curve, "scores": {28: {"mean": 94.0, "min": 93}}}, 90.2, False) == 27
    # the curve's own CQ (it scored 93 + the shortfall in samples) isn't lower: at least one step down
    assert converter._target_retry_cq({**curve, "scores": {30: {"mean": 96.0, "min": 95}}}, 91.5, False) == 28
    assert converter._target_retry_cq({**curve, "cq": 16}, 80.0, False) == vmaf_target.CQ_BEST  # clamped
    assert converter._target_retry_cq({**curve, "scores": {}}, 91.5, False) == 28  # 1.5 short: 2 steps
    assert converter._target_retry_cq({**curve, "cq": 30.5, "scores": {}}, 91.5, False) == 28  # whole steps


def test_the_retry_cq_changes_the_encoders_setting():
    """VideoToolbox gives every CQ from 29 up -q:v 40: a CQ that shares the
    first encode's setting isn't lower."""
    from backend.encoding_estimates import quality_settings

    def vt(cq):
        return quality_settings("videotoolbox", cq)
    curve = {"cq": 36, "target": 93, "scores": {32: {"mean": 96.0, "min": 95}}}
    assert converter._target_retry_cq(curve, 91.0, False, vt) == 28  # not 32 (the curve) nor 34: both q40
    # the curve's q40 CQ isn't lower; its q50 one is the answer (not q40 stepped down to q45)
    two = {**curve, "scores": {32: {"mean": 96.0, "min": 95}, 27: {"mean": 96.0, "min": 95}}}
    assert converter._target_retry_cq(two, 91.0, False, vt) == 27
    assert converter._target_retry_cq({**curve, "cq": 20}, 91.5, False, vt) == 18  # q60 → q62
    floor = converter._target_retry_cq({**curve, "cq": 16}, 90.0, False, vt)  # 14–16 all q65: nothing lower
    assert floor == vmaf_target.CQ_BEST and vt(floor) == vt(16)


def _retry(cq, vmaf, outcome, error=None):
    return {"cq": cq, "vmaf": vmaf, "outcome": outcome, "error": error}


@needs_vmaf
@pytest.mark.asyncio
async def test_a_miss_is_reencoded_once(run):
    said = []

    async def progress(**kw):
        said.append(kw)
    run.kwargs = {"progress_callback": progress}
    result = await run(90.0, 95.0)
    seen = run.seen
    assert result["success"] and not result.get("vmaf_rejected"), result.get("error")
    assert seen.crfs == [32, 29]  # CQ 30; 3 points short, no curve below it: CQ 27
    stats = result["encoding_stats"]
    assert stats["crf"] == 29 and result["vmaf_score"] == 95.0
    assert stats["vmaf_target"]["reencoded"] == {"from_cq": 30, "from_vmaf": 90.0}
    assert stats["vmaf_target"]["checked_score"] == 90.0  # the first encode's check
    assert stats["vmaf_target"]["retry"] == _retry(27, 95.0, "placed")
    assert stats["vmaf_target"]["cq"] == 30  # where the search landed, for the folder's next search
    assert "first_kept" not in stats["vmaf_target"]
    assert _left(seen.src.parent) == ["Film (2020) x265.mkv"] == [Path(result["output_path"]).name]
    assert seen.onto_existing == []
    # the re-encode says so while it runs: its encode's progress carries the step
    label = next(i for i, kw in enumerate(said) if kw.get("step_key") == "steps.vmafTargetReencode")
    assert said[label]["step_params"] == {"cq": 27}
    encode = [kw for kw in said[label:] if not kw.get("step")]
    assert encode and all(kw.get("step_key") == "steps.vmafTargetReencode" for kw in encode)
    assert not any(kw.get("step_key") == "steps.vmafTargetReencode" for kw in said[:label])


@needs_vmaf
@pytest.mark.asyncio
async def test_the_reencode_is_kept_whatever_it_scores(run):
    result = await run(90.0, 89.0)  # misses again: no second re-encode
    assert run.seen.crfs == [32, 29]
    assert result["success"] and result["vmaf_score"] == 89.0
    assert result["encoding_stats"]["vmaf_target"]["reencoded"] == {"from_cq": 30, "from_vmaf": 90.0}
    assert result["encoding_stats"]["vmaf_target"]["retry"] == _retry(27, 89.0, "placed")
    assert _left(run.seen.src.parent) == ["Film (2020) x265.mkv"]


@needs_vmaf
@pytest.mark.asyncio
async def test_within_a_point_is_kept(run):
    result = await run(92.2)
    assert run.seen.crfs == [32]
    stats = result["encoding_stats"]["vmaf_target"]
    assert stats["checked_score"] == 92.2 and not {"reencoded", "first_kept", "retry"} & set(stats)


@needs_vmaf
@pytest.mark.asyncio
@pytest.mark.parametrize("check", [{"score": 60.0, "min": 0.0, "max": 100.0},  # bimodal: measurement-uncertain
                                   {"score": None, "min": None, "max": None, "error": "no score"}])
async def test_an_unsure_check_isnt_retried(run, check):
    result = await run(check)
    assert result["success"] and run.seen.crfs == [32]
    assert "retry" not in result["encoding_stats"]["vmaf_target"]


@needs_vmaf
@pytest.mark.asyncio
async def test_the_floor_isnt_retried(run):
    floor = {"cq": 14, "vmaf": 90.5, "min": 90.0, "reached": False, "scores": {14: {"mean": 90.5, "min": 90.0}}}
    result = await run(90.0, found=floor)
    assert result["success"] and run.seen.crfs == [16]
    assert not {"reencoded", "retry"} & set(result["encoding_stats"]["vmaf_target"])


def _collapsed(monkeypatch, crf_for):
    """libx265's CRF for a CQ as `crf_for` says — CQs sharing one setting,
    the way VideoToolbox's do."""
    import backend.encoding_estimates as estimates
    real = estimates.quality_settings

    def quality_settings(encoder, cq):
        return {"libx265_crf": crf_for(cq)} if encoder == "libx265" else real(encoder, cq)
    monkeypatch.setattr(estimates, "quality_settings", quality_settings)


@needs_vmaf
@pytest.mark.asyncio
async def test_cqs_sharing_the_setting_are_stepped_past(run, monkeypatch):
    _collapsed(monkeypatch, lambda cq: 30 if cq >= 26 else cq + 2)
    result = await run(90.0, 95.0)
    assert run.seen.crfs == [30, 27]  # CQ 27 is CRF 30 too: CQ 25
    assert result["encoding_stats"]["vmaf_target"]["retry"] == _retry(25, 95.0, "placed")


@needs_vmaf
@pytest.mark.asyncio
async def test_no_lower_setting_no_reencode(run, monkeypatch):
    _collapsed(monkeypatch, lambda cq: 30)
    result = await run(90.0)
    assert result["success"] and run.seen.crfs == [30]
    stats = result["encoding_stats"]["vmaf_target"]
    assert stats["checked_score"] == 90.0 < stats["target"] and "retry" not in stats  # the miss, reported


@needs_vmaf
@pytest.mark.asyncio
async def test_a_larger_reencode_keeps_the_first(run):
    """The first encode (CQ 42) is far smaller than the source, the
    re-encode (28 points short: CQ 14) far larger. With originals kept, a
    larger encode is held for Keep anyway — not a re-encode's."""
    result = await run(65.0, found={"cq": 42, "scores": {}}, source_crf=28, backup_original_days=1)
    seen = run.seen
    assert seen.crfs == [44, 16]
    assert result["success"] and not result.get("skipped_larger") and not result.get("held")
    stats = result["encoding_stats"]
    assert stats["crf"] == 44 and stats["vmaf_target"]["first_kept"] is True
    assert stats["vmaf_target"]["checked_score"] == 65.0 == result["vmaf_score"]
    assert stats["vmaf_target"]["retry"] == _retry(14, None, "larger")
    assert "reencoded" not in stats["vmaf_target"]
    assert _left(seen.src.parent) == [".shrinkerr_backup/Film (2020) x264.mkv", "Film (2020) x265.mkv"]
    assert seen.onto_existing == []


@needs_vmaf
@pytest.mark.asyncio
async def test_a_larger_reencode_with_the_first_under_the_minimum(run):
    result = await run(65.0, found={"cq": 42, "scores": {}}, source_crf=28,
                       vmaf_analysis_enabled="true", vmaf_min_score=70)
    assert run.seen.crfs == [44, 16]
    assert result["success"] and result["skipped_larger"] and result["output_path"] == str(run.seen.src)
    stats = result["encoding_stats"]["vmaf_target"]  # both encodes reported
    assert stats["checked_score"] == 65.0 and stats["retry"] == _retry(14, None, "larger")
    assert "first_kept" not in stats and run.seen.src.read_bytes() == run.seen.before
    assert _left(run.seen.src.parent) == ["Film (2020) x264.mkv"]


@needs_vmaf
@pytest.mark.asyncio
async def test_a_rejected_reencode_keeps_a_first_that_passes(run):
    result = await run(88.0, 84.0, vmaf_analysis_enabled="true", vmaf_min_score=85)
    assert run.seen.crfs == [32, 27]  # 5 points short
    assert result["success"] and not result.get("vmaf_rejected") and result["vmaf_score"] == 88.0
    stats = result["encoding_stats"]
    assert stats["crf"] == 32 and stats["vmaf_target"]["first_kept"] is True
    assert stats["vmaf_target"]["retry"] == _retry(25, 84.0, "rejected")
    assert _left(run.seen.src.parent) == ["Film (2020) x265.mkv"]
    assert run.seen.onto_existing == []


@needs_vmaf
@pytest.mark.asyncio
async def test_analysis_off_scores_but_never_rejects(run):
    result = await run(80.0, 85.0, vmaf_analysis_enabled="false", vmaf_min_score=88)
    assert run.seen.crfs == [32, 19]  # 13 points short: CQ 17
    assert result["success"] and not result.get("vmaf_rejected") and result["vmaf_score"] == 85
    assert result["encoding_stats"]["vmaf_target"]["reencoded"] == {"from_cq": 30, "from_vmaf": 80.0}
    assert _left(run.seen.src.parent) == ["Film (2020) x265.mkv"]


@needs_vmaf
@pytest.mark.asyncio
async def test_analysis_on_still_rejects_below_the_minimum(run):
    result = await run(80.0, 85.0, vmaf_analysis_enabled="true", vmaf_min_score=88)
    assert run.seen.crfs == [32, 19]
    assert result["success"] and result["vmaf_rejected"] and result["vmaf_score"] == 85.0  # the re-encode's
    stats = result["encoding_stats"]["vmaf_target"]  # and the first's
    assert stats["checked_score"] == 80.0 and stats["retry"] == _retry(17, 85.0, "rejected")
    assert "first_kept" not in stats
    assert result["output_path"] == str(run.seen.src) and run.seen.src.read_bytes() == run.seen.before
    assert _left(run.seen.src.parent) == ["Film (2020) x264.mkv"]


@needs_vmaf
@pytest.mark.asyncio
async def test_a_settings_change_meanwhile_doesnt_change_the_reencode(run, monkeypatch):
    """Settings' minimum raised while the first encode was checked: the
    re-encode is judged by the job's settings, not the new ones."""
    real, reads = converter.get_live_encoding_settings, []

    async def live():
        reads.append(1)
        settings = await real()
        return settings if len(reads) == 1 else {**settings, "vmaf_analysis_enabled": True, "vmaf_min_score": 99.0}
    monkeypatch.setattr(converter, "get_live_encoding_settings", live)
    result = await run(90.0, 95.0)
    assert result["success"] and result["encoding_stats"]["vmaf_target"]["retry"]["outcome"] == "placed"
    assert reads == [1]


@needs_vmaf
@pytest.mark.asyncio
@pytest.mark.parametrize("how", ["fails", "raises"])
async def test_a_failed_reencode_keeps_the_first(run, monkeypatch, how):
    async def outcome(**kw):
        Path(converter.get_temp_path(kw["input_path"])).write_bytes(b"left by the re-encode")
        if how == "raises":
            raise RuntimeError("boom")
        return {"success": False, "output_path": None, "space_saved": 0,
                "error": "ffmpeg exited with code 1\n\nthe ffmpeg detail", "error_key": "errors.ffmpegExited"}
    calls = _intercept_retry(monkeypatch, outcome)
    result = await run(90.0, with_probe=True)
    assert result["success"], result.get("error")
    assert run.seen.crfs == [32]  # the re-encode's own run is the stub
    [call] = calls
    assert (call["override_cq"], call["override_crf"]) == (27, 29)
    assert call["_target_retry"]["search"]["cq"] == 30 and call["pre_settings"]["vmaf_target_enabled"] is True
    # the caller's probe as it was given (the first call patches its tracks' languages in place)
    assert call["pre_probe"] == run.seen.probe and call["pre_probe"] is not run.seen.probe
    stats = result["encoding_stats"]
    assert stats["crf"] == 32 and stats["vmaf_target"]["first_kept"] is True
    error = "RuntimeError: boom" if how == "raises" else "ffmpeg exited with code 1"
    assert stats["vmaf_target"]["retry"] == _retry(27, None, "failed", error)
    out = Path(result["output_path"])
    assert _left(run.seen.src.parent) == [out.name] and out.read_bytes() != b"left by the re-encode"
    assert run.seen.onto_existing == []


@needs_vmaf
@pytest.mark.asyncio
@pytest.mark.parametrize("code, key", [(-11, "errors.ffmpegExited"),  # crashed (SIGSEGV)
                                       (-9, "errors.ffmpegTimedOut"),  # killed by its own timeout
                                       (-9, None)])  # killed on a progress error
async def test_a_crashed_or_timed_out_reencode_keeps_the_first(run, monkeypatch, code, key):
    async def outcome(**kw):
        kw["proc_callback"](SimpleNamespace(returncode=code))
        return {"success": False, "output_path": None, "space_saved": 0, "error": f"ffmpeg ended ({code})",
                **({"error_key": key} if key else {})}
    _intercept_retry(monkeypatch, outcome)
    result = await run(90.0)
    assert result["success"] and result["encoding_stats"]["vmaf_target"]["first_kept"] is True
    assert result["encoding_stats"]["vmaf_target"]["retry"] == _retry(27, None, "failed", f"ffmpeg ended ({code})")
    assert _left(run.seen.src.parent) == ["Film (2020) x265.mkv"]


@needs_vmaf
@pytest.mark.asyncio
async def test_a_leftover_that_wont_go_isnt_renamed_over(run, monkeypatch):
    """The re-encode left a file at the temp name that can't be removed: the
    first encode is placed from where it was set aside instead."""
    leftover = []

    async def outcome(**kw):
        leftover.append(Path(converter.get_temp_path(kw["input_path"])))
        leftover[0].write_bytes(b"left by the re-encode")
        return {"success": False, "output_path": None, "space_saved": 0, "error": "ffmpeg exited with code 1"}
    _intercept_retry(monkeypatch, outcome)
    real_unlink = Path.unlink

    def unlink(self, *a, **kw):
        if leftover and self == leftover[0]:
            raise PermissionError(self)
        return real_unlink(self, *a, **kw)
    monkeypatch.setattr(Path, "unlink", unlink)
    result = await run(90.0)
    assert result["success"] and result["encoding_stats"]["vmaf_target"]["first_kept"] is True
    assert result["encoding_stats"]["crf"] == 32 and run.seen.onto_existing == []
    assert _left(run.seen.src.parent) == ["Film (2020) x264.converting.mkv", "Film (2020) x265.mkv"]
    assert leftover[0].read_bytes() == b"left by the re-encode"


@needs_vmaf
@pytest.mark.asyncio
async def test_a_failed_reencode_leaves_the_minimum_to_judge_the_first(run, monkeypatch):
    async def outcome(**kw):
        return {"success": False, "output_path": None, "space_saved": 0, "error": "ffmpeg exited with code 1"}
    _intercept_retry(monkeypatch, outcome)
    result = await run(80.0, vmaf_analysis_enabled="true", vmaf_min_score=88)
    assert result["success"] and result["vmaf_rejected"] and result["vmaf_score"] == 80.0  # as before the target
    stats = result["encoding_stats"]["vmaf_target"]  # the rejection reports the re-encode too
    assert stats["checked_score"] == 80.0 and stats["retry"] == _retry(17, None, "failed", "ffmpeg exited with code 1")
    assert "first_kept" not in stats
    assert run.seen.src.read_bytes() == run.seen.before
    assert _left(run.seen.src.parent) == ["Film (2020) x264.mkv"]


@needs_vmaf
@pytest.mark.asyncio
async def test_a_cancelled_reencode_places_nothing(run, monkeypatch):
    """A Cancel kills the re-encode's ffmpeg (SIGKILL): the job stops there,
    the original kept — the first encode isn't placed instead."""
    async def outcome(**kw):
        kw["proc_callback"](SimpleNamespace(returncode=-9))  # registered, then killed
        return {"success": False, "output_path": None, "space_saved": 0, "error": "ffmpeg exited with code -9",
                "error_key": "errors.ffmpegExited", "error_params": {"code": "-9"}}
    _intercept_retry(monkeypatch, outcome)
    procs = []
    run.kwargs = {"proc_callback": procs.append}
    result = await run(90.0)
    assert not result["success"] and procs[-1].returncode == -9  # the queue's callback got it too
    assert result["error_key"] == "errors.ffmpegExited"  # the queue reads it as the Cancel
    assert result["encoding_stats"]["vmaf_target"]["retry"] == _retry(27, None, "failed", "ffmpeg exited with code -9")
    assert run.seen.src.read_bytes() == run.seen.before
    assert _left(run.seen.src.parent) == ["Film (2020) x264.mkv"]


@needs_vmaf
@pytest.mark.asyncio
async def test_a_reencode_that_raised_after_placing_isnt_placed_over(run, monkeypatch):
    async def outcome(**kw):
        Path(converter.get_output_path(kw["input_path"], suffix="", encoder="libx265")).write_bytes(b"placed")
        raise RuntimeError("after placing")
    _intercept_retry(monkeypatch, outcome)
    with pytest.raises(RuntimeError, match="after placing"):
        await run(90.0)
    assert _left(run.seen.src.parent) == ["Film (2020) x264.mkv", "Film (2020) x265.mkv"]
    assert (run.seen.src.parent / "Film (2020) x265.mkv").read_bytes() == b"placed"
    assert run.seen.onto_existing == []


@needs_vmaf
@pytest.mark.asyncio
async def test_review_mode_holds_the_reencode(run):
    result = await run(90.0, 95.0, review_before_replace="true")
    assert result["success"] and result["review"] and run.seen.crfs == [32, 29]
    assert result["encoding_stats"]["crf"] == 29 and result["vmaf_score"] == 95.0
    assert result["encoding_stats"]["vmaf_target"]["retry"] == _retry(27, 95.0, "placed")
    held = Path(result["review_state"]["review_path"])
    assert _left(run.seen.src.parent) == [".shrinkerr-review/Film (2020) x265.mkv", "Film (2020) x264.mkv"]
    assert held.name == "Film (2020) x265.mkv" and run.seen.src.read_bytes() == run.seen.before


# A Cancel during the first encode's VMAF pass finds no ffmpeg running: the
# re-encode's, started after, is killed as it's registered (v0.10.0).

async def _two_encodes(proc_callback, cancel):
    """A conversion's two ffmpegs: the first ends, Cancel comes (its VMAF
    pass), then the re-encode's starts — killed at registration, or a minute."""
    first = await asyncio.create_subprocess_exec("true")
    proc_callback(first)
    await first.wait()
    await cancel()
    second = await asyncio.create_subprocess_exec("sleep", "60")
    proc_callback(second)
    await asyncio.wait_for(second.wait(), timeout=10)
    return second, {"success": False, "output_path": None, "space_saved": 0,
                    "error": f"ffmpeg exited with code {second.returncode}", "error_key": "errors.ffmpegExited",
                    "error_params": {"code": str(second.returncode)}}


@pytest.mark.asyncio
async def test_the_queue_kills_an_ffmpeg_started_after_a_cancel(test_db, tmp_path, monkeypatch):
    import sys

    import backend.scanner
    from backend.queue import JobQueue, QueueWorker
    for name, module in list(sys.modules.items()):
        if name.startswith("backend.") and isinstance(getattr(module, "DB_PATH", None), str):
            monkeypatch.setattr(module, "DB_PATH", test_db)
    # the queue's cancel path asks backend.main's node manager: a stand-in
    # (importing it binds the auth database for every later test)
    monkeypatch.setitem(sys.modules, "backend.main", SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace())))
    src = tmp_path / "Film (2020).mkv"
    src.write_bytes(b"x")

    async def probe(path, *a, **kw):
        return {"duration": 1400.0, "file_size": 1000, "video_codec": "h264", "audio_tracks": [],
                "subtitle_tracks": []}
    monkeypatch.setattr(backend.scanner, "probe_file", probe)
    queue, worker, killed = JobQueue(test_db), QueueWorker(test_db), []
    job_id = await queue.add_job(str(src), "convert", encoder="libx265")

    async def convert(**kw):
        second, result = await _two_encodes(kw["proc_callback"], lambda: worker.cancel_current(job_id))
        killed.append(second.returncode)
        return result
    monkeypatch.setattr(converter, "convert_file", convert)
    job = next(j for j in await queue.get_all_jobs() if j["id"] == job_id)
    await asyncio.wait_for(worker._process_job(job), timeout=20)
    assert killed == [-9]
    async with aiosqlite.connect(test_db) as db:
        async with db.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)) as cur:
            assert (await cur.fetchone())[0] == "cancelled"


@pytest.mark.asyncio
async def test_a_remote_worker_kills_an_ffmpeg_started_after_a_cancel(tmp_path, monkeypatch):
    import backend.scanner
    from backend import worker_mode
    src = tmp_path / "Film (2020).mkv"
    src.write_bytes(b"x")

    class Client:
        completed: list = []

        async def report_progress(self, *a, **kw):
            return True  # the server says it was cancelled

        async def report_complete(self, node_id, job_id, success, **kw):
            self.completed.append((success, kw.get("error_key")))

    async def probe(path, *a, **kw):
        return {"duration": 1400.0, "file_size": 1000, "video_codec": "h264", "audio_tracks": [],
                "subtitle_tracks": []}
    monkeypatch.setattr(backend.scanner, "probe_file", probe)
    killed = []

    async def convert(**kw):
        async def cancel():  # the next progress report hears of it
            await kw["progress_callback"](progress=50.0, step="VMAF analysis")
        second, result = await _two_encodes(kw["proc_callback"], cancel)
        killed.append(second.returncode)
        return result
    monkeypatch.setattr(converter, "convert_file", convert)
    client = Client()
    await asyncio.wait_for(worker_mode.execute_job(client, "node-1", {"id": 7, "file_path": str(src),
                                                                     "job_type": "convert", "encoder": "libx265"},
                                                   ["libx265"]), timeout=20)
    assert killed == [-9] and client.completed == [(False, "errors.cancelledByUser")]
