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
    monkeypatch.setattr(stats, "_get_current_version", lambda: current)
    stats._update_cache.clear()
    stats._update_cache.update({"version": None, "checked_at": 0})

    result = await stats.refresh_update_check()
    assert result["current"] == current
    assert result["update_available"] is expected
