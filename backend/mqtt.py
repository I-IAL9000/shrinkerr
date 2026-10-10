"""Home Assistant over MQTT (v0.10.0).

With an MQTT broker set up (Settings → Integrations → Home Assistant),
Shrinkerr announces itself through Home Assistant's MQTT discovery: a
"Shrinkerr" device with sensors (queued, running and failed jobs, space
saved, files to convert, the current file and its progress) and a switch
that starts and pauses the queue. Entity names follow the notification
language.

Topics, with the default base topic `shrinkerr`:
  shrinkerr/status       online / offline (retained; the broker sends
                         offline if Shrinkerr goes away: the last will)
  shrinkerr/state        everything, as JSON, every STATE_SECONDS
  shrinkerr/queue/set    ON / OFF: start / pause the queue
  homeassistant/<component>/shrinkerr/<key>/config   discovery (retained)
"""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import PurePosixPath

from backend.database import connect_db

STATE_SECONDS = 15
RETRY_SECONDS = 30
SETTINGS_SECONDS = 5
KEYS = ("mqtt_enabled", "mqtt_host", "mqtt_port", "mqtt_username", "mqtt_password", "mqtt_tls",
        "mqtt_discovery_prefix", "mqtt_base_topic")
TOPIC_RE = re.compile(r"^[^#+/\s][^#+\s]*[^#+/\s]$|^[^#+/\s]$")  # no wildcards, no leading / trailing slash
GB = 1024 ** 3

# (key, unit, device_class, state_class, icon, value template)
SENSORS = (
    ("pending", None, None, "measurement", "mdi:tray-full", "{{ value_json.pending }}"),
    ("running", None, None, "measurement", "mdi:cog-play", "{{ value_json.running }}"),
    ("failed", None, None, "measurement", "mdi:alert-circle", "{{ value_json.failed }}"),
    ("completed", None, None, "total_increasing", "mdi:check-circle", "{{ value_json.completed }}"),
    ("saved", "GB", "data_size", "total_increasing", None, "{{ value_json.saved_gb }}"),
    ("to_convert", None, None, "measurement", "mdi:file-video", "{{ value_json.to_convert }}"),
    ("estimated_savings", "GB", "data_size", "measurement", None, "{{ value_json.estimated_savings_gb }}"),
    ("current_file", None, None, None, "mdi:movie-open", "{{ value_json.current_file }}"),
    ("progress", "%", None, "measurement", "mdi:progress-clock", "{{ value_json.progress }}"),
)


async def load_settings() -> dict:
    db = await connect_db()
    try:
        async with db.execute(f"SELECT key, value FROM settings WHERE key IN ({','.join('?' * len(KEYS))})",
                              KEYS) as cur:
            s = {r["key"]: r["value"] for r in await cur.fetchall()}
    finally:
        await db.close()
    try:
        port = int(s.get("mqtt_port") or 1883)
    except ValueError:
        port = 1883
    return {"enabled": (s.get("mqtt_enabled") or "false").lower() == "true",
            "host": (s.get("mqtt_host") or "").strip(), "port": port,
            "username": s.get("mqtt_username") or None, "password": s.get("mqtt_password") or None,
            "tls": (s.get("mqtt_tls") or "false").lower() == "true",
            "prefix": (s.get("mqtt_discovery_prefix") or "homeassistant").strip(),
            "base": (s.get("mqtt_base_topic") or "shrinkerr").strip()}


def discovery(cfg: dict, version: str) -> list[tuple[str, dict]]:
    """Home Assistant's discovery messages: (topic, config)."""
    from backend.i18n import t
    base, prefix = cfg["base"], cfg["prefix"]
    node = re.sub(r"[^a-zA-Z0-9_-]", "_", base)  # several instances: one device each
    device = {"identifiers": [node], "name": "Shrinkerr", "manufacturer": "Shrinkerr", "model": "Shrinkerr",
              "sw_version": version}
    common = {"availability_topic": f"{base}/status", "state_topic": f"{base}/state", "device": device,
              "has_entity_name": True}
    out = []
    for key, unit, device_class, state_class, icon, template in SENSORS:
        config = {**common, "name": t(f"homeAssistant:entities.{key}"), "unique_id": f"{node}_{key}",
                  "value_template": template}
        for field, value in (("unit_of_measurement", unit), ("device_class", device_class),
                             ("state_class", state_class), ("icon", icon)):
            if value:
                config[field] = value
        out.append((f"{prefix}/sensor/{node}/{key}/config", config))
    out.append((f"{prefix}/switch/{node}/queue/config", {
        **common, "name": t("homeAssistant:entities.queue"), "unique_id": f"{node}_queue", "icon": "mdi:play-pause",
        "command_topic": f"{base}/queue/set", "payload_on": "ON", "payload_off": "OFF",
        "value_template": "{{ 'OFF' if value_json.paused else 'ON' }}"}))
    return out


async def state() -> dict:
    """The state topic's JSON."""
    from backend.i18n import t
    from backend.routes.metrics import collect
    s = await collect()
    db = await connect_db()
    try:
        async with db.execute("SELECT file_path, progress FROM jobs WHERE status = 'running' "
                              "ORDER BY started_at LIMIT 1") as cur:
            current = await cur.fetchone()
    finally:
        await db.close()
    return {"pending": s["jobs"]["pending"], "running": s["jobs"]["running"], "failed": s["jobs"]["failed"],
            "completed": s["jobs"]["completed"], "paused": s["paused"],
            "saved_gb": round(s["saved_bytes"] / GB, 2), "to_convert": s["to_convert"],
            "estimated_savings_gb": round(s["estimated_savings_bytes"] / GB, 2),
            "current_file": PurePosixPath(current["file_path"]).name if current else t("homeAssistant:idle"),
            "progress": round(current["progress"] or 0, 1) if current else 0}


def handle_command(topic: str, payload: bytes, base: str) -> str | None:
    """Act on a command message; what was done (None: not ours)."""
    from backend.routes import jobs as jobs_route
    worker = jobs_route._worker
    if topic != f"{base}/queue/set" or worker is None:
        return None
    value = payload.decode(errors="replace").strip().upper()
    if value == "ON":
        worker.start()
        return "started"
    if value == "OFF":
        worker.pause()
        return "paused"
    return None


def _client(cfg: dict, **kw):
    import aiomqtt
    tls = aiomqtt.TLSParameters() if cfg["tls"] else None
    return aiomqtt.Client(cfg["host"], cfg["port"], username=cfg["username"], password=cfg["password"],
                          tls_params=tls, timeout=10, **kw)


async def test_connection(cfg: dict) -> dict:
    """Connect to the broker once (Settings' Test button)."""
    if not cfg["host"]:
        return {"success": False, "error": "Enter the MQTT broker's host first"}
    try:
        async with _client(cfg):
            pass
        return {"success": True}
    except Exception as exc:
        return {"success": False, "error": str(exc) or type(exc).__name__}


async def _session(cfg: dict) -> None:
    """One connection: announce, then publish state and take commands until
    the connection drops or the settings change."""
    import aiomqtt
    from backend.routes.stats import _get_current_version
    base = cfg["base"]
    will = aiomqtt.Will(f"{base}/status", "offline", qos=1, retain=True)
    async with _client(cfg, will=will) as client:
        for topic, config in discovery(cfg, _get_current_version()):
            await client.publish(topic, json.dumps(config), qos=1, retain=True)
        await client.publish(f"{base}/status", "online", qos=1, retain=True)
        await client.subscribe(f"{base}/queue/set")
        print(f"[MQTT] Connected to {cfg['host']}:{cfg['port']}", flush=True)

        async def publish_state():
            while True:
                await client.publish(f"{base}/state", json.dumps(await state()), retain=True)
                await asyncio.sleep(STATE_SECONDS)

        async def listen():
            async for message in client.messages:
                done = handle_command(str(message.topic), message.payload or b"", base)
                if done:
                    print(f"[MQTT] Queue {done} from Home Assistant", flush=True)
                    await client.publish(f"{base}/state", json.dumps(await state()), retain=True)

        async def watch_settings():
            while await load_settings() == cfg:
                await asyncio.sleep(SETTINGS_SECONDS)
            await client.publish(f"{base}/status", "offline", qos=1, retain=True)

        tasks = [asyncio.create_task(c()) for c in (publish_state, listen, watch_settings)]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()  # a dropped connection raises here
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


async def mqtt_loop() -> None:
    """Keep connected while it's enabled; reconnect after errors and when
    the settings change."""
    while True:
        try:
            cfg = await load_settings()
            if not cfg["enabled"] or not cfg["host"]:
                await asyncio.sleep(SETTINGS_SECONDS)
                continue
            await _session(cfg)  # returns when the settings change
        except Exception as exc:
            print(f"[MQTT] {exc or type(exc).__name__}; retrying in {RETRY_SECONDS} s", flush=True)
            await asyncio.sleep(RETRY_SECONDS)
