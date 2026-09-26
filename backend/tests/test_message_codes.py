"""Server message codes: every key the backend emits must exist in the UI
catalogs (en + es) with matching {{placeholders}}, and the DB/API plumbing
must store and return key + params.

Channels -> catalog namespace (frontend/src/i18n/locales/<lang>/<ns>.json):
  summary_key (file_events)            -> serverEvents
  step_key    (WS job_progress)        -> serverJobs   ("steps.*")
  error_key   (jobs.error_key, WS)     -> serverJobs   ("errors.*")
  stage_key   (WS detect_progress)     -> serverJobs   ("detect.*")
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import aiosqlite
import pytest

REPO = Path(__file__).resolve().parents[2]
BACKEND = REPO / "backend"
LOCALES = REPO / "frontend" / "src" / "i18n" / "locales"
LANGS = ("en", "es")
NAMESPACE = {"summary_key": "serverEvents", "step_key": "serverJobs", "error_key": "serverJobs",
             "stage_key": "serverJobs"}

# `summary_key="x"`, `summary_key = "x" if cond else "y"` and dict-literal
# `"error_key": "x"` (converter/audio result dicts).
_KEY_RE = re.compile(
    r"\b(summary_key|step_key|error_key|stage_key)[\"']?\s*(?:=(?!=)|:)\s*\"([\w.]+)\""
    r"(?:\s+if\b[^,\n]*?\belse\s+\"([\w.]+)\")?"
)
_PLACEHOLDER_RE = re.compile(r"\{\{\s*([\w.]+)\s*\}\}")
_NEST_RE = re.compile(r"\$t\((\w+):([\w.]+)\.\{\{\s*\w+\s*\}\}\)")


def _load(lang: str, ns: str) -> dict:
    return json.loads((LOCALES / lang / f"{ns}.json").read_text(encoding="utf-8"))


def _lookup(catalog: dict, dotted: str):
    node = catalog
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _variants(catalog: dict, key: str) -> list[str]:
    """The string(s) a key resolves to: the base key, or its plural forms."""
    parent, _, leaf = key.rpartition(".")
    node = _lookup(catalog, parent) if parent else catalog
    if not isinstance(node, dict):
        return []
    out = []
    if isinstance(node.get(leaf), str):
        out.append(node[leaf])
    for k, v in node.items():
        if isinstance(v, str) and k.startswith(leaf + "_"):
            out.append(v)
    return out


def _emitted_keys() -> dict[str, set[str]]:
    found: dict[str, set[str]] = {k: set() for k in NAMESPACE}
    files = sorted(BACKEND.glob("*.py")) + sorted((BACKEND / "routes").glob("*.py"))
    for f in files:
        src = f.read_text(encoding="utf-8")
        for m in _KEY_RE.finditer(src):
            found[m.group(1)].update(k for k in m.group(2, 3) if k)
    from backend.websocket import STEP_KEYS
    found["step_key"].update(STEP_KEYS.values())
    return found


def _leaves(node, prefix=""):
    for k, v in node.items():
        path = f"{prefix}{k}"
        if isinstance(v, dict):
            yield from _leaves(v, path + ".")
        else:
            yield path, v


def test_scanner_finds_keys_on_every_channel():
    keys = _emitted_keys()
    # Sanity: the static scan must actually be seeing the call sites.
    assert "convertedSaved" in keys["summary_key"]
    assert "healthCheckResult" in keys["summary_key"]
    assert "errors.ffmpegExited" in keys["error_key"]  # converter result dict
    assert "errors.sourceGoneLikelyConverted" in keys["error_key"]
    assert "steps.healthCheckMode" in keys["step_key"]
    assert "steps.removingTracks" in keys["step_key"]
    assert {"detect.extractingTrack", "detect.ocrLatin", "detect.ocrNonLatin"} <= keys["stage_key"]
    assert len(keys["summary_key"]) >= 20


@pytest.mark.parametrize("lang", LANGS)
def test_every_emitted_key_is_in_catalog(lang):
    missing = []
    for channel, keys in _emitted_keys().items():
        catalog = _load(lang, NAMESPACE[channel])
        for key in sorted(keys):
            if not _variants(catalog, key):
                missing.append(f"{NAMESPACE[channel]}:{key} ({channel})")
    assert not missing, f"keys missing from {lang} catalogs: {missing}"


@pytest.mark.parametrize("ns", sorted(set(NAMESPACE.values())))
def test_en_es_catalogs_match(ns):
    en = dict(_leaves(_load("en", ns)))
    es = dict(_leaves(_load("es", ns)))
    # Spanish adds a `_many` plural form wherever English has `_other`.
    expected_es = set(en) | {k[: -len("_other")] + "_many" for k in en if k.endswith("_other")}
    assert set(es) == expected_es, (
        f"{ns}: only in en={sorted(expected_es - set(es))} only in es={sorted(set(es) - expected_es)}"
    )
    for key, en_text in en.items():
        assert isinstance(en_text, str) and en_text.strip(), f"{ns}:{key} empty"
        assert set(_PLACEHOLDER_RE.findall(en_text)) == set(_PLACEHOLDER_RE.findall(es[key])), (
            f"{ns}:{key} placeholder mismatch: en={en_text!r} es={es[key]!r}"
        )


@pytest.mark.parametrize("lang", LANGS)
def test_nested_label_groups_exist(lang):
    """`$t(ns:group.{{param}})` needs `group` to exist with identical value
    sets in every language — a missing entry renders as a raw key path."""
    for ns in set(NAMESPACE.values()):
        for key, text in _leaves(_load(lang, ns)):
            for ref_ns, group in _NEST_RE.findall(text):
                grp = _lookup(_load(lang, ref_ns), group)
                assert isinstance(grp, dict) and grp, f"{lang}/{ns}:{key} -> missing {ref_ns}:{group}"
                en_grp = _lookup(_load("en", ref_ns), group)
                assert set(grp) == set(en_grp), f"{lang} {ref_ns}:{group} values differ from en"


def _render_en(ns: str, key: str, params: dict) -> str:
    """Minimal i18next-style render (interpolation, then $t nesting)."""
    text = _variants(_load("en", ns), key)[0]
    text = _PLACEHOLDER_RE.sub(lambda m: str(params[m.group(1)]), text)
    return re.sub(r"\$t\((\w+):([\w.]+)\)", lambda m: _lookup(_load("en", m.group(1)), m.group(2)), text)


def test_english_catalog_matches_server_english():
    from backend.file_events import health_check_code, HEALTH_MODES, HEALTH_STATUSES
    for status in HEALTH_STATUSES:
        for mode in HEALTH_MODES:
            kw = health_check_code(status, mode)
            assert _render_en("serverEvents", kw["summary_key"], kw["summary_params"]) == \
                f"Health check: {status} ({mode})"
    assert health_check_code("bogus", "quick") == {}

    from backend.routes.arr import _summary_for_action
    for flags in ({}, {"blocklisted": 1}, {"deleted": 1, "searched": 1},
                  {"blocklisted": 1, "deleted": 1, "searched": 1}):
        for title_field in ({"series": "Show (2020)"}, {}):
            result = {"service": "sonarr", **title_field, **flags}
            english, key, params = _summary_for_action("replace", result)
            assert _render_en("serverEvents", key, params) == english
    english, key, params = _summary_for_action("upgrade", {"service": "sonarr", "series": "S", "episode_ids": [1, 2]})
    assert _render_en("serverEvents", key, params) == english


@pytest.mark.asyncio
async def test_migration_adds_columns_to_existing_db(test_db):
    # Simulate a pre-message-codes DB, then re-run startup migrations.
    async with aiosqlite.connect(test_db) as db:
        for table, col in (("file_events", "summary_key"), ("file_events", "summary_params"),
                           ("jobs", "error_key"), ("jobs", "error_params")):
            await db.execute(f"ALTER TABLE {table} DROP COLUMN {col}")
        await db.commit()
    import backend.database
    await backend.database.init_db()
    async with aiosqlite.connect(test_db) as db:
        for table, cols in (("file_events", {"summary_key", "summary_params"}),
                            ("jobs", {"error_key", "error_params"})):
            async with db.execute(f"PRAGMA table_info({table})") as cur:
                have = {r[1] for r in await cur.fetchall()}
            assert cols <= have, f"{table} missing {cols - have}"


@pytest.mark.asyncio
async def test_log_event_stores_and_api_returns_key(test_db, monkeypatch):
    import backend.file_events as fe
    monkeypatch.setattr(fe, "DB_PATH", test_db)
    await fe.log_event("/m/a.mkv", fe.EVENT_COMPLETED, "Converted: saved 0.90 GB (70%)",
                       {"job_id": 1}, summary_key="convertedSaved",
                       summary_params={"gb": "0.90", "pct": "70"})
    await fe.log_event("/m/a.mkv", fe.EVENT_UNIGNORED, "Unignored")  # legacy-style, no key
    await fe.log_events_bulk([
        ("/m/b.mkv", fe.EVENT_QUEUED, "Queued for convert", None, "queuedForJobType", {"jobType": "convert"}),
        ("/m/c.mkv", fe.EVENT_QUEUED, "Queued for audio", None),  # old 4-tuple still accepted
    ])

    from backend.routes.activity import activity_feed, file_history
    feed = await activity_feed(event_type=None, search=None, since=None, until=None, limit=100, offset=0)
    by_summary = {e["summary"]: e for e in feed["events"]}
    ev = by_summary["Converted: saved 0.90 GB (70%)"]
    assert ev["summary_key"] == "convertedSaved"
    assert ev["summary_params"] == {"gb": "0.90", "pct": "70"}
    assert by_summary["Unignored"]["summary_key"] is None
    assert by_summary["Unignored"]["summary_params"] is None
    assert by_summary["Queued for convert"]["summary_params"] == {"jobType": "convert"}
    assert by_summary["Queued for audio"]["summary_key"] is None

    hist = await file_history(path="/m/a.mkv", limit=100)
    assert {e["summary_key"] for e in hist["events"]} == {"convertedSaved", None}


@pytest.mark.asyncio
async def test_update_status_stores_error_key_and_list_parses_params(test_db):
    from backend.queue import JobQueue
    q = JobQueue(test_db)
    jid = await q.add_job("/m/x.mkv", "convert")
    await q.update_status(jid, "failed", error_log="ffmpeg exited with code 1\n\nboom",
                          error_key="errors.ffmpegExited", error_params={"code": "1"})
    [row] = await q.get_jobs_by_status("failed")
    assert row["error_key"] == "errors.ffmpegExited"
    assert row["error_params"] == {"code": "1"}
    assert row["error_log"].startswith("ffmpeg exited with code 1")
    # A later status change without a key clears it (no stale translation).
    await q.update_status(jid, "running")
    [row] = await q.get_all_jobs()
    assert row["error_key"] is None and row["error_params"] is None


@pytest.mark.asyncio
async def test_job_progress_carries_step_key(monkeypatch):
    from backend.websocket import ConnectionManager
    mgr = ConnectionManager()
    sent: list[dict] = []

    async def capture(msg):
        sent.append(msg)
    monkeypatch.setattr(mgr, "broadcast", capture)
    common = dict(file_name="f.mkv", progress=100.0, fps=None, eta=None,
                  jobs_completed=0, jobs_total=0, total_saved=0)
    await mgr.send_job_progress(job_id=1, step="removing tracks", **common)
    await mgr.send_job_progress(job_id=2, step="health-check (quick)", step_key="steps.healthCheckMode",
                                step_params={"mode": "quick"}, **common)
    await mgr.send_job_progress(job_id=3, step="something new", **common)
    assert sent[0]["step"] == "removing tracks" and sent[0]["step_key"] == "steps.removingTracks"
    assert sent[1]["step_key"] == "steps.healthCheckMode" and sent[1]["step_params"] == {"mode": "quick"}
    assert "step_key" not in sent[2] and sent[2]["step"] == "something new"


@pytest.mark.asyncio
async def test_detect_progress_carries_stage_key(monkeypatch):
    from backend.websocket import ConnectionManager
    mgr = ConnectionManager()
    sent: list[dict] = []

    async def capture(msg):
        sent.append(msg)
    monkeypatch.setattr(mgr, "broadcast", capture)
    await mgr.send_detect_progress("/m/f.mkv", "OCR (Latin) on subtitle track 3…",
                                   stage_key="detect.ocrLatin", stage_params={"track": 3})
    await mgr.send_detect_progress("/m/f.mkv", "legacy stage")
    assert sent[0]["stage_key"] == "detect.ocrLatin" and sent[0]["stage_params"] == {"track": 3}
    assert _render_en("serverJobs", sent[0]["stage_key"], sent[0]["stage_params"]) == sent[0]["stage"]
    assert "stage_key" not in sent[1] and sent[1]["stage"] == "legacy stage"


@pytest.mark.asyncio
async def test_image_ocr_reports_stage_keys(monkeypatch):
    """Every stage detect_image_sub_language reports carries a key whose
    English render equals the English stage text."""
    import backend.image_sub_ocr as io

    async def fake_extract(fp, idx, workdir, sample_seconds=None):
        return "/tmp/x.sup"
    monkeypatch.setattr(io, "_extract_sup", fake_extract)
    monkeypatch.setattr(io, "_pgs_sample_seconds", lambda: 60)
    monkeypatch.setattr(io, "_pgsrip_to_text", lambda sup, langs: "")
    seen: list[tuple] = []

    async def cb(stage, stage_key=None, stage_params=None):
        seen.append((stage, stage_key, stage_params))
    assert await io.detect_image_sub_language("/m/f.mkv", 4, "hdmv_pgs_subtitle", progress_cb=cb) == (None, 0.0)
    assert len(seen) >= 4
    for stage, key, params in seen:
        assert key and _render_en("serverJobs", key, params) == stage
