"""Advanced property search.

GET  /api/scan/search/properties   the conditions the modal offers
POST /api/scan/search              how many files match, and the filter token
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from backend.api_errors import ApiError
from pydantic import BaseModel

from backend.scan_filters import ADVANCED_PROPERTIES, encode_advanced


router = APIRouter(prefix="/api/scan")


# The property catalog and the conditions' meaning live with the Scanner's
# filters (backend/scan_filters.py, v0.10.0) — one implementation.
PROPERTIES = ADVANCED_PROPERTIES


@router.get("/search/properties")
async def list_properties():
    """Return the property catalog for the UI."""
    out = {}
    for key, p in PROPERTIES.items():
        out[key] = {
            "label": p.get("label", key),
            "group": p.get("group", "Other"),
            "type": p.get("type", "string"),
            "ops": p.get("ops", []),
            "examples": p.get("examples"),
            "options": p.get("options"),
            "option_labels": p.get("option_labels"),
        }
    return out


# ---- Search -----------------------------------------------------------------

class Predicate(BaseModel):
    property: str
    op: str
    value: Any = None
    value2: Any = None  # for "between"


class SearchRequest(BaseModel):
    predicates: list[Predicate] = []
    match_mode: str = "all"  # "all" = AND, "any" = OR


@router.post("/search")
async def advanced_search(req: SearchRequest):
    """How many files match the conditions, and the filter-string token that
    applies them (the Scanner adds it to its filter, so the tree, the lists,
    the counts and Add to Queue all use it — no path list, no cap)."""
    for pred in req.predicates:
        prop = PROPERTIES.get(pred.property)
        if not prop:
            raise ApiError(400, f"Unknown property: {pred.property}", code="search.unknownProperty", params={"property": pred.property})
        if pred.op not in prop.get("ops", []):
            raise ApiError(400, f"Unsupported op '{pred.op}' for property '{pred.property}'", code="search.unsupportedOp", params={"op": pred.op, "property": pred.property})
    if not req.predicates:
        return {"total": 0, "filter": None}
    from backend.routes.scan import _paths_matching
    token = encode_advanced([p.model_dump() for p in req.predicates], req.match_mode)
    return {"total": len(await _paths_matching(token)), "filter": token}
