"""Which listed rows each filter matches: the scan_filters definitions, run
over every listed row or over some ids (an incremental refresh).

SQL filters run in one pass, packed 62 to an integer column per row (bit
63 is SQLite's sign), so the json_each filters' cost is paid once, as in
count_all. Python filters run on the PY_COLUMNS rows with the enrichment
context."""
from __future__ import annotations

import asyncio
import json
from typing import Optional

from backend.scan_filters import LISTED, PY_COLUMNS, Filter

_PER_COLUMN = 62  # bit 63 is the sign; 62 leaves one spare


def _where(ids: Optional[list[int]]) -> tuple[str, list]:
    """The WHERE clause and its parameters: the listed rows, or the listed
    ones among `ids` (passed as JSON, so nothing is written)."""
    if ids is None:
        return LISTED, []
    return f"{LISTED} AND id IN (SELECT value FROM json_each(?))", [json.dumps(list(ids))]


async def evaluate(db, filters: list[Filter], ctx: Optional[dict], ids: Optional[list[int]] = None
                   ) -> tuple[list[int], dict[str, list[int]], Optional[dict[int, int]]]:
    """(alive, members, savings) for `filters` over the listed rows, or over
    `ids` only: the listed ids among them; {filter id: the ids it matches};
    and when a Python filter ran, {id: estimated savings} for rows with any
    (else None). `ctx`: the enrichment context, for Python filters.

    With ids=None the SQL and Python passes read separately; a row written
    in between is re-evaluated by the next refresh (it's in the change log)."""
    where, where_params = _where(ids)
    members: dict[str, list[int]] = {f.id: [] for f in filters}
    alive: list[int] = []

    sql_filters = [f for f in filters if f.sql]
    chunks = [sql_filters[i:i + _PER_COLUMN] for i in range(0, len(sql_filters), _PER_COLUMN)]
    params: list = []
    columns = []
    for chunk in chunks:
        parts = []
        for bit, f in enumerate(chunk):
            if f.params:
                params.extend(f.params())
            parts.append(f"(CASE WHEN ({f.sql}) THEN {1 << bit} ELSE 0 END)")
        columns.append(" | ".join(parts))
    async with db.execute(f"SELECT id{''.join(f', {c}' for c in columns)} FROM scan_results WHERE {where}",
                          [*params, *where_params]) as cur:
        rows = await cur.fetchall()

    def spread() -> None:
        for row in rows:
            alive.append(row[0])
            for n, chunk in enumerate(chunks):
                mask = row[1 + n]
                while mask:
                    low = mask & -mask
                    members[chunk[low.bit_length() - 1].id].append(row[0])
                    mask ^= low
    await asyncio.to_thread(spread)

    py_filters = [f for f in filters if f.py]
    if not py_filters:
        return alive, members, None
    async with db.execute(f"SELECT id, {PY_COLUMNS} FROM scan_results WHERE {where}", where_params) as cur:
        names = [d[0] for d in cur.description]
        py_rows = [dict(zip(names, values)) for values in await cur.fetchall()]
    savings: dict[int, int] = {}

    def judge() -> None:
        for r in py_rows:
            if r["video_conv_savings_bytes"]:
                savings[r["id"]] = r["video_conv_savings_bytes"]
            for f in py_filters:
                if f.py(r, ctx):
                    members[f.id].append(r["id"])
    await asyncio.to_thread(judge)
    return alive, members, savings
