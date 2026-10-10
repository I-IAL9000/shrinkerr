"""The NZBGet / SABnzbd scripts' Sonarr / Radarr tag check (v0.10.0).

With "Check Sonarr tags" or "Check Radarr tags" turned off, downloads of
that kind were never checked — and so never processed: every one was
skipped as "Not found in Sonarr or Radarr" unless neither app was set up.
A check that's off (or whose app isn't set up) now lets those downloads
through, as having no Sonarr / Radarr at all always did.
"""
import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = [REPO / "nzbget-extension" / "Shrinkerr" / "Shrinkerr.py", REPO / "sabnzbd-script" / "shrinkerr.py"]

BOTH = {"sonarr_url": "http://sonarr", "sonarr_api_key": "s", "radarr_url": "http://radarr", "radarr_api_key": "r",
        "tags": ["convert"]}
TV, MOVIE = "Show.S01E02.1080p.WEB-DL", "Film.2021.1080p.BluRay"


@pytest.fixture(params=SCRIPTS, ids=["nzbget", "sabnzbd"])
def script(request, monkeypatch):
    spec = importlib.util.spec_from_file_location(f"script_{request.node.callspec.id}", request.param)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.lookups = []
    library = {"sonarr": {}, "radarr": {}}

    def find_series(name, url, key):
        mod.lookups.append("sonarr")
        return library["sonarr"].get(name)

    def find_movie(name, url, key):
        mod.lookups.append("radarr")
        return library["radarr"].get(name)
    monkeypatch.setattr(mod, "find_series_in_sonarr", find_series)
    monkeypatch.setattr(mod, "find_movie_in_radarr", find_movie)
    # The two scripts keep Sonarr / Radarr's tag list in different shapes.
    tag_list = {1: "convert"} if "nzbget" in str(request.param) else [{"id": 1, "label": "convert"}]
    monkeypatch.setattr(mod, "get_sonarr_tags", lambda url, key: tag_list)
    monkeypatch.setattr(mod, "get_radarr_tags", lambda url, key: tag_list)
    monkeypatch.setattr(mod, "log", lambda msg: None)
    mod.library = library
    return mod


def test_a_check_thats_off_lets_that_kind_through(script):
    assert script.tag_check(TV, {**BOTH, "check_sonarr_tags": False}) is True
    assert script.tag_check(MOVIE, {**BOTH, "check_radarr_tags": False}) is True
    off = {**BOTH, "check_sonarr_tags": False, "check_radarr_tags": False}
    assert script.tag_check(TV, off) is True and script.tag_check(MOVIE, off) is True
    assert script.lookups == []


def test_an_app_thats_not_set_up_lets_that_kind_through(script):
    only_sonarr = {"sonarr_url": "http://sonarr", "sonarr_api_key": "s", "tags": ["convert"]}
    assert script.tag_check(MOVIE, only_sonarr) is True
    assert script.tag_check(TV, {}) is True and script.tag_check(MOVIE, {}) is True


def test_a_check_thats_on_needs_the_tag(script):
    script.library["sonarr"][TV] = {"title": "Show", "tags": [1]}
    script.library["radarr"][MOVIE] = {"title": "Film", "tags": []}
    assert script.tag_check(TV, BOTH) is True
    assert script.tag_check(MOVIE, BOTH) is False          # no tag
    assert script.tag_check("Other.S01E01", BOTH) is False  # nobody knows it
    assert script.tag_check(TV, {**BOTH, "check_radarr_tags": False}) is True
