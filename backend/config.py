"""Application settings.

Historical note: this app was originally called "Squeezarr" and its env vars
were prefixed `SQUEEZARR_`. The canonical prefix is now `SHRINKERR_`, but the
old prefix is still honoured as a fallback so existing deployments don't
break on upgrade. The fallback is applied before pydantic reads the env, so
both `SHRINKERR_DB_PATH=...` and `SQUEEZARR_DB_PATH=...` Just Work, with the
new name winning if both are set.
"""
import os
from pydantic_settings import BaseSettings


# Backfill SHRINKERR_* from SQUEEZARR_* when the new name isn't set, so that
# old docker-compose files / existing `.env` files keep working verbatim.
# Runs once at module import. Safe to run multiple times (idempotent).
_LEGACY_ENV_PREFIX = "SQUEEZARR_"
_CURRENT_ENV_PREFIX = "SHRINKERR_"
for _key in list(os.environ.keys()):
    if _key.startswith(_LEGACY_ENV_PREFIX):
        _new_key = _CURRENT_ENV_PREFIX + _key[len(_LEGACY_ENV_PREFIX):]
        os.environ.setdefault(_new_key, os.environ[_key])


class Settings(BaseSettings):
    db_path: str = "/app/data/shrinkerr.db"
    media_root: str = "/media"
    port: int = 6680
    default_encoder: str = "nvenc"
    nvenc_cq: int = 20
    libx265_crf: int = 20
    nvenc_preset: str = "p6"
    always_keep_languages: list[str] = []
    ignore_unknown_tracks: bool = True
    ffprobe_timeout: int = 30
    ffmpeg_timeout: int = 21600
    video_extensions: list[str] = [".mkv", ".mp4", ".avi", ".m4v", ".mov", ".ts", ".m2ts"]

    class Config:
        env_prefix = _CURRENT_ENV_PREFIX

settings = Settings()
