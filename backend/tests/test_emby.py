"""Synthetic API-shape tests for backend/emby.py.

These mock the HTTP responses with captured-from-docs JSON examples and
assert the parsing returns the expected shape. They don't catch real-server
quirks (the user validates those live against their Emby instance) but
they protect against regressions when emby.py is modified.
"""
import pytest
from unittest.mock import patch, AsyncMock
import httpx
from backend import emby


def test_translate_path_no_mapping():
    assert emby._translate_path("/media/foo.mkv", "") == "/media/foo.mkv"


def test_translate_path_with_mapping():
    result = emby._translate_path("/media/foo.mkv", "/media=/mnt/media")
    assert result == "/mnt/media/foo.mkv"


def test_reverse_translate_path():
    result = emby._reverse_translate_path("/mnt/media/foo.mkv", "/media=/mnt/media")
    assert result == "/media/foo.mkv"


@pytest.mark.asyncio
async def test_emby_connection_no_credentials():
    """Returns failure when URL/api_key not configured."""
    with patch("backend.emby._get_emby_settings", new=AsyncMock(return_value={})):
        result = await emby.test_emby_connection()
    assert result["success"] is False
    assert "URL and API key required" in result["error"]


@pytest.mark.asyncio
async def test_emby_connection_success():
    """Parses /System/Info + /Library/VirtualFolders correctly."""
    settings = {
        "emby_url": "http://emby.local:8096",
        "emby_api_key": "test-key",
    }
    system_info = {"ServerName": "Emby Server", "Version": "4.7.14.0"}
    libraries = [
        {"ItemId": "abc", "Name": "Movies", "CollectionType": "movies",
         "Locations": ["/media/Movies"]},
        {"ItemId": "def", "Name": "TV Shows", "CollectionType": "tvshows",
         "Locations": ["/media/TV"]},
    ]

    async def fake_get(self, url, **kwargs):
        if "/System/Info" in url:
            return httpx.Response(200, json=system_info)
        if "/Library/VirtualFolders" in url:
            return httpx.Response(200, json=libraries)
        raise AssertionError(f"unexpected URL: {url}")

    with patch("backend.emby._get_emby_settings", new=AsyncMock(return_value=settings)):
        with patch("httpx.AsyncClient.get", new=fake_get):
            result = await emby.test_emby_connection()

    assert result["success"] is True
    assert result["server_name"] == "Emby Server"
    assert result["library_count"] == 2
    assert len(result["libraries"]) == 2
    assert result["libraries"][0]["title"] == "Movies"


@pytest.mark.asyncio
async def test_get_user_id_stored():
    """Stored user_id short-circuits the lookup."""
    result = await emby._get_user_id("http://x", "key", stored_user_id="user-123")
    assert result == "user-123"


@pytest.mark.asyncio
async def test_get_user_id_auto_admin():
    """Picks the first admin user from /Users."""
    users = [
        {"Id": "user-1", "Name": "alice", "Policy": {"IsAdministrator": False}},
        {"Id": "user-2", "Name": "bob",   "Policy": {"IsAdministrator": True}},
        {"Id": "user-3", "Name": "carol", "Policy": {"IsAdministrator": True}},
    ]

    async def fake_get(self, url, **kwargs):
        return httpx.Response(200, json=users)

    with patch("httpx.AsyncClient.get", new=fake_get):
        result = await emby._get_user_id("http://x", "key", "")
    assert result == "user-2"  # first admin


@pytest.mark.asyncio
async def test_trigger_emby_scan_no_credentials():
    """Returns False when not configured."""
    with patch("backend.emby._get_emby_settings", new=AsyncMock(return_value={})):
        result = await emby.trigger_emby_scan("/media/foo.mkv")
    assert result is False


@pytest.mark.asyncio
async def test_trigger_emby_scan_success():
    """POSTs to /Library/Refresh and returns True on 204."""
    settings = {"emby_url": "http://x", "emby_api_key": "key"}
    captured = {}

    async def fake_post(self, url, **kwargs):
        captured["url"] = url
        return httpx.Response(204)

    with patch("backend.emby._get_emby_settings", new=AsyncMock(return_value=settings)):
        with patch("httpx.AsyncClient.post", new=fake_post):
            result = await emby.trigger_emby_scan("/media/foo.mkv")
    assert result is True
    assert "/Library/Refresh" in captured["url"]
