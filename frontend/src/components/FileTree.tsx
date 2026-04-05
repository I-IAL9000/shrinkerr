import { useState, useEffect, useRef, useCallback } from "react";
import type { ScannedFile } from "../types";
import { getScanFiles } from "../api";
import FileDetail from "./FileDetail";
import { useConfirm } from "./ConfirmModal";

export type SortBy = "name" | "size" | "files" | "date";
export type SortDirection = "asc" | "desc";

/** Server-returned folder info */
export interface FolderInfo {
  path: string;
  file_count: number;
  total_size: number;
  newest_mtime: number;
}

interface FileTreeProps {
  folders: FolderInfo[];
  filter?: string;
  search?: string;
  isSelected: (path: string) => boolean;
  onToggleSelect: (path: string, shiftKey?: boolean) => void;
  onToggleTrack: (filePath: string, streamIndex: number) => void;
  onRemoveFile: (filePath: string) => void;
  onIgnoreFile?: (filePath: string) => void;
  onUnignoreFile?: (filePath: string) => void;
  onRescanFolder?: (folderPath: string) => void;
  onDeleteFile?: (filePath: string) => void;
  onFolderFilesLoaded?: (folderPath: string, files: ScannedFile[]) => void;
  sortBy?: SortBy;
  sortDir?: SortDirection;
}

// ─── Tree node built from flat folder list ───

interface TreeNode {
  name: string;
  path: string;
  children: Map<string, TreeNode>;
  // Leaf folder stats (from server)
  file_count: number;
  total_size: number;
  newest_mtime: number;
  // Aggregated (includes children)
  agg_file_count: number;
  agg_total_size: number;
  agg_newest_mtime: number;
  isLeaf: boolean; // true = directly contains files
}

function buildTreeFromFolders(folders: FolderInfo[]): TreeNode {
  const root: TreeNode = {
    name: "root", path: "", children: new Map(),
    file_count: 0, total_size: 0, newest_mtime: 0,
    agg_file_count: 0, agg_total_size: 0, agg_newest_mtime: 0,
    isLeaf: false,
  };

  for (const folder of folders) {
    const parts = folder.path.split("/").filter(Boolean);
    let node = root;
    for (let i = 0; i < parts.length; i++) {
      const part = parts[i];
      if (!node.children.has(part)) {
        node.children.set(part, {
          name: part,
          path: "/" + parts.slice(0, i + 1).join("/"),
          children: new Map(),
          file_count: 0, total_size: 0, newest_mtime: 0,
          agg_file_count: 0, agg_total_size: 0, agg_newest_mtime: 0,
          isLeaf: false,
        });
      }
      node = node.children.get(part)!;
    }
    // This is a leaf folder (directly contains files)
    node.isLeaf = true;
    node.file_count = folder.file_count;
    node.total_size = folder.total_size;
    node.newest_mtime = folder.newest_mtime;
  }

  // Aggregate stats bottom-up
  function aggregate(node: TreeNode): void {
    node.agg_file_count = node.file_count;
    node.agg_total_size = node.total_size;
    node.agg_newest_mtime = node.newest_mtime;
    for (const child of node.children.values()) {
      aggregate(child);
      node.agg_file_count += child.agg_file_count;
      node.agg_total_size += child.agg_total_size;
      if (child.agg_newest_mtime > node.agg_newest_mtime) {
        node.agg_newest_mtime = child.agg_newest_mtime;
      }
    }
  }
  aggregate(root);

  return root;
}

/** Flat title-level tree — groups season folders under their show/movie title.
 *  Used when filters or search are active so users see titles directly. */
function buildFlatTitleTree(folders: FolderInfo[]): TreeNode {
  const root: TreeNode = {
    name: "root", path: "", children: new Map(),
    file_count: 0, total_size: 0, newest_mtime: 0,
    agg_file_count: 0, agg_total_size: 0, agg_newest_mtime: 0,
    isLeaf: false,
  };

  // Group folders by their title-level parent
  const groups = new Map<string, { name: string; folders: FolderInfo[] }>();
  for (const folder of folders) {
    const parts = folder.path.split("/").filter(Boolean);
    let titleIdx = -1;
    for (let i = 0; i < parts.length; i++) {
      if (/\[(?:tvdb-\d+|tt\d+)\]/.test(parts[i])) { titleIdx = i; break; }
    }
    let groupPath: string, groupName: string;
    if (titleIdx >= 0) {
      groupPath = "/" + parts.slice(0, titleIdx + 1).join("/");
      // Show parent > title for context (e.g. "TV4 > Show Name (2020) [tvdb-123]")
      const parentName = titleIdx > 0 ? parts[titleIdx - 1] : "";
      groupName = parentName ? `${parentName}  >  ${parts[titleIdx]}` : parts[titleIdx];
    } else {
      groupPath = folder.path;
      const parentName = parts.length > 1 ? parts[parts.length - 2] : "";
      const fileName = parts[parts.length - 1] || folder.path;
      groupName = parentName ? `${parentName}  >  ${fileName}` : fileName;
    }
    if (!groups.has(groupPath)) {
      groups.set(groupPath, { name: groupName, folders: [] });
    }
    groups.get(groupPath)!.folders.push(folder);
  }

  // Build shallow tree: root → title nodes (with season children if TV)
  for (const [groupPath, { name, folders: groupFolders }] of groups) {
    const titleNode: TreeNode = {
      name, path: groupPath, children: new Map(),
      file_count: 0, total_size: 0, newest_mtime: 0,
      agg_file_count: 0, agg_total_size: 0, agg_newest_mtime: 0,
      isLeaf: groupFolders.length === 1 && groupFolders[0].path === groupPath,
    };

    if (groupFolders.length === 1 && groupFolders[0].path === groupPath) {
      // Single folder (movie) — title is the leaf
      titleNode.isLeaf = true;
      titleNode.file_count = groupFolders[0].file_count;
      titleNode.total_size = groupFolders[0].total_size;
      titleNode.newest_mtime = groupFolders[0].newest_mtime;
    } else {
      // Multiple folders (TV seasons) — add as children
      for (const f of groupFolders) {
        const seasonName = f.path.substring(groupPath.length + 1).split("/").filter(Boolean)[0] || f.path.split("/").pop() || "Files";
        const seasonNode: TreeNode = {
          name: seasonName, path: f.path, children: new Map(),
          file_count: f.file_count, total_size: f.total_size, newest_mtime: f.newest_mtime,
          agg_file_count: f.file_count, agg_total_size: f.total_size, agg_newest_mtime: f.newest_mtime,
          isLeaf: true,
        };
        titleNode.children.set(seasonName, seasonNode);
      }
    }

    // Aggregate
    titleNode.agg_file_count = titleNode.file_count;
    titleNode.agg_total_size = titleNode.total_size;
    titleNode.agg_newest_mtime = titleNode.newest_mtime;
    for (const child of titleNode.children.values()) {
      titleNode.agg_file_count += child.agg_file_count;
      titleNode.agg_total_size += child.agg_total_size;
      if (child.agg_newest_mtime > titleNode.agg_newest_mtime) {
        titleNode.agg_newest_mtime = child.agg_newest_mtime;
      }
    }

    root.children.set(groupPath, titleNode);
  }

  // Aggregate root
  for (const child of root.children.values()) {
    root.agg_file_count += child.agg_file_count;
    root.agg_total_size += child.agg_total_size;
    if (child.agg_newest_mtime > root.agg_newest_mtime) {
      root.agg_newest_mtime = child.agg_newest_mtime;
    }
  }

  return root;
}

function sortNodes(nodes: TreeNode[], sortBy: SortBy, sortDir: SortDirection): TreeNode[] {
  const sorted = [...nodes].sort((a, b) => {
    if (sortBy === "size") return a.agg_total_size - b.agg_total_size;
    if (sortBy === "files") return a.agg_file_count - b.agg_file_count;
    if (sortBy === "date") return a.agg_newest_mtime - b.agg_newest_mtime;
    return a.name.localeCompare(b.name);
  });
  return sortDir === "desc" ? sorted.reverse() : sorted;
}

function sortFiles(files: ScannedFile[], sortBy: SortBy, sortDir: SortDirection): ScannedFile[] {
  const sorted = [...files].sort((a, b) => {
    if (sortBy === "size") return a.file_size - b.file_size;
    if (sortBy === "date") return (a.file_mtime || 0) - (b.file_mtime || 0);
    return a.file_name.localeCompare(b.file_name);
  });
  return sortDir === "desc" ? sorted.reverse() : sorted;
}

// ─── Visual order for shift-click selection ───

function getVisualOrderFromTree(
  root: TreeNode,
  expanded: Set<string>,
  folderFiles: Map<string, ScannedFile[]>,
  sortBy: SortBy,
  sortDir: SortDirection,
): string[] {
  const result: string[] = [];
  function walk(node: TreeNode) {
    for (const child of sortNodes(Array.from(node.children.values()), sortBy, sortDir)) {
      if (expanded.has(child.path)) {
        walk(child);
        // If this is a leaf with loaded files, include file paths
        const files = folderFiles.get(child.path);
        if (files && child.isLeaf) {
          for (const f of sortFiles(files, sortBy, sortDir)) {
            result.push(f.file_path);
          }
        }
      }
    }
  }
  walk(root);
  return result;
}

export { getVisualOrderFromTree as getVisualOrder };

// ─── Helper: media ID link ───

function parseMediaId(folderName: string): { type: "imdb"; id: string } | { type: "tvdb"; id: string } | null {
  const imdbMatch = folderName.match(/\[(tt\d+)\]/);
  if (imdbMatch) return { type: "imdb", id: imdbMatch[1] };
  const tvdbMatch = folderName.match(/\[tvdb-(\d+)\]/);
  if (tvdbMatch) return { type: "tvdb", id: tvdbMatch[1] };
  return null;
}

function MediaIdLink({ folderName }: { folderName: string }) {
  const mediaId = parseMediaId(folderName);
  if (!mediaId) return null;

  if (mediaId.type === "imdb") {
    return (
      <a
        href={`https://www.imdb.com/title/${mediaId.id}/`}
        target="_blank"
        rel="noopener noreferrer"
        onClick={(e) => e.stopPropagation()}
        title={`Open ${mediaId.id} on IMDb`}
        style={{ display: "inline-flex", alignItems: "center", marginLeft: 6, opacity: 0.7, transition: "opacity 0.15s" }}
        onMouseEnter={(e) => (e.currentTarget.style.opacity = "1")}
        onMouseLeave={(e) => (e.currentTarget.style.opacity = "0.7")}
      >
        <img src="/imdb-logo.svg" alt="IMDb" height="14" style={{ verticalAlign: "middle" }} />
      </a>
    );
  }

  return (
    <a
      href={`https://thetvdb.com/dereferrer/series/${mediaId.id}`}
      target="_blank"
      rel="noopener noreferrer"
      onClick={(e) => e.stopPropagation()}
      title={`Open on TheTVDB`}
      style={{ display: "inline-flex", alignItems: "center", marginLeft: 6, opacity: 0.7, transition: "opacity 0.15s" }}
      onMouseEnter={(e) => (e.currentTarget.style.opacity = "1")}
      onMouseLeave={(e) => (e.currentTarget.style.opacity = "0.7")}
    >
      <img src="/tvdb-logo.svg" alt="TVDB" height="14" style={{ verticalAlign: "middle" }} />
    </a>
  );
}

// ─── Flattened row types for virtual scroll ───

type FlatRow =
  | { type: "folder"; node: TreeNode; depth: number }
  | { type: "file"; file: ScannedFile; depth: number }
  | { type: "loading"; folderPath: string; depth: number };

function flattenTree(
  root: TreeNode,
  expanded: Set<string>,
  folderFiles: Map<string, ScannedFile[]>,
  loadingFolders: Set<string>,
  sortBy: SortBy,
  sortDir: SortDirection,
): FlatRow[] {
  const rows: FlatRow[] = [];

  function walk(node: TreeNode, depth: number) {
    for (const child of sortNodes(Array.from(node.children.values()), sortBy, sortDir)) {
      rows.push({ type: "folder", node: child, depth });
      if (expanded.has(child.path)) {
        // Recurse into subfolders first
        walk(child, depth + 1);
        // Then show files if this is a leaf folder
        if (child.isLeaf) {
          const files = folderFiles.get(child.path);
          if (files) {
            for (const f of sortFiles(files, sortBy, sortDir)) {
              rows.push({ type: "file", file: f, depth: depth + 1 });
            }
          } else if (loadingFolders.has(child.path)) {
            rows.push({ type: "loading", folderPath: child.path, depth: depth + 1 });
          }
        }
      }
    }
  }

  walk(root, 0);
  return rows;
}

// ─── Row renderers ───

const ROW_HEIGHT = 32;
const FILE_EXPANDED_HEIGHT = 300; // approximate for file detail panel

function FolderRow({
  node, depth, isExpanded, onToggle, allSelected, onSelectAll,
  onIgnoreFolder, onRescanFolder,
}: {
  node: TreeNode; depth: number; isExpanded: boolean;
  onToggle: () => void; allSelected: boolean;
  onSelectAll: (selectAll: boolean, shiftKey?: boolean) => void;
  onIgnoreFolder?: (path: string) => void;
  onRescanFolder?: (path: string) => void;
}) {
  const confirm = useConfirm();
  const hasMediaId = /\[(?:tvdb-\d+|tt\d+)\]/.test(node.name);
  const colorClass = hasMediaId ? "tree-season" : depth === 0 ? "tree-folder" : depth === 1 ? "tree-subfolder" : "tree-season";

  return (
    <div className="tree-row" onClick={onToggle} style={{ paddingLeft: depth * 16 }}>
      <input
        type="checkbox"
        checked={allSelected}
        readOnly
        onClick={(e) => { e.stopPropagation(); onSelectAll(!allSelected, e.shiftKey); }}
        style={{ marginRight: 4 }}
      />
      <span className={colorClass}>
        {isExpanded ? "\u25BC" : "\u25B6"} {node.name}/
      </span>
      <MediaIdLink folderName={node.name} />
      <span className="tree-file-size">
        {node.agg_file_count} files &middot; {(node.agg_total_size / (1024 ** 3)).toFixed(1)} GB
      </span>
      <div style={{ display: "inline-flex", alignItems: "center", gap: 2 }}>
        {onRescanFolder && parseMediaId(node.name) && (
          <button
            onClick={(e) => { e.stopPropagation(); onRescanFolder(node.path); }}
            style={{ background: "none", border: "none", color: "var(--text-muted)", cursor: "pointer", padding: 4, display: "inline-flex", alignItems: "center", borderRadius: 4, opacity: 0.6, transition: "opacity 0.15s" }}
            onMouseEnter={(e) => (e.currentTarget.style.opacity = "1")}
            onMouseLeave={(e) => (e.currentTarget.style.opacity = "0.6")}
            title={`Rescan ${node.name}`}
          >
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/>
            </svg>
          </button>
        )}
        {onIgnoreFolder && (
          <button
            onClick={async (e) => {
              e.stopPropagation();
              if (await confirm({ message: `Ignore all ${node.agg_file_count} files in ${node.name}/?`, confirmLabel: "Ignore all", danger: true })) {
                onIgnoreFolder(node.path + "/");
              }
            }}
            style={{ background: "none", border: "none", color: "var(--text-muted)", cursor: "pointer", padding: 4, display: "inline-flex", alignItems: "center", borderRadius: 4, fontSize: 14 }}
            title={`Ignore all files in ${node.name}/`}
          >
            &#x2298;
          </button>
        )}
      </div>
    </div>
  );
}

function FileRow({
  file, selected, depth, onToggleSelect, onRemoveFile, onIgnoreFile, onUnignoreFile, onDeleteFile,
  onToggleTrack, expanded, onToggleExpand,
}: {
  file: ScannedFile; selected: boolean; depth: number;
  onToggleSelect: (path: string, shiftKey?: boolean) => void;
  onToggleTrack: (filePath: string, streamIndex: number) => void;
  onRemoveFile: (filePath: string) => void;
  onIgnoreFile?: (filePath: string) => void;
  onUnignoreFile?: (filePath: string) => void;
  onDeleteFile?: (filePath: string) => void;
  expanded: boolean;
  onToggleExpand: () => void;
}) {
  const confirm = useConfirm();
  const codecClass = file.needs_conversion ? "x264" : "x265";

  return (
    <div style={{ paddingLeft: depth * 16 }}>
      <div className="tree-row" onClick={onToggleExpand}>
        <input
          type="checkbox"
          checked={selected}
          readOnly
          onClick={(e) => { e.stopPropagation(); onToggleSelect(file.file_path, e.shiftKey); }}
          style={{ marginRight: 4 }}
        />
        <span style={{ cursor: "pointer" }}>{expanded ? "\u25BC" : "\u25B6"} {file.file_name}</span>
        <span className="tree-file-size">{file.file_size_gb} GB</span>
        <span className={`codec-badge ${codecClass}`}>
          {file.needs_conversion ? "x264" : "x265"}
        </span>
        {file.converted && (
          <span style={{ color: "var(--success)", fontSize: 14, display: "inline-flex", alignItems: "center" }} title="Converted by Squeezarr">&#x2713;</span>
        )}
        {file.is_new && (
          <span style={{ fontSize: 9, fontWeight: "bold", color: "white", background: "var(--accent)", padding: "2px 6px", borderRadius: 3, display: "inline-flex", alignItems: "center" }}>NEW</span>
        )}
        {file.queued && (
          <span style={{ fontSize: 9, fontWeight: "bold", color: "var(--text-secondary)", background: "var(--border)", padding: "2px 6px", borderRadius: 3, display: "inline-flex", alignItems: "center" }}>QUEUED</span>
        )}
        {file.ignored && onUnignoreFile && (
          <button
            onClick={(e) => { e.stopPropagation(); onUnignoreFile(file.file_path); }}
            style={{ fontSize: 9, color: "var(--text-muted)", background: "var(--border)", padding: "2px 6px", borderRadius: 3, border: "none", cursor: "pointer", whiteSpace: "nowrap", display: "inline-flex", alignItems: "center" }}
            title="Click to unignore"
          >ignored ✕</button>
        )}
        <div style={{ display: "inline-flex", alignItems: "center", gap: 2 }}>
          {!file.ignored && onIgnoreFile && (
            <button onClick={(e) => { e.stopPropagation(); onIgnoreFile(file.file_path); }}
              style={{ background: "none", border: "none", color: "var(--text-muted)", cursor: "pointer", padding: 4, display: "inline-flex", alignItems: "center", borderRadius: 4, fontSize: 14 }}
              title="Ignore this file">&#x2298;</button>
          )}
          <button onClick={(e) => { e.stopPropagation(); onRemoveFile(file.file_path); }}
            style={{ background: "none", border: "none", color: "var(--text-muted)", cursor: "pointer", padding: 4, display: "inline-flex", alignItems: "center", borderRadius: 4, fontSize: 16 }}
            title="Remove from list">&times;</button>
          {onDeleteFile && (
            <button
              onClick={async (e) => {
                e.stopPropagation();
                if (await confirm({ message: `Move this file to trash?\n\n${file.file_name}`, confirmLabel: "Move to trash", danger: true })) {
                  onDeleteFile(file.file_path);
                }
              }}
              style={{ background: "none", border: "none", color: "#e94560", cursor: "pointer", padding: 4, display: "inline-flex", alignItems: "center", borderRadius: 4, opacity: 0.6 }}
              onMouseEnter={(e) => (e.currentTarget.style.opacity = "1")}
              onMouseLeave={(e) => (e.currentTarget.style.opacity = "0.6")}
              title="Move to trash"
            >
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 01-2 2H7a2 2 0 01-2-2V6m3 0V4a2 2 0 012-2h4a2 2 0 012 2v2"/>
              </svg>
            </button>
          )}
        </div>
      </div>
      {expanded && (
        <FileDetail file={file} onToggleTrack={onToggleTrack} />
      )}
    </div>
  );
}

// ─── Main component ───

export default function FileTree({
  folders, filter = "all",
  isSelected, onToggleSelect, onToggleTrack, onRemoveFile,
  onIgnoreFile, onUnignoreFile, onRescanFolder, onDeleteFile,
  onFolderFilesLoaded,
  sortBy = "name", sortDir = "asc", search = "",
}: FileTreeProps) {
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [folderFiles, setFolderFiles] = useState<Map<string, ScannedFile[]>>(new Map());
  const [loadingFolders, setLoadingFolders] = useState<Set<string>>(new Set());
  const [expandedFiles, setExpandedFiles] = useState<Set<string>>(new Set());
  const containerRef = useRef<HTMLDivElement>(null);
  const [scrollTop, setScrollTop] = useState(0);

  // When filter or search is active, show a flat title-level tree (no deep hierarchy)
  const isFiltered = (filter !== undefined && filter !== "all" && filter !== "") || search.trim() !== "";

  // Build tree from server-provided folder data
  const tree = isFiltered ? buildFlatTitleTree(folders) : buildTreeFromFolders(folders);

  // Auto-expand single-child paths on first load (e.g., /media → M2T2 → TV4)
  const prevFolderCount = useRef(0);
  useEffect(() => {
    if (folders.length > 0 && prevFolderCount.current === 0) {
      const autoExpand = new Set<string>();
      let node = tree;
      while (node.children.size === 1 && !node.isLeaf) {
        const child = Array.from(node.children.values())[0];
        autoExpand.add(child.path);
        node = child;
      }
      if (autoExpand.size > 0) {
        setExpanded(prev => {
          if (prev.size > 0) return prev; // Don't override user's expansion state
          return autoExpand;
        });
      }
    }
    prevFolderCount.current = folders.length;
  }, [folders, tree]);

  // Flatten for virtual scrolling
  const flatRows = flattenTree(tree, expanded, folderFiles, loadingFolders, sortBy, sortDir);

  // Virtual scroll calculations
  const containerHeight = typeof window !== "undefined" ? window.innerHeight : 800;
  const overscan = 8;
  const totalHeight = flatRows.reduce((h, row) => {
    if (row.type === "file" && expandedFiles.has(row.file.file_path)) {
      return h + ROW_HEIGHT + FILE_EXPANDED_HEIGHT;
    }
    return h + ROW_HEIGHT;
  }, 0);

  // For variable height rows, compute positions
  const rowPositions: number[] = [];
  let pos = 0;
  for (const row of flatRows) {
    rowPositions.push(pos);
    if (row.type === "file" && expandedFiles.has(row.file.file_path)) {
      pos += ROW_HEIGHT + FILE_EXPANDED_HEIGHT;
    } else {
      pos += ROW_HEIGHT;
    }
  }

  // Find visible range via binary search on positions
  const startIdx = Math.max(0, rowPositions.findIndex(p => p + ROW_HEIGHT > scrollTop) - overscan);
  let endBase = rowPositions.findIndex(p => p > scrollTop + containerHeight);
  if (endBase === -1) endBase = flatRows.length;
  const endIdx = Math.min(flatRows.length, endBase + overscan);


  // Load files when a leaf folder is expanded
  const loadFolderFiles = useCallback(async (folderPath: string) => {
    setLoadingFolders(prev => new Set(prev).add(folderPath));
    try {
      const data = await getScanFiles(folderPath, filter);
      const parsed: ScannedFile[] = (Array.isArray(data) ? data : []).map((row: any) => ({
        ...row,
        file_name: row.file_path.split("/").pop(),
        folder_name: row.file_path.split("/").slice(-2, -1)[0],
        file_size_gb: +(row.file_size / (1024 ** 3)).toFixed(2),
        audio_tracks: row.audio_tracks || [],
        subtitle_tracks: row.subtitle_tracks || [],
        has_removable_tracks: row.has_removable_tracks || false,
        has_removable_subs: row.has_removable_subs || false,
        estimated_savings_bytes: 0,
        estimated_savings_gb: 0,
        language_source: row.language_source || "heuristic",
        ignored: row.ignored || false,
        is_new: row.is_new || false,
        queued: row.queued || false,
        converted: row.converted || false,
        low_bitrate: row.low_bitrate || false,
        has_lossless_audio: row.has_lossless_audio || false,
        duration: row.duration || 0,
        file_mtime: row.file_mtime || null,
      }));
      setFolderFiles(prev => {
        const next = new Map(prev);
        next.set(folderPath, parsed);
        return next;
      });
      onFolderFilesLoaded?.(folderPath, parsed);
    } catch (err) {
      console.error("Failed to load folder files:", err);
    } finally {
      setLoadingFolders(prev => {
        const next = new Set(prev);
        next.delete(folderPath);
        return next;
      });
    }
  }, [filter, onFolderFilesLoaded]);

  // Find all leaf folder paths under a tree node
  const getLeafPaths = useCallback((node: TreeNode): string[] => {
    const result: string[] = [];
    if (node.isLeaf) result.push(node.path);
    for (const child of node.children.values()) {
      result.push(...getLeafPaths(child));
    }
    return result;
  }, []);

  const toggleFolder = useCallback((path: string, isLeaf: boolean) => {
    setExpanded(prev => {
      const next = new Set(prev);
      if (next.has(path)) {
        next.delete(path);
      } else {
        next.add(path);
        // Load files only for leaf folders (folders that directly contain files)
        if (isLeaf && !folderFiles.has(path)) {
          loadFolderFiles(path);
        }
      }
      return next;
    });
  }, [folderFiles, loadFolderFiles]);

  // When filter changes, clear cached folder files so they reload with new filter
  useEffect(() => {
    setFolderFiles(new Map());
    // Reload files for currently expanded leaf folders
    for (const path of expanded) {
      // Check if this is a leaf by looking at the tree
      const isLeaf = folders.some(f => f.path === path);
      if (isLeaf) {
        loadFolderFiles(path);
      }
    }
  }, [filter]);

  // Helper: get all loaded file paths in a folder (recursive)
  const getLoadedFilesInFolder = useCallback((node: TreeNode): string[] => {
    const paths: string[] = [];
    if (node.isLeaf) {
      const files = folderFiles.get(node.path);
      if (files) {
        for (const f of files) paths.push(f.file_path);
      }
    }
    for (const child of node.children.values()) {
      paths.push(...getLoadedFilesInFolder(child));
    }
    return paths;
  }, [folderFiles]);

  // Handle folder select-all checkbox — just toggles the folder path
  // No file loading needed; the server resolves folder paths to files when actions are taken
  const handleFolderSelectAll = useCallback((node: TreeNode, _selectAll: boolean, shiftKey?: boolean) => {
    onToggleSelect(node.path + "/", shiftKey);
  }, [onToggleSelect]);

  // Check if a folder is selected (by its folder path)
  const isFolderAllSelected = useCallback((node: TreeNode): boolean => {
    return isSelected(node.path + "/");
  }, [isSelected]);

  const visibleRows = flatRows.slice(startIdx, endIdx);

  // Track scroll position from the scrolling ancestor (.main-content)
  useEffect(() => {
    const scrollParent = containerRef.current?.closest(".main-content") as HTMLElement | null;
    if (!scrollParent) return;

    const handler = () => {
      if (containerRef.current) {
        // Container's top relative to the scroll parent's viewport
        const containerTop = containerRef.current.offsetTop;
        const scrolled = scrollParent.scrollTop;
        setScrollTop(Math.max(0, scrolled - containerTop));
      }
    };
    scrollParent.addEventListener("scroll", handler, { passive: true });
    handler();
    return () => scrollParent.removeEventListener("scroll", handler);
  }, [flatRows.length]); // Re-attach when rows change (tree updates)

  return (
    <div
      ref={containerRef}
      className="tree-container"
      style={{
        position: "relative",
        minHeight: totalHeight || 100,
      }}
    >
      <div style={{ height: totalHeight, position: "relative" }}>
        <div
          style={{
            position: "absolute",
            top: rowPositions[startIdx] || 0,
            left: 0,
            right: 0,
          }}
        >
          {visibleRows.map((row) => {
            if (row.type === "folder") {
              const node = row.node;
              return (
                <div key={`f-${node.path}`} style={{ height: ROW_HEIGHT }}>
                  <FolderRow
                    node={node}
                    depth={row.depth}
                    isExpanded={expanded.has(node.path)}
                    onToggle={() => toggleFolder(node.path, node.isLeaf)}
                    allSelected={isFolderAllSelected(node)}
                    onSelectAll={(sel, shift) => handleFolderSelectAll(node, sel, shift)}
                    onIgnoreFolder={onIgnoreFile ? (path) => onIgnoreFile!(path) : undefined}
                    onRescanFolder={onRescanFolder}
                  />
                </div>
              );
            }
            if (row.type === "file") {
              const file = row.file;
              const isFileExpanded = expandedFiles.has(file.file_path);
              return (
                <div key={`e-${file.file_path}`} style={{ minHeight: ROW_HEIGHT }}>
                  <FileRow
                    file={file}
                    selected={isSelected(file.file_path)}
                    depth={row.depth}
                    onToggleSelect={onToggleSelect}
                    onToggleTrack={onToggleTrack}
                    onRemoveFile={onRemoveFile}
                    onIgnoreFile={onIgnoreFile}
                    onUnignoreFile={onUnignoreFile}
                    onDeleteFile={onDeleteFile}
                    expanded={isFileExpanded}
                    onToggleExpand={() => {
                      setExpandedFiles(prev => {
                        const next = new Set(prev);
                        if (next.has(file.file_path)) next.delete(file.file_path);
                        else next.add(file.file_path);
                        return next;
                      });
                    }}
                  />
                </div>
              );
            }
            // Loading spinner
            return (
              <div key={`l-${row.folderPath}`} style={{ height: ROW_HEIGHT, paddingLeft: row.depth * 16, display: "flex", alignItems: "center", gap: 8 }}>
                <div className="spinner" style={{ width: 14, height: 14 }} />
                <span style={{ fontSize: 12, opacity: 0.5 }}>Loading files...</span>
              </div>
            );
          })}
        </div>
      </div>
      {folders.length === 0 && (
        <div style={{ textAlign: "center", padding: 40, opacity: 0.5 }}>
          No scan results. Select directories and click Scan to start.
        </div>
      )}
    </div>
  );
}
