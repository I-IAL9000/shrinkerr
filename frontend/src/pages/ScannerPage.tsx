import { useState, useEffect, useRef, useCallback, useMemo, type CSSProperties } from "react";
import { getScanTree, getScanStats, getMediaDirs, startScan, cancelScan, getScanStatus, refreshMetadata, cancelMetadata, removeScanResult, updateAudioTracks, updateSubtitleTracks, rescanFolder, addJobsFromScan, ignoreFile, unignoreFile, getEncodingSettings, deleteFileFromDisk } from "../api";
import { fmtNum } from "../fmt";
import StatsCards from "../components/StatsCards";
import AdvancedSearchModal from "../components/AdvancedSearchModal";
import FilterBar, { FILTER_LABELS } from "../components/FilterBar";
import FileTree from "../components/FileTree";
import PosterGrid from "../components/PosterGrid";
import type { FolderInfo } from "../components/FileTree";
import { useToast } from "../useToast";
import { useConfirm } from "../components/ConfirmModal";
import EstimateModal from "../components/EstimateModal";
import RenameModal from "../components/RenameModal";
import type { ScannedFile, ScanProgress } from "../types";

// Module-level cache for tree data
let _cachedFolders: FolderInfo[] | null = null;
let _cachedFilter: string = "all";
let _cacheTimestamp: number = 0;

// Shared style for dropdown menu items (arr actions menu, etc)
const menuItemStyle: CSSProperties = {
  display: "flex",
  alignItems: "flex-start",
  gap: 10,
  padding: "8px 10px",
  background: "transparent",
  border: "none",
  borderRadius: 4,
  cursor: "pointer",
  textAlign: "left",
  width: "100%",
};

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
  const [searchInput, setSearchInput] = useState("");  // raw input, debounced into `search`
  const [advSearchOpen, setAdvSearchOpen] = useState(false);
  const [advSearchPredicates, setAdvSearchPredicates] = useState<any[]>([]);
  const [advSearchResults, setAdvSearchResults] = useState<Set<string> | null>(null);
  const [encodingSettings, setEncodingSettings] = useState<any>(null);
  const [bulkAction, setBulkAction] = useState<string | null>(null);
  const [filtersOpen, setFiltersOpen] = useState(false);
  const [arrMenuOpen, setArrMenuOpen] = useState(false);
  const arrMenuRef = useRef<HTMLDivElement | null>(null);
  const [healthMenuOpen, setHealthMenuOpen] = useState(false);
  const healthMenuRef = useRef<HTMLDivElement | null>(null);
  // Fall back to the legacy squeezarr_viewMode key so users upgrading from
  // the old app name keep their view preference. New writes use the canonical
  // shrinkerr_viewMode key below.
  const [viewMode, setViewMode] = useState<"tree" | "poster">(() =>
    (localStorage.getItem("shrinkerr_viewMode") as "tree" | "poster") ||
    (localStorage.getItem("squeezarr_viewMode") as "tree" | "poster") ||
    "tree"
  );
  const [posterPrefetching, setPosterPrefetching] = useState(false);
  const [posterProgress, setPosterProgress] = useState({ total: 0, resolved: 0 });
  const [serverStats, setServerStats] = useState<any>(null);
  const [estimatePaths, setEstimatePaths] = useState<string[] | null>(null);
  const [estimateHasIgnored, setEstimateHasIgnored] = useState(false);
  const [renamePaths, setRenamePaths] = useState<string[] | null>(null);
  // While the add-to-queue API call is in flight. Holds the count being
  // added so the overlay can render a meaningful message; null when idle.
  // Adding hundreds/thousands of items is a multi-second operation server-
  // side, and the prior behavior closed the estimate modal immediately and
  // then went silent until the toast fired — looked frozen. v0.3.57+.
  const [addingToQueueCount, setAddingToQueueCount] = useState<number | null>(null);

  // Debounce the search input 250ms so rapid typing doesn't trigger
  // a poster re-resolution per keystroke
  useEffect(() => {
    const t = setTimeout(() => setSearch(searchInput), 250);
    return () => clearTimeout(t);
  }, [searchInput]);
  // Track all loaded files across expanded folders (for selection/bulk ops)
  const [loadedFiles, setLoadedFiles] = useState<Map<string, ScannedFile[]>>(new Map());
  // Prevent overlapping stats/tree requests during scanning
  const statsInFlight = useRef(false);
  const treeInFlight = useRef(false);
  // Per-request generation — only the latest response wins.
  // Prevents stale responses from overwriting newer results when filters change rapidly.
  const treeRequestGen = useRef(0);
  const treeAbortCtrl = useRef<AbortController | null>(null);

  // Derive scanning state from WebSocket progress
  const scanning = scanStarted || (scanProgress != null && scanProgress.status !== "done" && scanProgress.status !== "health_check_complete" && scanProgress.status !== "cancelled");

  const refreshStats = useCallback(() => {
    if (statsInFlight.current) return;
    statsInFlight.current = true;
    getScanStats().then(setServerStats).catch(() => {}).finally(() => { statsInFlight.current = false; });
  }, []);

  const loadTree = useCallback(async (activeFilter?: string) => {
    const f = activeFilter ?? filter;
    // Cancel any in-flight request so its result can't overwrite the newer one
    if (treeAbortCtrl.current) {
      try { treeAbortCtrl.current.abort(); } catch {}
    }
    const ctrl = new AbortController();
    treeAbortCtrl.current = ctrl;
    const myGen = ++treeRequestGen.current;
    treeInFlight.current = true;
    try {
      const data = await getScanTree(f, ctrl.signal);
      // Ignore if a newer request has been fired since we started
      if (myGen !== treeRequestGen.current) return;
      const result = data.folders || [];
      _cachedFolders = result;
      _cachedFilter = f;
      _cacheTimestamp = Date.now();
      setFolders(result);
      setLoading(false);
      setUpdating(false);
    } catch (err: any) {
      if (err?.name === "AbortError") return;  // Superseded — normal
      if (myGen !== treeRequestGen.current) return;
      console.error("Failed to load tree:", err);
      setLoading(false);
      setUpdating(false);
    } finally {
      if (myGen === treeRequestGen.current) {
        treeInFlight.current = false;
      }
    }
  }, [filter]);

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

  // Close the *arr actions dropdown when clicking outside it.
  useEffect(() => {
    if (!arrMenuOpen) return;
    const onDocClick = (e: MouseEvent) => {
      if (arrMenuRef.current && !arrMenuRef.current.contains(e.target as Node)) {
        setArrMenuOpen(false);
      }
    };
    document.addEventListener("mousedown", onDocClick);
    return () => document.removeEventListener("mousedown", onDocClick);
  }, [arrMenuOpen]);

  // Close the Health-check dropdown when clicking outside it.
  useEffect(() => {
    if (!healthMenuOpen) return;
    const onDocClick = (e: MouseEvent) => {
      if (healthMenuRef.current && !healthMenuRef.current.contains(e.target as Node)) {
        setHealthMenuOpen(false);
      }
    };
    document.addEventListener("mousedown", onDocClick);
    return () => document.removeEventListener("mousedown", onDocClick);
  }, [healthMenuOpen]);

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

  // When filter changes, debounce then reload.
  // Don't clear folders to [] — keep showing stale data under a "Updating..." indicator
  // until the new result arrives. This avoids the flash of "all items" when the user
  // changes filters quickly.
  useEffect(() => {
    if (loading) return;
    // Invalidate cache so a full reload happens
    _cachedFolders = null;
    _cachedFilter = "";
    setUpdating(true);
    // Debounce 200ms — rapid filter clicks get coalesced into a single request
    const handle = setTimeout(() => {
      loadTree(filter);
    }, 200);
    return () => clearTimeout(handle);
  }, [filter, loadTree]);

  const handleScan = async () => {
    setScanStarted(true);
    // Skip dirs marked auto_scan=false (e.g. NZBGet/SABnzbd landing zones
    // the user wants webhook-eligible but not crawled). v0.3.49+.
    const paths = selectedDir === "all"
      ? dirs.filter((d: any) => d.auto_scan !== false && d.auto_scan !== 0).map((d: any) => d.path)
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
        // Check if selecting individual files (inside poster accordion)
        const isFileSelect = !path.endsWith("/") && !lastClickedPathRef.current.endsWith("/");

        // When advanced search is active, the visible set is narrower than the full folder/file list.
        // Shift-range should operate ONLY over what's currently visible, not the whole library.
        const hasAdv = !!(advSearchResults && advSearchResults.size > 0);

        if (isFileSelect) {
          // Range-select files — limit to advanced-search matches when active
          const allFiles: string[] = [];
          for (const files of loadedFiles.values()) {
            for (const f of files) {
              if (hasAdv && !advSearchResults!.has(f.file_path)) continue;
              allFiles.push(f.file_path);
            }
          }
          allFiles.sort();
          const lastIdx = allFiles.indexOf(lastClickedPathRef.current);
          const curIdx = allFiles.indexOf(path);
          if (lastIdx !== -1 && curIdx !== -1) {
            const start = Math.min(lastIdx, curIdx);
            const end = Math.max(lastIdx, curIdx);
            for (let i = start; i <= end; i++) next.add(allFiles[i]);
          } else {
            if (next.has(path)) next.delete(path); else next.add(path);
          }
        } else {
          // Range-select folders — build the same visible subset the UI renders
          const isFiltered = filter !== "all";
          const searchLower = search.trim().toLowerCase();
          const searchWords = searchLower ? searchLower.split(/\s+/) : [];

          // If adv search active, the matched folders are the parents of matched files
          let advFolderSet: Set<string> | null = null;
          if (hasAdv) {
            advFolderSet = new Set();
            for (const fp of advSearchResults!) {
              const slash = fp.lastIndexOf("/");
              if (slash > 0) advFolderSet.add(fp.slice(0, slash));
            }
          }

          const pathSet = new Set<string>();
          for (const f of folders) {
            // Apply search filter
            if (searchWords.length > 0) {
              const hay = f.path.toLowerCase();
              if (!searchWords.every(w => hay.includes(w))) continue;
            }
            // Apply advanced search filter
            if (advFolderSet) {
              const inAdv = advFolderSet.has(f.path) || Array.from(advFolderSet).some(mf => mf.startsWith(f.path + "/"));
              if (!inAdv) continue;
            }

            if (isFiltered) {
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
            for (let i = start; i <= end; i++) next.add(allPaths[i]);
          } else {
            if (next.has(path)) next.delete(path); else next.add(path);
          }
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

  const handleToggleSubTrack = (filePath: string, streamIndex: number) => {
    setLoadedFiles(prev => {
      const next = new Map(prev);
      for (const [folder, files] of next) {
        const updated = files.map(f => {
          if (f.file_path !== filePath) return f;
          const newTracks = (f.subtitle_tracks || []).map(t =>
            t.stream_index === streamIndex ? { ...t, keep: !t.keep } : t
          );
          if (f.id) {
            updateSubtitleTracks(f.id, JSON.stringify(newTracks)).catch(() => {});
          }
          return { ...f, subtitle_tracks: newTracks };
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
    if (!await confirm({ message: `Move ${selected.length} file(s) to trash? This cannot be undone from Shrinkerr.`, confirmLabel: `Trash ${selected.length} files`, danger: true })) return;
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
    // Build folder set from selections — handle both file paths and folder paths
    const paths = Array.from(selectedPaths);
    const folderSet = new Set<string>();
    for (const p of paths) {
      if (p.endsWith("/")) {
        // Folder selection from poster view — strip trailing slash for the rescan path
        folderSet.add(p.slice(0, -1));
      } else {
        // File selection — derive parent folder
        const parts = p.split("/");
        folderSet.add(parts.slice(0, -1).join("/"));
      }
    }
    // Also check loadedFiles for any resolved file selections
    const selected = getSelectedFiles();
    for (const f of selected) {
      const parts = f.file_path.split("/");
      folderSet.add(parts.slice(0, -1).join("/"));
    }
    if (folderSet.size === 0) {
      toast("No files or folders selected");
      return;
    }
    if (!await confirm({ message: `Rescan ${folderSet.size} folder(s)?`, confirmLabel: "Rescan" })) return;
    for (const folder of folderSet) {
      await rescanFolder([folder]);
    }
    setSelectAllActive(false);
    setSelectedPaths(new Set());
    toast(`Rescanning ${folderSet.size} folder(s)...`, "success");
  };

  const handleHealthCheck = async (mode: "quick" | "thorough") => {
    const { queueHealthChecks } = await import("../api");
    const paths = selectAllActive ? folders.map(f => f.path + "/") : Array.from(selectedPaths);
    if (!paths.length && !selectAllActive) {
      toast("No files or folders selected");
      return;
    }
    if (mode === "thorough") {
      if (!await confirm({
        message: `Thorough check decodes every frame of every file — this can take a long time (roughly duration / 10 per file). Proceed?`,
        confirmLabel: "Run thorough check",
      })) return;
    }
    try {
      const res = await queueHealthChecks(
        selectAllActive ? [] : paths,
        mode,
        filter,
        selectAllActive,
      );
      if (res.added > 0) {
        toast(`Queued ${res.added} ${mode} health check${res.added !== 1 ? "s" : ""}`, "success");
        setSelectAllActive(false);
        setSelectedPaths(new Set());
      } else {
        toast("No files to check (already queued or no matches)");
      }
    } catch (err: any) {
      toast(`Failed to queue health checks: ${err.message || "unknown error"}`);
    }
  };

  const handleBulkArrAction = async (action: "replace" | "upgrade" | "missing") => {
    const { arrActionBulk } = await import("../api");

    // Pass the raw selection through — the backend now expands folder paths
    // to their files via scan_results (for replace/upgrade) and resolves
    // folder paths to the owning series via path-walking (for missing).
    // No frontend tree-expansion required.
    const paths = Array.from(selectedPaths);
    if (paths.length === 0) {
      toast("No files or folders selected");
      return;
    }

    const labels = {
      replace: { name: "Request replacement", danger: true, verb: "re-requested" },
      upgrade: { name: "Search for upgrade", danger: false, verb: "upgrade-searched" },
      missing: { name: "Search missing episodes", danger: false, verb: "searched" },
    };
    const cfg = labels[action];

    // Only replace needs a confirmation (destructive: deletes + blocklists)
    if (action === "replace") {
      if (!await confirm({
        message: `${cfg.name} for ${paths.length} file(s)?\n\nThis will blocklist the current release, delete each file, and trigger a fresh search.`,
        confirmLabel: `Replace ${paths.length}`,
        danger: true,
      })) return;
    } else if (action === "missing") {
      if (!await confirm({
        message: `Search for missing episodes across the series covered by your selection?\n\nShrinkerr will resolve unique series from ${paths.length} path(s) and ask Sonarr to search for any missing monitored episodes.`,
        confirmLabel: "Search missing",
      })) return;
    }

    try {
      const res: any = await arrActionBulk(paths, action, true);

      if (action === "missing") {
        // Aggregate response shape from search_missing_episodes
        if (res.success) {
          const summary = `${res.series_searched || 0}/${res.series_resolved || 0} series searched, ${res.total_episode_ids || 0} missing episode(s)`
            + (res.skipped_movie ? ` · skipped ${res.skipped_movie} movie(s)` : "")
            + (res.unresolved ? ` · ${res.unresolved} unresolved path(s)` : "");
          toast(summary, "success");
        } else {
          toast(`Missing search failed: ${res.error || "unknown error"}`, "error");
        }
        return;
      }

      // replace/upgrade: per-file results
      if (res.failed === 0) {
        toast(`${cfg.name}: ${res.succeeded} file(s) ${cfg.verb}`, "success");
      } else {
        toast(`${res.succeeded} ${cfg.verb}, ${res.failed} failed (check logs)`, res.succeeded > 0 ? "success" : "error");
      }
    } catch (exc: any) {
      toast(`${cfg.name} failed: ${exc?.message || exc}`, "error");
    }
  };

  const handleResetCorruptFlags = async () => {
    const { resetHealthStatus } = await import("../api");
    if (!await confirm({
      message: "Clear the 'corrupt' flag on every file currently marked corrupt and un-ignore them? They'll go back to being considered healthy until the next health check runs.",
      confirmLabel: "Clear corrupt flags",
    })) return;
    try {
      const res = await resetHealthStatus({ reset_all_corrupt: true, unignore: true });
      toast(`Cleared ${res.reset} corrupt flag(s)${res.unignored ? `, un-ignored ${res.unignored}` : ""}`, "success");
      loadTree();
    } catch (err: any) {
      toast(`Failed to reset: ${err.message || "unknown error"}`, "error");
    }
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

    // Check if any selected files are ignored
    const allFiles = Array.from(loadedFiles.values()).flat();
    const hasIgnored = allFiles.some(f => f.ignored && (
      paths.includes(f.file_path) || paths.some(p => p.endsWith("/") && f.file_path.startsWith(p.slice(0, -1)))
    ));

    if (selectAllActive) {
      // Select all: use the filter-based server-side resolution
      const anyIgnored = allFiles.some(f => f.ignored);
      setEstimatePaths(folders.map(f => f.path + "/"));
      setEstimateHasIgnored(anyIgnored);
      return;
    }
    if (!paths.length) {
      toast("No files or folders selected");
      return;
    }
    setEstimatePaths(paths);
    setEstimateHasIgnored(hasIgnored);
  };

  const handleConfirmAdd = async (priority: number, overrideRules: boolean = false, encodingOverrides?: any) => {
    if (!estimatePaths) return;
    // Snapshot the count before clearing estimatePaths — drives the
    // in-flight overlay's "Adding N items to queue…" copy. We can't
    // distinguish folder placeholders from individual files here without
    // hitting the tree, so the count reported is "selections submitted",
    // which is close enough as a progress affordance for the user. The
    // final toast still uses the server's authoritative result.added.
    const submittedCount = estimatePaths.length;
    setEstimatePaths(null);
    setAddingToQueueCount(submittedCount);
    try {
      const extra: Record<string, any> = {};
      if (encodingOverrides) {
        if (encodingOverrides.encoder) extra.encoder_override = encodingOverrides.encoder;
        if (encodingOverrides.nvenc_preset) extra.nvenc_preset_override = encodingOverrides.nvenc_preset;
        if (encodingOverrides.nvenc_cq != null) extra.nvenc_cq_override = encodingOverrides.nvenc_cq;
        if (encodingOverrides.libx265_preset) extra.libx265_preset_override = encodingOverrides.libx265_preset;
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
    } finally {
      setAddingToQueueCount(null);
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
  const selectedCount = useMemo(() => {
    // When advanced search is active, only count files that actually match the search.
    // Folder selections should contribute only their matched files, not f.file_count.
    const hasAdv = !!(advSearchResults && advSearchResults.size > 0);

    if (selectAllActive) {
      if (hasAdv) return advSearchResults!.size;
      return folders.reduce((sum, f) => sum + f.file_count, 0);
    }

    let count = 0;
    // Individual file selections
    for (const p of selectedPaths) {
      if (!p.endsWith("/")) {
        if (!hasAdv || advSearchResults!.has(p)) count += 1;
      }
    }

    // Folder selections
    if (selectedFolderPrefixes.length > 0) {
      if (hasAdv) {
        // Count only matched files under any selected prefix
        for (const fp of advSearchResults!) {
          // Skip if we already counted this path as an individual selection
          if (selectedPaths.has(fp)) continue;
          for (const prefix of selectedFolderPrefixes) {
            if (fp.startsWith(prefix)) { count += 1; break; }
          }
        }
      } else {
        for (const f of folders) {
          const fp = f.path + "/";
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
    }
    return count;
  }, [selectAllActive, selectedPaths, selectedFolderPrefixes, folders, advSearchResults]);

  // Server-computed stats for StatsCards
  const ss = serverStats?.summary;
  const stats = ss ? {
    filesToConvert: ss.files_to_convert || 0,
    audioCleanup: ss.audio_cleanup || 0,
    ignoredCount: ss.ignored_count || 0,
    corruptCount: serverStats?.counts?.corrupt || 0,
    estimatedSavingsGB: (ss.estimated_savings_bytes || 0) / (1024 ** 3),
    totalScannedGB: (ss.total_size || 0) / (1024 ** 3),
  } : {
    filesToConvert: 0, audioCleanup: 0, ignoredCount: 0, corruptCount: 0,
    estimatedSavingsGB: 0, totalScannedGB: 0,
  };

  // Pre-compute the set of ancestor paths for advanced search results.
  // A folder f should be shown if f.path is an ancestor of any matched file.
  // Important: when advSearchResults is set but empty (0 matches), we must still
  // return an empty Set — not null — so the filter runs and yields 0 folders,
  // rather than showing all folders.
  const advAncestorPaths = useMemo(() => {
    if (!advSearchResults) return null;  // no advanced search active
    const ancestors = new Set<string>();
    for (const fp of advSearchResults) {
      const parts = fp.split("/");
      for (let i = 1; i < parts.length; i++) {
        ancestors.add(parts.slice(0, i).join("/"));
      }
    }
    return ancestors;  // may be empty if 0 matches
  }, [advSearchResults]);

  // Memoize the filtered folder list so this doesn't rerun on every render.
  // Previous code ran O(N×M) + thousands of allocations on every render.
  const displayFolders = useMemo(() => {
    let result = folders;
    const s = search.trim().toLowerCase();
    if (s) {
      const words = s.split(/\s+/);
      result = result.filter(f => {
        const haystack = f.path.toLowerCase();
        return words.every(w => haystack.includes(w));
      });
    }
    if (advAncestorPaths) {
      result = result.filter(f => advAncestorPaths.has(f.path));
    }
    return result;
  }, [folders, search, advAncestorPaths]);

  /**
   * Shared controls row: search + advanced + sort + view-toggle + refresh
   * posters. Rendered inline with the filter toggle when the filter panel
   * is collapsed, or on its own row below the FilterBar when expanded —
   * two positions, one JSX source of truth. Adding a new control here
   * automatically shows up in both states.
   */
  const scannerControls = (
    <>
      <div style={{ position: "relative", flex: "1 1 200px", minWidth: 160, maxWidth: 300 }}>
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="var(--text-muted)" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"
          style={{ position: "absolute", left: 10, top: "50%", transform: "translateY(-50%)", pointerEvents: "none" }}>
          <circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/>
        </svg>
        <input type="text" placeholder="Search folders..." value={searchInput}
          onChange={(e) => setSearchInput(e.target.value)}
          style={{ width: "100%", padding: "6px 12px 6px 30px", fontSize: 12, lineHeight: "1.4", background: "var(--bg-card)", color: "var(--text-secondary)", border: "1px solid var(--border)", borderRadius: 16, outline: "none", boxSizing: "border-box" as const }} />
        {searchInput && (
          <button onClick={() => { setSearchInput(""); setSearch(""); }}
            style={{ position: "absolute", right: 10, top: "50%", transform: "translateY(-50%)", background: "none", border: "none", color: "var(--text-muted)", cursor: "pointer", fontSize: 14, lineHeight: 1 }}>&times;</button>
        )}
      </div>
      <button
        className="sort-pill"
        title="Advanced search — query files by codec, bitrate, audio channels, VMAF, and more"
        onClick={() => setAdvSearchOpen(true)}
        style={{
          display: "inline-flex", alignItems: "center", gap: 5, whiteSpace: "nowrap",
          background: advSearchPredicates.length > 0 ? "var(--accent-bg)" : undefined,
          color: advSearchPredicates.length > 0 ? "var(--accent)" : undefined,
          borderColor: advSearchPredicates.length > 0 ? "var(--accent)" : undefined,
        }}
      >
        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          {/* Microscope icon */}
          <path d="M6 18h8"/>
          <path d="M3 22h18"/>
          <path d="M14 22a7 7 0 1 0 0-14h-1"/>
          <path d="M9 14h2"/>
          <path d="M9 12a2 2 0 0 1-2-2V6h6v4a2 2 0 0 1-2 2Z"/>
          <path d="M12 6V3a1 1 0 0 0-1-1H9a1 1 0 0 0-1 1v3"/>
        </svg>
        Advanced
        {advSearchPredicates.length > 0 && (
          <span style={{ fontSize: 10, padding: "0 5px", borderRadius: 8, background: "var(--accent)", color: "#fff", marginLeft: 2 }}>
            {advSearchPredicates.length}
          </span>
        )}
      </button>
      <span style={{ width: 1, height: 16, background: "var(--border)" }} />
      <span style={{ fontSize: 12, opacity: 0.5, whiteSpace: "nowrap" }}>Sort:</span>
      {([["name", "A-Z"], ["size", "Size"], ["files", "Files"], ["date", "Date"]] as const).map(([val, label]) => (
        <button key={val}
          className={`sort-pill ${sortBy === val ? "active" : ""}`}
          onClick={() => { if (sortBy === val) setSortDir(d => d === "asc" ? "desc" : "asc"); else { setSortBy(val); setSortDir(val === "size" || val === "date" ? "desc" : "asc"); } }}>
          {label} {sortBy === val && (sortDir === "asc"
            ? <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round" style={{ verticalAlign: "middle", marginLeft: 2 }}><polyline points="12 5 6 11"/><polyline points="12 5 18 11"/><line x1="12" y1="5" x2="12" y2="19"/></svg>
            : <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round" style={{ verticalAlign: "middle", marginLeft: 2 }}><polyline points="12 19 6 13"/><polyline points="12 19 18 13"/><line x1="12" y1="19" x2="12" y2="5"/></svg>
          )}
        </button>
      ))}
      {/* View toggle */}
      <span style={{ width: 1, height: 16, background: "var(--border)" }} />
      <button
        className="sort-pill"
        onClick={() => { const next = viewMode === "tree" ? "poster" : "tree"; setViewMode(next); localStorage.setItem("shrinkerr_viewMode", next); localStorage.removeItem("squeezarr_viewMode"); }}
        style={{ display: "inline-flex", alignItems: "center", gap: 5 }}
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
  );

  return (
    <div>
      <div style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 24 }}>
        <select
          value={selectedDir}
          onChange={(e) => setSelectedDir(e.target.value)}
          style={{
            // backgroundColor (not the `background` shorthand) so React
            // doesn't emit a `background:` declaration that wipes the
            // explicit backgroundImage chevron below it.
            backgroundColor: "var(--bg-card)", color: "var(--text-secondary)",
            border: "1px solid var(--border)", padding: "6px 28px 6px 10px",
            borderRadius: 4, fontSize: 13, height: 36, boxSizing: "border-box" as const,
            backgroundImage: `url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='10' height='6' viewBox='0 0 10 6'%3E%3Cpath d='M1 1l4 4 4-4' stroke='%23827b9a' fill='none' stroke-width='1.5' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E")`,
            backgroundRepeat: "no-repeat",
            backgroundPosition: "right 10px center",
          }}
        >
          <option value="all">All configured paths</option>
          {dirs
            .filter((d: any) => d.auto_scan !== false && d.auto_scan !== 0)
            .map((d: any) => (
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
        {scanning && scanProgress && scanProgress.status !== "metadata" && !scanProgress.status?.startsWith("health_check") && (
          <span style={{ fontSize: 12, opacity: 0.6 }}>
            {fmtNum(scanProgress.probed)} / {fmtNum(scanProgress.total)} files probed
          </span>
        )}
        {refreshingMetadata && scanProgress && scanProgress.status === "metadata" && (
          <span style={{ fontSize: 12, opacity: 0.6 }}>
            Metadata: {fmtNum(scanProgress.probed)} / {fmtNum(scanProgress.total)} checked
          </span>
        )}
        {scanning && scanProgress && scanProgress.status?.startsWith("health_check") && (
          <span style={{ fontSize: 12, opacity: 0.6 }}>
            Health check ({scanProgress.status === "health_check_thorough" ? "thorough" : "quick"}):{" "}
            {fmtNum(scanProgress.probed)} / {fmtNum(scanProgress.total)}
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
          {/* Filter toggle + active filter indicator */}
          <div style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 12, flexWrap: "wrap" }}>
            <button
              className="filter-pill"
              onClick={() => setFiltersOpen(!filtersOpen)}
              style={{
                display: "inline-flex", alignItems: "center", gap: 6, whiteSpace: "nowrap",
                background: filtersOpen ? "#4920f0" : "var(--bg-card)",
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
                  {treeTotalFiles > 99999 ? `${(treeTotalFiles / 1000).toFixed(0)}k` : fmtNum(treeTotalFiles)}
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
            {/* Collapsed: the controls live here, inline with the filter
                toggle, so the whole thing fits on one row when filters
                aren't active. Expanded: rendered below the FilterBar
                instead — see the `{filtersOpen && …}` block lower down. */}
            {!filtersOpen && scannerControls}
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

          {/* When Corrupt filter is active, offer a one-click "clear false positives"
              action. Handy after upgrading Shrinkerr with a fixed health-check
              classifier (e.g. the ref-frames-exceeds-max false positive). */}
          {filters.includes("corrupt") && (
            <div style={{
              display: "flex",
              alignItems: "center",
              gap: 10,
              padding: "8px 12px",
              marginBottom: 10,
              background: "rgba(229,160,13,0.08)",
              border: "1px solid rgba(229,160,13,0.25)",
              borderRadius: 6,
              fontSize: 12,
            }}>
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#e5a00d" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/>
              </svg>
              <span style={{ color: "var(--text-secondary)", flex: 1 }}>
                Some files may be flagged corrupt due to benign ffmpeg warnings (e.g. "number of reference frames exceeds max"). Clear the flags and let them re-check.
              </span>
              <button
                className="btn btn-secondary"
                style={{ fontSize: 11, padding: "4px 10px", whiteSpace: "nowrap" }}
                onClick={handleResetCorruptFlags}
              >
                Clear all corrupt flags
              </button>
            </div>
          )}

          {/* Expanded-filter layout: FilterBar pill grid first, then the
              shared scannerControls on their own row beneath it. Keeping
              the controls on their own row here means the pill grid can
              consume all the width without forcing search/sort to wrap. */}
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
                {scannerControls}
              </div>
            </>
          )}

          {/* Selection control panel */}
          <div className="queue-control-panel" style={{
            display: "flex", flexWrap: "wrap", gap: 6, alignItems: "center",
            padding: "8px 12px", marginBottom: 12,
            background: selectedCount > 0 ? "var(--bg-card)" : "var(--bg-secondary)",
            border: "1px solid var(--border)",
            borderRadius: 6, transition: "background 0.15s",
            position: "sticky" as const, top: 0, zIndex: 50,
          }}>
            <button className="btn btn-secondary" style={{ fontSize: 12, padding: "6px 12px", borderRadius: 16, whiteSpace: "nowrap" }} onClick={handleSelectAll}>
              {selectAllActive || selectedPaths.size > 0 ? "Deselect all" : "Select all"}
            </button>
            {selectedCount > 0 && (() => {
              const selectedFiles = getSelectedFiles();
              const allSelectedIgnored = selectedFiles.length > 0 && selectedFiles.every(f => f.ignored);
              const someSelectedIgnored = selectedFiles.some(f => f.ignored);
              return <>
                <span style={{ fontSize: 11, color: "var(--accent)", fontWeight: 600 }}>{selectedCount} selected</span>
                <span style={{ width: 1, height: 14, background: "var(--border)" }} />
                <button className="btn btn-primary" style={{ fontSize: 12, padding: "6px 12px", borderRadius: 16, whiteSpace: "nowrap", display: "inline-flex", alignItems: "center", gap: 4 }} onClick={handleAddToQueue}>
                  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
                  Add to queue
                </button>
                <button className="btn btn-secondary" style={{ fontSize: 12, padding: "6px 12px", borderRadius: 16, whiteSpace: "nowrap", display: "inline-flex", alignItems: "center", gap: 4 }} onClick={handleBulkRescan}>
                  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></svg>
                  Rescan
                </button>
                <button
                  className="btn btn-secondary"
                  title="Rename selected files using the renaming patterns"
                  style={{ fontSize: 12, padding: "6px 12px", borderRadius: 16, whiteSpace: "nowrap", display: "inline-flex", alignItems: "center", gap: 4 }}
                  onClick={() => {
                    // Pass all selected paths (folder + file) — server expands folders
                    const paths = Array.from(selectedPaths);
                    if (paths.length === 0) {
                      toast("No files or folders selected");
                      return;
                    }
                    setRenamePaths(paths);
                  }}
                >
                  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                    <path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7" />
                    <path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z" />
                  </svg>
                  Rename
                </button>
                {/* Health check dropdown: quick / thorough */}
                <div ref={healthMenuRef} style={{ position: "relative", display: "inline-flex" }}>
                  <button
                    className="btn btn-secondary"
                    title="Run a health check on the selected files"
                    style={{ fontSize: 12, padding: "6px 12px", borderRadius: 16, whiteSpace: "nowrap", display: "inline-flex", alignItems: "center", gap: 4 }}
                    onClick={() => setHealthMenuOpen(o => !o)}
                  >
                    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                      <path d="M20.84 4.61a5.5 5.5 0 0 0-7.78 0L12 5.67l-1.06-1.06a5.5 5.5 0 0 0-7.78 7.78l1.06 1.06L12 21.23l7.78-7.78 1.06-1.06a5.5 5.5 0 0 0 0-7.78z"/>
                    </svg>
                    Health check
                    <span style={{ fontSize: 9, opacity: 0.6 }}>{healthMenuOpen ? "▲" : "▼"}</span>
                  </button>
                  {healthMenuOpen && (
                    <div
                      style={{
                        position: "absolute",
                        top: "calc(100% + 4px)",
                        right: 0,
                        zIndex: 100,
                        background: "var(--bg-card)",
                        border: "1px solid var(--border)",
                        borderRadius: 6,
                        boxShadow: "0 6px 20px rgba(0,0,0,0.4)",
                        padding: 4,
                        minWidth: 240,
                        display: "flex",
                        flexDirection: "column",
                      }}
                    >
                      <button
                        onClick={() => { setHealthMenuOpen(false); handleHealthCheck("quick"); }}
                        style={menuItemStyle}
                      >
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={{ color: "#6ce5b0" }}>
                          <path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/>
                        </svg>
                        <div style={{ textAlign: "left" }}>
                          <div style={{ fontSize: 12, color: "var(--text-secondary)", fontWeight: 500 }}>Quick check</div>
                          <div style={{ fontSize: 10, color: "var(--text-muted)" }}>Header / metadata parse. Fast — a few seconds per file.</div>
                        </div>
                      </button>
                      <button
                        onClick={() => { setHealthMenuOpen(false); handleHealthCheck("thorough"); }}
                        style={menuItemStyle}
                      >
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={{ color: "#ffa94d" }}>
                          <circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/>
                        </svg>
                        <div style={{ textAlign: "left" }}>
                          <div style={{ fontSize: 12, color: "var(--text-secondary)", fontWeight: 500 }}>Thorough check</div>
                          <div style={{ fontSize: 10, color: "var(--text-muted)" }}>Full frame-by-frame decode. Slow — roughly duration / 10 per file.</div>
                        </div>
                      </button>
                    </div>
                  )}
                </div>

                {/* *arr actions dropdown: upgrade / missing / replace */}
                <div ref={arrMenuRef} style={{ position: "relative", display: "inline-flex" }}>
                  <button
                    className="btn btn-secondary"
                    title="Sonarr/Radarr actions: upgrade, missing episodes, replacement"
                    style={{ fontSize: 12, padding: "6px 12px", borderRadius: 16, whiteSpace: "nowrap", display: "inline-flex", alignItems: "center", gap: 4 }}
                    onClick={() => setArrMenuOpen(o => !o)}
                  >
                    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                      <circle cx="12" cy="12" r="10"/>
                      <path d="M2 12h20M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/>
                    </svg>
                    *arr actions
                    <span style={{ fontSize: 9, opacity: 0.6 }}>{arrMenuOpen ? "▲" : "▼"}</span>
                  </button>
                  {arrMenuOpen && (
                    <div
                      style={{
                        position: "absolute",
                        top: "calc(100% + 4px)",
                        right: 0,
                        zIndex: 100,
                        background: "var(--bg-card)",
                        border: "1px solid var(--border)",
                        borderRadius: 6,
                        boxShadow: "0 6px 20px rgba(0,0,0,0.4)",
                        padding: 4,
                        minWidth: 240,
                        display: "flex",
                        flexDirection: "column",
                      }}
                    >
                      <button
                        onClick={() => { setArrMenuOpen(false); handleBulkArrAction("upgrade"); }}
                        style={menuItemStyle}
                      >
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={{ color: "#6ce5b0" }}>
                          <polyline points="17 11 12 6 7 11"/><polyline points="17 18 12 13 7 18"/>
                        </svg>
                        <div style={{ textAlign: "left" }}>
                          <div style={{ fontSize: 12, color: "var(--text-secondary)", fontWeight: 500 }}>Search for upgrades</div>
                          <div style={{ fontSize: 10, color: "var(--text-muted)" }}>Find better releases per quality profile. No delete.</div>
                        </div>
                      </button>
                      <button
                        onClick={() => { setArrMenuOpen(false); handleBulkArrAction("missing"); }}
                        style={menuItemStyle}
                      >
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={{ color: "#7cb4ff" }}>
                          <rect x="3" y="4" width="18" height="18" rx="2" ry="2"/><line x1="16" y1="2" x2="16" y2="6"/><line x1="8" y1="2" x2="8" y2="6"/><line x1="3" y1="10" x2="21" y2="10"/>
                        </svg>
                        <div style={{ textAlign: "left" }}>
                          <div style={{ fontSize: 12, color: "var(--text-secondary)", fontWeight: 500 }}>Search missing episodes</div>
                          <div style={{ fontSize: 10, color: "var(--text-muted)" }}>Per series covered by selection. Sonarr only.</div>
                        </div>
                      </button>
                      <button
                        onClick={() => { setArrMenuOpen(false); handleBulkArrAction("replace"); }}
                        style={menuItemStyle}
                      >
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={{ color: "#e94560" }}>
                          <path d="M21 12a9 9 0 11-3-6.7L21 8"/><path d="M21 3v5h-5"/>
                        </svg>
                        <div style={{ textAlign: "left" }}>
                          <div style={{ fontSize: 12, color: "var(--text-secondary)", fontWeight: 500 }}>Request replacements</div>
                          <div style={{ fontSize: 10, color: "var(--text-muted)" }}>Blocklist + delete + search fresh. Destructive.</div>
                        </div>
                      </button>
                    </div>
                  )}
                </div>
                {allSelectedIgnored ? (
                  <button className="btn btn-secondary" style={{ fontSize: 12, padding: "6px 12px", borderRadius: 16, whiteSpace: "nowrap", display: "inline-flex", alignItems: "center", gap: 4 }} onClick={handleBulkUnignore}>
                    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>
                    Unignore
                  </button>
                ) : (
                  <>
                    <button className="btn btn-secondary" style={{ fontSize: 12, padding: "6px 12px", borderRadius: 16, whiteSpace: "nowrap", display: "inline-flex", alignItems: "center", gap: 4 }} onClick={handleBulkIgnore}>
                      <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="10"/><line x1="4.93" y1="4.93" x2="19.07" y2="19.07"/></svg>
                      Ignore
                    </button>
                    {someSelectedIgnored && (
                      <button className="btn btn-secondary" style={{ fontSize: 12, padding: "6px 12px", borderRadius: 16, whiteSpace: "nowrap", display: "inline-flex", alignItems: "center", gap: 4 }} onClick={handleBulkUnignore}>
                        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>
                        Unignore
                      </button>
                    )}
                  </>
                )}
                <button className="btn btn-secondary" style={{ fontSize: 12, padding: "6px 12px", borderRadius: 16, whiteSpace: "nowrap", display: "inline-flex", alignItems: "center", gap: 4 }} onClick={handleBulkRemove}>
                  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
                  Remove
                </button>
                <span style={{ width: 1, height: 14, background: "var(--border)" }} />
                <button className="btn btn-secondary" style={{ fontSize: 12, padding: "6px 12px", borderRadius: 16, whiteSpace: "nowrap", color: "#e94560", display: "inline-flex", alignItems: "center", gap: 4 }} onClick={handleBulkDelete}>
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

          {/* Advanced search active banner */}
          {advSearchResults && (
            <div style={{
              display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap",
              padding: "8px 14px", marginBottom: 12,
              background: "var(--accent-bg)", border: "1px solid var(--accent)", borderRadius: 6,
            }}>
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="var(--accent)" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M6 18h8"/><path d="M3 22h18"/><path d="M14 22a7 7 0 1 0 0-14h-1"/><path d="M9 14h2"/><path d="M9 12a2 2 0 0 1-2-2V6h6v4a2 2 0 0 1-2 2Z"/><path d="M12 6V3a1 1 0 0 0-1-1H9a1 1 0 0 0-1 1v3"/>
              </svg>
              <span style={{ fontSize: 12, color: "var(--accent)" }}>
                <strong>Advanced search:</strong> {advSearchResults.size.toLocaleString()} match{advSearchResults.size === 1 ? "" : "es"} from {advSearchPredicates.length} condition{advSearchPredicates.length === 1 ? "" : "s"}
              </span>
              <button
                className="btn btn-secondary"
                style={{ fontSize: 11, padding: "3px 10px", marginLeft: "auto" }}
                onClick={() => setAdvSearchOpen(true)}
              >Edit</button>
              <button
                className="btn btn-secondary"
                style={{ fontSize: 11, padding: "3px 10px" }}
                onClick={() => { setAdvSearchResults(null); setAdvSearchPredicates([]); }}
              >Clear</button>
            </div>
          )}
          <div style={{
            position: "relative",
            opacity: updating ? 0.5 : 1,
            transition: "opacity 0.15s",
            pointerEvents: updating ? "none" : "auto",
          }}>
          {updating && (
            <div style={{
              position: "absolute", top: 8, right: 8, zIndex: 10,
              background: "var(--bg-card)", border: "1px solid var(--border)",
              padding: "4px 10px", borderRadius: 12, fontSize: 11,
              color: "var(--text-secondary)", display: "flex", alignItems: "center", gap: 6,
            }}>
              <div className="spinner" style={{ width: 10, height: 10 }} />
              Updating...
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
              onToggleSubTrack={handleToggleSubTrack}
              onRemoveFile={handleRemoveFile}
              onIgnoreFile={handleIgnoreFile}
              onUnignoreFile={handleUnignoreFile}
              onRescanFolder={handleRescanFolder}
              onDeleteFile={handleDeleteFile}
              onFolderFilesLoaded={handleFolderFilesLoaded}
              externalFiles={loadedFiles}
              mediaDirs={dirs.map((d: any) => d.path)}
              sortBy={sortBy}
              sortDir={sortDir}
              allowedPaths={advSearchResults || undefined}
            />
          ) : (
            <PosterGrid
              folders={displayFolders}
              filter={filter}
              isSelected={isSelected}
              onToggleSelect={handleToggleSelect}
              onToggleTrack={handleToggleTrack}
              onToggleSubTrack={handleToggleSubTrack}
              onRemoveFile={handleRemoveFile}
              onIgnoreFile={handleIgnoreFile}
              onUnignoreFile={handleUnignoreFile}
              onDeleteFile={handleDeleteFile}
              onFolderFilesLoaded={handleFolderFilesLoaded}
              mediaDirs={dirs.map((d: any) => d.path)}
              sortBy={sortBy}
              sortDir={sortDir}
            />
          )}
          </div>
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

      {/* "Adding to queue…" in-flight overlay. Renders while the
          /jobs/add-from-scan call is awaited so the user has feedback
          during the multi-second server-side fanout (folder→file
          resolution + rule matching + bulk insert). v0.3.57+. */}
      {addingToQueueCount !== null && (
        <div
          style={{
            position: "fixed", inset: 0, zIndex: 1100,
            background: "rgba(0,0,0,0.55)", backdropFilter: "blur(3px)",
            display: "flex", alignItems: "center", justifyContent: "center",
          }}
        >
          <div
            style={{
              background: "var(--bg-card)", border: "1px solid var(--border)",
              borderRadius: 8, padding: "20px 28px", minWidth: 280,
              display: "flex", alignItems: "center", gap: 14,
              boxShadow: "0 10px 40px rgba(0,0,0,0.5)",
            }}
          >
            <div className="spinner" style={{ width: 22, height: 22, flexShrink: 0 }} />
            <div style={{ display: "flex", flexDirection: "column" }}>
              <span style={{ color: "var(--text-primary)", fontWeight: 600, fontSize: 14 }}>
                Adding {addingToQueueCount.toLocaleString()} {addingToQueueCount === 1 ? "item" : "items"} to queue…
              </span>
              <span style={{ color: "var(--text-muted)", fontSize: 12, marginTop: 2 }}>
                Resolving rules, deduping, inserting jobs.
              </span>
            </div>
          </div>
        </div>
      )}

      {/* Rename modal */}
      {renamePaths && (
        <RenameModal
          filePaths={renamePaths}
          onClose={() => setRenamePaths(null)}
          onApplied={() => {
            setRenamePaths(null);
            loadTree(filter);
            toast("Rename applied", "success");
          }}
        />
      )}

      {/* Advanced search modal */}
      {advSearchOpen && (
        <AdvancedSearchModal
          initial={advSearchPredicates}
          onApply={(preds, paths) => {
            setAdvSearchPredicates(preds);
            setAdvSearchResults(new Set(paths));
            setAdvSearchOpen(false);
            toast(`Advanced search: ${paths.length} match${paths.length === 1 ? "" : "es"}`, "success");
          }}
          onClose={() => setAdvSearchOpen(false)}
        />
      )}
    </div>
  );
}
