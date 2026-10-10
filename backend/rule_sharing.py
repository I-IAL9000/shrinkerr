"""Shareable rules (v0.10.0): export the encoding rules — with the saved
views they use — as YAML, import such a file, or add a ready-made rule from
the bundled library (rule_library.yaml).

Format (version 1):

    shrinkerr_rules: 1
    rules:
      - name: Anime in AV1
        action: encode            # encode | ignore | skip
        enabled: true
        match: any                # any | all
        conditions:
          - {type: content_type, operator: is, value: anime}
        settings: {output_codec: av1, vmaf_min_score: 93}
    saved_views:                  # saved_view conditions name them
      - {name: Big x264, filter: "codec:x264 size:>10GB"}
"""
from __future__ import annotations

from pathlib import Path

import yaml

from backend.api_errors import ApiError
from backend.database import connect_db

VERSION = 1
LIBRARY = Path(__file__).parent / "rule_library.yaml"
MAX_RULES, MAX_CONDITIONS = 500, 100
# The rule fields besides its name, action, enabled flag and conditions.
SETTING_FIELDS = ("encoder", "nvenc_preset", "nvenc_cq", "libx265_crf", "libx265_preset", "output_codec",
                  "target_resolution", "audio_codec", "audio_bitrate", "queue_priority",
                  "audio_keep_languages", "sub_policy", "sub_keep_languages", "originals_policy",
                  "originals_days", "vmaf_min_score", "min_savings_pct")


def _invalid(error: str) -> ApiError:
    return ApiError(400, f"Not a valid rule: {error}", code="rules.import.invalid", params={"error": error})


async def export_rules() -> str:
    """Every rule, in order, and the saved views they use, as YAML."""
    from backend.rule_resolver import _parse_rule_conditions
    db = await connect_db()
    try:
        async with db.execute("SELECT * FROM encoding_rules ORDER BY priority ASC, id ASC") as cur:
            rows = [dict(r) for r in await cur.fetchall()]
        async with db.execute("SELECT id, name, filter FROM saved_views") as cur:
            views = {str(r["id"]): dict(r) for r in await cur.fetchall()}
    finally:
        await db.close()
    used: dict[str, dict] = {}
    rules = []
    for row in rows:
        mode, conds = _parse_rule_conditions(row)
        out = []
        for c in conds:
            if not isinstance(c, dict):
                continue
            c = {k: c[k] for k in ("type", "operator", "value") if c.get(k) is not None}
            if c.get("type") == "saved_view":
                view = views.get(str(c.get("value")))
                if view is None:
                    continue  # the view is gone: the condition matched nothing
                used[str(view["id"])] = view
                c["value"] = view["name"]
            out.append(c)
        rule = {"name": row["name"], "action": row["action"], "enabled": bool(row["enabled"]), "match": mode,
                "conditions": out}
        settings = {k: row[k] for k in SETTING_FIELDS if row.get(k) is not None}
        if settings:
            rule["settings"] = settings
        rules.append(rule)
    doc = {"shrinkerr_rules": VERSION, "rules": rules}
    if used:
        doc["saved_views"] = [{"name": v["name"], "filter": v["filter"]} for v in used.values()]
    return ("# Shrinkerr rules: import them in Settings → Automation → Rules\n"
            + yaml.safe_dump(doc, sort_keys=False, allow_unicode=True))


def parse(text: str) -> dict:
    """The rules file's contents, or an ApiError saying what's wrong."""
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ApiError(400, f"This isn't a YAML file: {str(exc).splitlines()[0]}", code="rules.import.notYaml",
                       params={"error": str(exc).splitlines()[0]})
    if not isinstance(doc, dict) or doc.get("shrinkerr_rules") != VERSION or not isinstance(doc.get("rules"), list):
        raise ApiError(400, "This isn't a Shrinkerr rules file.", code="rules.import.notRulesFile")
    if len(doc["rules"]) > MAX_RULES:
        raise ApiError(400, f"A rules file can hold up to {MAX_RULES} rules.", code="rules.import.tooMany",
                       params={"max": MAX_RULES})
    return doc


def _payload(item, view_ids: dict[str, int]):
    """The file's rule as a checked RuleCreate (its saved views by id), or
    an ApiError saying what's wrong with it."""
    from pydantic import ValidationError
    from backend.routes import rules as route
    if not isinstance(item, dict) or not str(item.get("name") or "").strip():
        raise ApiError(400, "A rule has no name.", code="rules.import.nameRequired")
    conds = item.get("conditions") or []
    if not isinstance(conds, list) or len(conds) > MAX_CONDITIONS:
        raise _invalid(f"conditions (a list of up to {MAX_CONDITIONS})")
    try:
        out = []
        for c in conds:
            c = dict(c)
            if c.get("type") == "saved_view":
                view = str(c.get("value"))
                if view not in view_ids:
                    raise ApiError(400, f"The saved view {view} is missing.", code="rules.import.missingView",
                                   params={"view": view})
                c["value"] = view_ids[view]
            c["value"] = str(c.get("value", ""))
            out.append(route.MatchCondition(**c))
        payload = route.RuleCreate(
            name=str(item["name"]).strip(), match_mode=item.get("match") or "any", match_conditions=out,
            action=item.get("action") or "encode", enabled=bool(item.get("enabled", True)),
            **{k: v for k, v in dict(item.get("settings") or {}).items() if k in SETTING_FIELDS})
    except ValidationError as exc:
        err = exc.errors()[0]
        raise _invalid(f"{'.'.join(str(p) for p in err.get('loc', ())) or '?'}: {err.get('msg')}")
    except (TypeError, ValueError) as exc:
        raise _invalid(str(exc))
    if payload.match_mode not in ("any", "all"):
        raise _invalid(f"match: {payload.match_mode}")
    route._check_rule(payload)
    return payload


async def import_rules(doc: dict) -> dict:
    """Add the file's rules after the existing ones; one named like an
    existing rule replaces it where it is. A saved view the file brings is
    created unless one of that name exists (which is used instead).
    {"added", "replaced", "errors": [{"name", "error", "code", "params"}]}."""
    from backend.routes import rules as route
    from backend.routes.views import SaveViewRequest, save_view
    db = await connect_db()
    try:
        async with db.execute("SELECT id, name FROM saved_views") as cur:
            view_ids = {r["name"]: r["id"] for r in await cur.fetchall()}
        async with db.execute("SELECT id, name FROM encoding_rules") as cur:
            rule_ids = {r["name"]: r["id"] for r in await cur.fetchall()}
    finally:
        await db.close()
    for view in doc.get("saved_views") or []:
        if isinstance(view, dict) and view.get("name") and str(view["name"]) not in view_ids:
            try:
                created = await save_view(SaveViewRequest(name=str(view["name"]), filter=str(view.get("filter") or "")))
            except ApiError:
                continue  # an empty filter: its rules say the view is missing
            view_ids[created["name"]] = created["id"]
    added = replaced = 0
    errors: list[dict] = []
    for item in doc["rules"]:
        name = str(item.get("name") or "").strip() if isinstance(item, dict) else ""
        try:
            payload = _payload(item, view_ids)
        except ApiError as exc:
            errors.append({"name": name, "error": exc.detail, "code": exc.code, "params": exc.params})
            continue
        if payload.name in rule_ids:
            fields = payload.model_dump(exclude={"name"})
            await route.update_rule(rule_ids[payload.name], route.RuleUpdate(**fields))
            replaced += 1
        else:
            rule_ids[payload.name] = (await route.create_rule(payload))["id"]
            added += 1
    return {"added": added, "replaced": replaced, "errors": errors}


def library() -> list[dict]:
    """The bundled ready-made rules: [{"id", "rule"}]."""
    return yaml.safe_load(LIBRARY.read_text(encoding="utf-8"))["presets"]


async def import_preset(preset_id: str, name: str) -> dict | None:
    """Add the library's rule under `name` (the UI's translated title)."""
    preset = next((p for p in library() if p["id"] == preset_id), None)
    if preset is None:
        return None
    return await import_rules({"shrinkerr_rules": VERSION, "rules": [{**preset["rule"], "name": name}]})
