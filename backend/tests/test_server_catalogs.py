"""Server-provided catalogs the UI translates by stable id: every entry must
have a UI catalog string (en + es) and the English must equal the server's
English, so a new property / token / reason can't ship untranslated.

  search PROPERTIES (routes/search.py)   -> serverSearch  properties.<id>.label,
                                             groups.<slug>, properties.<id>.options.<v>
  rename TOKEN_CATEGORIES (rename.py)    -> serverRename  categories.<slug>, tokens.<slug>.<token>
  NVENC_REASON_CODES (nodes.py)          -> serverJobs    monitor.*
"""
from __future__ import annotations

import re

import pytest

from backend.tests.test_message_codes import LANGS, REPO, _leaves, _load, _lookup, _render_en

_PLACEHOLDER_RE = re.compile(r"\{\{\s*([\w.]+)\s*\}\}")


def _en_es_match(ns: str):
    en = dict(_leaves(_load("en", ns)))
    es = dict(_leaves(_load("es", ns)))
    assert set(en) == set(es), f"{ns}: only in en={sorted(set(en) - set(es))} only in es={sorted(set(es) - set(en))}"
    for key, text in en.items():
        assert isinstance(text, str) and text.strip() and es[key].strip(), f"{ns}:{key} empty"
        assert set(_PLACEHOLDER_RE.findall(text)) == set(_PLACEHOLDER_RE.findall(es[key])), f"{ns}:{key}"


def _group_slugs_in_modal() -> dict[str, str]:
    src = (REPO / "frontend" / "src" / "components" / "AdvancedSearchModal.tsx").read_text(encoding="utf-8")
    block = re.search(r"const GROUP_SLUGS[^{]*\{(.*?)\};", src, re.S).group(1)
    return dict(re.findall(r"(\w+):\s*\"(\w+)\"", block))


@pytest.mark.parametrize("lang", LANGS)
def test_search_properties_translated(lang):
    from backend.routes.search import PROPERTIES
    cat = _load(lang, "serverSearch")
    slugs = _group_slugs_in_modal()
    for pid, p in PROPERTIES.items():
        assert isinstance(_lookup(cat, f"properties.{pid}.label"), str), f"{lang}: properties.{pid}.label"
        group = p.get("group", "Other")
        assert group in slugs, f"AdvancedSearchModal GROUP_SLUGS lacks {group!r}"
        assert isinstance(_lookup(cat, f"groups.{slugs[group]}"), str), f"{lang}: groups.{slugs[group]}"
        for value in (p.get("option_labels") or {}):
            assert isinstance(_lookup(cat, f"properties.{pid}.options.{value}"), str), f"{lang}: {pid} option {value}"
    assert isinstance(_lookup(cat, f"groups.{slugs['Other']}"), str)  # list_properties() default group


def test_search_english_matches_server():
    from backend.routes.search import PROPERTIES
    cat = _load("en", "serverSearch")
    slugs = _group_slugs_in_modal()
    for pid, p in PROPERTIES.items():
        assert _lookup(cat, f"properties.{pid}.label") == p["label"], pid
        assert _lookup(cat, f"groups.{slugs[p['group']]}") == p["group"], pid
        for value, label in (p.get("option_labels") or {}).items():
            assert _lookup(cat, f"properties.{pid}.options.{value}") == label
        # Catalog options are word-like values only; each must be a real option.
        for value in (_lookup(cat, f"properties.{pid}.options") or {}):
            assert value in [str(o) for o in p.get("options") or []], f"{pid}: stale option {value}"
    assert set(cat["properties"]) == set(PROPERTIES), "serverSearch has stale property ids"


@pytest.mark.parametrize("lang", LANGS)
def test_rename_tokens_translated(lang):
    from backend.rename import TOKEN_CATEGORIES
    cat = _load(lang, "serverRename")
    for c in TOKEN_CATEGORIES:
        slug = c["category"].lower()  # RenamingSettings keys categories by lowercase name
        assert isinstance(_lookup(cat, f"categories.{slug}"), str), f"{lang}: categories.{slug}"
        for tok in c["tokens"]:
            assert isinstance(_lookup(cat, f"tokens.{slug}.{tok['token']}"), str), f"{lang}: tokens.{slug}.{tok['token']}"


def test_rename_english_matches_server():
    from backend.rename import TOKEN_CATEGORIES
    cat = _load("en", "serverRename")
    for c in TOKEN_CATEGORIES:
        slug = c["category"].lower()
        assert cat["categories"][slug] == c["category"]
        assert cat["tokens"][slug] == {t["token"]: t["desc"] for t in c["tokens"]}, slug
    assert set(cat["categories"]) == {c["category"].lower() for c in TOKEN_CATEGORIES}


@pytest.mark.parametrize("ns", ["serverSearch", "serverRename"])
def test_en_es_catalogs_match(ns):
    _en_es_match(ns)


# Sample reasons as the two _detect_capabilities implementations build them.
_NVENC_SAMPLES = [
    "ffmpeg build has no hevc_nvenc encoder",
    "ffmpeg exited 1",
    "NVENC test crashed: [Errno 2] No such file",
    "ffmpeg not runnable: timed out",
    "capabilities forced via CAPABILITIES env var",
]


def test_nvenc_reason_codes():
    from backend.nodes import NVENC_REASON_CODES, nvenc_reason_code
    covered = set()
    for reason in _NVENC_SAMPLES:
        code = nvenc_reason_code(reason)
        key = code["nvenc_unavailable_reason_key"]
        covered.add(key)
        for lang in LANGS:
            assert isinstance(_lookup(_load(lang, "serverJobs"), key), str), f"{lang}: serverJobs:{key}"
        assert _render_en("serverJobs", key, code.get("nvenc_unavailable_reason_params") or {}) == reason
    assert covered == {k for _, k in NVENC_REASON_CODES}
    # Raw ffmpeg stderr tail: no key, shown verbatim.
    assert nvenc_reason_code("[hevc_nvenc @ 0x1] Driver does not support the required nvenc API version.") == {}
    assert nvenc_reason_code(None) == {}


def test_nvenc_literals_still_emitted():
    """The compute sites must still produce the literals the codes match."""
    for f in ("nodes.py", "worker_mode.py"):
        src = (REPO / "backend" / f).read_text(encoding="utf-8")
        for literal in ('"ffmpeg build has no hevc_nvenc encoder"', 'f"ffmpeg exited {test.returncode}"',
                        'f"NVENC test crashed: {exc}"', 'f"ffmpeg not runnable: {exc}"'):
            assert literal in src, f"{f} no longer emits {literal}"
    assert '"capabilities forced via CAPABILITIES env var"' in (REPO / "backend" / "worker_mode.py").read_text()
