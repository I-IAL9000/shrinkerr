import { useState, useEffect } from "react";
import { getScanResults, getMediaDirs, startScan, removeScanResult, addBulkJobs } from "../api";
import StatsCards from "../components/StatsCards";
import FilterBar from "../components/FilterBar";
import FileTree from "../components/FileTree";
import type { ScannedFile, ScanProgress } from "../types";

interface ScannerPageProps {
  scanProgress: ScanProgress | null;
}

export default function ScannerPage({ scanProgress }: ScannerPageProps) {
  const [files, setFiles] = useState<ScannedFile[]>([]);
  const [dirs, setDirs] = useState<any[]>([]);
  const [selectedDir, setSelectedDir] = useState<string>("all");
  const [filter, setFilter] = useState("all");
  const [selectedPaths, setSelectedPaths] = useState<Set<string>>(new Set());
  const [scanning, setScanning] = useState(false);

  useEffect(() => {
    getMediaDirs().then((r: any) => setDirs(Array.isArray(r) ? r : r.dirs || []));
    loadResults();
  }, []);

  useEffect(() => {
    if (scanProgress?.status === "complete") {
      setScanning(false);
      loadResults();
    }
  }, [scanProgress]);

  const loadResults = async () => {
    const data = await getScanResults();
    const rows = Array.isArray(data) ? data : [];
    const parsed = rows.map((row: any) => ({
      ...row,
      file_name: row.file_path.split("/").pop(),
      folder_name: row.file_path.split("/").slice(-2, -1)[0],
      file_size_gb: +(row.file_size / (1024 ** 3)).toFixed(2),
      audio_tracks: typeof row.audio_tracks === "string"
        ? JSON.parse(row.audio_tracks || "[]")
        : (row.audio_tracks || []),
      has_removable_tracks: (
        typeof row.audio_tracks === "string"
          ? JSON.parse(row.audio_tracks || "[]")
          : (row.audio_tracks || [])
      ).some((t: any) => !t.keep),
      estimated_savings_bytes: row.needs_conversion ? Math.round(row.file_size * 0.3) : 0,
      estimated_savings_gb: +(row.needs_conversion ? row.file_size * 0.3 / (1024 ** 3) : 0).toFixed(2),
    }));
    setFiles(parsed);
  };

  const handleScan = async () => {
    setScanning(true);
    const paths = selectedDir === "all"
      ? dirs.map((d: any) => d.path)
      : [selectedDir];
    await startScan(paths);
  };

  const handleToggleSelect = (path: string) => {
    setSelectedPaths((prev) => {
      const next = new Set(prev);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });
  };

  const handleToggleTrack = (filePath: string, streamIndex: number) => {
    setFiles((prev) =>
      prev.map((f) => {
        if (f.file_path !== filePath) return f;
        return {
          ...f,
          audio_tracks: f.audio_tracks.map((t) =>
            t.stream_index === streamIndex ? { ...t, keep: !t.keep } : t
          ),
        };
      })
    );
  };

  const handleRemoveFile = async (filePath: string) => {
    const file = files.find((f) => f.file_path === filePath);
    if (file?.id) await removeScanResult(file.id);
    setFiles((prev) => prev.filter((f) => f.file_path !== filePath));
    setSelectedPaths((prev) => { const n = new Set(prev); n.delete(filePath); return n; });
  };

  const handleAddToQueue = async () => {
    const selected = files.filter((f) => selectedPaths.has(f.file_path));
    const jobs = selected.map((f) => {
      const removeTracks = f.audio_tracks
        .filter((t) => !t.keep && !t.locked)
        .map((t) => t.stream_index);
      let jobType = "convert";
      if (f.needs_conversion && removeTracks.length > 0) jobType = "combined";
      else if (!f.needs_conversion && removeTracks.length > 0) jobType = "audio";
      return {
        file_path: f.file_path,
        job_type: jobType,
        encoder: "nvenc",
        audio_tracks_to_remove: removeTracks,
      };
    });
    await addBulkJobs(jobs);
    setSelectedPaths(new Set());
  };

  const filtered = files.filter((f) => {
    if (filter === "needs_conversion") return f.needs_conversion;
    if (filter === "audio_cleanup") return f.has_removable_tracks;
    if (filter === "optimized") return !f.needs_conversion && !f.has_removable_tracks;
    return true;
  });

  const stats = {
    filesToConvert: files.filter((f) => f.needs_conversion).length,
    audioCleanup: files.filter((f) => f.has_removable_tracks).length,
    estimatedSavingsGB: files.reduce((sum, f) => sum + f.estimated_savings_gb, 0),
    totalScannedGB: files.reduce((sum, f) => sum + f.file_size_gb, 0),
  };

  return (
    <div>
      <div style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 20 }}>
        <select
          value={selectedDir}
          onChange={(e) => setSelectedDir(e.target.value)}
          style={{
            background: "var(--bg-card)", color: "var(--text-secondary)",
            border: "1px solid var(--border)", padding: "6px 10px",
            borderRadius: 4, fontSize: 13,
          }}
        >
          <option value="all">All configured paths</option>
          {dirs.map((d: any) => (
            <option key={d.id} value={d.path}>{d.path}</option>
          ))}
        </select>
        <button className="btn btn-primary" onClick={handleScan} disabled={scanning}>
          {scanning ? "Scanning..." : "Scan"}
        </button>
        {scanning && scanProgress && (
          <span style={{ fontSize: 12, opacity: 0.6 }}>
            {scanProgress.probed} / {scanProgress.total} files probed
          </span>
        )}
      </div>

      {scanning && scanProgress && (
        <div style={{ marginBottom: 16 }}>
          <div className="progress-bar-track">
            <div
              className="progress-bar-fill"
              style={{
                width: `${scanProgress.total > 0
                  ? (scanProgress.probed / scanProgress.total) * 100
                  : 0}%`,
              }}
            />
          </div>
          <div style={{ fontSize: 11, opacity: 0.5, marginTop: 4 }}>
            Probing: {scanProgress.current_file}
          </div>
        </div>
      )}

      <StatsCards {...stats} />
      <FilterBar activeFilter={filter} onFilterChange={setFilter} onAddToQueue={handleAddToQueue} />
      <FileTree
        files={filtered}
        selectedPaths={selectedPaths}
        onToggleSelect={handleToggleSelect}
        onToggleTrack={handleToggleTrack}
        onRemoveFile={handleRemoveFile}
      />
    </div>
  );
}
