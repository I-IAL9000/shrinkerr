from pydantic import BaseModel
from typing import Optional

class AudioTrack(BaseModel):
    stream_index: int
    language: str
    codec: str
    channels: int
    title: str = ""
    bitrate: Optional[int] = None
    size_estimate_bytes: Optional[int] = None
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
    native_language: str
    has_removable_tracks: bool
    estimated_savings_bytes: int
    estimated_savings_gb: float

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
    default_encoder: Optional[str] = None
    nvenc_cq: Optional[int] = None
    libx265_crf: Optional[int] = None
    always_keep_languages: Optional[list[str]] = None
    ignore_unknown_tracks: Optional[bool] = None
