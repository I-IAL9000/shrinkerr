"""What the media servers and Sonarr / Radarr say about the library: Plex,
Jellyfin and Emby (labels, collections, genres, watched, plays), Sonarr /
Radarr (cutoff, monitoring) and Maintainerr (leaving soon).

Read after each full scan, and on its own (v0.10.0): a few minutes after
start-up and every few hours. A full scan of a big library takes hours, and
the filters that read these (Not watched in, Sonarr / Radarr, Plex, Leaving
soon) showed 0 until one had run.
"""
import asyncio

STARTUP_DELAY = 300      # seconds: after the start-up work
INTERVAL = 3 * 3600      # seconds between reads
_lock = asyncio.Lock()   # one read at a time: the loop's or a scan's


async def sync_all() -> None:
    """Read them all; each one on its own (one that fails or isn't set up
    leaves the rest)."""
    async with _lock:
        try:
            from backend.plex import sync_plex_metadata_cache
            result = await sync_plex_metadata_cache()
            if result.get("watched") or result.get("unwatched"):
                print(f"[SYNC] Plex watch status: {result.get('watched', 0)} watched, "
                      f"{result.get('unwatched', 0)} unwatched", flush=True)
        except Exception as exc:
            print(f"[SYNC] Plex sync skipped: {exc}", flush=True)
        # The Plex sync replaces the genre / library / watch rows Jellyfin and
        # Emby share with it: theirs come after. Both return at once when not
        # configured.
        try:
            from backend.jellyfin import sync_jellyfin_metadata_cache
            await sync_jellyfin_metadata_cache()
        except Exception as exc:
            print(f"[SYNC] Jellyfin sync skipped: {exc}", flush=True)
        try:
            from backend.emby import sync_emby_metadata_cache
            await sync_emby_metadata_cache()
        except Exception as exc:
            print(f"[SYNC] Emby sync skipped: {exc}", flush=True)
        try:
            from backend.arr import sync_arr_file_status
            await sync_arr_file_status()
        except Exception as exc:
            print(f"[SYNC] Sonarr/Radarr status sync skipped: {exc}", flush=True)
        try:
            from backend.maintainerr import sync_maintainerr
            await sync_maintainerr()
        except Exception as exc:
            print(f"[SYNC] Maintainerr sync skipped: {exc}", flush=True)


async def library_sync_loop() -> None:
    """sync_all() after start-up and every INTERVAL, skipped while a scan
    runs (it reads them itself when it's done)."""
    await asyncio.sleep(STARTUP_DELAY)
    from backend.routes.scan import scan_is_actively_running
    while True:
        if not scan_is_actively_running():
            try:
                await sync_all()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f"[SYNC] Reading the media servers and Sonarr / Radarr failed: {exc}", flush=True)
        await asyncio.sleep(INTERVAL)
