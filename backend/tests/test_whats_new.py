"""v0.10.0: release notes shown once after an update."""
import aiosqlite
import pytest

import backend.routes.stats as stats
from backend.routes.stats import _one_liner, whats_new_since

CHANGELOG = """# Changelog

## [Unreleased]

### Fixed
- Not released yet.

## [0.10.0] — 2026-10-20

### Added
- **Password step in the setup wizard.** It asks for a username and password.
- Optional PUID / PGID for the Docker images. Files are then owned like your other apps'.

### Fixed
- **Rules on Sonarr/Radarr tags now work** — they never matched.
- Links in the in-app changelog only open http(s) addresses.

## [0.9.157] — 2026-10-08

### Security
- **Custom ffmpeg flags can't write files.**

## [0.9.150] — 2026-10-01

### Fixed
- **Old fix.**
"""


@pytest.fixture
def changelog(tmp_path, monkeypatch, test_db):
    path = tmp_path / "CHANGELOG.md"
    path.write_text(CHANGELOG)
    version = tmp_path / "VERSION"
    version.write_text("0.10.0\n")
    monkeypatch.setattr(stats, "_CHANGELOG_FILE", path)
    monkeypatch.setattr(stats, "_VERSION_FILE", version)
    monkeypatch.setattr(stats, "_changelog_cache", {})
    return version


def test_one_liners():
    assert _one_liner("**Rules on Sonarr/Radarr tags now work** — they never matched.") == "Rules on Sonarr/Radarr tags now work"
    assert _one_liner("Optional `PUID` / PGID. Files are then owned like yours.") == "Optional PUID / PGID"
    assert _one_liner("See [the docs](https://x.y) for more") == "See the docs for more"


def test_notes_cover_every_release_since_the_last_one_seen(changelog):
    entries = stats._parse_changelog()
    notes = whats_new_since(entries, "0.9.150", "0.10.0")
    assert notes == {
        "new": ["Password step in the setup wizard", "Optional PUID / PGID for the Docker images"],
        "fixed": ["Rules on Sonarr/Radarr tags now work", "Custom ffmpeg flags can't write files"],
        "more_fixes": 1,
    }
    assert whats_new_since(entries, None, "0.10.0")["fixed"] == ["Rules on Sonarr/Radarr tags now work"]


async def _add_media_dir(db_path):
    async with aiosqlite.connect(db_path) as db:
        await db.execute("INSERT INTO media_dirs (path) VALUES ('/media')")
        await db.commit()


@pytest.mark.asyncio
async def test_shown_once_after_an_update(changelog, test_db):
    await _add_media_dir(test_db)
    got = await stats.whats_new()
    assert got["show"] and got["version"] == "0.10.0"
    await stats.whats_new_seen()
    assert (await stats.whats_new()) == {"show": False}


@pytest.mark.asyncio
async def test_not_on_a_fresh_install(changelog, test_db):
    assert (await stats.whats_new()) == {"show": False}
    await _add_media_dir(test_db)
    assert (await stats.whats_new()) == {"show": False}  # remembered as seen


@pytest.mark.asyncio
async def test_not_on_development_builds(changelog, test_db):
    await _add_media_dir(test_db)
    changelog.write_text("0.10.0-dev.12+abc\n")
    assert (await stats.whats_new()) == {"show": False}


@pytest.mark.asyncio
async def test_preview_shows_the_unreleased_notes_on_a_dev_build(changelog, test_db):
    changelog.write_text("0.10.0-dev.851+f1497f5\n")
    got = await stats.whats_new(preview=True)
    assert got["show"] and got["version"] == "0.10.0" and got["more_fixes"] == 1 and got["fixed"] == []


@pytest.mark.asyncio
async def test_preview_on_a_release_shows_its_notes_even_when_seen(changelog, test_db):
    await stats.whats_new_seen()
    got = await stats.whats_new(preview=True)
    assert got["show"] and got["fixed"] == ["Rules on Sonarr/Radarr tags now work"]
