"""Setup wizard v2 backend (v0.10.0): quality presets, the language preview
and the keep-language cache."""
import aiosqlite
import pytest

from backend.encoding_estimates import (
    QUALITY_PRESETS, WIZARD_PRESETS, cq_to_savings_pct, effective_cq, preset_settings, vt_quality_for_cq,
)


@pytest.mark.parametrize("encoder", ["nvenc", "libx265", "qsv", "vaapi", "videotoolbox"])
def test_each_preset_saves_more_than_the_one_before(encoder):
    savings = []
    for preset in QUALITY_PRESETS:
        values = preset_settings(encoder, preset)
        assert len(values) == 1  # only the encoder's own quality setting
        saved = {"default_encoder": encoder, **{k: str(v) for k, v in values.items()}}
        savings.append(cq_to_savings_pct(effective_cq(saved)))
    assert savings == sorted(savings) and len(set(savings)) == len(QUALITY_PRESETS), savings
    assert WIZARD_PRESETS == ("quality", "balanced", "max_savings")


def test_videotoolbox_gets_the_nearest_measured_quality():
    assert [vt_quality_for_cq(cq) for cq in (18, 20, 23, 26, 29)] == [62, 60, 55, 50, 40]
    assert vt_quality_for_cq(10) == 65 and vt_quality_for_cq(35) == 40  # clamped
    assert vt_quality_for_cq(24) == 55 and vt_quality_for_cq(25) == 50


def test_presets_are_the_same_quality_on_every_encoder():
    """Each preset lands within one CQ step of its target on every encoder,
    so the savings shown don't depend on the hardware much."""
    for preset, cq in QUALITY_PRESETS.items():
        for encoder in ("nvenc", "libx265", "qsv", "vaapi", "videotoolbox"):
            values = {k: str(v) for k, v in preset_settings(encoder, preset).items()}
            assert abs(effective_cq({"default_encoder": encoder, **values}) - cq) <= 1, (encoder, preset)


@pytest.fixture
def settings_route(test_db, monkeypatch):
    import backend.routes.settings as settings_route
    monkeypatch.setattr(settings_route, "DB_PATH", test_db)
    return settings_route


async def _set(db_path, **values):
    async with aiosqlite.connect(db_path) as db:
        for k, v in values.items():
            await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (k, str(v)))
        await db.commit()


@pytest.mark.asyncio
async def test_quality_presets_say_which_one_is_saved(settings_route, test_db):
    await _set(test_db, default_encoder="nvenc", nvenc_cq=23)
    r = await settings_route.get_quality_presets()
    assert r["encoder"] == "nvenc" and r["current"] == "balanced"
    assert [p["id"] for p in r["presets"]] == ["max_quality", "quality", "balanced", "max_savings", "smallest"]
    assert [p["settings"] for p in r["presets"]] == [{"nvenc_cq": c} for c in (18, 20, 23, 26, 29)]
    assert [p["wizard"] for p in r["presets"]] == [False, True, True, True, False]
    assert r["presets"][2]["job"] == {"nvenc_cq": 23, "libx265_crf": 25}
    await _set(test_db, nvenc_cq=24)
    assert (await settings_route.get_quality_presets())["current"] is None  # custom
    r = await settings_route.get_quality_presets(encoder="libx265")
    assert [p["settings"]["libx265_crf"] for p in r["presets"]] == [20, 22, 25, 28, 31]
    assert [p["savings_pct"] for p in r["presets"]] == [35, 45, 55, 70, 80]


SAMPLE = [{
    "path": "/media/Anime/Show/ep1.mkv", "duration": 1400,
    "audio": [
        {"stream_index": 1, "language": "jpn", "codec": "flac", "channels": 2, "title": ""},
        {"stream_index": 2, "language": "eng", "codec": "aac", "channels": 2, "title": ""},
        {"stream_index": 3, "language": "fra", "codec": "aac", "channels": 2, "title": ""},
    ],
    "subs": [
        {"stream_index": 4, "language": "eng", "codec": "ass", "forced": False},
        {"stream_index": 5, "language": "fra", "codec": "ass", "forced": False},
        {"stream_index": 6, "language": "fra", "codec": "ass", "forced": True},
    ],
}]


@pytest.mark.asyncio
async def test_language_preview_classifies_with_the_chosen_languages(settings_route, monkeypatch):
    import backend.scanner as scanner
    import backend.config
    monkeypatch.setattr(scanner, "settings", backend.config.settings)
    scanner.invalidate_sub_settings_cache()

    async def sample():
        return SAMPLE
    monkeypatch.setattr(settings_route, "_language_sample", sample)

    def kept(files, kind):
        return {(t["language"], t.get("forced", False)): t["keep"] for t in files[0][kind]}

    r = await settings_route.language_preview(settings_route._LanguagePreviewBody(audio_languages=["ENG"]))
    f = r["files"]
    assert f[0]["name"] == "ep1.mkv" and f[0]["native"] == "jpn"
    # English (chosen) and Japanese (the native) stay; French goes.
    assert kept(f, "audio") == {("jpn", False): True, ("eng", False): True, ("fra", False): False}
    # No subtitle languages yet: every subtitle stays (cleanup is off).
    assert all(kept(f, "subs").values())
    f = (await settings_route.language_preview(settings_route._LanguagePreviewBody(
        audio_languages=["eng"], sub_languages=["eng"])))["files"]
    # French goes, forced too: forced subtitles stay only in your languages.
    assert kept(f, "subs") == {("eng", False): True, ("fra", False): False, ("fra", True): False}
    f = (await settings_route.language_preview(settings_route._LanguagePreviewBody(
        audio_languages=["eng"], sub_languages=["eng", "fra"])))["files"]
    assert all(kept(f, "subs").values())


def test_the_sample_spreads_over_folders(tmp_path):
    import backend.routes.settings as settings_route
    for i in range(40):
        d = tmp_path / f"Title {i:02d}"
        d.mkdir()
        (d / "a.mkv").write_bytes(b"x")
        (d / "b.mkv").write_bytes(b"x")
        (d / "cover.jpg").write_bytes(b"x")
    (tmp_path / "Disc.iso").write_bytes(b"x")
    picks = settings_route._pick_sample_paths([str(tmp_path)], size=12)
    assert len(picks) == 12
    assert len({p.rsplit("/", 1)[0] for p in picks}) == 12  # one per folder
    assert all(p.endswith("a.mkv") for p in picks)


@pytest.mark.asyncio
async def test_saving_the_audio_keep_languages_reaches_the_scanner(settings_route, test_db):
    """The scanner caches the list; saving it must drop the cache (it took a
    restart before)."""
    import backend.scanner as scanner
    from backend.models import SettingsUpdate
    scanner._audio_settings_cache = {"fra"}
    scanner._audio_settings_loaded = True
    await settings_route.update_encoding_settings(SettingsUpdate(always_keep_languages=["eng"]))
    assert scanner._audio_settings_loaded is False


@pytest.mark.asyncio
@pytest.mark.parametrize("encoder,flag,value", [
    ("nvenc", "-cq", "26"), ("qsv", "-global_quality", "26"), ("vaapi", "-qp", "26"), ("videotoolbox", "-q:v", "50"),
])
async def test_a_jobs_own_quality_reaches_every_encoder(test_db, tmp_path, encoder, flag, value):
    """A queue preset / rule / content-type CQ is on NVENC's scale; QSV, VAAPI
    and VideoToolbox ignored it and used Settings' value."""
    import shutil
    import subprocess
    if not shutil.which("ffmpeg"):
        pytest.skip("needs ffmpeg")
    from backend.converter import convert_file
    clip = tmp_path / "Film (2020).mkv"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=320x240:r=25:d=2",
                    "-c:v", "libx264", "-preset", "ultrafast", str(clip)], check=True)
    cmd = (await convert_file(str(clip), encoder, 2.0, override_cq=26, command_only=True))["command"]
    assert cmd[cmd.index(flag) + 1] == value
    # Without one, Settings' value still applies.
    cmd = (await convert_file(str(clip), encoder, 2.0, command_only=True))["command"]
    assert cmd[cmd.index(flag) + 1] != value
