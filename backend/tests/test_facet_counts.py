"""With a filter active, each pill counts what clicking it would give
(v0.10.0): the files matching the other active groups and exclusions, and
the pill — its own group, or its own exclusion, set aside."""
import pytest

import backend.routes.scan as scan_route
from backend.scan_filters import FILTERS, encode_advanced
from backend.tests.test_filter_spec import ROWS, lib, names  # noqa: F401 (fixture)


def _group(fid: str) -> str:
    f = FILTERS[fid]
    return f.group or f"_{f.id}"


async def _clicked(active: list[str], fid: str) -> int:
    """The oracle: the filter clicking `fid` stands for, evaluated directly."""
    keep = [t for t in active
            if not (t in FILTERS and _group(t) == _group(fid)) and t != f"!{fid}"]
    return len(await names(",".join(keep + [fid])))


ADV = encode_advanced([{"property": "audio_lang", "op": "eq", "value": "eng"},
                       {"property": "media_type", "op": "eq", "value": "movie"}], "any")


@pytest.mark.asyncio
@pytest.mark.parametrize("active", [
    ["res_1080p"],
    ["res_1080p", "res_720p", "x264"],
    ["needs_conversion", "!type_movie"],
    ["x264", "!res_720p", "!ignored", "container_mkv"],
    ["type_movie", "plex_watched"],
    ["x265", ADV],
    ["res_1080p", "!res_720p", "x264"],  # 720p: in an active group and excluded
])
async def test_every_pill_counts_what_clicking_it_gives(lib, active):
    counts = (await scan_route.get_filter_counts(filter=",".join(active)))["counts"]
    assert counts["matching"] == len(await names(",".join(active)))
    for fid in FILTERS:
        assert counts[fid] == await _clicked(active, fid), (active, fid)


@pytest.mark.asyncio
async def test_without_a_filter_the_counts_are_the_librarys(lib):
    counts = (await scan_route.get_filter_counts(filter="all"))["counts"]
    stats = (await scan_route.get_scan_stats())["counts"]
    assert counts["all"] == len(ROWS) and {k: counts[k] for k in FILTERS} == {k: stats[k] for k in FILTERS}
