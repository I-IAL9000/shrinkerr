"""Rules' Sonarr/Radarr tag condition (v0.10.0): listing a whole library
takes Sonarr and Radarr 10-20 s, and the lists were kept for 5 minutes, so
an Add to Queue estimate opened later waited ~18 s for them. Rules now use
the last list while a newer one is fetched in the background, and start-up
fetches them when a rule needs them."""
import asyncio
import time

import pytest

from backend.tests.test_rule_conditions import FILE, _rule

OLD = [{"path": "/tv/Show (2010)", "tags": [1]}]
NEW = [{"path": "/tv/Show (2010)", "tags": [2]}]


@pytest.fixture
def sonarr(monkeypatch):
    """A Sonarr whose library listing waits until `listed` is set."""
    import backend.arr as arr

    async def settings():
        return {"sonarr_url": "http://sonarr", "sonarr_api_key": "k", "sonarr_path_mapping": "/media/TV=/tv"}

    async def labels(client, service, url, key):
        return {1: "old-tag", 2: "new-tag"}

    listed, fetches = asyncio.Event(), []

    async def fetch(client, service, url, key):
        fetches.append(service)
        await listed.wait()
        arr._sonarr_cache.update(data=NEW, fetched_at=time.monotonic())
        return NEW

    monkeypatch.setattr(arr, "_get_arr_settings", settings)
    monkeypatch.setattr(arr, "_arr_tag_labels", labels)
    monkeypatch.setattr(arr, "_fetch_items", fetch)
    monkeypatch.setattr(arr, "_sonarr_cache", {})
    monkeypatch.setattr(arr, "_refreshing", {})
    return arr, listed, fetches


@pytest.mark.asyncio
async def test_an_expired_list_answers_while_a_new_one_is_fetched(sonarr):
    arr, listed, fetches = sonarr
    arr._sonarr_cache.update(data=OLD, fetched_at=time.monotonic() - 3600)
    got = await asyncio.wait_for(arr.arr_tags_for_paths([FILE]), 1)  # doesn't wait for Sonarr
    await asyncio.sleep(0)  # (the refresh starts)
    assert got == {FILE: {"old-tag"}} and fetches == ["sonarr"]
    await asyncio.wait_for(arr.arr_tags_for_paths([FILE]), 1)
    assert fetches == ["sonarr"]  # one refresh at a time
    listed.set()
    await asyncio.wait_for(arr._refreshing["sonarr"], 1)
    assert await arr.arr_tags_for_paths([FILE]) == {FILE: {"new-tag"}}


@pytest.mark.asyncio
async def test_without_a_list_it_waits_for_one(sonarr):
    arr, listed, fetches = sonarr
    listed.set()
    assert await arr.arr_tags_for_paths([FILE]) == {FILE: {"new-tag"}} and fetches == ["sonarr"]


@pytest.mark.asyncio
async def test_start_up_fetches_them_only_for_a_rule_that_needs_them(test_db, sonarr):
    arr, listed, fetches = sonarr
    listed.set()
    await _rule(test_db, "codec", "h264")
    await arr.warm_rule_tags()
    assert fetches == []
    await _rule(test_db, "arr_tag", "keep-original")
    await arr.warm_rule_tags()
    assert fetches == ["sonarr"]
