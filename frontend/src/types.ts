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
  native_language: string;
  has_removable_tracks: boolean;
  estimated_savings_bytes: number;
  estimated_savings_gb: number;
}

export interface Job {
  id: number;
  file_path: string;
  file_name: string;
  job_type: string;
  status: string;
  encoder: string | null;
  audio_tracks_to_remove: number[];
  progress: number;
  fps: number | null;
  eta_seconds: number | null;
  error_log: string | null;
  space_saved: number;
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
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
}

export interface JobComplete {
  type: "job_complete";
  job_id: number;
  status: string;
  space_saved: number;
  error: string | null;
}

export type WSMessage = ScanProgress | JobProgress | JobComplete;
