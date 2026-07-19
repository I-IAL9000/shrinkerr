"""Unit tests for the add-from-scan job-type classifier.

Pure-function tests (no app/DB) that lock in the queue's job_type decision —
in particular that a forced re-encode of an already-h265 file is a "convert"
job, never a no-op "audio"/Remux job. v0.9.58.
"""
from backend.routes.jobs import _classify_job_type


def _c(**kw):
    base = dict(
        needs_conversion=False, force_reencode=False, cleanup_only=False,
        language_remux=False, has_audio_work=False, skip_conversion=False,
    )
    base.update(kw)
    return _classify_job_type(**base)


def test_force_reencode_x265_is_convert_not_remux():
    # Already-h265 (needs_conversion=False), no cleanup work, force ticked.
    # MUST be a video convert — not an "audio"/Remux job.
    assert _c(needs_conversion=False, force_reencode=True) == "convert"


def test_force_reencode_x265_with_audio_work_is_combined():
    assert _c(needs_conversion=False, force_reencode=True, has_audio_work=True) == "combined"


def test_h264_needs_conversion_is_convert():
    assert _c(needs_conversion=True) == "convert"


def test_h264_with_audio_work_is_combined():
    assert _c(needs_conversion=True, has_audio_work=True) == "combined"


def test_audio_work_only_is_audio():
    assert _c(needs_conversion=False, has_audio_work=True) == "audio"


def test_language_remux_is_audio():
    assert _c(language_remux=True) == "audio"
    # language_remux wins even if the source would otherwise convert
    assert _c(needs_conversion=True, language_remux=True) == "audio"


def test_cleanup_only_wins_over_force_reencode():
    # cleanup_only + force + no work → skipped (None), never a convert.
    assert _c(force_reencode=True, cleanup_only=True) is None
    # cleanup_only + actual sub/audio work → audio cleanup.
    assert _c(force_reencode=True, cleanup_only=True, has_audio_work=True) == "audio"


def test_ignore_rule_no_work_is_skipped():
    assert _c(skip_conversion=True) is None


def test_ignore_rule_with_force_still_converts():
    # force overrides an ignore ("skip video conversion") rule.
    assert _c(skip_conversion=True, force_reencode=True) == "convert"


def test_nothing_to_do_no_exemption_is_noop_audio():
    # An already-optimal file with no work and no exemption still yields a
    # (no-op) audio job today — documents current behaviour.
    assert _c(needs_conversion=False) == "audio"
