"""The Scanner's filter bar layout (frontend/src/filterCatalog.json) against
the server's filters: every section is one server group, so ticking more in
a section shows more and different sections narrow; every filter is shown
once or listed as hidden; every label exists in English and Spanish."""
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
            for option in section["options"]:
                yield category, section, option


def _lookup(lang: str, key: str):
    ns, _, path = key.partition(":")
    node = json.loads((LOCALES / lang / f"{ns}.json").read_text())
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def test_each_section_is_one_server_group():
    for _, section, option in _options():
        f = FILTERS[option["key"]]
        assert (f.group or f"_{f.id}") == section["key"], (section["key"], option["key"])


def test_every_filter_is_shown_once_or_hidden():
    shown = [option["key"] for _, _, option in _options()]
    hidden = [h["key"] for h in CATALOG["hidden"]]
    assert len(shown) == len(set(shown))
    assert sorted(shown + hidden) == sorted(FILTERS)
    assert set(CATALOG["quick"]) <= set(shown)


def test_every_label_exists_in_english_and_spanish():
    keys = set()
    for category, section, option in _options():
        keys |= {category["labelKey"], section["labelKey"], section.get("hintKey"), option.get("labelKey")}
        assert option.get("label") or option.get("labelKey"), option["key"]
    keys |= {h["labelKey"] for h in CATALOG["hidden"]}
    keys.discard(None)
    for lang in ("en", "es"):
        missing = [k for k in sorted(keys) if not isinstance(_lookup(lang, k), str)]
        assert not missing, (lang, missing)


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
