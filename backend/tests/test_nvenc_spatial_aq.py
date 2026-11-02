"""NVENC's anti-banding: spatial AQ (v0.10.0), Settings → Video's and a rule's.

Without it NVENC smoothed the faint noise of flat dark areas into bands:
Blade Runner 2049's foggy scene at 13:19 scored 0.60 on CAMBI (Netflix's
banding score; the original 0.03), 0.27 at strength 8 and 0.19 at 15, for
about 12% more bits at CQ 18. VMAF didn't notice (95.18 banded, 94.40 not),
so a VMAF target couldn't steer away from it. On bright 8-bit TV at CQ 36,
15 cost 71% more bits than none and 8 cost 29% (CAMBI 1.95 → 1.47 / 1.64).
So Light (8) is the default, Strong (15) and Off the other choices, and a
rule can choose its own (the user's choice). libx265 has had the equivalent
(aq-mode=3) all along.
"""
import shutil
import subprocess

import aiosqlite
import pytest

from backend.converter import _build_ffmpeg_cmd_impl
from backend.rule_actions import job_overrides, settings_overrides
from backend.tests.test_nvenc_level import _arg
from backend.tests.test_vmaf_target_rules import FILM, PLAIN, REALITY, _rule, _settings, library  # noqa: F401  (a fixture)


@pytest.mark.parametrize("output_codec", [None, "av1"])
@pytest.mark.parametrize("width", [1920, 3840])
@pytest.mark.parametrize("aq, strength", [(None, "8"), ("light", "8"), ("strong", "15"), ("nonsense", "8")])
def test_flat_areas_get_their_bits(output_codec, width, aq, strength):
    cmd = _build_ffmpeg_cmd_impl("in.mkv", "out.mkv", encoder="nvenc", cq=18, output_codec=output_codec,
                                 output_width=width, **({} if aq is None else {"nvenc_aq": aq}))
    assert (_arg(cmd, "-spatial-aq"), _arg(cmd, "-aq-strength")) == ("1", strength)
    assert cmd.index("-spatial-aq") < cmd.index("out.mkv")


def test_off():
    cmd = _build_ffmpeg_cmd_impl("in.mkv", "out.mkv", encoder="nvenc", cq=36, output_width=1920, nvenc_aq="off")
    assert "-spatial-aq" not in cmd and "-aq-strength" not in cmd


def test_other_encoders_are_unchanged():
    for encoder in ("libx265", "qsv", "vaapi", "videotoolbox"):
        assert "-spatial-aq" not in _build_ffmpeg_cmd_impl("in.mkv", "out.mkv", encoder=encoder,
                                                           output_width=1920, nvenc_aq="strong")


def test_a_job_carries_its_rules_choice():
    assert job_overrides({"nvenc_aq": "off"})["nvenc_aq"] == "off"
    assert job_overrides({"nvenc_aq": "loud"})["nvenc_aq"] is None and job_overrides(None)["nvenc_aq"] is None
    assert settings_overrides({"nvenc_aq": "strong"}) == {"nvenc_aq": "strong"}
    assert settings_overrides({"nvenc_aq": None}) == settings_overrides({}) == {}


@pytest.mark.asyncio
async def test_the_setting(test_db, monkeypatch):
    from backend.models import SettingsUpdate
    from backend.routes import settings as settings_route
    monkeypatch.setattr(settings_route, "DB_PATH", test_db)
    assert (await settings_route.get_encoding_settings())["nvenc_aq"] == "light"
    await settings_route.update_encoding_settings(SettingsUpdate(nvenc_aq="strong"))
    assert (await settings_route.get_encoding_settings())["nvenc_aq"] == "strong"
    await settings_route.update_encoding_settings(SettingsUpdate(nvenc_aq="loud"))
    assert (await settings_route.get_encoding_settings())["nvenc_aq"] == "light"


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
@pytest.mark.asyncio
@pytest.mark.parametrize("setting, job, strength", [(None, None, "8"), ("strong", None, "15"),
                                                    ("strong", "off", None), ("off", "light", "8")])
async def test_settings_unless_the_rule_chose(test_db, tmp_path, monkeypatch, setting, job, strength):
    """A conversion uses Settings' choice, or its rule's (on the job), as the
    queue hands its settings to convert_file — VMAF target samples and test
    encodes build their commands from the same settings."""
    from backend.converter import convert_file, get_live_encoding_settings
    from backend.models import SettingsUpdate
    from backend.routes import settings as settings_route
    monkeypatch.setattr(settings_route, "DB_PATH", test_db)
    if setting:
        await settings_route.update_encoding_settings(SettingsUpdate(nvenc_aq=setting))
    clip = tmp_path / "Film (2020).mkv"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=320x240:r=25:d=2",
                    "-c:v", "libx264", "-preset", "ultrafast", str(clip)], check=True)
    live = {**await get_live_encoding_settings(), **settings_overrides({"nvenc_aq": job})}
    cmd = (await convert_file(str(clip), "nvenc", 2.0, command_only=True, pre_settings=live))["command"]
    assert _arg(cmd, "-aq-strength") == strength and ("-spatial-aq" in cmd) == (strength is not None)


@pytest.mark.asyncio
async def test_a_rule_to_its_job_to_a_worker(test_db, tmp_path, monkeypatch):
    import sys
    from types import SimpleNamespace

    import backend.nodes as nodes
    import backend.routes.nodes as nodes_route
    from backend import worker_mode
    from backend.queue import JobQueue, QueueWorker
    from backend.routes import rules as route
    from backend.routes.jobs import BulkQueueFromScanRequest, add_jobs_from_scan, init_job_routes
    for name, module in list(sys.modules.items()):
        if name.startswith("backend.") and isinstance(getattr(module, "DB_PATH", None), str):
            monkeypatch.setattr(module, "DB_PATH", test_db)
    worker = QueueWorker(test_db)
    monkeypatch.setattr(worker, "start", lambda *a, **kw: None)
    init_job_routes(worker, JobQueue(test_db))
    async with aiosqlite.connect(test_db) as db:
        for path in ("/m/Reality/e1.mkv", "/m/Films/f.mkv"):
            await db.execute(
                "INSERT INTO scan_results (file_path, file_size, duration, video_codec, needs_conversion, "
                "audio_tracks_json, subtitle_tracks_json, native_language, scan_timestamp) "
                "VALUES (?, 1000, 3600, 'h264', 1, '[]', '[]', 'und', 'x')", (path,))
        await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('nvenc_aq', 'strong')")
        await db.execute("INSERT INTO worker_nodes (id, name, capabilities, status, registered_at) "
                         "VALUES ('n1', 'n1', '[\"nvenc\"]', 'online', 'x')")
        await db.commit()
    with pytest.raises(ValueError):
        route.RuleCreate(name="Bad", match_conditions=[route.MatchCondition(type="directory", value="/m")],
                         nvenc_aq="loud")
    await route.create_rule(route.RuleCreate(
        name="Reality", match_conditions=[route.MatchCondition(type="directory", value="/m/Reality")], nvenc_aq="off"))
    await add_jobs_from_scan(BulkQueueFromScanRequest(file_paths=["/m/Reality/e1.mkv", "/m/Films/f.mkv"]))
    async with aiosqlite.connect(test_db) as db:
        async with db.execute("SELECT file_path, nvenc_aq FROM jobs ORDER BY file_path") as cur:
            assert await cur.fetchall() == [("/m/Films/f.mkv", None), ("/m/Reality/e1.mkv", "off")]
        await db.execute("UPDATE jobs SET queue_order = CASE WHEN file_path LIKE '/m/Reality%' THEN 1 ELSE 2 END")
        await db.commit()

    async def no_token(*a, **kw):
        return None
    monkeypatch.setattr(nodes_route, "_require_node_token", no_token)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(node_manager=nodes.NodeManager())))
    sent = [(await nodes_route.request_job(nodes_route.RequestJobBody(node_id="n1"), request))["job"]
            for _ in range(2)]
    assert sorted((j["file_path"], j["nvenc_aq"]) for j in sent) == [("/m/Films/f.mkv", "strong"),
                                                                     ("/m/Reality/e1.mkv", "off")]

    import backend.converter
    import backend.scanner
    from backend.tests.test_worker_audio_remux import PROBE, FakeClient
    seen = {}

    async def fake_probe(path, *a, **kw):
        return dict(PROBE)

    async def fake_convert_file(**kwargs):
        seen.update(kwargs)
        return {"success": True, "output_path": kwargs.get("input_path"), "space_saved": 1, "error": None}
    monkeypatch.setattr(backend.scanner, "probe_file", fake_probe)
    monkeypatch.setattr(backend.converter, "convert_file", fake_convert_file)
    src = tmp_path / "e1.mkv"
    src.write_bytes(b"x")
    await worker_mode.execute_job(FakeClient(), "n1", {**sent[0], "id": 9, "file_path": str(src)}, ["nvenc"])
    assert seen["pre_settings"]["nvenc_aq"] == sent[0]["nvenc_aq"]


@pytest.mark.asyncio
async def test_a_rules_choice_is_shared_with_the_rule(test_db):
    from backend.routes import rules as route
    from backend.rule_sharing import export_rules, import_rules, parse
    for name, aq in (("Off", "off"), ("Follows", None)):
        await route.create_rule(route.RuleCreate(
            name=name, match_conditions=[route.MatchCondition(type="directory", value="/m")], nvenc_aq=aq))
    text = await export_rules()
    async with aiosqlite.connect(test_db) as db:
        await db.execute("DELETE FROM encoding_rules")
        await db.commit()
    assert (await import_rules(parse(text)))["added"] == 2
    async with aiosqlite.connect(test_db) as db:
        async with db.execute("SELECT name, nvenc_aq FROM encoding_rules ORDER BY id") as cur:
            assert await cur.fetchall() == [("Off", "off"), ("Follows", None)]
    from backend.routes.rules import list_rules
    assert [r.get("nvenc_aq") for r in await list_rules()] == ["off", None]


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
@pytest.mark.asyncio
async def test_a_test_encode_uses_the_rules_choice(test_db, tmp_path, monkeypatch):
    import backend.converter as converter
    import backend.test_encode as test_encode
    from backend.routes import rules as route
    from backend.routes.jobs import TestEncodeRequest, start_test_encode
    monkeypatch.setattr(test_encode, "TEMP_DIR", tmp_path / "samples")
    folder = tmp_path / "Reality"
    folder.mkdir()
    clip = folder / "e1.mkv"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=320x240:r=25:d=12",
                    "-c:v", "libx264", "-preset", "ultrafast", str(clip)], check=True)
    await route.create_rule(route.RuleCreate(
        name="Reality", match_conditions=[route.MatchCondition(type="directory", value=str(folder))], nvenc_aq="off"))
    seen = {}

    async def spy(*args, **kwargs):
        seen.update(kwargs)
        return {"command": None, "error": "stop here"}
    monkeypatch.setattr(converter, "convert_file", spy)
    result = await start_test_encode(TestEncodeRequest(file_path=str(clip), encoder="nvenc", cq=30))
    assert result["status"] != "complete" and seen["pre_settings"]["nvenc_aq"] == "off"
    # Add to Queue's own choice, over the rule's.
    seen.clear()
    await start_test_encode(TestEncodeRequest(file_path=str(clip), encoder="nvenc", cq=30, nvenc_aq="strong"))
    assert seen["pre_settings"]["nvenc_aq"] == "strong"


@pytest.mark.asyncio
async def test_add_to_queue_chooses_for_its_files(library):
    """The dialog's choice, over a rule's; none: the rule's, else Settings'
    when it runs. A file re-added while it waits takes the new choice."""
    from backend.routes.jobs import BulkQueueFromScanRequest, add_jobs_from_scan
    await _settings(library, default_encoder="nvenc")
    await _rule("Reality", "/m/Reality", nvenc_aq="off")

    async def jobs():
        async with aiosqlite.connect(library) as db:
            async with db.execute("SELECT file_path, nvenc_aq FROM jobs") as cur:
                return dict(await cur.fetchall())
    await add_jobs_from_scan(BulkQueueFromScanRequest(file_paths=[REALITY, FILM], nvenc_aq_override="strong"))
    await add_jobs_from_scan(BulkQueueFromScanRequest(file_paths=[PLAIN]))
    assert await jobs() == {REALITY: "strong", FILM: "strong", PLAIN: None}
    await add_jobs_from_scan(BulkQueueFromScanRequest(
        file_paths=[REALITY, PLAIN], nvenc_aq_override="light", update_pending=True))
    assert await jobs() == {REALITY: "light", FILM: "strong", PLAIN: "light"}
    await add_jobs_from_scan(BulkQueueFromScanRequest(file_paths=[REALITY], update_pending=True))
    assert (await jobs())[REALITY] == "off"  # no choice: the rule's again


@pytest.mark.asyncio
async def test_a_clean_up_encodes_nothing(library):
    from backend.routes.jobs import BulkQueueFromScanRequest, add_jobs_from_scan
    await _settings(library, default_encoder="nvenc")
    async with aiosqlite.connect(library) as db:
        await db.execute("UPDATE scan_results SET audio_tracks_json = ? WHERE file_path = ?",
                         ('[{"stream_index": 1, "language": "eng", "codec": "aac", "keep": true}, '
                          '{"stream_index": 2, "language": "fra", "codec": "aac", "keep": false}]', PLAIN))
        await db.commit()
    await add_jobs_from_scan(BulkQueueFromScanRequest(file_paths=[PLAIN], cleanup_only=True, nvenc_aq_override="strong"))
    async with aiosqlite.connect(library) as db:
        async with db.execute("SELECT job_type, nvenc_aq FROM jobs") as cur:
            assert await cur.fetchall() == [("audio", None)]


def test_only_off_light_or_strong():
    from pydantic import ValidationError

    from backend.routes.jobs import BulkQueueFromScanRequest, TestEncodeRequest
    with pytest.raises(ValidationError):
        BulkQueueFromScanRequest(file_paths=["/m/a.mkv"], nvenc_aq_override="loud")
    with pytest.raises(ValidationError):
        TestEncodeRequest(file_path="/m/a.mkv", nvenc_aq="loud")


@pytest.mark.asyncio
async def test_the_estimate_says_what_auto_is(library):
    """Add to Queue's "Auto" names Settings' anti-banding and the rules'
    instead, for the files NVENC converts."""
    from backend.routes.jobs import EstimateRequest, estimate_jobs
    await _settings(library, default_encoder="nvenc", nvenc_aq="strong")
    await _rule("Reality", "/m/Reality", nvenc_aq="off")
    files = [REALITY, FILM, PLAIN]
    assert (await estimate_jobs(EstimateRequest(file_paths=files)))["auto"]["nvenc_aq"] == {
        "settings": {"strong": 2}, "rules": {"off": 1}}
    assert (await estimate_jobs(EstimateRequest(file_paths=files, encoder="libx265")))["auto"]["nvenc_aq"] == {
        "settings": {}, "rules": {}}


def test_the_dialog_has_the_choice():
    """The frontend has no test runner: these check the source and catalogs."""
    from backend.tests.test_vmaf_target_ui import PLURALS, _placeholders, _source
    src = _source("EstimateModal.tsx")
    preset, aq, audio = (src.index(f'{{t("scannerModals:overrides.{k}")}}</label>')
                         for k in ("preset", "antiBanding.label", "audio"))
    assert preset < aq < audio  # under the preset
    assert 'effectiveEncoder === "nvenc"' in src
    assert all(f'<option value="{v}">' in src for v in ("off", "light", "strong"))
    assert 'autoLabel("nvenc_aq"' in src
    assert "if (aq !== null && isNvenc) o.nvenc_aq = aq;" in src
    assert "isNvenc ? aq ?? undefined : undefined" in src  # the test encode
    assert "extra.nvenc_aq_override = encodingOverrides.nvenc_aq" in _source("ScannerPage.tsx")
    for lang in PLURALS:
        for key in ("label", "off", "light", "strong"):
            assert _placeholders(lang, f"scannerModals:overrides.antiBanding.{key}") == [set()], (lang, key)
