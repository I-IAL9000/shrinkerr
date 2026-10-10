"""Shareable rules (v0.10.0): the rules export as a YAML file (with the
saved views they use, by name), import back — replacing same-name rules in
place — and the bundled library's ready-made rules import cleanly."""
import json

import aiosqlite
import pytest
import pytest_asyncio
import yaml

from backend.api_errors import ApiError
from backend.tests.test_message_codes import _load, _lookup, _render_en


async def _rules(db_path):
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM encoding_rules ORDER BY priority, id") as cur:
            return [dict(r) for r in await cur.fetchall()]


def _comparable(rule):
    out = {k: v for k, v in rule.items() if k not in ("id", "priority", "created_at", "match_type", "match_value")}
    out["match_conditions"] = json.loads(out["match_conditions"])
    return out


@pytest.fixture
def route(test_db):
    from backend.routes import rules
    return rules


@pytest_asyncio.fixture
async def two_rules(route):
    from backend.routes.views import SaveViewRequest, save_view
    view = await save_view(SaveViewRequest(name="Big x264", filter="x264"))
    await route.create_rule(route.RuleCreate(
        name="Anime", match_mode="all",
        match_conditions=[route.MatchCondition(type="content_type", value="anime"),
                          route.MatchCondition(type="saved_view", operator="is_not", value=str(view["id"]))],
        output_codec="av1", libx265_crf=24, sub_policy="languages", sub_keep_languages="eng,jpn",
        originals_policy="keep", originals_days=30, vmaf_min_score=92.5))
    await route.create_rule(route.RuleCreate(
        name="Samples", action="skip", enabled=False,
        match_conditions=[route.MatchCondition(type="title", operator="contains", value="sample")]))
    return view


@pytest.mark.asyncio
async def test_export_and_import_round_trip(test_db, route, two_rules):
    from backend.rule_sharing import export_rules, import_rules, parse
    before = [_comparable(r) for r in await _rules(test_db)]
    text = await export_rules()
    doc = yaml.safe_load(text)
    anime = doc["rules"][0]
    assert anime["conditions"][1] == {"type": "saved_view", "operator": "is_not", "value": "Big x264"}  # by name
    assert anime["settings"] == {"libx265_crf": 24, "output_codec": "av1", "sub_policy": "languages",
                                 "sub_keep_languages": "eng,jpn", "originals_policy": "keep", "originals_days": 30,
                                 "vmaf_min_score": 92.5}
    assert "settings" not in doc["rules"][1] and doc["saved_views"] == [{"name": "Big x264", "filter": "x264"}]
    async with aiosqlite.connect(test_db) as db:  # another install
        await db.execute("DELETE FROM encoding_rules")
        await db.execute("DELETE FROM saved_views")
        await db.commit()
    assert await import_rules(parse(text)) == {"added": 2, "replaced": 0, "errors": []}
    after = await _rules(test_db)
    async with aiosqlite.connect(test_db) as db:
        async with db.execute("SELECT id FROM saved_views WHERE name = 'Big x264'") as cur:
            new_view = (await cur.fetchone())[0]
    expected = json.loads(json.dumps(before).replace(f'"value": "{two_rules["id"]}"', f'"value": "{new_view}"'))
    assert [_comparable(r) for r in after] == expected


@pytest.mark.asyncio
async def test_a_same_name_rule_is_replaced_in_place(test_db, route, two_rules):
    from backend.rule_sharing import import_rules
    priorities = {r["name"]: r["priority"] for r in await _rules(test_db)}
    result = await import_rules({"shrinkerr_rules": 1, "saved_views": [{"name": "Big x264", "filter": "x265"}], "rules": [
        {"name": "Anime", "conditions": [{"type": "content_type", "operator": "is", "value": "animation"}],
         "settings": {"min_savings_pct": 15}},
        {"name": "New", "action": "ignore", "conditions": [{"type": "file_size", "operator": "less_than", "value": 1},
                                                            {"type": "saved_view", "value": "Big x264"}]},
    ]})
    assert result == {"added": 1, "replaced": 1, "errors": []}
    rules = {r["name"]: r for r in await _rules(test_db)}
    anime = rules["Anime"]
    assert anime["priority"] == priorities["Anime"] and anime["min_savings_pct"] == 15
    assert (anime["output_codec"], anime["originals_policy"], anime["vmaf_min_score"]) == (None, None, None)
    assert json.loads(anime["match_conditions"]) == {
        "match_mode": "any", "conditions": [{"type": "content_type", "operator": "is", "value": "animation"}]}
    assert rules["New"]["priority"] > priorities["Samples"] and rules["New"]["action"] == "ignore"
    new_conds = json.loads(rules["New"]["match_conditions"])["conditions"]
    assert (new_conds[0]["value"], new_conds[1]["value"]) == ("1", str(two_rules["id"]))  # the view you have
    async with aiosqlite.connect(test_db) as db:
        async with db.execute("SELECT filter FROM saved_views") as cur:
            assert [r[0] for r in await cur.fetchall()] == ["x264"]  # not overwritten


@pytest.mark.asyncio
async def test_a_deleted_views_condition_is_left_out(test_db, route):
    from backend.rule_sharing import export_rules
    await route.create_rule(route.RuleCreate(name="Gone", match_conditions=[
        route.MatchCondition(type="saved_view", value="999"), route.MatchCondition(type="directory", value="/m")]))
    doc = yaml.safe_load(await export_rules())
    assert doc["rules"][0]["conditions"] == [{"type": "directory", "operator": "is", "value": "/m"}]
    assert "saved_views" not in doc


@pytest.mark.asyncio
async def test_bad_rules_are_reported_and_the_rest_imported(test_db, route):
    from backend.rule_sharing import import_rules
    cond = [{"type": "directory", "value": "/m"}]
    result = await import_rules({"shrinkerr_rules": 1, "saved_views": [{"name": "Empty", "filter": ""}], "rules": [
        {"name": "Fine", "conditions": cond},
        {"name": "Newer", "conditions": cond,  # a newer version's settings are left out
         "settings": {"min_savings_pct": 10, "action": "skip", "some_new_setting": 1}},
        {"conditions": cond},
        {"name": "Odd type", "conditions": [{"type": "colour", "value": "red"}]},
        {"name": "Odd policy", "conditions": cond, "settings": {"sub_policy": "some"}},
        {"name": "No view", "conditions": [{"type": "saved_view", "value": "Empty"}]},
        {"name": "Odd action", "action": "delete", "conditions": cond},
        {"name": "Odd match", "match": "most", "conditions": cond},
        {"name": "No conditions", "conditions": []},
        {"name": "Not a list", "conditions": "directory"},
        "just text",
    ]})
    assert (result["added"], result["replaced"]) == (2, 0)
    codes = [(e["name"], e["code"]) for e in result["errors"]]
    assert codes == [("", "rules.import.nameRequired"), ("Odd type", "rules.invalidMatchType"),
                     ("Odd policy", "rules.import.invalid"), ("No view", "rules.import.missingView"),
                     ("Odd action", "rules.invalidAction"), ("Odd match", "rules.import.invalid"),
                     ("No conditions", "rules.conditionRequired"), ("Not a list", "rules.import.invalid"),
                     ("", "rules.import.nameRequired")]
    for e in result["errors"]:  # the UI translates them
        assert _render_en("serverApi", e["code"], e["params"]) == e["error"]
        assert isinstance(_lookup(_load("es", "serverApi"), e["code"]), str)
    assert [(r["name"], r["action"], r["min_savings_pct"]) for r in await _rules(test_db)] == [
        ("Fine", "encode", None), ("Newer", "encode", 10)]


def test_only_rules_files_are_read():
    from backend.rule_sharing import MAX_RULES, parse
    for text, code in (("a: [b", "rules.import.notYaml"), ("- 1\n- 2", "rules.import.notRulesFile"),
                       ("shrinkerr_rules: 2\nrules: []", "rules.import.notRulesFile"),
                       ("shrinkerr_rules: 1\nrules: {}", "rules.import.notRulesFile"),
                       (yaml.safe_dump({"shrinkerr_rules": 1, "rules": [{}] * (MAX_RULES + 1)}), "rules.import.tooMany")):
        with pytest.raises(ApiError) as exc:
            parse(text)
        assert exc.value.code == code
    assert parse("shrinkerr_rules: 1\nrules: []")["rules"] == []


@pytest.mark.asyncio
async def test_the_library(test_db, route):
    from backend.rule_sharing import library
    presets = library()
    assert len(presets) >= 5
    for lang in ("en", "es"):
        cat = _load(lang, "settingsIntegrations")
        for p in presets:  # named and described in the UI
            assert isinstance(_lookup(cat, f"rules.library.presets.{p['id']}.name"), str), (lang, p["id"])
            assert isinstance(_lookup(cat, f"rules.library.presets.{p['id']}.description"), str), (lang, p["id"])
    for p in presets:
        result = await route.import_library_rule(p["id"], route.LibraryImportRequest(name=f" {p['id']} "))
        assert result == {"added": 1, "replaced": 0, "errors": []}, p["id"]
    assert [r["name"] for r in await _rules(test_db)] == [p["id"] for p in presets]
    again = await route.import_library_rule(presets[0]["id"], route.LibraryImportRequest(name=presets[0]["id"]))
    assert again["replaced"] == 1 and len(await _rules(test_db)) == len(presets)
    with pytest.raises(ApiError) as exc:
        await route.import_library_rule("nope", route.LibraryImportRequest(name="x"))
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_the_routes(test_db, route, two_rules):
    resp = await route.export_rules_file()
    assert resp.headers["content-disposition"] == 'attachment; filename="shrinkerr-rules.yaml"'
    text = resp.body.decode()
    assert text.startswith("# Shrinkerr rules") and "Big x264" in text
    assert (await route.import_rules_file(route.ImportRulesRequest(text=text)))["replaced"] == 2
    with pytest.raises(ApiError):
        await route.import_rules_file(route.ImportRulesRequest(text="hello"))
