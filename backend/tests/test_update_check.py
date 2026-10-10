"""Tests for the update-available check (v0.9.29).

The pill compares the running version against GitHub's latest published
Release. It must use a semver comparison, not string inequality — otherwise
a lagging or failed Release-notes job (latest < current) wrongly shows
"update available", nagging users to "update" to an older version.
"""
import pytest

import backend.routes.stats as stats


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "current,latest,expected",
    [
        ("0.9.27", "0.9.28", True),    # behind -> update available
        ("0.9.28", "0.9.28", False),   # equal -> no update
        ("0.9.28", "0.9.27", False),   # AHEAD -> no phantom downgrade prompt
        ("0.9.28", None, False),       # no release info -> no update
        ("0.9.9", "0.9.10", True),     # numeric, not lexical (0.9.10 > 0.9.9)
    ],
)
async def test_update_available_is_semver(monkeypatch, current, latest, expected):
    async def fake_fetch():
        return latest

    monkeypatch.setattr(stats, "_fetch_latest_release_tag", fake_fetch)
    _fresh(monkeypatch, current)

    result = await stats.refresh_update_check()
    assert result["current"] == current
    assert result["update_available"] is expected


@pytest.mark.parametrize("current,latest,expected", [
    ("0.10.0-rc.1", "0.10.0", True),        # an rc hears about its release
    ("0.10.0-rc.1", "0.10.0-rc.2", True),
    ("0.10.0", "0.10.0-rc.1", False),
    ("0.10.0", "v0.10.1", True),
    ("0.11.0-dev.9+aaaaaaa", "0.11.0-dev.10+bbbbbbb", True),   # develop: by build number
    ("0.11.0-dev.10+aaaaaaa", "0.11.0-dev.9+bbbbbbb", False),
])
def test_version_order(current, latest, expected):
    assert stats._newer(latest, current) is expected


def _fresh(monkeypatch, current, enabled=True):
    async def check_enabled():
        return enabled
    monkeypatch.setattr(stats, "_get_current_version", lambda: current)
    monkeypatch.setattr(stats, "_update_check_enabled", check_enabled)
    stats._update_cache.clear()
    stats._update_cache.update({"version": None, "checked_at": 0})


@pytest.mark.asyncio
async def test_a_develop_build_hears_about_develop_builds(monkeypatch):
    asked = []

    async def develop():
        asked.append("develop")
        return "0.11.0-dev.12+bbbbbbb"

    async def release():
        asked.append("release")
        return "0.10.0"
    monkeypatch.setattr(stats, "_fetch_latest_develop_build", develop)
    monkeypatch.setattr(stats, "_fetch_latest_release_tag", release)
    _fresh(monkeypatch, "0.11.0-dev.9+aaaaaaa")
    result = await stats.refresh_update_check()
    assert asked == ["develop"]
    assert (result["channel"], result["latest"], result["update_available"]) == ("develop", "0.11.0-dev.12+bbbbbbb", True)
    assert result["compare_url"] == "https://github.com/I-IAL9000/shrinkerr/compare/aaaaaaa...bbbbbbb"
    _fresh(monkeypatch, "0.10.0")
    result = await stats.refresh_update_check()
    assert asked[-1] == "release" and result["channel"] == "stable" and result["compare_url"] is None


@pytest.mark.asyncio
async def test_the_check_can_be_turned_off(monkeypatch):
    async def never():
        raise AssertionError("asked GitHub")
    monkeypatch.setattr(stats, "_fetch_latest_release_tag", never)
    _fresh(monkeypatch, "0.9.1", enabled=False)
    stats._update_cache.update({"version": "0.10.0", "checked_at": 0})
    result = await stats.refresh_update_check()
    assert (result["check_enabled"], result["latest"], result["update_available"]) == (False, None, False)


@pytest.mark.asyncio
async def test_the_setting(test_db, monkeypatch):
    import backend.routes.settings as settings_route
    from backend.models import SettingsUpdate
    monkeypatch.setattr(settings_route, "DB_PATH", test_db)
    assert await stats._update_check_enabled() is True  # on by default
    stats._update_cache["checked_at"] = 123
    await settings_route.update_encoding_settings(SettingsUpdate(update_check=False))
    assert await stats._update_check_enabled() is False and stats._update_cache["checked_at"] == 0
    assert (await settings_route.get_encoding_settings())["update_check"] is False


@pytest.mark.asyncio
async def test_finding_the_latest_develop_build(monkeypatch):
    import httpx
    version = {"text": "0.11.0\n"}

    def handler(request):
        if request.url.host == "api.github.com":
            assert request.url.params["branch"] == "develop" and request.url.params["status"] == "success"
            return httpx.Response(200, json={"workflow_runs": [{"head_sha": "b" * 40, "run_number": 912}]})
        assert request.url.path == f"/I-IAL9000/shrinkerr/{'b' * 40}/VERSION"
        return httpx.Response(200, text=version["text"])
    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler)))
    assert await stats._fetch_latest_develop_build() == "0.11.0-dev.912+bbbbbbb"
    version["text"] = "<html>rate limited</html>"
    assert await stats._fetch_latest_develop_build() is None
