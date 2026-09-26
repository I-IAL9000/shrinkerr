"""Translated outbound notifications (backend/i18n.py + notifications.py)."""

import json
import re

import pytest

import backend.i18n as i18n
import backend.notifications as notifications
from backend.i18n import t

LOCALES = i18n.LOCALES_DIR


@pytest.fixture(autouse=True)
def _reset_i18n():
    i18n.clear_cache()
    i18n.set_current_language("en")
    yield
    i18n.clear_cache()
    i18n.set_current_language("en")


@pytest.fixture
def captured(monkeypatch):
    """Capture what every provider would send, without network I/O."""
    sent = {}

    async def discord(url, title, message, fields, color=0):
        sent["discord"] = (title, message, dict(fields))
        return True

    async def telegram(token, chat_id, title, message, fields):
        sent["telegram"] = (title, message, dict(fields))
        return True

    async def email(config, subject, body):
        sent["email"] = (subject, body)
        return True

    async def webhook(url, event, title, message, fields):
        sent["webhook"] = (event, title, message, dict(fields))
        return True

    monkeypatch.setattr(notifications, "_send_discord", discord)
    monkeypatch.setattr(notifications, "_send_telegram", telegram)
    monkeypatch.setattr(notifications, "_send_email", email)
    monkeypatch.setattr(notifications, "_send_webhook", webhook)
    return sent


async def _set(db_path, **values):
    import aiosqlite
    async with aiosqlite.connect(db_path) as db:
        for k, v in values.items():
            await db.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (k, v),
            )
        await db.commit()


_PROVIDERS = dict(
    discord_webhook_url="https://discord.example/hook",
    telegram_bot_token="tok", telegram_chat_id="1",
    smtp_host="smtp.example", email_to="a@example.com",
    webhook_url="https://hook.example/x",
    notify_job_failed="true", notify_queue_complete="true", notify_disk_low="true",
)


# --- English default reproduces pre-i18n wording -------------------------

@pytest.mark.asyncio
async def test_english_default_matches_legacy_wording(test_db, captured):
    await _set(test_db, **_PROVIDERS)  # no notification_language row → en

    await notifications.notify_job_failed("Movie.mkv", "boom")
    assert captured["discord"] == ("Job Failed", "Movie.mkv failed during conversion", {"Error": "boom"})
    assert captured["email"] == ("Shrinkerr: Job Failed", "Movie.mkv failed during conversion\n\nError: boom")
    assert captured["webhook"][0] == "job_failed"

    await notifications.notify_queue_complete(3, "1.2 GB")
    assert captured["telegram"] == ("Queue Complete", "All jobs finished! 3 completed.", {"Total saved": "1.2 GB"})
    await notifications.notify_queue_complete(1, "1 MB")
    assert captured["telegram"][1] == "All jobs finished! 1 completed."

    await notifications.notify_disk_low("/media", 12.345, 50, 3.21)
    assert captured["discord"] == (
        "Low Disk Space",
        "Free space is 12.3 GB (threshold: 50 GB)",
        {"Path": "/media", "Free": "12.3 GB", "Total": "3.2 TB"},
    )


@pytest.mark.asyncio
async def test_english_test_notification_matches_legacy_wording(test_db, captured):
    await _set(test_db, **_PROVIDERS)
    await notifications.test_notifications()
    fields = {"Status": "Test successful"}
    assert captured["discord"] == ("Shrinkerr Test", "Test notification from Shrinkerr", fields)
    assert captured["telegram"] == ("Shrinkerr Test", "Test notification from Shrinkerr", fields)
    assert captured["email"] == ("Shrinkerr: Test Notification", "Test notification from Shrinkerr")
    assert captured["webhook"] == ("test", "Shrinkerr Test", "Test notification", fields)


# --- Spanish when the setting is "es" --------------------------------------

@pytest.mark.asyncio
async def test_spanish_when_setting_is_es(test_db, captured):
    await _set(test_db, notification_language="es", **_PROVIDERS)

    await notifications.notify_job_failed("Película.mkv", "boom")
    assert captured["discord"] == ("Trabajo fallido", "Película.mkv falló durante la conversión", {"Error": "boom"})
    assert captured["email"][0] == "Shrinkerr: Trabajo fallido"

    await notifications.notify_queue_complete(3, "1.2 GB")
    assert captured["discord"] == (
        "Cola completada", "¡Todos los trabajos han terminado! 3 completados.", {"Total ahorrado": "1.2 GB"},
    )
    await notifications.notify_queue_complete(1, "1 MB")
    assert captured["discord"][1] == "¡Todos los trabajos han terminado! 1 completado."

    await notifications.test_notifications()
    assert captured["discord"] == (
        "Prueba de Shrinkerr", "Notificación de prueba de Shrinkerr", {"Estado": "Prueba exitosa"},
    )
    # Reading settings also primes t(lang=None).
    assert i18n.get_current_language() == "es"
    assert t("notifications:diskLow.title") == "Poco espacio en disco"


# --- Translator behaviour ----------------------------------------------------

def test_missing_key_falls_back_to_english(tmp_path, monkeypatch):
    (tmp_path / "en").mkdir()
    (tmp_path / "es").mkdir()
    (tmp_path / "en" / "ns.json").write_text(json.dumps({"a": {"b": "Hello {{name}}"}, "only_en": "EN"}))
    (tmp_path / "es" / "ns.json").write_text(json.dumps({"a": {"b": "Hola {{name}}"}}))
    monkeypatch.setattr(i18n, "LOCALES_DIR", tmp_path)

    assert t("ns:a.b", "es", name="Ana") == "Hola Ana"
    assert t("ns:only_en", "es") == "EN"             # key missing in es
    assert t("ns:a.b", "fr", name="Ana") == "Hello Ana"  # language missing
    assert t("ns:nope", "es") == "ns:nope"            # missing everywhere
    assert t("no-namespace", "es") == "no-namespace"
    assert t("ns:a.b", "es") == "Hola {{name}}"       # unknown param left as-is
    assert t("ns:a.b", "../../etc", name="x") == "Hello x"  # no traversal


def test_plural_selection_including_spanish_many(tmp_path, monkeypatch):
    (tmp_path / "en").mkdir()
    (tmp_path / "es").mkdir()
    (tmp_path / "en" / "ns.json").write_text(json.dumps({
        "files_one": "{{count}} file", "files_other": "{{count}} files",
        "bare": "{{count}} bare",
    }))
    (tmp_path / "es" / "ns.json").write_text(json.dumps({
        "files_one": "{{count}} archivo", "files_many": "{{count}} de archivos",
        "files_other": "{{count}} archivos",
    }))
    monkeypatch.setattr(i18n, "LOCALES_DIR", tmp_path)

    assert t("ns:files", "en", count=1) == "1 file"
    assert t("ns:files", "en", count=0) == "0 files"
    assert t("ns:files", "en", count=1_000_000) == "1000000 files"  # en has no _many
    assert t("ns:files", "es", count=1) == "1 archivo"
    assert t("ns:files", "es", count=2) == "2 archivos"
    assert t("ns:files", "es", count=0) == "0 archivos"
    assert t("ns:files", "es", count=1_000_000) == "1000000 de archivos"
    assert t("ns:files", "es", count=2_000_000) == "2000000 de archivos"
    assert t("ns:files", "es", count=1_000_001) == "1000001 archivos"
    assert t("ns:bare", "en", count=5) == "5 bare"   # falls back to bare key
    assert t("ns:bare", "es", count=5) == "5 bare"   # …and to English


def _flatten(d, prefix=""):
    out = {}
    for k, v in d.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(_flatten(v, key + "."))
        else:
            out[key] = v
    return out


def test_en_es_catalogs_have_same_keys_and_placeholders():
    en = _flatten(json.loads((LOCALES / "en" / "notifications.json").read_text(encoding="utf-8")))
    es = _flatten(json.loads((LOCALES / "es" / "notifications.json").read_text(encoding="utf-8")))
    assert set(en) == set(es)
    ph = re.compile(r"\{\{\s*(\w+)\s*\}\}")
    for key in en:
        assert set(ph.findall(en[key])) == set(ph.findall(es[key])), key


# --- Setting validation --------------------------------------------------------

def _settings_route():
    pytest.importorskip("apscheduler")  # first save imports backend.main
    try:
        import backend.routes.settings as settings_route
    except (ImportError, RuntimeError) as exc:  # e.g. python-multipart absent locally
        pytest.skip(f"settings routes not importable here: {exc}")
    return settings_route


@pytest.mark.asyncio
async def test_notification_language_setting_validation(test_db, monkeypatch):
    settings_route = _settings_route()
    from fastapi import HTTPException
    from backend.models import SettingsUpdate
    monkeypatch.setattr(settings_route, "DB_PATH", test_db)

    assert settings_route._ENCODING_DEFAULTS["notification_language"] == "en"
    assert (await settings_route.get_encoding_settings())["notification_language"] == "en"

    for bad in ("xx", "../en", ""):
        with pytest.raises(HTTPException) as exc:
            await settings_route.update_encoding_settings(SettingsUpdate(notification_language=bad))
        assert exc.value.status_code == 400

    res = await settings_route.update_encoding_settings(SettingsUpdate(notification_language="es"))
    assert "notification_language" in res["keys"]
    assert (await settings_route.get_encoding_settings())["notification_language"] == "es"
    assert i18n.get_current_language() == "es"
