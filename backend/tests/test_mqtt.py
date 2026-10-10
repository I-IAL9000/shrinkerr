"""Home Assistant over MQTT (v0.10.0): discovery announces a Shrinkerr
device with sensors and a queue switch; the state topic carries the numbers;
ON / OFF on the command topic starts and pauses the queue; a settings change
reconnects."""
import asyncio
import json
from types import SimpleNamespace

import aiomqtt
import aiosqlite
import pytest

from backend import i18n, mqtt
from backend.api_errors import ApiError

CFG = {"enabled": True, "host": "broker", "port": 1883, "username": "ha", "password": "pw", "tls": False,
       "prefix": "homeassistant", "base": "shrinkerr"}


@pytest.fixture(autouse=True)
def english():
    i18n.set_current_language("en")
    yield
    i18n.set_current_language("en")


def test_discovery():
    configs = dict(mqtt.discovery(CFG, "0.10.0"))
    assert len(configs) == len(mqtt.SENSORS) + 1
    saved = configs["homeassistant/sensor/shrinkerr/saved/config"]
    assert saved["name"] == "Space saved" and saved["unique_id"] == "shrinkerr_saved"
    assert (saved["state_topic"], saved["availability_topic"]) == ("shrinkerr/state", "shrinkerr/status")
    assert (saved["unit_of_measurement"], saved["device_class"]) == ("GB", "data_size")
    assert saved["device"] == {"identifiers": ["shrinkerr"], "name": "Shrinkerr", "manufacturer": "Shrinkerr",
                               "model": "Shrinkerr", "sw_version": "0.10.0"}
    switch = configs["homeassistant/switch/shrinkerr/queue/config"]
    assert switch["command_topic"] == "shrinkerr/queue/set" and "paused" in switch["value_template"]
    assert "unit_of_measurement" not in configs["homeassistant/sensor/shrinkerr/current_file/config"]
    i18n.set_current_language("es")
    assert dict(mqtt.discovery(CFG, "x"))["homeassistant/switch/shrinkerr/queue/config"]["name"] == "Cola"
    other = dict(mqtt.discovery({**CFG, "base": "nas/shrinkerr 2", "prefix": "ha"}, "x"))  # a second instance
    assert "ha/sensor/nas_shrinkerr_2/pending/config" in other


@pytest.mark.asyncio
async def test_the_state(test_db, monkeypatch):
    from backend.routes import jobs as jobs_route
    monkeypatch.setattr(jobs_route, "_worker", SimpleNamespace(_paused=True))
    assert (await mqtt.state())["current_file"] == "Idle"
    async with aiosqlite.connect(test_db) as db:
        await db.executemany(
            "INSERT INTO jobs (file_path, job_type, status, created_at, started_at, progress, space_saved, original_size) "
            "VALUES (?, 'convert', ?, 'x', ?, ?, ?, ?)",
            [("/m/a.mkv", "pending", None, 0, 0, 0), ("/m/Film (2020)/Film.mkv", "running", "2026-10-10", 42.26, 0, 0),
             ("/m/c.mkv", "completed", "2026-10-09", 100, 3 * mqtt.GB, 5 * mqtt.GB), ("/m/d.mkv", "failed", None, 0, 0, 0)])
        await db.commit()
    s = await mqtt.state()
    assert s == {"pending": 1, "running": 1, "failed": 1, "completed": 1, "paused": True, "saved_gb": 3.0,
                 "to_convert": 0, "estimated_savings_gb": 0.0, "current_file": "Film.mkv", "progress": 42.3}


def test_commands(monkeypatch):
    from backend.routes import jobs as jobs_route
    calls = []
    monkeypatch.setattr(jobs_route, "_worker", SimpleNamespace(start=lambda: calls.append("start"),
                                                               pause=lambda: calls.append("pause")))
    assert mqtt.handle_command("shrinkerr/queue/set", b"ON", "shrinkerr") == "started"
    assert mqtt.handle_command("shrinkerr/queue/set", b" off\n", "shrinkerr") == "paused"
    assert mqtt.handle_command("shrinkerr/queue/set", b"maybe", "shrinkerr") is None
    assert mqtt.handle_command("other/queue/set", b"ON", "shrinkerr") is None
    assert calls == ["start", "pause"]


class FakeClient:
    """aiomqtt.Client's surface: an async context manager with publish,
    subscribe and a `messages` async iterator."""
    instances: list = []

    def __init__(self, hostname, port=1883, *, username=None, password=None, tls_params=None, timeout=None,
                 will=None):
        self.args = (hostname, port, username, password, tls_params, will)
        self.published, self.subscribed = [], []
        self.inbox: asyncio.Queue = asyncio.Queue()
        FakeClient.instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def publish(self, topic, payload=None, qos=0, retain=False):
        self.published.append((topic, payload, retain))

    async def subscribe(self, topic, qos=0):
        self.subscribed.append(topic)

    @property
    def messages(self):
        inbox = self.inbox

        async def gen():
            while True:
                topic, payload = await inbox.get()
                yield SimpleNamespace(topic=aiomqtt.Topic(topic), payload=payload)
        return gen()


async def _until(check, timeout=2.0):
    async def wait():
        while not check():
            await asyncio.sleep(0.01)
    await asyncio.wait_for(wait(), timeout)


@pytest.mark.asyncio
async def test_a_session(test_db, monkeypatch):
    from backend.routes import jobs as jobs_route
    FakeClient.instances = []
    monkeypatch.setattr(aiomqtt, "Client", FakeClient)
    monkeypatch.setattr(mqtt, "SETTINGS_SECONDS", 0.01)
    monkeypatch.setattr(mqtt, "STATE_SECONDS", 60)  # once at the start; then after commands
    worker = SimpleNamespace(_paused=True, pause=lambda: None)
    worker.start = lambda: setattr(worker, "_paused", False)
    monkeypatch.setattr(jobs_route, "_worker", worker)
    current = dict(CFG)

    async def settings():
        return dict(current)
    monkeypatch.setattr(mqtt, "load_settings", settings)
    session = asyncio.create_task(mqtt._session(dict(CFG)))
    await _until(lambda: FakeClient.instances and FakeClient.instances[0].subscribed)
    client = FakeClient.instances[0]
    assert client.args[:4] == ("broker", 1883, "ha", "pw") and client.args[4] is None
    assert client.args[5] == aiomqtt.Will("shrinkerr/status", "offline", qos=1, retain=True)
    assert client.subscribed == ["shrinkerr/queue/set"]
    discovery = [(t, json.loads(p)) for t, p, retain in client.published if t.endswith("/config") and retain]
    assert len(discovery) == len(mqtt.SENSORS) + 1
    assert ("shrinkerr/status", "online", True) in client.published

    def states():
        return [json.loads(p) for t, p, _ in client.published if t == "shrinkerr/state"]
    await _until(lambda: states())
    assert states()[-1]["paused"] is True
    await client.inbox.put(("shrinkerr/queue/set", b"ON"))  # Home Assistant flips the switch
    await _until(lambda: states()[-1]["paused"] is False)
    current["base"] = "elsewhere"  # the settings changed: offline, and the session ends
    await asyncio.wait_for(session, 2)
    assert client.published[-1] == ("shrinkerr/status", "offline", True)


@pytest.mark.asyncio
async def test_tls_and_the_connection_test(monkeypatch):
    monkeypatch.setattr(aiomqtt, "Client", FakeClient)
    FakeClient.instances = []
    assert await mqtt.test_connection({**CFG, "tls": True}) == {"success": True}
    assert isinstance(FakeClient.instances[0].args[4], aiomqtt.TLSParameters)
    assert (await mqtt.test_connection({**CFG, "host": ""}))["success"] is False

    class Refused(FakeClient):
        async def __aenter__(self):
            raise aiomqtt.MqttError("[Errno 111] Connection refused")
    monkeypatch.setattr(aiomqtt, "Client", Refused)
    assert await mqtt.test_connection(CFG) == {"success": False, "error": "[Errno 111] Connection refused"}


@pytest.mark.asyncio
async def test_the_settings(test_db, monkeypatch):
    import backend.routes.settings as settings_route
    from backend.models import SettingsUpdate
    monkeypatch.setattr(settings_route, "DB_PATH", test_db)
    await settings_route.update_encoding_settings(SettingsUpdate(
        mqtt_enabled=True, mqtt_host=" 192.168.1.5 ", mqtt_port=8883, mqtt_username="ha", mqtt_password="secret",
        mqtt_tls=True, mqtt_base_topic="nas/shrinkerr"))
    got = await settings_route.get_encoding_settings()
    assert (got["mqtt_enabled"], got["mqtt_host"], got["mqtt_port"], got["mqtt_password"], got["mqtt_tls"]) == (
        True, "192.168.1.5", 8883, "********", True)
    await settings_route.update_encoding_settings(SettingsUpdate(mqtt_password="********"))  # masked: kept
    assert (await mqtt.load_settings()) == {**CFG, "host": "192.168.1.5", "port": 8883, "password": "secret",
                                             "tls": True, "base": "nas/shrinkerr"}
    assert "mqtt_password" in settings_route._SECRET_SETTINGS_KEYS
    for bad, code in ((dict(mqtt_host="mqtt://broker"), "settings.mqttHostInvalid"),
                      (dict(mqtt_port=70000), "settings.mqttPortInvalid"),
                      (dict(mqtt_base_topic="shrinkerr/#"), "settings.mqttTopicInvalid"),
                      (dict(mqtt_discovery_prefix="/homeassistant"), "settings.mqttTopicInvalid"),
                      (dict(mqtt_base_topic=""), "settings.mqttTopicInvalid")):
        with pytest.raises(ApiError) as exc:
            await settings_route.update_encoding_settings(SettingsUpdate(**bad))
        assert exc.value.code == code, bad
    monkeypatch.setattr(aiomqtt, "Client", FakeClient)  # never a real broker
    FakeClient.instances = []
    assert await settings_route.test_api_key(settings_route.TestApiRequest(service="mqtt")) == {"success": True}
    assert FakeClient.instances[0].args[:4] == ("192.168.1.5", 8883, "ha", "secret")


@pytest.mark.asyncio
async def test_the_loop_waits_until_enabled_and_retries(monkeypatch):
    monkeypatch.setattr(mqtt, "SETTINGS_SECONDS", 0.01)
    monkeypatch.setattr(mqtt, "RETRY_SECONDS", 0.01)
    current = {**CFG, "enabled": False}
    sessions = []

    async def settings():
        return dict(current)

    async def session(cfg):
        sessions.append(cfg)
        raise aiomqtt.MqttError("Connection refused")
    monkeypatch.setattr(mqtt, "load_settings", settings)
    monkeypatch.setattr(mqtt, "_session", session)
    loop = asyncio.create_task(mqtt.mqtt_loop())
    await asyncio.sleep(0.1)
    assert sessions == []  # off: nothing
    current["enabled"] = True
    await _until(lambda: len(sessions) >= 2)  # a failed connection is retried
    loop.cancel()
    with pytest.raises(asyncio.CancelledError):
        await loop
