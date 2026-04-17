import { useState, useEffect, useRef, useCallback, useMemo, memo } from "react";
import type { ScannedFile } from "../types";
import { getScanFiles, getScanFilesByPaths } from "../api";
import { getCodecLabel } from "../codecLabels";
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
  onToggleSubTrack?: (filePath: string, streamIndex: number) => void;
  onRemoveFile: (filePath: string) => void;
  onIgnoreFile?: (filePath: string) => void;
  onUnignoreFile?: (filePath: string) => void;
  onRescanFolder?: (folderPath: string) => void;
  onDeleteFile?: (filePath: string) => void;
  onFolderFilesLoaded?: (folderPath: string, files: ScannedFile[]) => void;
  externalFiles?: Map<string, ScannedFile[]>;
  mediaDirs?: string[];
  sortBy?: SortBy;
  sortDir?: SortDirection;
  allowedPaths?: Set<string>;
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
      // Show up to 2 levels of parent context (e.g. "KrakkaTV IS > Kennarastofan (2024) > Season 1")
      const contextParts = parts.slice(-3);  // last 3 segments
      groupName = contextParts.join("  >  ");
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
  allowedPaths?: Set<string>,
): FlatRow[] {
  const rows: FlatRow[] = [];

  function walk(node: TreeNode, depth: number) {
    for (const child of sortNodes(Array.from(node.children.values()), sortBy, sortDir)) {
      rows.push({ type: "folder", node: child, depth });
      if (expanded.has(child.path)) {
        // Recurse into subfolders first
        walk(child, depth + 1);
        // Then show files for this folder (if any were loaded)
        let files = folderFiles.get(child.path);
        if (files && allowedPaths) {
          files = files.filter(f => allowedPaths.has(f.file_path));
        }
        if (files && files.length > 0) {
          for (const f of sortFiles(files, sortBy, sortDir)) {
            rows.push({ type: "file", file: f, depth: depth + 1 });
          }
        } else if (loadingFolders.has(child.path) && !allowedPaths) {
          rows.push({ type: "loading", folderPath: child.path, depth: depth + 1 });
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

const FolderRow = memo(function FolderRow({
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
  const isFlat = node.name.includes("  >  ");
  const colorClass = hasMediaId ? "tree-season" : isFlat ? "tree-season" : depth === 0 ? "tree-folder" : depth === 1 ? "tree-subfolder" : "tree-season";

  return (
    <div className="tree-row" onClick={onToggle} style={{ paddingLeft: depth * 16 }}>
      <input
        type="checkbox"
        checked={allSelected}
        readOnly
        onClick={(e) => { e.stopPropagation(); onSelectAll(!allSelected, e.shiftKey); }}
        style={{ marginRight: 4 }}
      />
      <span style={{ fontSize: 10, color: "var(--text-muted)", width: 12, textAlign: "center", flexShrink: 0 }}>
        {isExpanded ? "\u25BC" : "\u25B6"}
      </span>
      <span className={`tree-name ${colorClass}`}>
        {node.name}/
      </span>
      <MediaIdLink folderName={node.name} />
      <span className="tree-file-size">
        {node.agg_file_count} files &middot; {node.agg_total_size >= 1024 ** 4 ? `${(node.agg_total_size / (1024 ** 4)).toFixed(1)} TB` : `${(node.agg_total_size / (1024 ** 3)).toFixed(1)} GB`}
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
}, (prev, next) => {
  // Skip re-render if only function props changed — only re-render when visual state differs
  return prev.node === next.node
    && prev.depth === next.depth
    && prev.isExpanded === next.isExpanded
    && prev.allSelected === next.allSelected;
});

const FileRow = memo(function FileRow({
  file, selected, depth, onToggleSelect, onRemoveFile, onIgnoreFile, onUnignoreFile, onDeleteFile,
  onToggleTrack, onToggleSubTrack, expanded, onToggleExpand,
}: {
  file: ScannedFile; selected: boolean; depth: number;
  onToggleSelect: (path: string, shiftKey?: boolean) => void;
  onToggleTrack: (filePath: string, streamIndex: number) => void;
  onToggleSubTrack?: (filePath: string, streamIndex: number) => void;
  onRemoveFile: (filePath: string) => void;
  onIgnoreFile?: (filePath: string) => void;
  onUnignoreFile?: (filePath: string) => void;
  onDeleteFile?: (filePath: string) => void;
  expanded: boolean;
  onToggleExpand: () => void;
}) {
  const confirm = useConfirm();
  const codecLabel = getCodecLabel(file.video_codec, file.needs_conversion);
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
        <span style={{ fontSize: 10, color: "var(--text-muted)", width: 12, textAlign: "center", flexShrink: 0 }}>
          {expanded ? "\u25BC" : "\u25B6"}
        </span>
        <span className="tree-name" style={{ cursor: "pointer" }}>{file.file_name}</span>
        <span className="tree-file-size">{file.file_size_gb} GB</span>
        <span className={`codec-badge ${codecClass}`}>
          {codecLabel}
        </span>
        {(file.health_status === "corrupt" || file.probe_status === "corrupt") && (
          <span
            title={`Corrupt${file.health_check_type ? ` (${file.health_check_type} check)` : ""}`}
            style={{ display: "inline-flex", alignItems: "center", color: "var(--danger)" }}
          >
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/>
              <line x1="12" y1="9" x2="12" y2="13"/>
              <line x1="12" y1="17" x2="12.01" y2="17"/>
            </svg>
          </span>
        )}
        {file.health_status === "healthy" && (
          <span
            title={`Healthy (${file.health_check_type || "checked"})`}
            style={{ display: "inline-flex", alignItems: "center", color: "var(--success)", opacity: 0.7 }}
          >
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
              <polyline points="20 6 9 17 4 12"/>
            </svg>
          </span>
        )}
        {file.converted && (
          <span style={{ color: "var(--success)", fontSize: 14, display: "inline-flex", alignItems: "center" }} title="Converted by Shrinkerr">&#x2713;</span>
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
        <FileDetail file={file} onToggleTrack={onToggleTrack} onToggleSubTrack={onToggleSubTrack} />
      )}
    </div>
  );
}, (prev, next) => {
  // Re-render only when file data, selection state, depth, or expanded state changes
  return prev.file === next.file
    && prev.selected === next.selected
    && prev.depth === next.depth
    && prev.expanded === next.expanded;
});

// ─── Main component ───

export default function FileTree({
  folders, filter = "all",
  isSelected, onToggleSelect, onToggleTrack, onToggleSubTrack, onRemoveFile,
  onIgnoreFile, onUnignoreFile, onRescanFolder, onDeleteFile,
  onFolderFilesLoaded, externalFiles, mediaDirs,
  sortBy = "name", sortDir = "asc", search = "", allowedPaths,
}: FileTreeProps) {
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [internalFiles, setInternalFiles] = useState<Map<string, ScannedFile[]>>(new Map());
  // Use external files (from parent, updated on track toggle) when available, fallback to internal
  const folderFiles = externalFiles || internalFiles;
  const [loadingFolders, setLoadingFolders] = useState<Set<string>>(new Set());
  const [expandedFiles, setExpandedFiles] = useState<Set<string>>(new Set());
  const containerRef = useRef<HTMLDivElement>(null);
  const [scrollTop, setScrollTop] = useState(0);

  // When filter, search, or advanced-search is active, show a flat title-level tree
  const isFiltered = (filter !== undefined && filter !== "all" && filter !== "") || search.trim() !== "" || (allowedPaths != null && allowedPaths.size > 0);

  // Build tree from server-provided folder data — memoized so scroll doesn't rebuild it
  const tree = useMemo(
    () => isFiltered ? buildFlatTitleTree(folders) : buildTreeFromFolders(folders),
    [folders, isFiltered],
  );

  // (advanced-search auto-expand effect lives below loadFolderFiles)
  const prevAllowedSize = useRef(0);

  // Auto-expand single-child paths on first load (e.g., /media → M2T2 → TV4)
  const prevFolderCount = useRef(0);
  useEffect(() => {
    if (folders.length > 0 && prevFolderCount.current === 0) {
      // Auto-expand tree to reveal all configured media directories
      const autoExpand = new Set<string>();
      if (mediaDirs && mediaDirs.length > 0) {
        // For each media dir, expand all ancestor nodes in the tree
        for (const dir of mediaDirs) {
          const parts = dir.replace(/^\//, "").replace(/\/$/, "").split("/");
          let path = "";
          // Expand ancestors only — stop before the media dir itself
          for (let i = 0; i < parts.length - 1; i++) {
            path = path ? `${path}/${parts[i]}` : `/${parts[i]}`;
            autoExpand.add(path);
          }
        }
      } else {
        // Fallback: expand single-child paths
        let node = tree;
        while (node.children.size === 1 && !node.isLeaf) {
          const child = Array.from(node.children.values())[0];
          autoExpand.add(child.path);
          node = child;
        }
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

  // Flatten for virtual scrolling — memoized so scroll events don't re-flatten
  const flatRows = useMemo(
    () => flattenTree(tree, expanded, folderFiles, loadingFolders, sortBy, sortDir, allowedPaths),
    [tree, expanded, folderFiles, loadingFolders, sortBy, sortDir, allowedPaths],
  );

  const overscan = 8;

  // Row positions + total height — only recompute when rows or expanded-files set changes.
  const { rowPositions, totalHeight } = useMemo(() => {
    const positions: number[] = new Array(flatRows.length);
    let pos = 0;
    for (let i = 0; i < flatRows.length; i++) {
      positions[i] = pos;
      const row = flatRows[i];
      if (row.type === "file" && expandedFiles.has(row.file.file_path)) {
        pos += ROW_HEIGHT + FILE_EXPANDED_HEIGHT;
      } else {
        pos += ROW_HEIGHT;
      }
    }
    return { rowPositions: positions, totalHeight: pos };
  }, [flatRows, expandedFiles]);

  // Read container height from state so resizes update the visible range
  const [containerHeight, setContainerHeight] = useState(() => typeof window !== "undefined" ? window.innerHeight : 800);
  useEffect(() => {
    const onResize = () => setContainerHeight(window.innerHeight);
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);

  // Binary-search the visible row range.
  // Use the NEXT row's position (rowPositions[i+1] or totalHeight) as the end of row i.
  // This correctly accounts for expanded-file rows that are taller than ROW_HEIGHT.
  const { startIdx, endIdx } = useMemo(() => {
    const n = rowPositions.length;
    if (n === 0) return { startIdx: 0, endIdx: 0 };

    const rowEnd = (i: number) => (i + 1 < n ? rowPositions[i + 1] : totalHeight);

    // firstVisible: first row where its end > scrollTop
    let lo = 0, hi = n;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (rowEnd(mid) > scrollTop) hi = mid;
      else lo = mid + 1;
    }
    const firstVisible = lo;

    // lastVisible: first row whose top > viewportEnd
    lo = firstVisible; hi = n;
    const viewportEnd = scrollTop + containerHeight;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (rowPositions[mid] > viewportEnd) hi = mid;
      else lo = mid + 1;
    }
    const lastVisible = lo;

    return {
      startIdx: Math.max(0, firstVisible - overscan),
      endIdx: Math.min(n, lastVisible + overscan),
    };
  }, [rowPositions, totalHeight, scrollTop, containerHeight]);


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
        estimated_savings_bytes: (() => {
          let s = row.needs_conversion ? (row.file_size || 0) * 0.3 : 0;
          for (const t of (row.audio_tracks || [])) {
            if (!t.keep && !t.locked && t.size_estimate_bytes) s += t.size_estimate_bytes;
          }
          return Math.round(s);
        })(),
        estimated_savings_gb: +(((row.needs_conversion ? (row.file_size || 0) * 0.3 : 0) +
          (row.audio_tracks || []).filter((t: any) => !t.keep && !t.locked && t.size_estimate_bytes).reduce((s: number, t: any) => s + t.size_estimate_bytes, 0)
        ) / (1024**3)).toFixed(1),
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
      setInternalFiles(prev => {
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

  // When advanced search is active, auto-expand parent folders of matched files
  // AND fetch the matching files in ONE batch call (not N per-folder calls).
  useEffect(() => {
    if (!allowedPaths || allowedPaths.size === 0) {
      prevAllowedSize.current = 0;
      return;
    }
    if (allowedPaths.size === prevAllowedSize.current) return;
    prevAllowedSize.current = allowedPaths.size;

    const toExpand = new Set<string>();
    const leafFolders = new Set<string>();
    for (const fp of allowedPaths) {
      const parts = fp.split("/");
      for (let i = 1; i < parts.length - 1; i++) {
        toExpand.add(parts.slice(0, i + 1).join("/"));
      }
      const parent = parts.slice(0, parts.length - 1).join("/");
      if (parent) leafFolders.add(parent);
    }
    setExpanded(toExpand);

    // Skip leaf folders we already have files for
    const foldersToFetch = new Set<string>();
    for (const folder of leafFolders) {
      if (!folderFiles.has(folder)) foldersToFetch.add(folder);
    }
    if (foldersToFetch.size === 0) return;

    // Batch: fetch just the matched files in one request
    // (much faster than N /files calls per folder)
    (async () => {
      // Mark all folders as loading
      setLoadingFolders(prev => {
        const next = new Set(prev);
        for (const f of foldersToFetch) next.add(f);
        return next;
      });
      try {
        const data = await getScanFilesByPaths(Array.from(allowedPaths), filter);
        // Group returned files by parent folder
        const byFolder = new Map<string, ScannedFile[]>();
        for (const row of data) {
          const parts = row.file_path.split("/");
          const folder = parts.slice(0, -1).join("/");
          const parsed: ScannedFile = {
            ...row,
            file_name: parts[parts.length - 1],
            folder_name: parts[parts.length - 2],
            file_size_gb: +(row.file_size / (1024 ** 3)).toFixed(2),
            audio_tracks: row.audio_tracks || [],
            subtitle_tracks: row.subtitle_tracks || [],
            has_removable_tracks: row.has_removable_tracks || false,
            has_removable_subs: row.has_removable_subs || false,
            estimated_savings_bytes: (() => {
              let s = row.needs_conversion ? (row.file_size || 0) * 0.3 : 0;
              for (const t of (row.audio_tracks || [])) {
                if (!t.keep && !t.locked && t.size_estimate_bytes) s += t.size_estimate_bytes;
              }
              return Math.round(s);
            })(),
            estimated_savings_gb: +(((row.needs_conversion ? (row.file_size || 0) * 0.3 : 0) +
              (row.audio_tracks || []).filter((t: any) => !t.keep && !t.locked && t.size_estimate_bytes).reduce((s: number, t: any) => s + t.size_estimate_bytes, 0)
            ) / (1024 ** 3)).toFixed(1),
            language_source: row.language_source || "heuristic",
            ignored: row.ignored || false,
            is_new: row.is_new || false,
            queued: row.queued || false,
            converted: row.converted || false,
            low_bitrate: row.low_bitrate || false,
            has_lossless_audio: row.has_lossless_audio || false,
            duration: row.duration || 0,
            file_mtime: row.file_mtime || null,
          };
          if (!byFolder.has(folder)) byFolder.set(folder, []);
          byFolder.get(folder)!.push(parsed);
        }
        // Merge into state in a single update
        setInternalFiles(prev => {
          const next = new Map(prev);
          for (const [folder, files] of byFolder) {
            next.set(folder, files);
            onFolderFilesLoaded?.(folder, files);
          }
          // Ensure empty folders also get an entry so they show "no matching files" instead of a spinner
          for (const folder of foldersToFetch) {
            if (!byFolder.has(folder)) next.set(folder, []);
          }
          return next;
        });
      } catch (err) {
        console.error("Batch file load failed:", err);
      } finally {
        setLoadingFolders(prev => {
          const next = new Set(prev);
          for (const f of foldersToFetch) next.delete(f);
          return next;
        });
      }
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [allowedPaths]);

  // Find all leaf folder paths under a tree node
  const getLeafPaths = useCallback((node: TreeNode): string[] => {
    const result: string[] = [];
    if (node.isLeaf) result.push(node.path);
    for (const child of node.children.values()) {
      result.push(...getLeafPaths(child));
    }
    return result;
  }, []);

  const toggleFolder = useCallback((path: string, _isLeaf: boolean) => {
    setExpanded(prev => {
      const next = new Set(prev);
      if (next.has(path)) {
        next.delete(path);
      } else {
        next.add(path);
        // Load files for leaf folders or any folder that might contain files
        if (!folderFiles.has(path)) {
          loadFolderFiles(path);
        }
      }
      return next;
    });
  }, [folderFiles, loadFolderFiles]);

  // When filter changes, clear cached folder files so they reload with new filter
  useEffect(() => {
    setInternalFiles(new Map());
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

  const visibleRows = useMemo(() => flatRows.slice(startIdx, endIdx), [flatRows, startIdx, endIdx]);

  // Track scroll position from the scrolling ancestor (.main-content)
  // Throttle via requestAnimationFrame so a fast scroll triggers at most one setState per frame.
  useEffect(() => {
    const scrollParent = containerRef.current?.closest(".main-content") as HTMLElement | null;
    if (!scrollParent) return;

    let rafId = 0;
    let pending = false;
    const handler = () => {
      if (pending) return;
      pending = true;
      rafId = requestAnimationFrame(() => {
        pending = false;
        if (containerRef.current) {
          const containerTop = containerRef.current.offsetTop;
          const scrolled = scrollParent.scrollTop;
          setScrollTop(Math.max(0, scrolled - containerTop));
        }
      });
    };
    scrollParent.addEventListener("scroll", handler, { passive: true });
    handler();
    return () => {
      cancelAnimationFrame(rafId);
      scrollParent.removeEventListener("scroll", handler);
    };
  }, [flatRows.length]);

  return (
    <div
      ref={containerRef}
      className="tree-container file-tree-virtual"
      style={{
        position: "relative",
        minHeight: totalHeight || 100,
      }}
    >
      {/* Flow-based virtualization: a top spacer puts the visible window at the right
          scroll position, then rows render naturally (so their real heights — which
          vary widely for expanded file detail panels — drive layout), then a bottom
          spacer keeps the total scrollable area consistent. This avoids the visual
          drift that happens when rowPositions under/over-estimate expanded heights. */}
      <div style={{ height: rowPositions[startIdx] || 0 }} />
      <div
        style={{
          position: "relative",
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
                    onToggleSubTrack={onToggleSubTrack}
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
      {/* Bottom spacer to preserve total scroll height */}
      <div style={{ height: Math.max(0, totalHeight - (rowPositions[endIdx] || totalHeight)) }} />
      {folders.length === 0 && (
        <div style={{ textAlign: "center", padding: 40, opacity: 0.5 }}>
          {search || allowedPaths || (filter && filter !== "all") ? (
            // Empty because of a filter/search — not loading
            <div style={{ fontSize: 13 }}>No files match the current filter.</div>
          ) : (
            <>
              <div className="spinner" style={{ width: 20, height: 20, margin: "0 auto 12px" }} />
              Loading files...
            </>
          )}
        </div>
      )}
    </div>
  );
}
