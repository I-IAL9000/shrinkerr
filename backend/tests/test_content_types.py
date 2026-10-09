"""Per-content-type settings (v0.10.0): the content-type CQ table is editable in
Settings (recommended values, reset, a switch per type), and "Content type" is
an encoding-rule condition, so a type can get its own encoder, preset,
resolution or audio."""
import pytest

from backend.content_detect import CQ_TABLE, content_cq_table, smart_cq, smart_cq_settings

ANIME = "/m/anime/[SubsPlease] Frieren - 01 (1080p).mkv"
REMUX = "/m/movies/Film (2001)/Film.2001.1080p.BluRay.REMUX.AVC.mkv"
PLAIN = "/m/movies/Plain (2010)/Plain.mkv"


def test_the_recommended_values_give_sensitive_content_more_quality():
    """Reviewed defaults: anime, animation, grain and remux get a CQ at or
    below everyday content's at every resolution (they used to get more)."""
    for ctype in ("anime", "animation", "grain", "remux"):
        for tier in ("4k", "1080p", "720p", "sd"):
            assert CQ_TABLE[ctype][tier] <= CQ_TABLE["default"][tier], (ctype, tier)


def test_a_partial_or_broken_table_falls_back_to_the_recommended_values():
    table = content_cq_table('{"anime": {"1080p": 25, "4k": "x", "enabled": false}, "remux": {"sd": 99}}')
    assert table["anime"] == {**CQ_TABLE["anime"], "1080p": 25, "enabled": False}
    assert table["remux"]["sd"] == 51                      # clamped
    assert table["grain"] == {**CQ_TABLE["grain"], "enabled": True}
    assert content_cq_table("not json") == content_cq_table(None) == content_cq_table({})
    assert set(table) == {"anime", "animation", "grain", "remux"}


def _settings(table, **values):
    import json
    return smart_cq_settings({"content_type_detection": "true", "content_type_cq": json.dumps(table), **values})


def test_jobs_use_the_users_table():
    s = _settings({"anime": {"1080p": 25}})
    assert smart_cq(ANIME, 1920, 1080, s) == (25, "anime")
    assert smart_cq(ANIME, 3840, 2160, s) == (CQ_TABLE["anime"]["4k"], "anime")  # untouched cell


def test_a_switched_off_type_is_treated_like_everything_else():
    s = _settings({"remux": {"enabled": False}}, resolution_aware_cq="true", resolution_cq_1080p="21")
    assert smart_cq(REMUX, 1920, 1080, s) == (21, None)     # resolution-aware, not the remux row
    assert smart_cq(REMUX, 1920, 1080, _settings({"remux": {"enabled": False}})) == (None, None)


@pytest.mark.asyncio
async def test_the_table_saves_and_loads(test_db, monkeypatch):
    pytest.importorskip("apscheduler")
    import backend.routes.settings as settings_route
    from backend.models import SettingsUpdate
    monkeypatch.setattr(settings_route, "DB_PATH", test_db)
    before = await settings_route.get_encoding_settings()
    assert before["content_type_cq"] == before["content_type_cq_recommended"]
    await settings_route.update_encoding_settings(SettingsUpdate(content_type_cq={
        "anime": {"enabled": True, "4k": 24, "1080p": 21, "720p": 19, "sd": 17},
        "grain": {"enabled": False},
    }))
    after = await settings_route.get_encoding_settings()
    assert after["content_type_cq"]["anime"] == {"enabled": True, "4k": 24, "1080p": 21, "720p": 19, "sd": 17}
    assert after["content_type_cq"]["grain"] == {**CQ_TABLE["grain"], "enabled": False}
    assert after["content_type_cq_recommended"]["anime"] == {**CQ_TABLE["anime"], "enabled": True}


@pytest.mark.parametrize("path, op, value, expected", [
    (ANIME, "is", "anime", True),
    (ANIME, "is_not", "anime", False),
    (REMUX, "is", "remux", True),
    (PLAIN, "is", "other", True),
    (PLAIN, "is", "anime", False),
])
def test_the_content_type_rule_condition(path, op, value, expected):
    from backend.rule_resolver import _check_condition
    assert _check_condition({"type": "content_type", "operator": op, "value": value}, path, {}, []) is expected


def test_rules_accept_the_condition():
    from backend.routes.rules import _check_condition_types
    _check_condition_types([{"type": "content_type", "operator": "is", "value": "anime"}])
