"""The filter index keeps every value of the indexed lists as a bitset
"<facet>:<value>", patched like the other filters, and answers each list's
values with their counts under a filter: what the SQL engine would list."""
import aiosqlite
import pytest

from backend.filter_index.bits import ids_of
from backend.filter_index.evaluate import evaluate
from backend.filter_index.index import FilterIndex
from backend.scan_filters import _FILTER_LIST, INDEXED_FACETS, PY_COLUMNS, parse_filter
from backend.tests.filter_index_library import EXPRESSIONS, mutate, seed


async def _index(db_path) -> FilterIndex:
    from backend.routes.scan import _build_enrichment_context
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        ctx = await _build_enrichment_context(db, titles=True)
        alive, members, savings = await evaluate(db, _FILTER_LIST, ctx, facets=INDEXED_FACETS)
    return FilterIndex.of(alive, members, savings or {}, version="v", db_id="d")


async def _rows(db_path, text) -> set[int]:
    """The ids parse_filter lists (the tree's way)."""
    from backend.routes.scan import _build_enrichment_context
    expr = parse_filter(text)
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        ctx = await _build_enrichment_context(db, titles=True)
        async with db.execute(f"SELECT id, {PY_COLUMNS}{expr.select_sql} FROM scan_results "
                              f"WHERE removed_from_list = 0{expr.where_sql}",
                              [*expr.select_params, *expr.where_params]) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
    return {r["id"] for r in rows if expr.post_filter(r, ctx)}


@pytest.mark.asyncio
async def test_every_value_is_the_rows_its_token_lists(test_db):
    await seed(test_db)
    index = await _index(test_db)
    values = [k for k in index.bits if ":" in k]
    assert {k.split(":")[0] for k in values} == {f.key for f in INDEXED_FACETS}
    for key in values:
        from backend.scan_filters import facet_token
        facet, _, value = key.partition(":")
        assert set(ids_of(index.bits[key])) == await _rows(test_db, facet_token(facet, value)), key


@pytest.mark.asyncio
async def test_a_lists_values_and_counts_under_a_filter(test_db):
    await seed(test_db)
    index = await _index(test_db)
    lists = index.facet_values([], [], selected=frozenset({"alang:xxx"}))
    alang = dict(lists["alang"])
    assert alang["eng"] == len(await _rows(test_db, "alang:eng")) and alang["xxx"] == 0  # selected: kept at 0
    # Under a filter: what adding the value would give, its own list set aside.
    expr = parse_filter("alang:fre,res_1080p")
    groups = [(g[0].group or f"_{g[0].id}", 0) for g in expr.groups]
    bits = {"alang": index.bits["alang:fre"], "resolution": index.bits["res_1080p"]}
    lists = index.facet_values([(k, bits[k]) for k, _ in groups], [])
    assert dict(lists["alang"])["eng"] == len(await _rows(test_db, "alang:eng,res_1080p"))
    assert dict(lists["acodec"]).get("truehd", 0) == len(await _rows(test_db, "alang:fre,res_1080p,acodec:truehd"))
    assert all(n > 0 for values in lists.values() for _, n in values)  # zeros aren't listed unless selected


def _values(index: FilterIndex) -> dict[str, int]:
    return {k: v for k, v in index.bits.items() if ":" in k}


@pytest.mark.asyncio
async def test_patching_rows_adds_and_drops_values(test_db):
    await seed(test_db)
    index = await _index(test_db)
    async with aiosqlite.connect(test_db) as db:
        async with db.execute("SELECT MIN(id) FROM scan_results") as cur:
            row_id = (await cur.fetchone())[0]

    async def patch(language: str) -> None:
        from backend.routes.scan import _build_enrichment_context
        async with aiosqlite.connect(test_db) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("UPDATE scan_results SET audio_tracks_json = ? WHERE id = ?",
                             (f'[{{"language": "{language}", "codec": "aac", "channels": 2}}]', row_id))
            await db.commit()
            ctx = await _build_enrichment_context(db, titles=True)
            alive, members, savings = await evaluate(db, _FILTER_LIST, ctx, ids=[row_id], facets=INDEXED_FACETS)
        index.patch([row_id], alive, members, savings or {})

    await patch("isl")
    assert ids_of(index.bits["alang:ice"]) == [row_id]  # "isl" is Icelandic's T form: kept as "ice"
    await patch("eng")
    assert "alang:ice" not in index.bits  # no row has it any more
    assert _values(index) == _values(await _index(test_db))


@pytest.mark.asyncio
async def test_replacing_lists_takes_their_values_over(test_db):
    """An input of some lists changed (the titles' metadata): their values
    evaluated again over the library replace theirs, the others' stay."""
    await seed(test_db)
    index = await _index(test_db)
    async with aiosqlite.connect(test_db) as db:
        await db.execute("UPDATE poster_cache SET network = 'Netflix' WHERE network = 'HBO'")
        await db.commit()
    titles = [f for f in INDEXED_FACETS if f.titles]
    from backend.routes.scan import _build_enrichment_context
    async with aiosqlite.connect(test_db) as db:
        db.row_factory = aiosqlite.Row
        ctx = await _build_enrichment_context(db, titles=True)
        _, members, _ = await evaluate(db, [], ctx, facets=titles)
    index.replace_facets([f.key for f in titles], members)
    assert "network:HBO" not in index.bits and "network:Netflix" in index.bits
    assert _values(index) == _values(await _index(test_db))


@pytest.mark.asyncio
async def test_count_all_is_the_fixed_filters_only(test_db):
    await seed(test_db)
    index = await _index(test_db)
    assert not [k for k in index.count_all() if ":" in k]


@pytest.mark.asyncio
async def test_facet_expressions_count_like_the_sql_engine(test_db):
    """The index's counts under list values equal the SQL engine's."""
    from backend.tests.filter_index_library import sql_counts
    await seed(test_db)
    await mutate(test_db)
    index = await _index(test_db)
    from backend.filter_index import service
    service.reset()  # _adhoc_bits keeps rows: none from another test's database
    for text in [e for e in EXPRESSIONS if ":" in e and not e.startswith("adv:")]:
        expr = parse_filter(text)
        groups = []
        for group in expr.groups:
            bits = 0
            for f in group:
                bits |= index.bits.get(f.id) if f.id in index.bits else await service._adhoc_bits(f)
            groups.append((group[0].group or f"_{group[0].id}", bits))
        excluded = [(f.id, index.bits[f.id] if f.id in index.bits else await service._adhoc_bits(f))
                    for f in expr.excluded]
        assert index.facet_counts(groups, excluded) == await sql_counts(test_db, text), text
    service.reset()
