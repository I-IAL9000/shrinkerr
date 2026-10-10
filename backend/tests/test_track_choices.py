"""Keep/remove choices made by hand (v0.10.0).

Changing the languages to keep now updates the files already scanned, and a
rescan re-applies the rules — but a track whose keep/remove the user set
stays as they set it."""
import json

import aiosqlite
import pytest

from backend.scanner import TrackRules, keep_manual_choices, mark_manual_choices


def tr(si, lang, keep, codec="aac", channels=2, **extra):
    return {"stream_index": si, "language": lang, "codec": codec, "channels": channels,
            "keep": keep, "locked": False, **extra}


def test_an_edit_marks_the_tracks_it_changed():
    stored = [tr(1, "eng", True), tr(2, "fra", False), tr(3, "spa", False, manual=True)]
    edited = [tr(1, "eng", True), tr(2, "fra", True), tr(3, "spa", False)]
    out = mark_manual_choices(edited, stored)
    assert [t.get("manual", False) for t in out] == [False, True, True]  # 3: marked before


def test_choices_carry_onto_the_same_stream_only():
    stored = [tr(1, "und", False, manual=True), tr(2, "fra", True, manual=True)]
    fresh = [tr(1, "eng", True), tr(2, "fra", False, codec="ac3")]
    out = keep_manual_choices(fresh, stored)
    # Stream 1: same stream, its language since detected — the choice stays.
    assert out[0]["keep"] is False and out[0]["manual"] is True
    # Stream 2: a different codec — another track now; the rules decide.
    assert out[1]["keep"] is False and "manual" not in out[1]


@pytest.fixture
def scan_route(test_db, monkeypatch):
    import backend.config
    import backend.routes.scan as scan_route
    import backend.scanner as scanner
    monkeypatch.setattr(scan_route, "DB_PATH", test_db)
    monkeypatch.setattr(scanner, "settings", backend.config.settings)
    scanner.invalidate_sub_settings_cache()
    yield scan_route
    scanner.invalidate_sub_settings_cache()


async def _insert(db_path, path, audio, subs=None, native="jpn"):
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute(
            "INSERT INTO scan_results (file_path, file_size, scan_timestamp, native_language, duration, "
            "audio_tracks_json, subtitle_tracks_json, has_removable_tracks_flag) VALUES (?, 1, 'x', ?, 60, ?, ?, 0)",
            (path, native, json.dumps(audio), json.dumps(subs) if subs is not None else None))
        await db.commit()
        return cur.lastrowid


async def _row(db_path, rid):
    async with aiosqlite.connect(db_path) as db:
        async with db.execute(
            "SELECT audio_tracks_json, subtitle_tracks_json, has_removable_tracks_flag FROM scan_results WHERE id = ?",
            (rid,)) as cur:
            a, s, rem = await cur.fetchone()
    return json.loads(a), (json.loads(s) if s else None), rem


@pytest.mark.asyncio
async def test_editing_tracks_marks_them_and_keeps_the_flag_right(scan_route, test_db):
    rid = await _insert(test_db, "/m/a.mkv", [tr(1, "jpn", True), tr(2, "eng", True)])
    await scan_route.update_audio_tracks(rid, scan_route.UpdateTracksRequest(
        audio_tracks_json=json.dumps([tr(1, "jpn", True), tr(2, "eng", False)])))
    audio, _, rem = await _row(test_db, rid)
    assert [t.get("manual", False) for t in audio] == [False, True]
    assert rem == 1  # "has removable audio" follows the edit (it went stale)


async def _set(db_path, **values):
    async with aiosqlite.connect(db_path) as db:
        for k, v in values.items():
            await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (k, v))
        await db.commit()


@pytest.mark.asyncio
async def test_new_languages_reach_scanned_files_but_not_choices_made_by_hand(scan_route, test_db):
    """Old rules: keep English. New: English and French."""
    old = TrackRules(audio_keep=frozenset({"eng"}), subs_enabled=False)
    # As the old rules classified it (Japanese is the original language).
    plain = await _insert(test_db, "/m/plain.mkv", [tr(1, "jpn", True), tr(2, "eng", True), tr(3, "fra", False)])
    # French removed by hand (marked): stays removed.
    marked = await _insert(test_db, "/m/marked.mkv",
                           [tr(1, "jpn", True), tr(2, "eng", True), tr(3, "fra", False, manual=True)])
    # English removed by hand before marks existed: the old rules would keep
    # it, so it was a choice — it stays.
    legacy = await _insert(test_db, "/m/legacy.mkv", [tr(1, "jpn", True), tr(2, "eng", False), tr(3, "fra", False)])
    await _set(test_db, always_keep_languages='["eng", "fra"]', sub_cleanup_enabled="false")
    from backend.scanner import current_track_rules
    assert current_track_rules().audio_keep == {"eng", "fra"}

    assert await scan_route.reapply_track_rules(old) == 2
    keeps = lambda rid: _row(test_db, rid)
    assert [t["keep"] for t in (await keeps(plain))[0]] == [True, True, True]
    assert (await keeps(plain))[2] == 0  # nothing left to remove
    assert [t["keep"] for t in (await keeps(marked))[0]] == [True, True, False]
    assert [t["keep"] for t in (await keeps(legacy))[0]] == [True, False, True]  # French follows; English stays
    # Same rules again: nothing to do.
    assert await scan_route.reapply_track_rules(current_track_rules()) == 0


@pytest.mark.asyncio
async def test_a_file_is_never_left_without_audio(scan_route, test_db):
    old = TrackRules(audio_keep=frozenset({"eng", "fra"}), keep_native_audio=False, subs_enabled=False)
    # English removed by hand; French kept by the old rules. Dropping French
    # from the list would leave no audio — the file is left as it is.
    rid = await _insert(test_db, "/m/x.mkv", [tr(1, "eng", False, manual=True), tr(2, "fra", True)], native="eng")
    await _set(test_db, always_keep_languages='["eng"]', keep_native_language="false", sub_cleanup_enabled="false")
    assert await scan_route.reapply_track_rules(old) == 0
    assert [t["keep"] for t in (await _row(test_db, rid))[0]] == [False, True]


@pytest.mark.asyncio
async def test_a_rescan_keeps_choices_made_by_hand(scan_route, test_db):
    from backend.models import AudioTrack, ScannedFile
    rid = await _insert(test_db, "/m/A/a.mkv",
                        [tr(1, "jpn", True), tr(2, "eng", False, manual=True), tr(3, "fra", False)])

    def scanned():
        return ScannedFile(
            file_path="/m/A/a.mkv", file_name="a.mkv", folder_name="A", file_size=1, file_size_gb=0.0,
            video_codec="h264", needs_conversion=True, native_language="jpn", has_removable_tracks=False,
            estimated_savings_bytes=0, estimated_savings_gb=0.0,
            audio_tracks=[AudioTrack(stream_index=1, language="jpn", codec="aac", channels=2, keep=True),
                          AudioTrack(stream_index=2, language="eng", codec="aac", channels=2, keep=True),
                          AudioTrack(stream_index=3, language="fra", codec="aac", channels=2, keep=True)])

    scan_route._write_batch_sync(test_db, [scanned()], "2026-10-10T00:00:00")
    audio, _, rem = await _row(test_db, rid)
    # The rescan's own classification wins except on the hand-set track.
    assert [(t["keep"], t.get("manual", False)) for t in audio] == [(True, False), (False, True), (True, False)]
    assert rem == 1


def test_metadata_reclassification_leaves_choices_made_by_hand(scan_route):
    """The original language corrected from Japanese to English: the rules
    move, the French kept by hand stays."""
    from backend.routes.scan import _reclassify_keep_flags
    audio = [tr(1, "jpn", True), tr(2, "eng", True), tr(3, "fra", True, manual=True)]
    out = _reclassify_keep_flags(json.dumps(audio), None, "eng", 60)
    assert [t["keep"] for t in json.loads(out[0])] == [False, True, True]


def test_choices_never_remove_every_audio_track(scan_route):
    from backend.routes.scan import _reclassify_keep_flags
    # English removed by hand; corrected to English-original, the rules drop
    # Japanese — the first track the rules keep (English) stays.
    audio = [tr(1, "jpn", True), tr(2, "eng", False, manual=True)]
    out = _reclassify_keep_flags(json.dumps(audio), None, "eng", 60)
    assert [t["keep"] for t in json.loads(out[0])] == [False, True]
    fresh = [tr(1, "jpn", False), tr(2, "eng", True)]
    assert [t["keep"] for t in keep_manual_choices(fresh, audio, audio=True)] == [False, True]
    # Subtitles may all go.
    subs = [tr(4, "eng", True)]
    assert [t["keep"] for t in keep_manual_choices(subs, [tr(4, "eng", False, manual=True)])] == [False]
