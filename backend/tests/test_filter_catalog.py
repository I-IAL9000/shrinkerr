"""The Scanner's filter bar layout (frontend/src/filterCatalog.json) against
the server's filters: every section is one server group, so ticking more in
a section shows more and different sections narrow; every filter is shown
once or listed as hidden; every label exists in English and Spanish, and
every filter bar string the frontend code asks for exists."""
import json
import re
from pathlib import Path

from backend.scan_filters import FILTERS

ROOT = Path(__file__).resolve().parents[2]
CATALOG = json.loads((ROOT / "frontend/src/filterCatalog.json").read_text())
LOCALES = ROOT / "frontend/src/i18n/locales"
_PLACEHOLDER = re.compile(r"\{\{\s*(\w+)")


def _options():
    for category in CATALOG["categories"]:
        for section in category["sections"]:
            for option in section.get("options", []):
                yield category, section, option


def _lists():
    # sections whose values come from the server: a list (alang:fre, genre:drama)
    for category in CATALOG["categories"]:
        for section in category["sections"]:
            if "facet" in section:
                yield category, section


def _lookup(lang: str, key: str):
    ns, _, path = key.partition(":")
    node = json.loads((LOCALES / lang / f"{ns}.json").read_text())
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def test_section_keys_are_unique():
    # two sections with one key would be one server group: they'd widen together
    keys = [section["key"] for category in CATALOG["categories"] for section in category["sections"]]
    assert len(keys) == len(set(keys))


def test_each_section_is_one_server_group():
    for _, section, option in _options():
        f = FILTERS[option["key"]]
        assert (f.group or f"_{f.id}") == section["key"], (section["key"], option["key"])


def test_no_hidden_filter_shares_a_sections_group():
    # it would widen with that section, while its chip doesn't say where it is
    sections = {section["key"] for category in CATALOG["categories"] for section in category["sections"]}
    for hidden in CATALOG["hidden"]:
        if hidden.get("alias"):
            continue  # the bar reads it as its alias: its chip is the alias's
        f = FILTERS[hidden["key"]]
        assert (f.group or f"_{f.id}") not in sections, hidden["key"]


def test_every_filter_is_shown_once_or_hidden():
    shown = [option["key"] for _, _, option in _options()]
    hidden = [h["key"] for h in CATALOG["hidden"]]
    assert len(shown) == len(set(shown))
    assert sorted(shown + hidden) == sorted(FILTERS)
    assert set(CATALOG["quick"]) <= set(shown)


def test_every_label_exists_in_english_and_spanish():
    keys = set()
    for category in CATALOG["categories"]:
        keys.add(category["labelKey"])
        for section in category["sections"]:
            keys |= {section["labelKey"], section.get("hintKey"), section.get("chipLabelKey")}
    for category, section, option in _options():
        keys.add(option.get("labelKey"))
        assert (option.get("label") or "").strip() or option.get("labelKey"), option["key"]
    for hidden in CATALOG["hidden"]:
        keys.add(hidden.get("labelKey"))
        assert (hidden.get("label") or "").strip() or hidden.get("labelKey"), hidden["key"]
    keys.discard(None)
    for lang in ("en", "es"):
        missing = [k for k in sorted(keys) if not (isinstance(_lookup(lang, k), str) and _lookup(lang, k).strip())]
        assert not missing, (lang, missing)


def test_list_sections_are_the_servers_lists():
    from backend.scan_filters import FACETS
    lists = [section for _, section in _lists()]
    assert sorted(s["facet"] for s in lists) == sorted(FACETS)  # every list shown once
    for s in lists:
        assert s["key"] == s["facet"]  # the section is the list's group
    groups = {f.group for f in FILTERS.values() if f.group}
    assert not groups & set(FACETS)  # no fixed filter shares a list's group


def test_hidden_aliases_point_at_something_shown():
    shown = {o["key"] for _, _, o in _options()} | {s["facet"] for _, s in _lists()}
    for hidden in CATALOG["hidden"]:
        alias = hidden.get("alias")
        if alias:
            assert alias.split(":")[0] in shown, alias


def test_list_value_labels_exist_in_english_and_spanish():
    keys = [f"scanner:facets.channels.{v}" for v in ("1", "2", "6", "8")]
    keys += [f"scanner:facets.rating.{v}" for v in ("8", "7", "6", "5", "lt5", "none")]
    keys += [f"scanner:facets.status.{v}" for v in ("returning", "ended", "canceled", "in_production", "planned", "pilot")]
    keys += ["scanner:facets.unknownLanguage", "scanner:facets.searchIn",
             "scanner:facets.showAll", "scanner:facets.showFewer", "scanner:facets.noMatches",
             "scanner:facets.none"]
    for lang in ("en", "es"):
        for key in keys:
            assert _lookup(lang, key), (lang, key)


def test_the_bar_strings_match_in_english_and_spanish():
    def leaves(node, prefix=""):
        for k, v in node.items():
            if isinstance(v, dict):
                yield from leaves(v, f"{prefix}{k}.")
            else:
                yield f"{prefix}{k}", v
    en = dict(leaves(_lookup("en", "scanner:filterBar")))
    es = dict(leaves(_lookup("es", "scanner:filterBar")))
    expected = set(en) | {k[: -len("_other")] + "_many" for k in en if k.endswith("_other")}
    assert set(es) == expected
    for key, text in en.items():
        assert set(_PLACEHOLDER.findall(text)) == set(_PLACEHOLDER.findall(es[key])), key
        if key.endswith("_other"):
            many = key[: -len("_other")] + "_many"
            assert set(_PLACEHOLDER.findall(text)) == set(_PLACEHOLDER.findall(es[many])), many


def test_every_bar_string_the_code_uses_exists_in_both_languages():
    src = ROOT / "frontend/src"
    refs = {}  # key -> a file that uses it; "…$" or "….": built at run time, or a sentence's full stop
    for path in [*src.rglob("*.ts"), *src.rglob("*.tsx")]:
        for m in re.finditer(r"scanner:filterBar\.([\w.]*\$?)", path.read_text()):
            refs.setdefault(m.group(1), path.name)
    assert refs, "no scanner:filterBar.* reference found: is the pattern or the path wrong?"
    plurals = {"en": ("_one", "_other"), "es": ("_one", "_many", "_other")}
    for ref, where in sorted(refs.items()):
        for lang, suffixes in plurals.items():
            key = "scanner:filterBar." + ref.rstrip("$").rstrip(".")
            if ref.endswith(("$", ".")):
                assert isinstance(_lookup(lang, key), (dict, str)), (lang, ref, where)
            else:
                found = isinstance(_lookup(lang, key), str) or all(
                    isinstance(_lookup(lang, key + s), str) for s in suffixes
                )
                assert found, (lang, ref, where)
