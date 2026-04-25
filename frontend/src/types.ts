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
  // VMAF score for the last encode of this file. Used by the Scanner's
  // VMAF filters and by FileDetail's History tab to always surface the
  // score even when the corresponding file-event wasn't logged (or was
  // logged against a pre-rename path that no longer matches).
  vmaf_score?: number | null;
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
  // True when libvmaf desynced on every analysis window we tried, so the
  // recorded score is the user's best estimate but isn't a trustworthy
  // verdict. UI shows a ⚠ glyph alongside the score. v0.3.32+.
  vmaf_uncertain?: boolean;
  // VMAF score on completed jobs (mirrored from scan_results).
  vmaf_score?: number | null;
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
  // Per-node auth token state (v0.3.30+). `has_token` is a bool indicator;
  // the token value itself never leaves the server. `token_issued_at` is
  // the ISO timestamp of the last bootstrap / rotation and is surfaced so
  // the UI can show when the channel was last re-keyed.
  has_token?: boolean;
  token_issued_at?: string | null;
  // Admin path-mappings override (v0.3.31+). null means "no override — use
  // the worker's env-var reported mappings". A list (possibly empty) means
  // the UI has explicitly set them, bypassing env-var.
  path_mappings_override?: { server: string; worker: string }[] | null;
}

export interface NodeSettings {
  paused?: boolean;
  max_jobs?: number;
  job_affinity?: "any" | "cpu_only" | "nvenc_only";
  translate_encoder?: boolean;
  schedule_enabled?: boolean;
  schedule_hours?: number[];
  // Tri-state: absent (not patching), null (clear override), array (set override).
  // Sending this as undefined leaves the server value alone; `null` clears the
  // override to revert to the worker's env-var mappings.
  path_mappings_override?: { server: string; worker: string }[] | null;
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
