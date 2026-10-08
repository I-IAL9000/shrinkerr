"""v0.10.0 (SC-08): language codes are compared in one spelling.

Only English/Spanish/Portuguese had their 2-letter forms in the equivalence
table, ISO "is" mapped to the terminology form "isl" while tracks say "ice",
Georgian "ka" mapped to "kat" while tracks say "geo", and region tags
("de-DE") never matched. So Bazarr's `.is.srt` subtitles on an Icelandic
film, or a German film's "de-DE" track, were marked for removal.
"""
import json

import aiosqlite
import pytest

from backend.scanner import classify_audio_tracks, languages_match, normalize_lang


@pytest.mark.parametrize("code,expected", [
    ("is", "ice"), ("isl", "ice"), ("ICE", "ice"),
    ("de", "ger"), ("deu", "ger"), ("de-DE", "ger"), ("de_DE", "ger"),
    ("fr-CA", "fre"), ("zh-Hans", "chi"), ("pt-BR", "por"), ("en-US", "eng"),
    ("ka", "geo"), ("kat", "geo"), ("kk", "kaz"), ("sh", "hbs"),
    ("xx", "und"), ("", "und"), (None, "und"), ("jpn", "jpn"),
])
def test_normalize_lang(code, expected):
    assert normalize_lang(code) == expected


@pytest.mark.parametrize("a,b", [
    ("is", "ice"), ("isl", "ice"), ("de-DE", "ger"), ("kat", "geo"), ("ka", "geo"),
    ("kk", "kaz"), ("fr", "fre"), ("ja", "jpn"), ("yue", "chi"), ("nob", "nor"),
])
def test_codes_for_the_same_language_match(a, b):
    assert languages_match(a, b) and languages_match(b, a)


def test_different_languages_still_differ():
    assert not languages_match("ice", "ger")
    assert not languages_match("is", "it")


async def _enable_cleanup(db_path, monkeypatch):
    import backend.config
    import backend.scanner as scanner
    from backend.scanner import invalidate_sub_settings_cache
    # scanner.py holds its own reference to the settings object; point it at
    # this test's database.
    monkeypatch.setattr(scanner, "settings", backend.config.settings)
    async with aiosqlite.connect(db_path) as db:
        for key in ("audio_cleanup_enabled", "sub_cleanup_enabled"):
            await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, 'true')", (key,))
        await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('sub_keep_languages', '[\"ice\"]')")
        await db.commit()
    invalidate_sub_settings_cache()


@pytest.mark.asyncio
async def test_icelandic_track_tagged_is_is_kept(test_db, monkeypatch):
    await _enable_cleanup(test_db, monkeypatch)
    tracks = classify_audio_tracks([
        {"stream_index": 1, "language": "eng", "codec": "ac3", "channels": 2},
        {"stream_index": 2, "language": "is", "codec": "ac3", "channels": 6},
        {"stream_index": 3, "language": "rus", "codec": "ac3", "channels": 2},
    ], "ice")
    assert {t.language: t.keep for t in tracks} == {"eng": False, "is": True, "rus": False}


@pytest.mark.asyncio
async def test_backfill_only_ever_turns_removals_into_keeps(test_db, monkeypatch):
    """Rows classified before v0.10.0 keep their wrong flags until rescanned;
    a one-time pass re-checks them. It may only turn a removal into a keep —
    never the reverse — so it can't add removals or undo a user's choice."""
    import backend.routes.scan as scan_route
    from backend.routes.scan import backfill_normalized_language_keeps
    monkeypatch.setattr(scan_route, "DB_PATH", test_db)
    await _enable_cleanup(test_db, monkeypatch)
    audio = [
        {"stream_index": 1, "language": "ice", "codec": "ac3", "channels": 6, "keep": True},
        {"stream_index": 2, "language": "eng", "codec": "ac3", "channels": 2, "keep": True},   # user kept it
        {"stream_index": 3, "language": "rus", "codec": "ac3", "channels": 2, "keep": False},
    ]
    subs = [
        {"stream_index": 4, "language": "is", "codec": "subrip", "keep": False},             # the bug
        {"stream_index": 5, "language": "ger", "codec": "subrip", "keep": False},
    ]
    async with aiosqlite.connect(test_db) as db:
        await db.execute(
            "INSERT INTO scan_results (file_path, file_size, audio_tracks_json, subtitle_tracks_json, "
            "native_language, language_source, scan_timestamp, has_removable_tracks_flag, has_removable_subs_flag) "
            "VALUES ('/media/Film (2020)/film.mkv', 1, ?, ?, 'ice', 'api', '2026-10-08T00:00:00', 1, 1)",
            (json.dumps(audio), json.dumps(subs)))
        await db.commit()

    assert await backfill_normalized_language_keeps() == 1
    assert await backfill_normalized_language_keeps() == 0   # runs once

    async with aiosqlite.connect(test_db) as db:
        async with db.execute("SELECT audio_tracks_json, subtitle_tracks_json, has_removable_tracks_flag, "
                              "has_removable_subs_flag FROM scan_results") as cur:
            a_json, s_json, rem_a, rem_s = await cur.fetchone()
    assert {t["language"]: t["keep"] for t in json.loads(a_json)} == {"ice": True, "eng": True, "rus": False}
    assert {t["language"]: t["keep"] for t in json.loads(s_json)} == {"is": True, "ger": False}
    assert (rem_a, rem_s) == (1, 1)
