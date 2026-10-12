"""The filter index keeps every value of the indexed lists as a bitset
"<facet>:<value>", patched like the other filters, and answers each list's
values with their counts under a filter: what the SQL engine would list."""
import aiosqlite
import pytest

from backend.filter_index.bits import ids_of
from backend.filter_index.evaluate import evaluate
from backend.filter_index.index import FilterIndex
from backend.scan_filters import _FILTER_LIST, _FACET_LIST, PY_COLUMNS, _group_key, facet_token, parse_filter
from backend.tests.filter_index_library import EXPRESSIONS, mutate, seed

# The list expressions (Advanced Search ones are the service's: test_filter_index_service.py).
LIST_EXPRESSIONS = [e for e in EXPRESSIONS if ":" in e and not e.startswith("adv:")]


async def _index(db_path) -> FilterIndex:
    from backend.routes.scan import _build_enrichment_context
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        ctx = await _build_enrichment_context(db, titles=True)
        alive, members, savings = await evaluate(db, _FILTER_LIST, ctx, facets=_FACET_LIST)
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


async def _constraints(index: FilterIndex, expr) -> tuple[list[tuple[str, int]], list[tuple[str, int]]]:
    """facet_counts' / facet_values' groups and exclusions for `expr`; a
    filter the index hasn't (an Advanced Search condition) evaluated as the
    service does."""
    from backend.filter_index import service

    async def bits_of(f) -> int:
        return index.bits[f.id] if f.id in index.bits else await service._adhoc_bits(f)
    groups = []
    for group in expr.groups:
        bits = 0
        for f in group:
            bits |= await bits_of(f)
        groups.append((_group_key(group[0]), bits))
    return groups, [(f.id, await bits_of(f)) for f in expr.excluded]


@pytest.mark.asyncio
async def test_every_value_is_the_rows_its_token_lists(test_db):
    await seed(test_db)
    index = await _index(test_db)
    values = [k for k in index.bits if ":" in k]
    assert {k.split(":")[0] for k in values} == {f.key for f in _FACET_LIST}
    for key in values:
        from backend.scan_filters import facet_token
        facet, _, value = key.partition(":")
        assert set(ids_of(index.bits[key])) == await _rows(test_db, facet_token(facet, value)), key


@pytest.mark.asyncio
async def test_lists_read_in_one_pass_are_their_own_definitions(test_db):
    """The track lists are read one column at a time (Facet.combine), not
    each through its pairs_sql: the same values, the same rows."""
    import dataclasses
    await seed(test_db)
    combined = [f for f in _FACET_LIST if f.combine]
    assert {f.key for f in combined} == {"alang", "slang", "acodec", "achan"}
    async with aiosqlite.connect(test_db) as db:
        db.row_factory = aiosqlite.Row
        _, together, _ = await evaluate(db, [], None, facets=combined)
        _, apart, _ = await evaluate(db, [], None, facets=[dataclasses.replace(f, combine=None) for f in combined])
    assert {k: sorted(v) for k, v in together.items()} == {k: sorted(v) for k, v in apart.items()}
    assert {k.split(":")[0] for k in together} == {f.key for f in combined}


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
            alive, members, savings = await evaluate(db, _FILTER_LIST, ctx, ids=[row_id], facets=_FACET_LIST)
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
    titles = [f for f in _FACET_LIST if f.titles]
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
    for text in LIST_EXPRESSIONS:
        groups, excluded = await _constraints(index, parse_filter(text))
        assert index.facet_counts(groups, excluded) == await sql_counts(test_db, text), text
    service.reset()


@pytest.mark.asyncio
async def test_each_value_counts_without_its_own_list_and_exclusion(test_db):
    """Under each list expression, a value's count is what the SQL engine
    lists for the expression without the value's list and its exclusion,
    plus the value; it's listed when that isn't 0, or when it's selected."""
    from backend.filter_index import service
    from backend.routes.scan import _build_enrichment_context
    await seed(test_db)
    index = await _index(test_db)
    service.reset()  # _adhoc_bits keeps rows: none from another test's database
    async with aiosqlite.connect(test_db) as db:
        db.row_factory = aiosqlite.Row
        ctx = await _build_enrichment_context(db, titles=True)

        async def count(tokens: list[str]) -> int:
            expr = parse_filter(",".join(tokens))
            async with db.execute(f"SELECT id, {PY_COLUMNS}{expr.select_sql} FROM scan_results "
                                  f"WHERE removed_from_list = 0{expr.where_sql}",
                                  [*expr.select_params, *expr.where_params]) as cur:
                rows = [dict(r) for r in await cur.fetchall()]
            return sum(1 for r in rows if expr.post_filter(r, ctx))

        def own(token: str, key: str, fid: str) -> bool:
            """The token is the value's list or its exclusion."""
            e = parse_filter(token)
            return any(_group_key(g[0]) == key for g in e.groups) or any(f.id == fid for f in e.excluded)

        for text in LIST_EXPRESSIONS:
            expr = parse_filter(text)
            groups, excluded = await _constraints(index, expr)
            selected = frozenset(f.id for f in [*(f for g in expr.groups for f in g), *expr.excluded] if ":" in f.id)
            lists = index.facet_values(groups, excluded, selected=selected, top=None)
            for facet in _FACET_LIST:
                listed = dict(lists[facet.key])
                for fid in [k for k in index.bits if k.startswith(f"{facet.key}:")]:
                    value = fid.partition(":")[2]
                    want = await count([t for t in text.split(",") if not own(t, facet.key, fid)]
                                       + [facet_token(facet.key, value)])
                    assert listed.get(value, 0) == want, (text, fid)
                    assert (value in listed) == (want > 0 or fid in selected), (text, fid)
    service.reset()


@pytest.mark.asyncio
async def test_one_list_and_the_top_values(test_db):
    await seed(test_db)
    index = await _index(test_db)
    assert list(index.facet_values([], [], facet="alang")) == ["alang"]
    every = index.facet_values([], [], top=None)
    for key, values in every.items():  # no filter: every value a file has, the most first
        rows = {k.partition(":")[2]: bits.bit_count() for k, bits in index.bits.items() if k.startswith(f"{key}:")}
        assert dict(values) == rows and [n for _, n in values] == sorted(rows.values(), reverse=True), key
    assert any(len(values) > 2 for values in every.values())
    top = index.facet_values([], [], selected=frozenset({"alang:xxx"}), top=2)
    for key, values in top.items():
        assert values[:2] == every[key][:2], key
        assert len(values) == min(2, len(every[key])) + (key == "alang"), key
    assert top["alang"][-1] == ("xxx", 0)


@pytest.fixture
def service_env(test_db, tmp_path, monkeypatch):
    from backend.filter_index import service
    service.reset()
    monkeypatch.setattr(service, "index_path", lambda: str(tmp_path / "filter-index.bin"))
    yield service
    service.reset()


@pytest.mark.asyncio
async def test_the_counts_endpoint_returns_the_lists(service_env, test_db):
    from backend.routes.scan import get_facet_values, get_filter_counts
    await seed(test_db)
    service_env._state.index = await service_env.build()
    body = await get_filter_counts("alang:fre")
    assert body["counts"]["matching"] == len(await _rows(test_db, "alang:fre"))
    langs = {v["value"]: v["count"] for v in body["facets"]["alang"]}
    assert langs["fre"] == len(await _rows(test_db, "alang:fre"))  # its own list set aside
    assert len(body["facets"]["alang"]) <= 50
    async with aiosqlite.connect(test_db) as db:
        async with db.execute("SELECT id, path FROM media_dirs") as cur:
            paths = {str(i): p for i, p in await cur.fetchall()}
    library = body["facets"]["library"]
    assert library and all(v["label"] == paths[v["value"]] for v in library)  # by path: labels repeat ("TV Shows")
    every = (await get_facet_values("genre", "all"))["values"]
    assert every and [v["count"] for v in every] == sorted((v["count"] for v in every), reverse=True)


@pytest.mark.asyncio
async def test_a_title_change_reaches_the_lists(service_env, test_db):
    await seed(test_db)
    service_env._state.index = await service_env.build()
    async with aiosqlite.connect(test_db) as db:
        await db.execute("UPDATE poster_cache SET network = 'Channel 4' WHERE network = 'HBO'")
        await db.commit()
    counts, lists = await service_env.answer(parse_filter("all"))
    networks = {v for v, _ in lists["network"]}
    assert "Channel 4" in networks and "HBO" not in networks


@pytest.mark.asyncio
async def test_a_saved_combined_genre_counts_its_titles(service_env, test_db):
    """"genre:action & adventure", saved before genres were kept split, is
    the titles with both parts; "genre:science fiction" is sci-fi."""
    await seed(test_db)
    async with aiosqlite.connect(test_db) as db:
        await db.execute("UPDATE poster_cache SET genres = 'Action & Adventure' WHERE genres = 'Comedy'")
        await db.execute("UPDATE poster_cache SET genres = 'Action' WHERE genres = 'Crime, Drama'")
        await db.commit()
    service_env._state.index = await service_env.build()
    for value, kept in (("action & adventure", "action & adventure"), ("science fiction", "sci-fi")):
        token = facet_token("genre", value)
        rows = await _rows(test_db, token)
        counts, lists = await service_env.answer(parse_filter(token))
        assert rows and counts["matching"] == len(rows), value
        assert (kept, len(rows)) in lists["genre"], value
    assert len(await _rows(test_db, "genre:action")) > len(await _rows(test_db, facet_token("genre", "action & adventure")))


@pytest.mark.asyncio
async def test_lists_wait_for_the_index(service_env, test_db, monkeypatch):
    from backend.routes.scan import get_filter_counts
    await seed(test_db)
    monkeypatch.setattr(service_env, "_start_rebuild", lambda: None)  # not built yet
    body = await get_filter_counts("all", facets_only=True)
    assert body == {"counts": None, "facets": None}


@pytest.mark.asyncio
async def test_list_values_come_from_the_index_only(service_env, test_db, monkeypatch):
    """A list value the index hasn't has no rows (patch drops a value no row
    has): 0, not evaluated now. Only Advanced Search conditions are."""
    await seed(test_db)
    service_env._state.index = await service_env.build()
    evaluated = []
    real = service_env._adhoc_bits

    async def adhoc_bits(f):
        evaluated.append(f.id)
        return await real(f)
    monkeypatch.setattr(service_env, "_adhoc_bits", adhoc_bits)
    counts, lists = await service_env.answer(
        parse_filter("alang:xxx,genre:western,!network:Nowhere"))
    assert evaluated == []
    assert counts["matching"] == 0
    assert lists["alang"] == [("xxx", 0)] and ("Nowhere", 0) in lists["network"]


@pytest.mark.asyncio
async def test_a_value_dropped_meanwhile_counts_as_none(service_env, test_db, monkeypatch):
    """A refresh between the unlocked step and the counting can drop a value
    the filter names: no rows, not an error."""
    await seed(test_db)
    service_env._state.index = await service_env.build()
    real = service_env._adhoc_for

    async def adhoc_then_refreshed(expr, index):
        out = await real(expr, index)
        del service_env._state.index.bits["alang:fre"]  # as patch() drops a value no row has any more
        return out
    monkeypatch.setattr(service_env, "_adhoc_for", adhoc_then_refreshed)
    result = await service_env.answer(parse_filter("alang:fre"))
    assert result is not None and result[0]["matching"] == 0


@pytest.mark.asyncio
async def test_an_unknown_list_is_an_error(service_env, test_db):
    from backend.api_errors import ApiError
    from backend.routes.scan import get_facet_values
    with pytest.raises(ApiError) as raised:
        await get_facet_values("nope")
    assert raised.value.status_code == 404 and raised.value.code == "scan.noSuchList"


@pytest.fixture
def clock(monkeypatch):
    """The service's wall clock, moved by hand: clock[0] += seconds."""
    import time as real_time
    from types import SimpleNamespace

    from backend.filter_index import service
    now = [real_time.time()]
    monkeypatch.setattr(service, "time", SimpleNamespace(time=lambda: now[0], monotonic=real_time.monotonic,
                                                         perf_counter=real_time.perf_counter))
    return now


async def _write(db_path, sql, *params) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute(sql, params)
        await db.commit()


@pytest.mark.asyncio
async def test_title_changes_are_applied_at_most_every_minute(service_env, test_db, clock):
    """A Poster prefetch writes title metadata several times a second, and
    each change evaluates the Title lists over the whole library again: at
    most every TITLES_REFRESH seconds. Other changes apply at once."""
    from backend.tests.filter_index_library import sql_counts
    await seed(test_db)
    service_env._state.index = await service_env.build()

    async def networks() -> set[str]:
        _, lists = await service_env.answer(parse_filter("all"))
        return {v for v, _ in lists["network"]}
    await _write(test_db, "UPDATE poster_cache SET network = 'Channel 4' WHERE network = 'HBO'")
    assert "Channel 4" in await networks()  # the first one at once
    await _write(test_db, "UPDATE poster_cache SET network = 'Sky' WHERE network = 'Channel 4'")
    ignored = (await service_env.counts(parse_filter("all")))["ignored"]
    await _write(test_db, "INSERT INTO ignored_files (file_path, reason, ignored_at) SELECT file_path, 'manual', 'x' "
                          "FROM scan_results WHERE file_path LIKE '/demo/Movies/%' AND removed_from_list = 0 "
                          "AND file_path NOT IN (SELECT file_path FROM ignored_files) ORDER BY id LIMIT 1")
    clock[0] += service_env.TITLES_REFRESH - 1
    assert "Sky" not in await networks() and "Channel 4" in await networks()  # waits
    counts = await service_env.counts(parse_filter("all"))
    assert counts["ignored"] == ignored + 1 and counts == await sql_counts(test_db)
    clock[0] += 1
    assert "Sky" in await networks() and "Channel 4" not in await networks()


@pytest.mark.asyncio
async def test_the_lists_answer_without_the_media_folder_labels(service_env, test_db, monkeypatch):
    import backend.routes.scan as scan_route
    await seed(test_db)
    service_env._state.index = await service_env.build()

    async def broken():
        raise RuntimeError("database is locked")
    monkeypatch.setattr(scan_route, "connect_db", broken)
    body = await scan_route.get_filter_counts("all")
    assert body["counts"]["all"] and body["facets"]["library"]
    assert all(v["label"] == v["value"] for v in body["facets"]["library"])
