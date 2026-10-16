"""Per-rule actions beyond the encoder settings (v0.10.0) — per-library
policies without a separate system: the languages to keep, a subtitle
policy, what happens to originals, a VMAF minimum, and a savings threshold.

A rule's languages decide its files' track removals when they're queued
(and when pending jobs are refreshed); the Scanner keeps showing the global
choice. Tracks chosen by hand stay as chosen."""
from __future__ import annotations

import json
from dataclasses import replace
from typing import Optional

SUB_POLICIES = ("keep_all", "remove_all", "languages")
ORIGINALS_POLICIES = ("keep", "trash", "delete")


def _langs(value) -> list[str]:
    from backend.scanner import normalize_lang
    if not value:
        return []
    items = value if isinstance(value, list) else str(value).split(",")
    return [normalize_lang(v) for v in items if str(v).strip()]


def tracks_for_rule(row: dict, rule: Optional[dict]) -> dict:
    """`row` (scan_results columns) with its tracks' keep flags re-decided by
    the rule's audio languages and subtitle policy; unchanged when it has
    neither. A file always keeps an audio track."""
    langs = _langs((rule or {}).get("audio_keep_languages"))
    policy = (rule or {}).get("sub_policy")
    if not langs and policy not in SUB_POLICIES:
        return row
    from backend.scanner import classify_audio_tracks, classify_subtitle_tracks, current_track_rules
    try:
        audio = json.loads(row.get("audio_tracks_json") or "[]")
        subs = json.loads(row.get("subtitle_tracks_json") or "[]")
    except (ValueError, TypeError):
        return row
    native = row.get("native_language") or "und"
    rules = current_track_rules()
    if langs:
        rules = replace(rules, audio_enabled=True, audio_keep=frozenset(langs))
        flags = {t.stream_index: (t.keep, t.locked)
                 for t in classify_audio_tracks(list(audio), native, row.get("duration") or 0, rules=rules)}
        for t in audio:
            if not t.get("manual") and t.get("stream_index") in flags:
                t["keep"], t["locked"] = flags[t["stream_index"]]
        if audio and not any(t.get("keep", True) for t in audio):
            audio[0]["keep"] = True
    if policy == "remove_all":
        for t in subs:
            if not t.get("manual"):
                t["keep"] = False
    elif policy in ("keep_all", "languages"):
        rules = (replace(rules, subs_enabled=False) if policy == "keep_all" else
                 replace(rules, subs_enabled=True, sub_keep=frozenset(_langs(rule.get("sub_keep_languages"))),
                         sub_keep_unknown=False))
        flags = {t.stream_index: (t.keep, t.locked) for t in classify_subtitle_tracks(list(subs), native, rules=rules)}
        for t in subs:
            if not t.get("manual") and t.get("stream_index") in flags:
                t["keep"], t["locked"] = flags[t["stream_index"]]
    return {**row, "audio_tracks_json": json.dumps(audio), "subtitle_tracks_json": json.dumps(subs)}


def below_savings(rule: Optional[dict], row: dict) -> bool:
    """The file's estimated video savings fall short of the rule's minimum
    (a percentage of its size)."""
    minimum = (rule or {}).get("min_savings_pct")
    size = row.get("file_size") or 0
    if not minimum or size <= 0:
        return False
    return (row.get("video_conv_savings_bytes") or 0) / size * 100 < float(minimum)


def job_overrides(rule: Optional[dict]) -> dict:
    """The rule's settings a job carries (jobs columns)."""
    rule = rule or {}
    return {"originals_policy": rule.get("originals_policy") if rule.get("originals_policy") in ORIGINALS_POLICIES else None,
            "originals_days": rule.get("originals_days"),
            "vmaf_min_score": rule.get("vmaf_min_score")}


def settings_overrides(job: dict) -> dict:
    """A job's own originals policy and VMAF minimum, as the encoding
    settings convert_file reads (empty: the global ones apply). A "Fix for
    direct play" job (backend/direct_play.py) reads image subtitles into SRT
    and adds a compatibility track where the audio needs one — those it
    was asked to."""
    from backend.direct_play import job_fixes
    out: dict = {}
    fixes = job_fixes(job)
    if "subs" in fixes:
        out["image_subs_to_srt"] = True
    if "audio" in fixes:
        out["compat_for_direct_play"] = True
    policy = job.get("originals_policy")
    if policy == "keep":
        out.update(backup_original_days=max(1, int(job.get("originals_days") or 7)),
                   trash_original_after_conversion=False)
    elif policy == "trash":
        out.update(backup_original_days=0, trash_original_after_conversion=True)
    elif policy == "delete":
        out.update(backup_original_days=0, trash_original_after_conversion=False)
    if job.get("vmaf_min_score") is not None:
        out["vmaf_min_score"] = float(job["vmaf_min_score"])
    return out
