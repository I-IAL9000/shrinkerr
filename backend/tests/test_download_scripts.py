"""Settings' NZBGet and SABnzbd script downloads (v0.10.0): they read the
script from nzbget-extension/ and sabnzbd-script/, which the Docker images
never copied — so in Docker the download failed ("Site wasn't available")."""
from pathlib import Path

import pytest
from starlette.requests import Request

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = {"download_nzbget_script": "nzbget-extension/", "download_sabnzbd_script": "sabnzbd-script/"}


def _request() -> Request:
    return Request({"type": "http", "scheme": "http", "server": ("nas", 6680), "path": "/", "root_path": "",
                    "headers": [], "query_string": b""})


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", sorted(SCRIPTS))
async def test_the_script_downloads_with_this_server_baked_in(test_db, monkeypatch, endpoint):
    import backend.routes.settings as route
    monkeypatch.setattr(route, "DB_PATH", test_db, raising=False)
    response = await getattr(route, endpoint)(_request())
    body = response.body.decode()
    assert "http://nas:6680" in body and "__SHRINKERR_URL__" not in body


@pytest.mark.parametrize("dockerfile", ["Dockerfile", "Dockerfile.nvenc"])
def test_the_images_hold_the_scripts(dockerfile):
    copies = [line.split()[1] for line in (ROOT / dockerfile).read_text().splitlines() if line.startswith("COPY ")]
    assert set(SCRIPTS.values()) <= set(copies), copies
