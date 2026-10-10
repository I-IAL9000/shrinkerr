"""Advanced Search on the Scanner's filters (v0.10.0).

Its conditions travel in the filter string as one "adv:" token, so every
list, count and "selection + filter" action applies them — no 5,000-path
list — and they mean what the pills mean. Fixed on the way: "regex" was a
substring match, "Filename" searched the whole path, "Type" disagreed with
the pills, and "audio codec is not X" meant "some track isn't X"."""
import aiosqlite
import pytest

import backend.routes.scan as scan_route
from backend.scan_filters import decode_advanced, encode_advanced
from backend.tests.test_filter_spec import EXPECTED, ROWS, lib, names  # noqa: F401 (fixture)


def adv(*preds, match="all"):
    return encode_advanced([dict(zip(("property", "op", "value", "value2"), p)) for p in preds], match)


def test_the_token_round_trips():
    token = adv(("video_codec", "eq", "h264"), ("file_path", "contains", "a,b %_"), match="any")
    assert "," not in token and token.startswith("adv:")
    assert decode_advanced(token) == {"m": "any", "p": [
        {"property": "video_codec", "op": "eq", "value": "h264"},
        {"property": "file_path", "op": "contains", "value": "a,b %_"}]}
    assert decode_advanced("adv:not-base64!") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("cond,expected", [
    (("video_codec", "eq", "h264"), EXPECTED["x264"]),
    # "is not": files without that codec, including ones never probed.
    (("video_codec", "ne", "hevc"), set(ROWS) - EXPECTED["x265"]),
    (("video_height", "gte", 1080), {"dune", "heat_web", "corrupt", "big", "web_in_br"}),
    (("file_size_gb", "between", 5, 13), {"heat_br", "alien", "corrupt"}),
    (("audio_codec", "eq", "truehd"), {"dune"}),
    # No track is AAC (it meant "some track isn't AAC").
    (("audio_codec", "ne", "aac"), {"dune", "heat_br", "heat_web", "clip", "alien", "broken", "corrupt", "wmv"}),
    (("audio_lang", "in", "fra, spa"), {"clip", "big"}),
    (("audio_channels", "gte", 8), {"dune"}),
    (("audio_track_count", "eq", 0), {"broken"}),
    (("subtitle_lang", "eq", "eng"), {"dune", "heat_br", "big"}),
    (("subtitle_lang", "contains", "fr"), {"alien"}),
    (("source", "eq", "remux"), {"dune"}),
    (("source", "in", "bluray,unknown"), {"heat_br", "web_in_br", "clip", "alien", "broken", "corrupt", "big", "wmv"}),
    (("source", "eq", "webrip"), EXPECTED["src_webdl"]),  # older saved searches
    (("file_path", "contains", "Heat"), {"heat_br", "heat_web"}),
    (("file_path", "regex", r"S0\dE0[12]"), {"ep1", "ep2"}),  # a real regex now
    (("file_name", "contains", "1995"), {"heat_br", "heat_web"}),  # the name, not the folders
    (("file_name", "contains", "Shelf"), set()),  # "Blu-ray Shelf" is a folder
    (("media_type", "eq", "tv"), EXPECTED["type_tv"]),  # the pills' classification
    (("media_type", "ne", "movie"), EXPECTED["type_tv"] | EXPECTED["type_other"]),
    (("health_status", "eq", "warnings"), {"old"}),
    (("duplicate_count", "gte", 2), EXPECTED["duplicates"]),
    (("vmaf_score", "exists", None), {"old", "corrupt", "big"}),
    (("has_lossless_audio", "eq", True), {"dune"}),
    (("frame_rate", "gte", 50), {"heat_br"}),
    (("frame_rate", "between", 23, 24), {"big"}),
    (("bit_depth", "eq", 10), {"dune", "ep1"}),
    (("bits_per_pixel", "gt", 0.5), {"alien", "big"}),  # 0.86 and 0.72
])
async def test_each_condition(lib, cond, expected):
    assert await names(adv(cond)) == expected


@pytest.mark.asyncio
async def test_hdr_from_the_probe_or_the_name(lib):
    hdr = {"ep1", "corrupt", "web_in_br"}  # HDR10, HLG, Dolby Vision
    assert await names(adv(("hdr", "eq", True))) == hdr
    async with aiosqlite.connect(lib) as db:
        await db.execute("UPDATE scan_results SET hdr_format = 'hdr10' WHERE file_path LIKE '%Dune%'")
        await db.commit()
    assert await names(adv(("hdr", "eq", True))) == hdr | {"dune"}
    assert await names(adv(("hdr", "eq", False))) == set(ROWS) - hdr - {"dune"}
    # Scanned before the format was stored: the name decides.
    from backend.scan_filters import compile_condition
    is_hdr = compile_condition(0, {"property": "hdr", "op": "eq", "value": True}).py
    assert is_hdr({"file_path": "/m/Movie.2160p.HDR.DV.mkv", "hdr_format": None}, None) is True
    assert is_hdr({"file_path": "/m/Hdrive/Movie.mkv", "hdr_format": None}, None) is False
    # The pills tell them apart (by name, too).
    from backend.scan_filters import hdr_kind
    assert [hdr_kind({"file_path": f, "hdr_format": None}) for f in (
        "/m/Movie.2160p.DV.HDR10.mkv", "/m/Movie.2160p.HDR10+.mkv", "/m/Show.HLG.mkv",
        "/m/Movie.DVDRip.mkv", "/m/Movie.mkv")] == ["dv", "hdr10", "hlg", None, None]
    assert hdr_kind({"file_path": "/m/Movie.HDR.mkv", "hdr_format": "dv5"}) == "dv"  # the probe wins


@pytest.mark.asyncio
async def test_all_any_and_with_the_pills(lib):
    any_ = adv(("video_codec", "eq", "av1"), ("media_type", "eq", "tv"), match="any")  # SQL + Python in one group
    assert await names(any_) == {"corrupt", "ep1", "ep2"}
    all_ = adv(("video_codec", "eq", "h264"), ("media_type", "eq", "tv"))
    assert await names(all_) == {"ep1", "ep2"}
    assert await names(f"res_720p,!queued,{all_}") == {"ep1"}
    # An invalid condition is dropped, not fatal (an edited URL).
    assert await names(adv(("video_codec", "eq", "h264"), ("no_such", "eq", 1))) == EXPECTED["x264"]


@pytest.mark.asyncio
async def test_the_lists_and_counts_agree_with_the_search(lib):
    token = adv(("audio_lang", "eq", "eng"), ("source", "in", "bluray,remux"), match="all")
    listed = await names(token)
    assert listed == {"dune", "heat_br", "web_in_br"}
    tree = (await scan_route.get_scan_tree(filter=token))["folders"]
    assert sum(f["file_count"] for f in tree if not f.get("is_file")) == len(listed)
    from backend.routes.search import SearchRequest, advanced_search
    r = await advanced_search(SearchRequest(predicates=[
        {"property": "audio_lang", "op": "eq", "value": "eng"},
        {"property": "source", "op": "in", "value": "bluray,remux"}]))
    assert r["total"] == 3 and decode_advanced(r["filter"])["p"][0]["value"] == "eng"


@pytest.mark.asyncio
async def test_release_group(lib):
    """As the rules read it: the name's last "-GROUP"."""
    flux = "/media/Movies/Film (2020)/Film.2020.1080p.WEB-DL.DDP5.1.H.264-FLUX.mkv"
    ntb = "/media/TV/Show/Show.S01E01.1080p.WEB-DL-NTb.mkv"
    async with aiosqlite.connect(lib) as db:
        for fp in (flux, ntb):
            await db.execute("INSERT INTO scan_results (file_path, file_size, scan_timestamp) VALUES (?, 1, '2026-10-10')", (fp,))
        await db.commit()

    async def paths(*cond):
        return set(await scan_route._paths_matching(adv(cond)))
    assert await paths("release_group", "eq", "flux") == {flux}
    assert await paths("release_group", "in", "ntb, FLUX") == {flux, ntb}
    assert await paths("release_group", "contains", "lu") == {flux}
    assert flux not in await paths("release_group", "ne", "flux") and ntb in await paths("release_group", "ne", "flux")


@pytest.mark.asyncio
async def test_unknown_properties_are_refused(lib):
    from backend.api_errors import ApiError
    from backend.routes.search import SearchRequest, advanced_search
    with pytest.raises(ApiError):
        await advanced_search(SearchRequest(predicates=[{"property": "nope", "op": "eq", "value": 1}]))
