export interface AudioTrack {
  stream_index: number;
  language: string;
  codec: string;
  channels: number;
  title: string;
  bitrate: number | null;
  size_estimate_bytes: number | null;
  keep: boolean;
  locked: boolean;
}

export interface SubtitleTrack {
  stream_index: number;
  language: string;
  codec: string;
  title: string;
  forced: boolean;
  keep: boolean;
  locked: boolean;
  external?: boolean;
  external_path?: string;
}

export interface ScannedFile {
  id: number;
  file_path: string;
  file_name: string;
  folder_name: string;
  file_size: number;
  file_size_gb: number;
  video_codec: string;
  needs_conversion: boolean;
  audio_tracks: AudioTrack[];
  subtitle_tracks: SubtitleTrack[];
  native_language: string;
  language_source: string; // "api" | "heuristic"
  has_removable_tracks: boolean;
  has_removable_subs: boolean;
  estimated_savings_bytes: number;
  estimated_savings_gb: number;
  ignored: boolean;
  is_new: boolean;
  queued: boolean;
  converted: boolean;
  low_bitrate: boolean;
  has_lossless_audio: boolean;
  duration: number;
  file_mtime: number | null;
  probe_status?: string;
  health_status?: "healthy" | "corrupt" | "warnings" | null;
  health_check_type?: "quick" | "thorough" | null;
  health_checked_at?: string | null;
}

export interface Job {
  id: number;
  file_path: string;
  file_name: string;
  job_type: string;
  status: string;
  encoder: string | null;
  audio_tracks_to_remove: number[];
  subtitle_tracks_to_remove: number[];
  progress: number;
  fps: number | null;
  eta_seconds: number | null;
  error_log: string | null;
  space_saved: number;
  original_size: number;
  nvenc_preset: string | null;
  nvenc_cq: number | null;
  libx265_preset: string | null;
  libx265_crf: number | null;
  audio_codec: string | null;
  audio_bitrate: number | null;
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
  health_status?: "healthy" | "corrupt" | "warnings" | null;
  health_errors_json?: string | null;
  health_check_type?: "quick" | "thorough" | null;
  health_check_seconds?: number | null;
}

export interface WorkerNode {
  id: string;
  name: string;
  hostname: string;
  capabilities: string[];
  status: "online" | "offline" | "working" | "error";
  last_heartbeat: string | null;
  registered_at: string;
  current_job_id: number | null;
  current_job_file?: string;
  current_job_progress?: number;
  jobs_completed: number;
  total_space_saved: number;
  path_mappings: { server: string; worker: string }[];
  ffmpeg_version: string | null;
  gpu_name: string | null;
  os_info: string | null;
  max_jobs: number;
  consecutive_failures: number;
  paused: boolean;
  job_affinity: "any" | "cpu_only" | "nvenc_only";
  translate_encoder: boolean;
  schedule_enabled: boolean;
  schedule_hours: number[];
}

export interface NodeSettings {
  paused?: boolean;
  max_jobs?: number;
  job_affinity?: "any" | "cpu_only" | "nvenc_only";
  translate_encoder?: boolean;
  schedule_enabled?: boolean;
  schedule_hours?: number[];
}

export interface QueueStats {
  total_jobs: number;
  pending: number;
  running: number;
  completed: number;
  failed: number;
  total_space_saved: number;
}

export interface MediaDir {
  id: number;
  path: string;
  label: string;
  enabled: boolean;
}

export interface ScanProgress {
  type: "scan_progress";
  status: string;
  current_file: string;
  total: number;
  probed: number;
}

export interface JobProgress {
  type: "job_progress";
  job_id: number;
  file_name: string;
  progress: number;
  fps: number | null;
  eta: number | null;
  step: string;
  jobs_completed: number;
  jobs_total: number;
  total_saved: number;
  node_name?: string;
  node_id?: string;
}

export interface JobComplete {
  type: "job_complete";
  job_id: number;
  status: string;
  space_saved: number;
  error: string | null;
}

export type WSMessage = ScanProgress | JobProgress | JobComplete;
