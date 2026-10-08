"""v0.10.0 (FE#3): every setting the UI shows can be saved and read back.

Content-type detection and resolution-aware CQ were in neither the save
model nor GET /encoding, so edits were silently dropped (and the toggle
showed off while jobs used the "on" default); the Jellyfin/Emby
"scan after conversion" toggles saved but weren't returned.
"""
import pytest

from backend.models import SettingsUpdate


def _route(test_db, monkeypatch):
    pytest.importorskip("apscheduler")
    import backend.routes.settings as settings_route
    monkeypatch.setattr(settings_route, "DB_PATH", test_db)
    return settings_route


# Stored but not user settings: secrets/auth state written elsewhere.
NOT_SETTINGS = {"auth_password_hash", "session_secret"}


def test_every_default_is_saveable(monkeypatch, test_db):
    route = _route(test_db, monkeypatch)
    missing = set(route._ENCODING_DEFAULTS) - set(SettingsUpdate.model_fields) - NOT_SETTINGS
    assert not missing, f"settings with a default but no way to save them: {sorted(missing)}"


@pytest.mark.asyncio
async def test_every_default_is_returned(monkeypatch, test_db):
    route = _route(test_db, monkeypatch)
    got = await route.get_encoding_settings()
    missing = set(route._ENCODING_DEFAULTS) - set(got) - NOT_SETTINGS
    assert not missing, f"settings the UI can't read back: {sorted(missing)}"


@pytest.mark.asyncio
async def test_quality_tuning_settings_round_trip(monkeypatch, test_db):
    route = _route(test_db, monkeypatch)
    await route.update_encoding_settings(SettingsUpdate(
        content_type_detection=False, resolution_aware_cq=True,
        resolution_cq_4k=26, resolution_cq_1080p=21, resolution_cq_720p=19, resolution_cq_sd=17,
        jellyfin_scan_after_conversion=False, emby_scan_after_conversion=False,
    ))
    got = await route.get_encoding_settings()
    assert got["content_type_detection"] is False
    assert got["resolution_aware_cq"] is True
    assert (got["resolution_cq_4k"], got["resolution_cq_1080p"], got["resolution_cq_720p"],
            got["resolution_cq_sd"]) == (26, 21, 19, 17)
    assert got["jellyfin_scan_after_conversion"] is False
    assert got["emby_scan_after_conversion"] is False


@pytest.mark.asyncio
async def test_auth_cannot_be_enabled_without_credentials(monkeypatch, test_db):
    """Ticking "Enable authentication" and pressing any other section's Save
    (they send the whole page) turned auth on with no password — locked out
    unless you knew the API key."""
    route = _route(test_db, monkeypatch)
    with pytest.raises(Exception) as exc:
        await route.update_encoding_settings(SettingsUpdate(auth_enabled=True, nvenc_cq=22))
    assert getattr(exc.value, "code", None) == "settings.authNeedsPassword"
    assert (await route.get_encoding_settings())["auth_enabled"] is False

    await route.update_encoding_settings(SettingsUpdate(
        auth_enabled=True, auth_username="admin", auth_password="correct horse"))
    assert (await route.get_encoding_settings())["auth_enabled"] is True
    # Saving the page again later (no password in the request) is fine.
    await route.update_encoding_settings(SettingsUpdate(auth_enabled=True, nvenc_cq=22))
