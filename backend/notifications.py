"""Notification system — Discord, Telegram, email, generic webhook, and
(v0.10.0) ntfy, Gotify and Apprise (100+ services by URL)."""

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
            "OR key LIKE 'ntfy_%' OR key LIKE 'gotify_%' OR key LIKE 'apprise_%' "
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


# Events that need attention: sent with a higher priority where it exists.
_URGENT = ("failed", "low", "rejected", "offline")


def _plain(message: str, fields: dict) -> str:
    return message + ("\n\n" + "\n".join(f"{k}: {v}" for k, v in fields.items()) if fields else "")


async def _send_ntfy(url: str, token: str, event: str, title: str, message: str, fields: dict) -> bool:
    """ntfy (v0.10.0): `url` is the topic's URL, e.g. https://ntfy.sh/my-topic.
    Published as JSON, so the title can be any language."""
    base, _, topic = url.rstrip("/").rpartition("/")
    payload = {"topic": topic, "title": title, "message": _plain(message, fields),
               "priority": 4 if any(w in event for w in _URGENT) else 3, "tags": ["shrinkerr"]}
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(base, json=payload, headers=headers)
            resp.raise_for_status()
        return True
    except Exception as exc:
        print(f"[NOTIFY] ntfy failed: {exc}", flush=True)
        return False


async def _send_gotify(url: str, token: str, event: str, title: str, message: str, fields: dict) -> bool:
    """Gotify (v0.10.0): the server's URL and an application token."""
    payload = {"title": title, "message": _plain(message, fields),
               "priority": 8 if any(w in event for w in _URGENT) else 5}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(f"{url.rstrip('/')}/message", json=payload, headers={"X-Gotify-Key": token})
            resp.raise_for_status()
        return True
    except Exception as exc:
        print(f"[NOTIFY] Gotify failed: {exc}", flush=True)
        return False


async def _send_apprise(urls: str, title: str, message: str, fields: dict) -> bool:
    """Apprise (v0.10.0): one service URL per line (pover://, tgram://, slack://,
    matrix://, ...; see the Apprise wiki)."""
    targets = [u.strip() for u in urls.replace(",", "\n").splitlines() if u.strip()]

    def send() -> bool:
        import apprise
        sender = apprise.Apprise()
        for target in targets:
            sender.add(target)
        return bool(len(sender)) and bool(sender.notify(title=title, body=_plain(message, fields)))
    try:
        return await asyncio.to_thread(send)
    except Exception as exc:
        print(f"[NOTIFY] Apprise failed: {exc}", flush=True)
        return False


async def _send_more(settings: dict, event: str, title: str, message: str, fields: dict) -> dict:
    """The v0.10.0 providers: ntfy, Gotify, Apprise."""
    results = {}
    if (settings.get("ntfy_url") or "").strip():
        results["ntfy"] = await _send_ntfy(settings["ntfy_url"].strip(), settings.get("ntfy_token", ""),
                                           event, title, message, fields)
    if (settings.get("gotify_url") or "").strip() and settings.get("gotify_token"):
        results["gotify"] = await _send_gotify(settings["gotify_url"].strip(), settings["gotify_token"],
                                               event, title, message, fields)
    if (settings.get("apprise_urls") or "").strip():
        results["apprise"] = await _send_apprise(settings["apprise_urls"], title, message, fields)
    return results


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

    results.update(await _send_more(settings, event, title, message, fields))

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

    results.update(await _send_more(settings, "test", title, message, fields))
    return results


async def notify_vmaf_rejected(file_name: str, score, threshold) -> dict:
    """`vmaf_rejected` (v0.10.0): a conversion was thrown away for its VMAF
    score; the original is untouched."""
    settings = await _get_notification_settings()
    lang = _lang(settings)
    fields = {t("notifications:vmafRejected.fieldScore", lang): f"{float(score):.1f}"} if score is not None else {}
    if threshold:
        fields[t("notifications:vmafRejected.fieldThreshold", lang)] = f"{float(threshold):g}"
    return await _dispatch(
        settings, "vmaf_rejected",
        t("notifications:vmafRejected.title", lang),
        t("notifications:vmafRejected.message", lang, fileName=file_name),
        fields,
    )


async def notify_node_offline(name: str, last_seen: str | None) -> dict:
    """`node_offline` (v0.10.0): a remote worker stopped checking in; its job
    went back to the queue."""
    settings = await _get_notification_settings()
    lang = _lang(settings)
    return await _dispatch(
        settings, "node_offline",
        t("notifications:nodeOffline.title", lang),
        t("notifications:nodeOffline.message", lang, name=name),
        {t("notifications:nodeOffline.fieldLastSeen", lang): last_seen or "—"},
    )


def _fmt_size(n) -> str:
    size = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.0f} B" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


async def notify_weekly_digest() -> dict:
    """`weekly_digest` (v0.10.0): the last seven days — files converted, space
    saved, failures — and what's waiting in the queue."""
    from datetime import datetime, timedelta, timezone
    settings = await _get_notification_settings()
    lang = _lang(settings)
    since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    db = await connect_db()
    try:
        async with db.execute(
            "SELECT COUNT(*), COALESCE(SUM(CASE WHEN space_saved > 0 THEN space_saved ELSE 0 END), 0) "
            "FROM jobs WHERE status = 'completed' AND completed_at >= ?", (since,)) as cur:
            done, saved = await cur.fetchone()
        async with db.execute("SELECT COUNT(*) FROM jobs WHERE status = 'failed' AND completed_at >= ?", (since,)) as cur:
            failed = (await cur.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM jobs WHERE status = 'pending'") as cur:
            pending = (await cur.fetchone())[0]
        async with db.execute(
            "SELECT COALESCE(SUM(CASE WHEN space_saved > 0 THEN space_saved ELSE 0 END), 0) FROM jobs "
            "WHERE status = 'completed'") as cur:
            total = (await cur.fetchone())[0]
    finally:
        await db.close()
    return await _dispatch(
        settings, "weekly_digest",
        t("notifications:weeklyDigest.title", lang),
        t("notifications:weeklyDigest.message", lang, count=done, saved=_fmt_size(saved)),
        {t("notifications:weeklyDigest.fieldFailed", lang): failed,
         t("notifications:weeklyDigest.fieldPending", lang): pending,
         t("notifications:weeklyDigest.fieldTotal", lang): _fmt_size(total)},
    )


async def weekly_digest_due(now=None) -> bool:
    """Whether the weekly digest should go out now — a week after the last
    one (or after it was turned on: the first doesn't go out at once)."""
    from datetime import datetime, timedelta, timezone
    now = now or datetime.now(timezone.utc)
    settings = await _get_notification_settings()
    if not _is_enabled(settings, "weekly_digest"):
        return False
    db = await connect_db()
    try:
        async with db.execute("SELECT value FROM settings WHERE key = 'weekly_digest_last_sent'") as cur:
            row = await cur.fetchone()
        last = None
        try:
            last = datetime.fromisoformat(row[0]) if row and row[0] else None
        except ValueError:
            pass
        due = last is not None and now - last >= timedelta(days=7)
        if last is None or due:
            await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('weekly_digest_last_sent', ?)",
                             (now.isoformat(),))
            await db.commit()
        return due
    finally:
        await db.close()


async def weekly_digest_loop() -> None:
    """Checks hourly; sends the digest when it's due."""
    while True:
        try:
            if await weekly_digest_due():
                await notify_weekly_digest()
        except Exception as exc:
            print(f"[NOTIFY] Weekly digest failed: {exc}", flush=True)
        await asyncio.sleep(3600)
