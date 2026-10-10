"""v0.10.0: the Telegram bot token was the one notification secret the
settings API returned in full (the email password and ntfy / Gotify tokens
show as ****1234)."""
import pytest


@pytest.mark.asyncio
async def test_the_bot_token_is_masked_and_survives_a_save(test_db, monkeypatch):
    import aiosqlite
    import backend.routes.settings as settings_route
    from backend.models import SettingsUpdate
    monkeypatch.setattr(settings_route, "DB_PATH", test_db)
    await settings_route.update_encoding_settings(SettingsUpdate(telegram_bot_token="123456:ABC-secret"))

    shown = (await settings_route.get_encoding_settings())["telegram_bot_token"]
    assert shown == "****cret"

    await settings_route.update_encoding_settings(SettingsUpdate(telegram_bot_token=shown, telegram_chat_id="42"))
    async with aiosqlite.connect(test_db) as db:
        async with db.execute("SELECT value FROM settings WHERE key = 'telegram_bot_token'") as cur:
            assert (await cur.fetchone())[0] == "123456:ABC-secret"
