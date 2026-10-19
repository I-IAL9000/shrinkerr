"""Rename patterns (v0.10.0): the separators a pattern has are kept, and
only the ones an empty token leaves behind go. A clean-up that dropped every
"- " took the " - " out of "{SeriesTitle} - S{season:00}E{episode:00} - …",
and the codec and source were respelled ("h265" → "x265", "WEBDL" →
"WEB-DL")."""
import pytest

from backend.rename import (
    DEFAULT_MOVIE_FILE_PATTERN, DEFAULT_TV_FILE_PATTERN, RenameMeta, RenameSettings, _normalize_source,
    _normalize_video_codec, render_pattern,
)

TV = ("{SeriesTitle} - S{season:00}E{episode:00} - {EpisodeTitle} - {Quality} {VideoDynamicRange} "
      "{AudioCodec} {AudioChannels} {VideoCodec}-{ReleaseGroup}")


def _klovn(**changes) -> RenameMeta:
    meta = RenameMeta(series_title="Klovn", title="Klovn", season=7, episode=1, episode_title="The Muslim Piano",
                      resolution="720p", source="WEBDL", video_codec="h265", audio_codec="AAC",
                      audio_channels="2.0", release_group="DBRETAiL", media_type="tv")
    for key, value in changes.items():
        setattr(meta, key, value)
    return meta


def test_the_patterns_separators_are_kept():
    assert render_pattern(TV, _klovn(), RenameSettings()) == \
        "Klovn - S07E01 - The Muslim Piano - 720p WEBDL AAC 2.0 h265-DBRETAiL"
    assert render_pattern(DEFAULT_TV_FILE_PATTERN, _klovn(), RenameSettings()) == \
        "Klovn - S07E01 - The Muslim Piano 720p WEBDL AAC 2.0 h265-DBRETAiL"


@pytest.mark.parametrize("changes, name", [
    ({"episode_title": ""}, "Klovn - S07E01 - 720p WEBDL AAC 2.0 h265-DBRETAiL"),
    ({"release_group": ""}, "Klovn - S07E01 - The Muslim Piano - 720p WEBDL AAC 2.0 h265"),
    ({"video_codec": ""}, "Klovn - S07E01 - The Muslim Piano - 720p WEBDL AAC 2.0 DBRETAiL"),
    ({"resolution": "", "source": "", "audio_codec": "", "audio_channels": "", "video_codec": "",
      "release_group": ""}, "Klovn - S07E01 - The Muslim Piano"),
    ({"episode_title": "", "resolution": "", "source": "", "audio_codec": "", "audio_channels": "",
      "video_codec": "", "release_group": ""}, "Klovn - S07E01"),
])
def test_what_an_empty_token_leaves_goes(changes, name):
    assert render_pattern(TV, _klovn(**changes), RenameSettings()) == name


def test_titles_keep_their_dashes():
    meta = _klovn(series_title="Spider-Man - The Animated Series", episode_title="Night of the Lizard")
    assert render_pattern(TV, meta, RenameSettings()).startswith(
        "Spider-Man - The Animated Series - S07E01 - Night of the Lizard - 720p")


def test_movies_as_before():
    meta = RenameMeta(title="Heat", year="1995", resolution="1080p", source="BluRay", video_codec="x264",
                      audio_codec="DTS", audio_channels="5.1", release_group="GRP")
    assert render_pattern(DEFAULT_MOVIE_FILE_PATTERN, meta, RenameSettings()) == "Heat (1995) 1080p BluRay DTS 5.1 x264-GRP"
    assert render_pattern(DEFAULT_MOVIE_FILE_PATTERN, RenameMeta(title="Heat"), RenameSettings()) == "Heat"


@pytest.mark.parametrize("separator, name", [
    ("dot", "Klovn.S07E01.The.Muslim.Piano.720p.WEBDL.AAC.2.0.h265-DBRETAiL"),
    ("dash", "Klovn-S07E01-The-Muslim-Piano-720p-WEBDL-AAC-2.0-h265-DBRETAiL"),
    ("underscore", "Klovn_S07E01_The_Muslim_Piano_720p_WEBDL_AAC_2.0_h265-DBRETAiL"),
])
def test_other_separators_as_before(separator, name):
    """A dot, dash or underscore name has no spaced " - " (as before)."""
    assert render_pattern(TV, _klovn(), RenameSettings(separator=separator)) == name


@pytest.mark.parametrize("parsed, codec", [
    ("x265", "x265"), ("X265", "x265"), ("h265", "h265"), ("H.265", "h265"), ("HEVC", "h265"),
    ("x264", "x264"), ("h264", "h264"), ("H.264", "h264"), ("AVC", "h264"), ("AV1", "AV1"),
])
def test_the_codec_says_what_the_name_said(parsed, codec):
    """x265 / x264 is that encoder, h265 / h264 any other (as conversions name
    their output, converter._hevc_tag_for_encoder)."""
    assert _normalize_video_codec(parsed) == codec


@pytest.mark.parametrize("parsed, source", [
    ("WEBDL", "WEBDL"), ("webdl", "WEBDL"), ("WEB-DL", "WEB-DL"), ("web.dl", "WEB-DL"), ("www", "WEB"),
    ("webrip", "WEBRip"), ("bluray", "BluRay"), ("HDTV", "HDTV"),
])
def test_the_source_keeps_its_spelling(parsed, source):
    assert _normalize_source(parsed) == source
