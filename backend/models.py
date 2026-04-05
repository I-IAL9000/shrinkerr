from pydantic import BaseModel
from typing import Any, Optional

class AudioTrack(BaseModel):
    stream_index: int
    language: str
    codec: str
    channels: int
    title: str = ""
    bitrate: Optional[int] = None
    profile: Optional[str] = None
    size_estimate_bytes: Optional[int] = None
    keep: bool = True
    locked: bool = False

class SubtitleTrack(BaseModel):
    stream_index: int
    language: str
    codec: str
    title: str = ""
    forced: bool = False
    keep: bool = True
    locked: bool = False

class ScannedFile(BaseModel):
    id: Optional[int] = None
    file_path: str
    file_name: str
    folder_name: str
    file_size: int
    file_size_gb: float
    video_codec: str
    needs_conversion: bool
    audio_tracks: list[AudioTrack]
    subtitle_tracks: list[SubtitleTrack] = []
    native_language: str
    has_removable_tracks: bool
    has_removable_subs: bool = False
    estimated_savings_bytes: int
    estimated_savings_gb: float
    language_source: str = "heuristic"
    ignored: bool = False
    file_mtime: Optional[float] = None  # File modification time (Unix timestamp)
    duration: Optional[float] = None  # Duration in seconds (from ffprobe)
    probe_status: str = "ok"  # "ok", "corrupt", "truncated"
    video_height: int = 0  # Video resolution height (e.g., 1080, 2160)

class ScanRequest(BaseModel):
    paths: list[str]

class ScanProgress(BaseModel):
    status: str
    current_file: str = ""
    files_found: int = 0
    files_probed: int = 0
    total_files: int = 0

class JobCreate(BaseModel):
    file_path: str
    job_type: str
    encoder: str = "nvenc"
    audio_tracks_to_remove: list[int] = []

class Job(BaseModel):
    id: int
    file_path: str
    file_name: str
    job_type: str
    status: str
    encoder: Optional[str] = None
    audio_tracks_to_remove: list[int] = []
    progress: float = 0
    fps: Optional[float] = None
    eta_seconds: Optional[int] = None
    error_log: Optional[str] = None
    space_saved: int = 0
    created_at: str
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    queue_order: Optional[int] = None

class JobProgress(BaseModel):
    job_id: int
    file_name: str
    progress: float
    fps: Optional[float] = None
    eta_seconds: Optional[int] = None
    current_step: str = ""
    jobs_completed: int = 0
    jobs_total: int = 0
    total_space_saved: int = 0

class QueueStats(BaseModel):
    total_jobs: int
    pending: int
    running: int
    completed: int
    failed: int
    total_space_saved: int
    estimated_time_remaining: Optional[int] = None

class ScheduleCreate(BaseModel):
    scheduled_start: str

class MediaDir(BaseModel):
    id: Optional[int] = None
    path: str
    label: str = ""
    enabled: bool = True

class SettingsUpdate(BaseModel):
    model_config = {"extra": "ignore"}  # Allow extra fields from GET response passthrough
    default_encoder: Optional[str] = None
    nvenc_cq: Optional[int] = None
    libx265_crf: Optional[int] = None
    nvenc_preset: Optional[str] = None
    parallel_jobs: Optional[int] = None
    ffmpeg_timeout: Optional[int] = None
    ffprobe_timeout: Optional[int] = None
    audio_cleanup_enabled: Optional[bool] = None
    always_keep_languages: Optional[list[str]] = None
    ignore_unknown_tracks: Optional[bool] = None
    target_codec: Optional[str] = None
    target_resolution: Optional[str] = None
    source_codecs: Optional[list[str]] = None
    sub_cleanup_enabled: Optional[bool] = None
    sub_keep_languages: Optional[list[str]] = None
    sub_keep_unknown: Optional[bool] = None
    audio_codec: Optional[str] = None
    audio_bitrate: Optional[int] = None
    audio_downmix: Optional[bool] = None
    auto_queue_new: Optional[bool] = None
    auto_convert_lossless: Optional[bool] = None
    lossless_target_codec: Optional[str] = None
    lossless_target_bitrate: Optional[int] = None
    tmdb_api_key: Optional[str] = None
    tvdb_api_key: Optional[str] = None
    plex_url: Optional[str] = None
    plex_token: Optional[str] = None
    plex_path_mapping: Optional[str] = None
    plex_ignore_labels: Optional[str] = None
    plex_empty_trash_after_scan: Optional[bool] = None
    min_bitrate_mbps: Optional[Any] = None
    max_bitrate_mbps: Optional[Any] = None
    min_file_size_mb: Optional[Any] = None
    plex_prioritize_unwatched: Optional[bool] = None
    plex_pause_on_stream: Optional[bool] = None
    plex_pause_stream_threshold: Optional[Any] = None
    plex_pause_transcode_only: Optional[bool] = None
    trash_original_after_conversion: Optional[bool] = None
    backup_original_days: Optional[Any] = None
    backup_folder: Optional[str] = None
    vmaf_analysis_enabled: Optional[bool] = None
    filename_suffix: Optional[str] = None
    custom_ffmpeg_flags: Optional[str] = None
    max_plex_api_calls: Optional[Any] = None
    api_key: Optional[str] = None
    skip_files_newer_enabled: Optional[bool] = None
    skip_files_newer_than_minutes: Optional[Any] = None
    sonarr_url: Optional[str] = None
    sonarr_api_key: Optional[str] = None
    sonarr_path_mapping: Optional[str] = None
    radarr_url: Optional[str] = None
    radarr_api_key: Optional[str] = None
    radarr_path_mapping: Optional[str] = None
    # Quiet hours
    quiet_hours_enabled: Optional[bool] = None
    quiet_hours_start: Optional[Any] = None
    quiet_hours_end: Optional[Any] = None
    quiet_hours_parallel: Optional[Any] = None
    quiet_hours_nice: Optional[bool] = None
    # Notifications
    discord_webhook_url: Optional[str] = None
    telegram_bot_token: Optional[str] = None
    telegram_chat_id: Optional[str] = None
    smtp_host: Optional[str] = None
    smtp_port: Optional[Any] = None
    smtp_user: Optional[str] = None
    smtp_pass: Optional[str] = None
    smtp_from: Optional[str] = None
    email_to: Optional[str] = None
    webhook_url: Optional[str] = None
    notify_queue_complete: Optional[bool] = None
    notify_job_failed: Optional[bool] = None
    notify_disk_low: Optional[bool] = None
    disk_space_threshold_gb: Optional[Any] = None
