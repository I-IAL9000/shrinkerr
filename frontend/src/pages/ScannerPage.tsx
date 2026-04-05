import { useState, useEffect, useRef, useCallback, useMemo } from "react";
import { getScanTree, getScanStats, getMediaDirs, startScan, cancelScan, getScanStatus, refreshMetadata, cancelMetadata, removeScanResult, updateAudioTracks, rescanFolder, addJobsFromScan, ignoreFile, unignoreFile, getEncodingSettings, deleteFileFromDisk } from "../api";
import StatsCards from "../components/StatsCards";
import FilterBar, { FILTER_LABELS } from "../components/FilterBar";
import FileTree from "../components/FileTree";
import PosterGrid from "../components/PosterGrid";
import type { FolderInfo } from "../components/FileTree";
import { useToast } from "../useToast";
import { useConfirm } from "../components/ConfirmModal";
import EstimateModal from "../components/EstimateModal";
import type { ScannedFile, ScanProgress } from "../types";

// Module-level cache for tree data
let _cachedFolders: FolderInfo[] | null = null;
let _cachedFilter: string = "all";
let _cacheTimestamp: number = 0;

interface ScannerPageProps {
  scanProgress: ScanProgress | null;
  onClearScanProgress?: () => void;
}

export default function ScannerPage({ scanProgress, onClearScanProgress }: ScannerPageProps) {
  const toast = useToast();
  const confirm = useConfirm();
  const [folders, setFolders] = useState<FolderInfo[]>([]);
  const [dirs, setDirs] = useState<any[]>([]);
  const [selectedDir, setSelectedDir] = useState<string>("all");
  const [filters, setFilters] = useState<string[]>(["all"]);
  // Compute filter string for backend (comma-separated for multi-filter)
  const filter = filters.length === 0 || (filters.length === 1 && filters[0] === "all") ? "all" : filters.filter(f => f !== "all").join(",");
  const [selectedPaths, setSelectedPaths] = useState<Set<string>>(new Set());
  const [selectAllActive, setSelectAllActive] = useState(false);
  const [scanStarted, setScanStarted] = useState(false);
  const [refreshingMetadata, setRefreshingMetadata] = useState(false);
  const [loading, setLoading] = useState(true);
  const [updating, setUpdating] = useState(false);
  const [sortBy, setSortBy] = useState<"name" | "size" | "files" | "date">("name");
  const [sortDir, setSortDir] = useState<"asc" | "desc">("asc");
  const [search, setSearch] = useState("");
  const [encodingSettings, setEncodingSettings] = useState<any>(null);
  const [bulkAction, setBulkAction] = useState<string | null>(null);
  const [filtersOpen, setFiltersOpen] = useState(false);
  const [viewMode, setViewMode] = useState<"tree" | "poster">(() => (localStorage.getItem("squeezarr_viewMode") as "tree" | "poster") || "tree");
  const [posterPrefetching, setPosterPrefetching] = useState(false);
  const [posterProgress, setPosterProgress] = useState({ total: 0, resolved: 0 });
  const [serverStats, setServerStats] = useState<any>(null);
  const [estimatePaths, setEstimatePaths] = useState<string[] | null>(null);
  const [estimateHasIgnored, setEstimateHasIgnored] = useState(false);
  // Track all loaded files across expanded folders (for selection/bulk ops)
  const [loadedFiles, setLoadedFiles] = useState<Map<string, ScannedFile[]>>(new Map());
  // Prevent overlapping stats/tree requests during scanning
  const statsInFlight = useRef(false);
  const treeInFlight = useRef(false);

  // Derive scanning state from WebSocket progress
  const scanning = scanStarted || (scanProgress != null && scanProgress.status !== "done");

  const refreshStats = useCallback(() => {
    if (statsInFlight.current) return;
    statsInFlight.current = true;
    getScanStats().then(setServerStats).catch(() => {}).finally(() => { statsInFlight.current = false; });
  }, []);

  const loadTree = useCallback(async (activeFilter?: string) => {
    if (treeInFlight.current && !activeFilter) return; // skip if already loading (unless forced by filter change)
    treeInFlight.current = true;
    const f = activeFilter ?? filter;
    try {
      const data = await getScanTree(f);
      const result = data.folders || [];
      _cachedFolders = result;
      _cachedFilter = f;
      _cacheTimestamp = Date.now();
      setFolders(result);
      setLoading(false);
      setUpdating(false);
    } catch (err) {
      console.error("Failed to load tree:", err);
      setLoading(false);
      setUpdating(false);
    } finally {
      treeInFlight.current = false;
    }
  }, [filters]);

  useEffect(() => {
    getMediaDirs().then((r: any) => setDirs(Array.isArray(r) ? r : r.dirs || []));
    getEncodingSettings().then(setEncodingSettings).catch(() => {});
    // Check if poster prefetch is already running
    import("../api").then(({ getPosterPrefetchStatus }) => {
      getPosterPrefetchStatus().then(s => {
        if (s.status === "running") {
          setPosterPrefetching(true);
          setPosterProgress({ total: s.total, resolved: s.resolved });
        }
      }).catch(() => {});
    });
    // Load server-computed stats immediately (lightweight, <100ms)
    refreshStats();
    // Show cached tree if recent, otherwise show loading spinner
    const cacheAge = Date.now() - _cacheTimestamp;
    if (_cachedFolders && _cachedFilter === filter && cacheAge < 120_000) {
      setFolders(_cachedFolders);
      setLoading(false);
      // Still check for updates in background
      setUpdating(true);
      loadTree();
    } else {
      loadTree();
    }
  }, []);

  // When scan/metadata refresh completes, reload tree and stats
  useEffect(() => {
    if (scanProgress?.status === "done" || scanProgress?.status === "cancelled") {
      setScanStarted(false);
      if (refreshingMetadata) {
        setRefreshingMetadata(false);
        toast("Metadata refresh complete", "success");
      }
      loadTree();
      refreshStats();
      // Clear loaded files so they get refreshed on next expand
      setLoadedFiles(new Map());
    }
  }, [scanProgress]);

  // While scanning, reload tree every 5s
  useEffect(() => {
    if (!scanning) return;
    const interval = setInterval(async () => {
      loadTree();
      refreshStats();
      try {
        const status = await getScanStatus();
        if (!status.scanning) {
          setScanStarted(false);
          onClearScanProgress?.();
        }
      } catch {}
    }, 5000);
    return () => clearInterval(interval);
  }, [scanning, scanStarted, loadTree]);

  // While idle, poll for updates every 30s
  useEffect(() => {
    if (scanning) return;
    const interval = setInterval(() => {
      loadTree();
      refreshStats();
    }, 30000);
    return () => clearInterval(interval);
  }, [scanning, loadTree]);

  // Poll poster prefetch progress
  useEffect(() => {
    if (!posterPrefetching) return;
    const poll = setInterval(async () => {
      try {
        const { getPosterPrefetchStatus } = await import("../api");
        const s = await getPosterPrefetchStatus();
        setPosterProgress({ total: s.total, resolved: s.resolved });
        if (s.status === "done" || s.status.startsWith("error")) {
          clearInterval(poll);
          setPosterPrefetching(false);
          if (s.status === "done") {
            toast(`Posters fetched: ${s.resolved}/${s.total}`, "success");
          } else {
            toast(`Poster fetch error: ${s.status}`);
          }
        }
      } catch { /* ignore */ }
    }, 2000);
    return () => clearInterval(poll);
  }, [posterPrefetching]);

  // When filter changes, reload tree with new filter
  useEffect(() => {
    if (!loading) {
      setUpdating(true);
      loadTree(filter);
    }
  }, [filters]);

  const handleScan = async () => {
    setScanStarted(true);
    const paths = selectedDir === "all"
      ? dirs.map((d: any) => d.path)
      : [selectedDir];
    await startScan(paths);
  };

  const lastClickedPathRef = useRef<string | null>(null);

  const handleToggleSelect = (path: string, shiftKey?: boolean) => {
    if (selectAllActive) {
      setSelectAllActive(false);
      const allPaths = new Set<string>();
      for (const files of loadedFiles.values()) {
        for (const f of files) allPaths.add(f.file_path);
      }
      allPaths.delete(path);
      setSelectedPaths(allPaths);
      lastClickedPathRef.current = path;
      return;
    }

    setSelectedPaths((prev) => {
      const next = new Set(prev);
      if (shiftKey && lastClickedPathRef.current) {
        // Build path list matching the current view:
        // - Filtered tree / poster: use title-level paths
        // - Unfiltered tree: use leaf folder paths
        const isFiltered = filter !== "all";
        const pathSet = new Set<string>();
        for (const f of folders) {
          if (isFiltered) {
            // Extract title-level path (matches flat tree nodes)
            const parts = f.path.split("/").filter(Boolean);
            let titlePath = f.path;
            for (let i = 0; i < parts.length; i++) {
              if (/\[(?:tvdb-\d+|tt\d+)\]/.test(parts[i])) {
                titlePath = "/" + parts.slice(0, i + 1).join("/");
                break;
              }
            }
            pathSet.add(titlePath + "/");
          } else {
            pathSet.add(f.path + "/");
          }
        }
        const allPaths = Array.from(pathSet).sort();

        const lastIdx = allPaths.indexOf(lastClickedPathRef.current);
        const curIdx = allPaths.indexOf(path);
        if (lastIdx !== -1 && curIdx !== -1) {
          const start = Math.min(lastIdx, curIdx);
          const end = Math.max(lastIdx, curIdx);
          for (let i = start; i <= end; i++) {
            next.add(allPaths[i]);
          }
        } else {
          if (next.has(path)) next.delete(path);
          else next.add(path);
        }
      } else {
        if (next.has(path)) next.delete(path);
        else next.add(path);
      }
      lastClickedPathRef.current = path;
      return next;
    });
  };

  const handleToggleTrack = (filePath: string, streamIndex: number) => {
    // Update in loaded files cache
    setLoadedFiles(prev => {
      const next = new Map(prev);
      for (const [folder, files] of next) {
        const updated = files.map(f => {
          if (f.file_path !== filePath) return f;
          const newTracks = f.audio_tracks.map(t =>
            t.stream_index === streamIndex ? { ...t, keep: !t.keep } : t
          );
          if (f.id) {
            updateAudioTracks(f.id, JSON.stringify(newTracks)).catch(() => {});
          }
          return { ...f, audio_tracks: newTracks };
        });
        next.set(folder, updated);
      }
      return next;
    });
  };

  const handleRescanFolder = async (folderPath: string) => {
    toast(`Rescanning ${folderPath.split("/").pop()}...`);
    await rescanFolder([folderPath]);
  };

  const handleDeleteFile = async (filePath: string) => {
    try {
      const res = await deleteFileFromDisk(filePath);
      if (res.file_deleted) {
        toast("File moved to trash", "success");
      } else {
        toast("File not found on disk, removed from database");
      }
      // Remove from loaded files and refresh tree
      setLoadedFiles(prev => {
        const next = new Map(prev);
        for (const [folder, files] of next) {
          next.set(folder, files.filter(f => f.file_path !== filePath));
        }
        return next;
      });
      loadTree();
      refreshStats();
    } catch (err: any) {
      toast(`Delete failed: ${err.message}`);
    }
  };

  const handleBulkDelete = async () => {
    const selected = getSelectedFiles();
    if (!selected.length) return;
    if (!await confirm({ message: `Move ${selected.length} file(s) to trash? This cannot be undone from Squeezarr.`, confirmLabel: `Trash ${selected.length} files`, danger: true })) return;
    setBulkAction(`Trashing ${selected.length} files...`);
    try {
      let deleted = 0;
      for (const f of selected) {
        try {
          await deleteFileFromDisk(f.file_path);
          deleted++;
        } catch { /* continue */ }
      }
      setSelectAllActive(false);
      setSelectedPaths(new Set());
      loadTree();
      refreshStats();
      setLoadedFiles(new Map());
      toast(`Trashed ${deleted} file(s)`, "success");
    } finally {
      setBulkAction(null);
    }
  };

  const handleBulkRemove = async () => {
    const selected = getSelectedFiles();
    if (!selected.length) return;
    if (!await confirm({ message: `Remove ${selected.length} file(s) from the list? Files stay on disk.`, confirmLabel: "Remove" })) return;
    setBulkAction(`Removing ${selected.length} files...`);
    try {
      for (const f of selected) {
        try { await removeScanResult(f.id); } catch {}
      }
      setSelectAllActive(false);
      setSelectedPaths(new Set());
      loadTree();
      refreshStats();
      setLoadedFiles(new Map());
      toast(`Removed ${selected.length} file(s) from list`, "success");
    } finally {
      setBulkAction(null);
    }
  };

  const handleIgnoreFile = async (filePath: string) => {
    await ignoreFile(filePath);
    // Update loaded files and refresh
    setLoadedFiles(prev => {
      const next = new Map(prev);
      for (const [folder, files] of next) {
        next.set(folder, files.map(f =>
          f.file_path === filePath ? { ...f, ignored: true } : f
        ));
      }
      return next;
    });
    refreshStats();
  };

  const handleUnignoreFile = async (filePath: string) => {
    await unignoreFile(filePath);
    setLoadedFiles(prev => {
      const next = new Map(prev);
      for (const [folder, files] of next) {
        next.set(folder, files.map(f =>
          f.file_path === filePath ? { ...f, ignored: false } : f
        ));
      }
      return next;
    });
    refreshStats();
  };

  const handleBulkUnignore = async () => {
    const selected = getSelectedFiles().filter(f => f.ignored);
    if (!selected.length) return;
    if (!await confirm({ message: `Unignore ${selected.length} selected file(s)?`, confirmLabel: "Unignore" })) return;
    setBulkAction(`Unignoring ${selected.length} files...`);
    try {
      await Promise.all(selected.map(f => unignoreFile(f.file_path)));
      setSelectAllActive(false);
      setSelectedPaths(new Set());
      loadTree();
      refreshStats();
      setLoadedFiles(new Map());
      toast(`Unignored ${selected.length} file(s)`, "success");
    } finally {
      setBulkAction(null);
    }
  };

  const handleBulkIgnore = async () => {
    const paths = Array.from(selectedPaths);
    if (!paths.length) return;
    if (!await confirm({ message: `Ignore ${selectedCount} selected file(s)?`, confirmLabel: "Ignore", danger: true })) return;
    setBulkAction(`Ignoring ${selectedCount} files...`);
    try {
      await Promise.all(paths.map(p => ignoreFile(p)));
      setSelectAllActive(false);
      setSelectedPaths(new Set());
      loadTree();
      refreshStats();
      setLoadedFiles(new Map());
      toast(`Ignored ${paths.length} item(s)`, "success");
    } finally {
      setBulkAction(null);
    }
  };

  const handleBulkRescan = async () => {
    const selected = getSelectedFiles();
    const folderSet = new Set(selected.map(f => {
      const parts = f.file_path.split("/");
      return parts.slice(0, -1).join("/");
    }));
    if (!await confirm({ message: `Rescan ${folderSet.size} folder(s) containing ${selected.length} selected file(s)?`, confirmLabel: "Rescan" })) return;
    for (const folder of folderSet) {
      await rescanFolder([folder]);
    }
    setSelectAllActive(false);
    setSelectedPaths(new Set());
    toast(`Rescanning ${folderSet.size} folder(s)...`, "success");
  };

  const handleRemoveFile = async (filePath: string) => {
    // Find file in loaded files to get ID
    for (const files of loadedFiles.values()) {
      const file = files.find(f => f.file_path === filePath);
      if (file?.id) {
        await removeScanResult(file.id);
        break;
      }
    }
    setLoadedFiles(prev => {
      const next = new Map(prev);
      for (const [folder, files] of next) {
        next.set(folder, files.filter(f => f.file_path !== filePath));
      }
      return next;
    });
    setSelectedPaths(prev => { const n = new Set(prev); n.delete(filePath); return n; });
    loadTree();
  };

  const handleSelectAll = () => {
    if (selectAllActive || selectedPaths.size > 0) {
      setSelectAllActive(false);
      setSelectedPaths(new Set());
    } else {
      setSelectAllActive(true);
      setSelectedPaths(new Set());
    }
  };

  // Pre-compute sorted folder prefixes for fast prefix matching
  const selectedFolderPrefixes = useMemo(() => {
    const prefixes: string[] = [];
    for (const sp of selectedPaths) {
      if (sp.endsWith("/")) prefixes.push(sp);
    }
    return prefixes.sort();
  }, [selectedPaths]);

  const isSelected = useCallback((path: string) => {
    if (selectAllActive) return true;
    if (selectedPaths.has(path)) return true;
    // Binary search for parent folder prefix
    if (selectedFolderPrefixes.length === 0) return false;
    // Check if any prefix matches using bisect
    let lo = 0, hi = selectedFolderPrefixes.length;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (selectedFolderPrefixes[mid] <= path) lo = mid + 1;
      else hi = mid;
    }
    // Check the entry just before (the largest prefix <= path)
    if (lo > 0 && path.startsWith(selectedFolderPrefixes[lo - 1])) return true;
    return false;
  }, [selectAllActive, selectedPaths, selectedFolderPrefixes]);

  // Get all currently loaded and selected files
  const getSelectedFiles = (): ScannedFile[] => {
    const result: ScannedFile[] = [];
    for (const files of loadedFiles.values()) {
      for (const f of files) {
        if (isSelected(f.file_path)) result.push(f);
      }
    }
    return result;
  };

  const handleAddToQueue = () => {
    // Send all selected paths (both folder paths ending with / and file paths)
    // The server resolves folder paths to actual files
    const paths = Array.from(selectedPaths);
    if (selectAllActive) {
      // Select all: use the filter-based server-side resolution
      setEstimatePaths(folders.map(f => f.path + "/"));
      setEstimateHasIgnored(false);
      return;
    }
    if (!paths.length) {
      toast("No files or folders selected");
      return;
    }
    setEstimatePaths(paths);
    setEstimateHasIgnored(false);
  };

  const handleConfirmAdd = async (priority: number, overrideRules: boolean = false, encodingOverrides?: any) => {
    if (!estimatePaths) return;
    setEstimatePaths(null);
    try {
      const extra: Record<string, any> = {};
      if (encodingOverrides) {
        if (encodingOverrides.encoder) extra.encoder_override = encodingOverrides.encoder;
        if (encodingOverrides.nvenc_preset) extra.nvenc_preset_override = encodingOverrides.nvenc_preset;
        if (encodingOverrides.nvenc_cq != null) extra.nvenc_cq_override = encodingOverrides.nvenc_cq;
        if (encodingOverrides.libx265_crf != null) extra.libx265_crf_override = encodingOverrides.libx265_crf;
        if (encodingOverrides.audio_codec) extra.audio_codec_override = encodingOverrides.audio_codec;
        if (encodingOverrides.audio_bitrate != null) extra.audio_bitrate_override = encodingOverrides.audio_bitrate;
        if (encodingOverrides.target_resolution) extra.target_resolution_override = encodingOverrides.target_resolution;
        if (encodingOverrides.force_reencode) extra.force_reencode = true;
      }
      if (filter !== "all") extra.filter = filter;
      const result = await addJobsFromScan(estimatePaths, priority, overrideRules, Object.keys(extra).length > 0 ? extra : undefined) as any;
      setSelectAllActive(false);
      setSelectedPaths(new Set());
      if (result.added > 0) {
        toast(`Added ${result.added} item${result.added !== 1 ? "s" : ""} to queue${priority > 0 ? ` (${["", "high", "highest"][priority]} priority)` : ""}`, "success");
      } else {
        toast(`No actionable items — files may already be optimized`);
      }
      loadTree();
      refreshStats();
    } catch (err: any) {
      toast(`Failed to add to queue: ${err.message || "unknown error"}`);
    }
  };

  // Callback when FileTree loads files for a folder
  const handleFolderFilesLoaded = useCallback((folderPath: string, files: ScannedFile[]) => {
    setLoadedFiles(prev => {
      const next = new Map(prev);
      next.set(folderPath, files);
      return next;
    });
  }, []);

  const newCount = serverStats?.counts?.new || 0;
  // Use tree-derived "all" count during scanning so pill stays in sync with tree
  const treeTotalFiles = folders.reduce((sum, f) => sum + f.file_count, 0);
  const filterCounts: Record<string, number> = serverStats?.counts
    ? { ...serverStats.counts, all: Math.max(serverStats.counts.all || 0, treeTotalFiles) }
    : { all: treeTotalFiles };

  // Count selected items — for folder paths, use the folder's file count from tree data
  const selectedCount = useMemo(() => selectAllActive
    ? folders.reduce((sum, f) => sum + f.file_count, 0)
    : (() => {
        let count = 0;
        // Count individual file selections
        for (const p of selectedPaths) {
          if (!p.endsWith("/")) { count += 1; continue; }
        }
        // For folder selections, iterate folders once and check against sorted prefixes
        if (selectedFolderPrefixes.length > 0) {
          for (const f of folders) {
            const fp = f.path + "/";
            // Binary search for matching prefix
            let lo = 0, hi = selectedFolderPrefixes.length;
            while (lo < hi) {
              const mid = (lo + hi) >> 1;
              if (selectedFolderPrefixes[mid] <= fp) lo = mid + 1;
              else hi = mid;
            }
            if (lo > 0 && fp.startsWith(selectedFolderPrefixes[lo - 1])) {
              count += f.file_count;
            }
          }
        }
        return count;
      })(), [selectAllActive, selectedPaths, selectedFolderPrefixes, folders]);

  // Server-computed stats for StatsCards
  const ss = serverStats?.summary;
  const stats = ss ? {
    filesToConvert: ss.files_to_convert || 0,
    audioCleanup: ss.audio_cleanup || 0,
    ignoredCount: ss.ignored_count || 0,
    estimatedSavingsGB: (ss.estimated_savings_bytes || 0) / (1024 ** 3),
    totalScannedGB: (ss.total_size || 0) / (1024 ** 3),
  } : {
    filesToConvert: 0, audioCleanup: 0, ignoredCount: 0,
    estimatedSavingsGB: 0, totalScannedGB: 0,
  };

  // Apply search filter to folders
  const displayFolders = search.trim()
    ? folders.filter(f => {
        const words = search.trim().toLowerCase().split(/\s+/);
        const haystack = f.path.toLowerCase();
        return words.every(w => haystack.includes(w));
      })
    : folders;

  return (
    <div>
      <div style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 20 }}>
        <select
          value={selectedDir}
          onChange={(e) => setSelectedDir(e.target.value)}
          style={{
            background: "var(--bg-card)", color: "var(--text-secondary)",
            border: "1px solid var(--border)", padding: "6px 10px",
            borderRadius: 4, fontSize: 13, height: 36, boxSizing: "border-box" as const,
          }}
        >
          <option value="all">All configured paths</option>
          {dirs.map((d: any) => (
            <option key={d.id} value={d.path}>{d.path}</option>
          ))}
        </select>
        <button className="btn btn-primary" onClick={handleScan} disabled={scanning} style={{ height: 36 }}>
          {scanning ? "Scanning..." : "Scan"}
        </button>
        {scanning && (
          <button
            className="btn btn-secondary"
            style={{ height: 36, color: "#e94560", borderColor: "rgba(233,69,96,0.4)" }}
            onClick={() => {
              cancelScan().catch(() => {});
              setScanStarted(false);
              onClearScanProgress?.();
              toast("Scan cancelling...");
            }}
          >
            Cancel
          </button>
        )}
        {!scanning && folders.length > 0 && (
          refreshingMetadata ? (
            <button
              onClick={async () => {
                await cancelMetadata();
                setRefreshingMetadata(false);
                onClearScanProgress?.();
                toast("Metadata refresh cancelled");
                loadTree();
              }}
              style={{
                background: "none", border: "1px solid rgba(233,69,96,0.4)",
                color: "#e94560", cursor: "pointer", borderRadius: 4,
                width: 36, height: 36, display: "flex", alignItems: "center", justifyContent: "center",
              }}
              title="Cancel metadata refresh"
            >
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>
              </svg>
            </button>
          ) : (
            <button
              onClick={async () => {
                setRefreshingMetadata(true);
                await refreshMetadata();
                toast("Updating languages from TMDB/TVDB...");
              }}
              style={{
                background: "none", border: "1px solid var(--border)",
                color: "var(--text-muted)", cursor: "pointer", borderRadius: 4,
                width: 36, height: 36, display: "flex", alignItems: "center", justifyContent: "center",
                opacity: 0.6, transition: "opacity 0.15s",
              }}
              onMouseEnter={(e) => (e.currentTarget.style.opacity = "1")}
              onMouseLeave={(e) => (e.currentTarget.style.opacity = "0.6")}
              title="Update languages from TMDB/TVDB (for files with heuristic detection)"
            >
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <circle cx="12" cy="12" r="10"/><line x1="2" y1="12" x2="22" y2="12"/><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/>
              </svg>
            </button>
          )
        )}
        {scanning && scanProgress && scanProgress.status !== "metadata" && (
          <span style={{ fontSize: 12, opacity: 0.6 }}>
            {scanProgress.probed} / {scanProgress.total} files probed
          </span>
        )}
        {refreshingMetadata && scanProgress && scanProgress.status === "metadata" && (
          <span style={{ fontSize: 12, opacity: 0.6 }}>
            Metadata: {scanProgress.probed} / {scanProgress.total} checked
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

      {loading ? (
        <div style={{ display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", padding: 60 }}>
          <div className="spinner" />
          <div style={{ marginTop: 12, fontSize: 13, opacity: 0.5 }}>Loading scan results...</div>
        </div>
      ) : (
        <>
          <StatsCards {...stats} settingsLabel={encodingSettings
            ? `${encodingSettings.nvenc_preset?.toUpperCase() || "P6"} / CQ ${encodingSettings.nvenc_cq ?? 20}`
            : undefined
          } />
          {updating && (
            <div style={{ display: "flex", alignItems: "center", gap: 8, padding: "6px 12px", marginBottom: 8, background: "rgba(145,53,255,0.1)", borderRadius: 4, fontSize: 12, color: "var(--text-muted)" }}>
              <div className="spinner" style={{ width: 14, height: 14 }} />
              Updating...
            </div>
          )}
          {/* Filter toggle + active filter indicator */}
          <div style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 12, flexWrap: "wrap" }}>
            <button
              className="filter-pill"
              onClick={() => setFiltersOpen(!filtersOpen)}
              style={{
                display: "inline-flex", alignItems: "center", gap: 6, whiteSpace: "nowrap",
                background: filtersOpen ? "var(--accent)" : "var(--bg-card)",
                color: filtersOpen ? "white" : "var(--text-muted)",
              }}
            >
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <line x1="4" y1="6" x2="20" y2="6"/><line x1="8" y1="12" x2="16" y2="12"/><line x1="11" y1="18" x2="13" y2="18"/>
              </svg>
              Filter{filter !== "all" ? `: ${filters.filter(f => f !== "all").map(f => FILTER_LABELS[f] || f.replace(/_/g, " ")).join(" | ")}` : ""}
              {filter !== "all" && (
                <span style={{
                  background: "rgba(255,255,255,0.2)",
                  color: "white",
                  fontSize: 10, fontWeight: "bold",
                  padding: "1px 6px", borderRadius: 8,
                  marginLeft: 4,
                  display: "inline-flex", alignItems: "center", lineHeight: 1.4,
                }}>
                  {treeTotalFiles > 99999 ? `${(treeTotalFiles / 1000).toFixed(0)}k` : treeTotalFiles.toLocaleString()}
                </span>
              )}
            </button>
            {filter !== "all" && (
              <button
                className="filter-pill"
                onClick={() => { setFilters(["all"]); }}
                style={{ background: "var(--bg-card)", color: "var(--text-muted)", gap: 4 }}
              >
                Clear
              </button>
            )}
            {/* Search inline when filters collapsed */}
            {!filtersOpen && (
              <>
                <div style={{ position: "relative", flex: "1 1 200px", minWidth: 160, maxWidth: 300 }}>
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="var(--text-muted)" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"
                    style={{ position: "absolute", left: 10, top: "50%", transform: "translateY(-50%)", pointerEvents: "none" }}>
                    <circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/>
                  </svg>
                  <input type="text" placeholder="Search folders..." value={search}
                    onChange={(e) => setSearch(e.target.value)}
                    style={{ width: "100%", padding: "5px 12px 5px 30px", fontSize: 12, lineHeight: "1.4", background: "var(--bg-card)", color: "var(--text-secondary)", border: "1px solid transparent", borderRadius: 16, outline: "none", boxSizing: "border-box" as const }} />
                  {search && (
                    <button onClick={() => setSearch("")}
                      style={{ position: "absolute", right: 10, top: "50%", transform: "translateY(-50%)", background: "none", border: "none", color: "var(--text-muted)", cursor: "pointer", fontSize: 14, lineHeight: 1 }}>&times;</button>
                  )}
                </div>
                <span style={{ fontSize: 12, opacity: 0.5, whiteSpace: "nowrap" }}>Sort:</span>
                {([["name", "A-Z"], ["size", "Size"], ["files", "Files"], ["date", "Date"]] as const).map(([val, label]) => (
                  <button key={val}
                    onClick={() => { if (sortBy === val) setSortDir(d => d === "asc" ? "desc" : "asc"); else { setSortBy(val); setSortDir(val === "size" || val === "date" ? "desc" : "asc"); } }}
                    style={{ padding: "5px 12px", borderRadius: 16, fontSize: 12, cursor: "pointer", border: "none", whiteSpace: "nowrap", background: sortBy === val ? "var(--accent)" : "var(--bg-card)", color: sortBy === val ? "white" : "var(--text-muted)" }}>
                    {label} {sortBy === val && (sortDir === "asc"
                      ? <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round" style={{ verticalAlign: "middle", marginLeft: 2 }}><polyline points="12 5 6 11"/><polyline points="12 5 18 11"/><line x1="12" y1="5" x2="12" y2="19"/></svg>
                      : <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round" style={{ verticalAlign: "middle", marginLeft: 2 }}><polyline points="12 19 6 13"/><polyline points="12 19 18 13"/><line x1="12" y1="19" x2="12" y2="5"/></svg>
                    )}
                  </button>
                ))}
                {/* View toggle */}
                <span style={{ width: 1, height: 16, background: "var(--border)", marginLeft: 4 }} />
                <button
                  onClick={() => { const next = viewMode === "tree" ? "poster" : "tree"; setViewMode(next); localStorage.setItem("squeezarr_viewMode", next); }}
                  style={{ padding: "5px 10px", borderRadius: 16, fontSize: 12, cursor: "pointer", border: "none", whiteSpace: "nowrap", background: "var(--bg-card)", color: "var(--text-muted)", display: "inline-flex", alignItems: "center", gap: 5 }}
                  title={`Switch to ${viewMode === "tree" ? "poster" : "tree"} view`}
                >
                  {viewMode === "tree" ? (
                    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/></svg>
                  ) : (
                    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><line x1="8" y1="6" x2="21" y2="6"/><line x1="8" y1="12" x2="21" y2="12"/><line x1="8" y1="18" x2="21" y2="18"/><line x1="3" y1="6" x2="3.01" y2="6"/><line x1="3" y1="12" x2="3.01" y2="12"/><line x1="3" y1="18" x2="3.01" y2="18"/></svg>
                  )}
                  {viewMode === "tree" ? "Posters" : "Tree"}
                </button>
                {viewMode === "poster" && (
                  <button
                    className="btn btn-secondary"
                    style={{ padding: "5px 10px", borderRadius: 16, fontSize: 12, whiteSpace: "nowrap", display: "inline-flex", alignItems: "center", gap: 4 }}
                    disabled={posterPrefetching}
                    onClick={async () => {
                      const { startPosterPrefetch } = await import("../api");
                      await startPosterPrefetch();
                      setPosterPrefetching(true);
                    }}
                  >
                    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                      <polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/>
                    </svg>
                    {posterPrefetching ? `Fetching ${posterProgress.resolved}/${posterProgress.total}...` : "Refresh Posters"}
                  </button>
                )}
              </>
            )}
          </div>

          {/* Poster prefetch progress bar */}
          {posterPrefetching && posterProgress.total > 0 && (
            <div style={{ marginBottom: 12 }}>
              <div className="progress-bar-track">
                <div className="progress-bar-fill" style={{ width: `${(posterProgress.resolved / posterProgress.total) * 100}%` }} />
              </div>
              <div style={{ fontSize: 11, opacity: 0.5, marginTop: 4 }}>
                Fetching posters: {posterProgress.resolved} / {posterProgress.total}
              </div>
            </div>
          )}

          {/* Collapsible filter panel */}
          {filtersOpen && (
            <>
              <FilterBar
                activeFilters={filters}
                onFilterToggle={(f) => {
                  if (f === "all") {
                    setFilters(["all"]);
                  } else {
                    setFilters(prev => {
                      const active = prev.filter(x => x !== "all");
                      if (active.includes(f)) {
                        const next = active.filter(x => x !== f);
                        return next.length === 0 ? ["all"] : next;
                      }
                      return [...active, f];
                    });
                  }
                }}
                newCount={newCount}
                counts={filterCounts}
              />
              <div style={{ display: "flex", gap: 6, alignItems: "center", marginBottom: 12, flexWrap: "wrap" }}>
            <div style={{ position: "relative", flex: "1 1 200px", minWidth: 160, maxWidth: 300 }}>
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="var(--text-muted)" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"
                style={{ position: "absolute", left: 10, top: "50%", transform: "translateY(-50%)", pointerEvents: "none" }}>
                <circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/>
              </svg>
              <input
                type="text"
                placeholder="Search folders..."
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                style={{
                  width: "100%", padding: "5px 12px 5px 30px", fontSize: 12, lineHeight: "1.4",
                  background: "var(--bg-card)", color: "var(--text-secondary)",
                  border: "1px solid transparent", borderRadius: 16,
                  outline: "none", boxSizing: "border-box",
                }}
              />
              {search && (
                <button
                  onClick={() => setSearch("")}
                  style={{
                    position: "absolute", right: 10, top: "50%", transform: "translateY(-50%)",
                    background: "none", border: "none", color: "var(--text-muted)",
                    cursor: "pointer", fontSize: 14, lineHeight: 1,
                  }}
                >&times;</button>
              )}
            </div>
            <span style={{ fontSize: 12, opacity: 0.5, whiteSpace: "nowrap" }}>Sort:</span>
            {([["name", "A-Z"], ["size", "Size"], ["files", "Files"], ["date", "Date"]] as const).map(([val, label]) => (
              <button
                key={val}
                onClick={() => {
                  if (sortBy === val) {
                    setSortDir(d => d === "asc" ? "desc" : "asc");
                  } else {
                    setSortBy(val);
                    setSortDir(val === "size" || val === "date" ? "desc" : "asc");
                  }
                }}
                style={{
                  padding: "5px 12px", borderRadius: 16, fontSize: 12, cursor: "pointer",
                  border: "none", whiteSpace: "nowrap",
                  background: sortBy === val ? "var(--accent)" : "var(--bg-card)",
                  color: sortBy === val ? "white" : "var(--text-muted)",
                }}
              >
                {label} {sortBy === val && (sortDir === "asc"
                  ? <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round" style={{ verticalAlign: "middle", marginLeft: 2 }}><polyline points="12 5 6 11"/><polyline points="12 5 18 11"/><line x1="12" y1="5" x2="12" y2="19"/></svg>
                  : <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round" style={{ verticalAlign: "middle", marginLeft: 2 }}><polyline points="12 19 6 13"/><polyline points="12 19 18 13"/><line x1="12" y1="19" x2="12" y2="5"/></svg>
                )}
              </button>
            ))}
            {/* View toggle (same as in collapsed view) */}
            <span style={{ width: 1, height: 16, background: "var(--border)", marginLeft: 4 }} />
            <button
              onClick={() => { const next = viewMode === "tree" ? "poster" : "tree"; setViewMode(next); localStorage.setItem("squeezarr_viewMode", next); }}
              style={{ padding: "5px 10px", borderRadius: 16, fontSize: 12, cursor: "pointer", border: "none", whiteSpace: "nowrap", background: "var(--bg-card)", color: "var(--text-muted)", display: "inline-flex", alignItems: "center", gap: 5 }}
              title={`Switch to ${viewMode === "tree" ? "poster" : "tree"} view`}
            >
              {viewMode === "tree" ? (
                <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/></svg>
              ) : (
                <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><line x1="8" y1="6" x2="21" y2="6"/><line x1="8" y1="12" x2="21" y2="12"/><line x1="8" y1="18" x2="21" y2="18"/><line x1="3" y1="6" x2="3.01" y2="6"/><line x1="3" y1="12" x2="3.01" y2="12"/><line x1="3" y1="18" x2="3.01" y2="18"/></svg>
              )}
              {viewMode === "tree" ? "Posters" : "Tree"}
            </button>
            {viewMode === "poster" && (
              <button
                className="btn btn-secondary"
                style={{ padding: "5px 10px", borderRadius: 16, fontSize: 12, whiteSpace: "nowrap", display: "inline-flex", alignItems: "center", gap: 4 }}
                disabled={posterPrefetching}
                onClick={async () => {
                  const { startPosterPrefetch } = await import("../api");
                  await startPosterPrefetch();
                  setPosterPrefetching(true);
                }}
              >
                <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/>
                </svg>
                {posterPrefetching ? `Fetching ${posterProgress.resolved}/${posterProgress.total}...` : "Refresh Posters"}
              </button>
            )}
          </div>
            </>
          )}
          {/* Selection control panel */}
          <div style={{
            display: "flex", flexWrap: "wrap", gap: 6, alignItems: "center",
            padding: "8px 12px", marginBottom: 12,
            background: selectedCount > 0 ? "var(--bg-card)" : "var(--bg-secondary)",
            borderRadius: 6, transition: "background 0.15s",
            position: "sticky" as const, top: 0, zIndex: 50,
          }}>
            <button className="btn btn-secondary" style={{ fontSize: 12, padding: "5px 12px", borderRadius: 16, whiteSpace: "nowrap" }} onClick={handleSelectAll}>
              {selectAllActive || selectedPaths.size > 0 ? "Deselect all" : "Select all"}
            </button>
            {selectedCount > 0 && (() => {
              const selectedFiles = getSelectedFiles();
              const allSelectedIgnored = selectedFiles.length > 0 && selectedFiles.every(f => f.ignored);
              const someSelectedIgnored = selectedFiles.some(f => f.ignored);
              return <>
                <span style={{ fontSize: 11, color: "var(--accent)", fontWeight: 600 }}>{selectedCount} selected</span>
                <span style={{ width: 1, height: 14, background: "var(--border)" }} />
                <button className="btn btn-primary" style={{ fontSize: 12, padding: "5px 12px", borderRadius: 16, whiteSpace: "nowrap", display: "inline-flex", alignItems: "center", gap: 4 }} onClick={handleAddToQueue}>
                  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
                  Add to queue
                </button>
                <button className="btn btn-secondary" style={{ fontSize: 12, padding: "5px 12px", borderRadius: 16, whiteSpace: "nowrap", display: "inline-flex", alignItems: "center", gap: 4 }} onClick={handleBulkRescan}>
                  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></svg>
                  Rescan
                </button>
                {allSelectedIgnored ? (
                  <button className="btn btn-secondary" style={{ fontSize: 12, padding: "5px 12px", borderRadius: 16, whiteSpace: "nowrap", display: "inline-flex", alignItems: "center", gap: 4 }} onClick={handleBulkUnignore}>
                    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>
                    Unignore
                  </button>
                ) : (
                  <>
                    <button className="btn btn-secondary" style={{ fontSize: 12, padding: "5px 12px", borderRadius: 16, whiteSpace: "nowrap", display: "inline-flex", alignItems: "center", gap: 4 }} onClick={handleBulkIgnore}>
                      <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="10"/><line x1="4.93" y1="4.93" x2="19.07" y2="19.07"/></svg>
                      Ignore
                    </button>
                    {someSelectedIgnored && (
                      <button className="btn btn-secondary" style={{ fontSize: 12, padding: "5px 12px", borderRadius: 16, whiteSpace: "nowrap", display: "inline-flex", alignItems: "center", gap: 4 }} onClick={handleBulkUnignore}>
                        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>
                        Unignore
                      </button>
                    )}
                  </>
                )}
                <button className="btn btn-secondary" style={{ fontSize: 12, padding: "5px 12px", borderRadius: 16, whiteSpace: "nowrap", display: "inline-flex", alignItems: "center", gap: 4 }} onClick={handleBulkRemove}>
                  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
                  Remove
                </button>
                <span style={{ width: 1, height: 14, background: "var(--border)" }} />
                <button className="btn btn-secondary" style={{ fontSize: 12, padding: "5px 12px", borderRadius: 16, whiteSpace: "nowrap", color: "#e94560", display: "inline-flex", alignItems: "center", gap: 4 }} onClick={handleBulkDelete}>
                  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 01-2 2H7a2 2 0 01-2-2V6m3 0V4a2 2 0 012-2h4a2 2 0 012 2v2"/></svg>
                  Trash files
                </button>
              </>;
            })()}
          </div>
          {bulkAction && (
            <div style={{
              display: "flex", alignItems: "center", gap: 10,
              padding: "10px 16px", marginBottom: 12,
              background: "var(--bg-card)", borderRadius: 6,
            }}>
              <div className="spinner" style={{ width: 18, height: 18 }} />
              <span style={{ fontSize: 13, color: "var(--text-secondary)" }}>{bulkAction}</span>
            </div>
          )}
          {viewMode === "tree" ? (
            <FileTree
              folders={displayFolders}
              filter={filter}
              search={search}
              isSelected={isSelected}
              onToggleSelect={handleToggleSelect}
              onToggleTrack={handleToggleTrack}
              onRemoveFile={handleRemoveFile}
              onIgnoreFile={handleIgnoreFile}
              onUnignoreFile={handleUnignoreFile}
              onRescanFolder={handleRescanFolder}
              onDeleteFile={handleDeleteFile}
              onFolderFilesLoaded={handleFolderFilesLoaded}
              sortBy={sortBy}
              sortDir={sortDir}
            />
          ) : (
            <PosterGrid
              folders={displayFolders}
              filter={filter}
              isSelected={isSelected}
              onToggleSelect={handleToggleSelect}
              onToggleTrack={handleToggleTrack}
              onRemoveFile={handleRemoveFile}
              onIgnoreFile={handleIgnoreFile}
              onUnignoreFile={handleUnignoreFile}
              onDeleteFile={handleDeleteFile}
              onFolderFilesLoaded={handleFolderFilesLoaded}
              sortBy={sortBy}
              sortDir={sortDir}
            />
          )}
        </>
      )}

      {/* Estimation modal */}
      {estimatePaths && (
        <EstimateModal
          filePaths={estimatePaths}
          hasIgnoredFiles={estimateHasIgnored}
          activeFilter={filter}
          onConfirm={handleConfirmAdd}
          onCancel={() => setEstimatePaths(null)}
        />
      )}
    </div>
  );
}
