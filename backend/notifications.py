"""Notification system — Discord, Telegram, email, and generic webhook."""

import asyncio
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import httpx

from backend.database import connect_db
from backend.i18n import set_current_language, t


async def _get_notification_settings() -> dict:
    """Read all notification-related settings from DB."""
    db = await connect_db()
    try:
        settings = {}
        async with db.execute(
            "SELECT key, value FROM settings WHERE key LIKE 'notify_%' OR key LIKE 'discord_%' "
            "OR key LIKE 'telegram_%' OR key LIKE 'smtp_%' OR key LIKE 'email_%' "
            "OR key LIKE 'webhook_%' OR key = 'disk_space_threshold_gb' "
            "OR key = 'notification_language'"
        ) as cur:
            for row in await cur.fetchall():
                settings[row["key"]] = row["value"]
        # Keep i18n's cached language in sync so t(lang=None) matches.
        set_current_language(settings.get("notification_language"))
        return settings
    finally:
        await db.close()


def _is_enabled(settings: dict, event: str) -> bool:
    return settings.get(f"notify_{event}", "false").lower() == "true"


async def _send_discord(url: str, title: str, message: str, fields: dict, color: int = 0x9135FF) -> bool:
    """Send a Discord webhook embed."""
    embed = {
        "title": title,
        "description": message,
        "color": color,
        "fields": [{"name": k, "value": str(v), "inline": True} for k, v in fields.items()] if fields else [],
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json={"embeds": [embed]})
            resp.raise_for_status()
        return True
    except Exception as exc:
        print(f"[NOTIFY] Discord failed: {exc}", flush=True)
        return False


async def _send_telegram(token: str, chat_id: str, title: str, message: str, fields: dict) -> bool:
    """Send a Telegram message via Bot API."""
    lines = [f"*{title}*", message]
    if fields:
        lines.extend(f"  {k}: {v}" for k, v in fields.items())
    text = "\n".join(lines)
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
            )
            resp.raise_for_status()
        return True
    except Exception as exc:
        print(f"[NOTIFY] Telegram failed: {exc}", flush=True)
        return False


async def _send_email(config: dict, subject: str, body: str) -> bool:
    """Send an email via SMTP."""
    host = config.get("smtp_host", "")
    port = int(config.get("smtp_port", "587"))
    user = config.get("smtp_user", "")
    password = config.get("smtp_pass", "")
    from_addr = config.get("smtp_from", user)
    to_addr = config.get("email_to", "")

    if not host or not to_addr:
        return False

    msg = MIMEMultipart()
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg.attach(MIMEText(body, "plain"))

    def _do_send():
        try:
            if port == 465:
                server = smtplib.SMTP_SSL(host, port, timeout=10)
            else:
                server = smtplib.SMTP(host, port, timeout=10)
                server.starttls()
            if user and password:
                server.login(user, password)
            server.sendmail(from_addr, [to_addr], msg.as_string())
            server.quit()
            return True
        except Exception as exc:
            print(f"[NOTIFY] Email failed: {exc}", flush=True)
            return False

    return await asyncio.to_thread(_do_send)


async def _send_webhook(url: str, event: str, title: str, message: str, fields: dict) -> bool:
    """Send a generic webhook POST."""
    payload = {
        "event": event,
        "title": title,
        "message": message,
        "fields": fields,
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
        return True
    except Exception as exc:
        print(f"[NOTIFY] Webhook failed: {exc}", flush=True)
        return False


async def send_notification(event: str, title: str, message: str, fields: dict | None = None) -> dict:
    """Send notifications for an event to all configured providers.

    `title`/`message`/`fields` are sent as given (callers pass already
    translated text — prefer the notify_* helpers below).
    Returns dict of provider -> success bool.
    """
    settings = await _get_notification_settings()
    return await _dispatch(settings, event, title, message, fields)


async def _dispatch(settings: dict, event: str, title: str, message: str, fields: dict | None = None) -> dict:
    if not _is_enabled(settings, event):
        return {}

    fields = fields or {}
    results = {}

    # Discord
    discord_url = settings.get("discord_webhook_url", "")
    if discord_url:
        color = 0xE94560 if "failed" in event or "low" in event else 0x18FFA5
        results["discord"] = await _send_discord(discord_url, title, message, fields, color)

    # Telegram
    tg_token = settings.get("telegram_bot_token", "")
    tg_chat = settings.get("telegram_chat_id", "")
    if tg_token and tg_chat:
        results["telegram"] = await _send_telegram(tg_token, tg_chat, title, message, fields)

    # Email
    smtp_host = settings.get("smtp_host", "")
    email_to = settings.get("email_to", "")
    if smtp_host and email_to:
        body = f"{message}\n\n" + "\n".join(f"{k}: {v}" for k, v in fields.items()) if fields else message
        subject = t("notifications:emailSubject", _lang(settings), title=title)
        results["email"] = await _send_email(settings, subject, body)

    # Generic webhook
    webhook_url = settings.get("webhook_url", "")
    if webhook_url:
        results["webhook"] = await _send_webhook(webhook_url, event, title, message, fields)

    if results:
        ok = [k for k, v in results.items() if v]
        fail = [k for k, v in results.items() if not v]
        print(f"[NOTIFY] {event}: sent={ok}, failed={fail}", flush=True)

    return results


def _lang(settings: dict) -> str:
    return settings.get("notification_language") or "en"


async def notify_job_failed(file_name: str, error: str) -> dict:
    """`job_failed` notification in the configured notification_language."""
    settings = await _get_notification_settings()
    lang = _lang(settings)
    return await _dispatch(
        settings, "job_failed",
        t("notifications:jobFailed.title", lang),
        t("notifications:jobFailed.message", lang, fileName=file_name),
        {t("notifications:jobFailed.fieldError", lang): error},
    )


async def notify_queue_complete(completed: int, total_saved: str) -> dict:
    """`queue_complete` notification. `total_saved` is a preformatted size (e.g. "1.2 TB")."""
    settings = await _get_notification_settings()
    lang = _lang(settings)
    return await _dispatch(
        settings, "queue_complete",
        t("notifications:queueComplete.title", lang),
        t("notifications:queueComplete.message", lang, count=completed),
        {t("notifications:queueComplete.fieldTotalSaved", lang): total_saved},
    )


async def notify_disk_low(path: str, free_gb: float, threshold_gb, total_tb: float) -> dict:
    """`disk_low` notification in the configured notification_language."""
    settings = await _get_notification_settings()
    lang = _lang(settings)
    return await _dispatch(
        settings, "disk_low",
        t("notifications:diskLow.title", lang),
        t("notifications:diskLow.message", lang, free=f"{free_gb:.1f}", threshold=threshold_gb),
        {
            t("notifications:diskLow.fieldPath", lang): path,
            t("notifications:diskLow.fieldFree", lang): f"{free_gb:.1f} GB",
            t("notifications:diskLow.fieldTotal", lang): f"{total_tb:.1f} TB",
        },
    )


async def test_notifications() -> dict:
    """Send a test notification to all configured providers (ignoring event toggles)."""
    settings = await _get_notification_settings()
    lang = _lang(settings)
    results = {}
    title = t("notifications:test.title", lang)
    message = t("notifications:test.message", lang)
    fields = {t("notifications:test.fieldStatus", lang): t("notifications:test.statusSuccess", lang)}

    discord_url = settings.get("discord_webhook_url", "")
    if discord_url:
        results["discord"] = await _send_discord(discord_url, title, message, fields)

    tg_token = settings.get("telegram_bot_token", "")
    tg_chat = settings.get("telegram_chat_id", "")
    if tg_token and tg_chat:
        results["telegram"] = await _send_telegram(tg_token, tg_chat, title, message, fields)

    smtp_host = settings.get("smtp_host", "")
    email_to = settings.get("email_to", "")
    if smtp_host and email_to:
        results["email"] = await _send_email(settings, t("notifications:test.emailSubject", lang), message)

    webhook_url = settings.get("webhook_url", "")
    if webhook_url:
        results["webhook"] = await _send_webhook(
            webhook_url, "test", title, t("notifications:test.webhookMessage", lang), fields,
        )

    return results
