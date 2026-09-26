"""v0.9.132: per-track language-detection notes carry message codes.

Notes are created as ``KeyedNote`` (a ``str`` subclass carrying ``key``/``params``)
so existing callers still get plain English text while the UI can translate via
``serverJobs:detectNotes.<key>``. These tests keep backend and catalogs in sync.
"""
import ast
import json
import re
from pathlib import Path

from backend.language_detection import KeyedNote

ROOT = Path(__file__).resolve().parents[2]
LOCALES = ROOT / "frontend" / "src" / "i18n" / "locales"
BACKEND = ROOT / "backend"


def _catalog(lang: str) -> dict:
    return json.loads((LOCALES / lang / "serverJobs.json").read_text())["detectNotes"]


def _placeholders(s: str) -> set[str]:
    return set(re.findall(r"\{\{\s*(\w+)\s*\}\}", s))


def _keyednote_calls():
    """Yield (file, text_node, key) for every KeyedNote(<text>, "<key>", ...) call."""
    for path in list(BACKEND.glob("*.py")) + list((BACKEND / "routes").glob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "KeyedNote" and len(node.args) >= 2
                    and isinstance(node.args[1], ast.Constant) and isinstance(node.args[1].value, str)):
                yield path.name, node.args[0], node.args[1].value


def test_keyednote_is_still_plain_english_str():
    n = KeyedNote("no speech detected", "noSpeech")
    assert n == "no speech detected" and isinstance(n, str)
    assert n.key == "noSpeech" and n.params == {}
    assert json.dumps({"note": n}) == '{"note": "no speech detected"}'


def test_every_emitted_key_is_in_both_catalogs_with_matching_placeholders():
    en, es = _catalog("en"), _catalog("es")
    calls = list(_keyednote_calls())
    assert len(calls) >= 12, "expected all detection notes to be keyed"
    for fname, _text, key in calls:
        assert key in en, f"{fname}: detectNotes.{key} missing in en"
        assert key in es, f"{fname}: detectNotes.{key} missing in es"
        assert _placeholders(en[key]) == _placeholders(es[key]), key


def test_literal_notes_render_back_to_the_server_english():
    en = _catalog("en")
    for fname, text, key in _keyednote_calls():
        if isinstance(text, ast.Constant):  # literal English, no params
            assert en[key] == text.value, f"{fname}: en detectNotes.{key} drifted from server text"


def test_catalogs_have_no_unused_or_mismatched_keys():
    en, es = _catalog("en"), _catalog("es")
    emitted = {key for _f, _t, key in _keyednote_calls()}
    assert set(en) == set(es) == emitted
