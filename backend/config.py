from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    db_path: str = "/app/data/shrinkarr.db"
    media_root: str = "/media"
    port: int = 6680
    default_encoder: str = "nvenc"
    nvenc_cq: int = 20
    libx265_crf: int = 20
    always_keep_languages: list[str] = ["eng", "isl", "ice"]
    ignore_unknown_tracks: bool = True
    ffprobe_timeout: int = 30
    ffmpeg_timeout: int = 21600
    video_extensions: list[str] = [".mkv", ".mp4", ".avi", ".m4v", ".mov", ".ts", ".m2ts"]

    class Config:
        env_prefix = "SHRINKARR_"

settings = Settings()
