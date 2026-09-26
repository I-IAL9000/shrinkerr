"""Tiny server-side translator for outbound text (notifications).

Catalogs live in ``backend/locales/<lang>/<namespace>.json`` (nested JSON,
i18next-style). Keys are ``"<namespace>:<dotted.path>"``, e.g.
``t("notifications:jobFailed.title")``. ``{{param}}`` placeholders are
interpolated from kwargs; a ``count`` kwarg selects a plural form
(``_one`` / ``_many`` / ``_other``). Never raises: a missing language or key
falls back to English, and a key missing even in English returns the key.
"""

import json
import re
from pathlib import Path

LOCALES_DIR = Path(__file__).parent / "locales"
DEFAULT_LANGUAGE = "en"

# Languages whose CLDR plural rules include a "many" category for integers
# (Spanish: 1.000.000 de ...). Everything else uses one/other.
_MANY_LANGS = {"es"}

_PLACEHOLDER_RE = re.compile(r"\{\{\s*(\w+)\s*\}\}")

# lang -> namespace -> nested dict. Loaded lazily, once per language.
_catalogs: dict[str, dict[str, dict]] = {}

# Cached value of the `notification_language` setting, so t(lang=None) is a
# pure in-memory read (safe in sync and async code, never touches the DB).
# Refreshed by backend.notifications whenever it reads settings, and by the
# settings save handler.
_current_language = DEFAULT_LANGUAGE


def available_languages() -> list[str]:
    """Language codes that have a backend/locales/<code>/ folder."""
    try:
        return sorted(p.name for p in LOCALES_DIR.iterdir() if p.is_dir())
    except OSError:
        return [DEFAULT_LANGUAGE]


def set_current_language(lang: str | None) -> None:
    """Update the cached `notification_language` value (unknown codes → en)."""
    global _current_language
    lang = (lang or "").strip()
    _current_language = lang if lang in available_languages() else DEFAULT_LANGUAGE


def get_current_language() -> str:
    return _current_language


def clear_cache() -> None:
    _catalogs.clear()


def _load(lang: str) -> dict[str, dict]:
    if lang in _catalogs:
        return _catalogs[lang]
    namespaces: dict[str, dict] = {}
    lang_dir = LOCALES_DIR / lang
    # Only load folders we actually ship (also blocks path traversal).
    if lang in available_languages():
        for f in sorted(lang_dir.glob("*.json")):
            try:
                namespaces[f.stem] = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                print(f"[I18N] Failed to load {f}: {exc}", flush=True)
    _catalogs[lang] = namespaces
    return namespaces


def _lookup(lang: str, ns: str, path: str) -> str | None:
    node = _load(lang).get(ns)
    for part in path.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node if isinstance(node, str) else None


def _candidates(path: str, lang: str, params: dict) -> list[str]:
    if "count" not in params:
        return [path]
    out = []
    try:
        count = params["count"]
        if count == 1:
            out.append(f"{path}_one")
        elif lang in _MANY_LANGS and count != 0 and count % 1_000_000 == 0:
            out.append(f"{path}_many")
    except Exception:
        pass
    out += [f"{path}_other", path]
    return out


def _interpolate(text: str, params: dict) -> str:
    def repl(m: re.Match) -> str:
        name = m.group(1)
        return str(params[name]) if name in params else m.group(0)
    return _PLACEHOLDER_RE.sub(repl, text)


def t(key: str, lang: str | None = None, **params) -> str:
    """Translate `key` into `lang` (default: the notification_language setting)."""
    try:
        lang = lang or _current_language
        ns, sep, path = key.partition(":")
        if not sep:
            return key
        for try_lang in dict.fromkeys((lang, DEFAULT_LANGUAGE)):
            for cand in _candidates(path, try_lang, params):
                text = _lookup(try_lang, ns, cand)
                if text is not None:
                    return _interpolate(text, params)
        return key
    except Exception:
        return key
