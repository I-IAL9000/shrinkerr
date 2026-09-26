"""Contract tests for coded API errors (backend/api_errors.py).

Every `ApiError(..., code=...)` raised in the backend must have a matching
entry in the UI's serverApi catalogs (en + es) with the same {{placeholders}}
as the `params` it passes, and the handler must render {detail, code, params}
while leaving plain HTTPException responses untouched.
"""
import ast
import json
import re
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from backend.api_errors import ApiError, api_error_handler

REPO = Path(__file__).resolve().parents[2]
BACKEND = REPO / "backend"
LOCALES = REPO / "frontend" / "src" / "i18n" / "locales"
LANGS = ("en", "es")
PLACEHOLDER = re.compile(r"\{\{\s*(\w+)\s*\}\}")


def _flatten(d: dict, prefix: str = "") -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in d.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(_flatten(v, key + "."))
        else:
            out[key] = v
    return out


def _catalog(lang: str) -> dict[str, str]:
    raw = json.loads((LOCALES / lang / "serverApi.json").read_text(encoding="utf-8"))
    flat = _flatten(raw)
    # Collapse plural variants (key_one/_many/_other) onto their base key.
    merged: dict[str, str] = {}
    for k, v in flat.items():
        base = re.sub(r"_(zero|one|two|few|many|other)$", "", k)
        merged[base] = merged.get(base, "") + " " + v
    return merged


def _api_error_sites() -> list[tuple[str, int, str, set[str] | None]]:
    """(file, line, code, param keys or None if params isn't a dict literal)."""
    sites = []
    for path in BACKEND.rglob("*.py"):
        if "tests" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            name = f.id if isinstance(f, ast.Name) else getattr(f, "attr", None)
            if name != "ApiError":
                continue
            kw = {k.arg: k.value for k in node.keywords}
            code_node = kw.get("code") or (node.args[2] if len(node.args) > 2 else None)
            assert isinstance(code_node, ast.Constant) and isinstance(code_node.value, str), (
                f"{path}:{node.lineno}: ApiError code must be a string literal"
            )
            params_node = kw.get("params") or (node.args[3] if len(node.args) > 3 else None)
            if params_node is None:
                keys: set[str] | None = set()
            elif isinstance(params_node, ast.Dict) and all(
                isinstance(k, ast.Constant) for k in params_node.keys
            ):
                keys = {k.value for k in params_node.keys}
            else:
                keys = None
            sites.append((str(path.relative_to(REPO)), node.lineno, code_node.value, keys))
    return sites


def test_api_error_sites_found():
    assert len(_api_error_sites()) > 50


@pytest.mark.parametrize("lang", LANGS)
def test_every_code_is_in_catalog_with_matching_placeholders(lang):
    catalog = _catalog(lang)
    problems = []
    for file, line, code, keys in _api_error_sites():
        if code not in catalog:
            problems.append(f"{file}:{line}: '{code}' missing from {lang}/serverApi.json")
            continue
        if keys is None:
            continue
        placeholders = set(PLACEHOLDER.findall(catalog[code]))
        if placeholders != keys:
            problems.append(
                f"{file}:{line}: '{code}' params {sorted(keys)} != {lang} placeholders {sorted(placeholders)}"
            )
    assert not problems, "\n".join(problems)


def test_catalogs_have_same_keys_and_no_unused_entries():
    en, es = _catalog("en"), _catalog("es")
    assert set(en) == set(es)
    used = {code for _, _, code, _ in _api_error_sites()}
    assert set(en) - used == set(), f"unused serverApi keys: {sorted(set(en) - used)}"
    for key in en:
        assert set(PLACEHOLDER.findall(en[key])) == set(PLACEHOLDER.findall(es[key])), key


def _detail_templates() -> list[tuple[str, int, str, str]]:
    """(file, line, code, detail with every interpolation replaced by {{?}})."""
    out = []
    for path in BACKEND.rglob("*.py"):
        if "tests" in path.parts:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not (isinstance(node, ast.Call) and getattr(node.func, "id", None) == "ApiError"):
                continue
            kw = {k.arg: k.value for k in node.keywords}
            detail = kw.get("detail") or (node.args[1] if len(node.args) > 1 else None)
            code = (kw.get("code") or node.args[2]).value
            if isinstance(detail, ast.Constant):
                tmpl = detail.value
            elif isinstance(detail, ast.JoinedStr):
                tmpl = "".join(
                    v.value if isinstance(v, ast.Constant) else "{{?}}" for v in detail.values
                )
            else:
                continue
            out.append((str(path.relative_to(REPO)), node.lineno, code, tmpl))
    return out


def test_english_catalog_matches_backend_detail():
    en = _catalog("en")
    problems = [
        f"{file}:{line}: '{code}' en={en.get(code)!r} vs detail={tmpl!r}"
        for file, line, code, tmpl in _detail_templates()
        if PLACEHOLDER.sub("{{?}}", en.get(code, "").strip()) != tmpl
    ]
    assert not problems, "\n".join(problems)


def _client() -> TestClient:
    app = FastAPI()
    app.add_exception_handler(ApiError, api_error_handler)

    @app.get("/coded")
    async def coded():
        raise ApiError(404, "Job not found", code="jobs.notFound")

    @app.get("/coded-params")
    async def coded_params():
        raise ApiError(
            400,
            "Cannot undo job with status 'failed'",
            code="jobs.cannotUndoStatus",
            params={"status": "failed"},
            headers={"X-Test": "1"},
        )

    @app.get("/plain")
    async def plain():
        raise HTTPException(status_code=418, detail="plain text")

    @app.get("/plain-dict")
    async def plain_dict():
        raise HTTPException(status_code=400, detail={"a": 1})

    return TestClient(app)


def test_api_error_response_has_detail_code_params():
    c = _client()
    r = c.get("/coded")
    assert r.status_code == 404
    assert r.json() == {"detail": "Job not found", "code": "jobs.notFound", "params": {}}

    r = c.get("/coded-params")
    assert r.status_code == 400
    assert r.headers["x-test"] == "1"
    assert r.json() == {
        "detail": "Cannot undo job with status 'failed'",
        "code": "jobs.cannotUndoStatus",
        "params": {"status": "failed"},
    }


def test_plain_http_exception_unchanged():
    c = _client()
    r = c.get("/plain")
    assert r.status_code == 418
    assert r.json() == {"detail": "plain text"}
    r = c.get("/plain-dict")
    assert r.json() == {"detail": {"a": 1}}


def test_api_error_is_http_exception():
    # Callers/tests that catch HTTPException keep working.
    exc = ApiError(400, "x", code="jobs.notFound")
    assert isinstance(exc, HTTPException)
    assert exc.detail == "x" and exc.status_code == 400


def test_main_app_registers_handler():
    # Checked statically on purpose: importing backend.main here — before any
    # test DB exists — makes the auth layer capture the default DB path at
    # import time, which then 503s every route test that runs later in the
    # same session. Parse main.py instead of importing it.
    tree = ast.parse((REPO / "backend" / "main.py").read_text())
    registered = any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "add_exception_handler"
        and len(node.args) == 2
        and all(isinstance(a, ast.Name) for a in node.args)
        and [a.id for a in node.args] == ["ApiError", "api_error_handler"]
        for node in ast.walk(tree)
    )
    assert registered, "backend/main.py must call app.add_exception_handler(ApiError, api_error_handler)"
