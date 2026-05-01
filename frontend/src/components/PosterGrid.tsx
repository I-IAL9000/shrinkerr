import { useState, useEffect, useCallback, useRef, useMemo } from "react";
import type { FolderInfo } from "./FileTree";
import type { ScannedFile } from "../types";
import { resolvePosterMetadata, getFilesByTitle } from "../api";
import { getCodecLabel } from "../codecLabels";
import PosterCard from "./PosterCard";
import FileDetail from "./FileDetail";
import PosterFixModal from "./PosterFixModal";

interface PosterMeta {
  title: string;
  year: string | null;
  poster_url: string | null;
  source: string;
  rating?: number | null;
  votes?: number | null;
  genres?: string | null;
  country?: string | null;
  media_type?: string | null;
  rating_source?: string | null;
}

interface TitleGroup {
  key: string;
  title: string;
  year: string | null;
  folders: FolderInfo[];
  fileCount: number;
  totalSize: number;
  // True when the group represents a single stray file (key is the file
  // path, not a directory prefix). Selection must use the exact path.
  isFile?: boolean;
}

interface PosterGridProps {
  folders: FolderInfo[];
  filter?: string;
  // `search` and `allowedPaths` are passed in only so the empty-state
  // can distinguish "no files match the search/filter" from "database
  // is empty, run a scan first". The actual filtering happens upstream
  // in ScannerPage — PosterGrid receives the already-filtered folder
  // list. v0.3.96+.
  search?: string;
  allowedPaths?: Set<string>;
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
  mediaDirs?: string[];
  sortBy?: "name" | "size" | "files" | "date";
  sortDir?: "asc" | "desc";
}

function groupFolders(folders: FolderInfo[], mediaRoots: Set<string>): TitleGroup[] {
  const groups = new Map<string, TitleGroup>();
  for (const folder of folders) {
    // Skip the aggregated media-root entry — stray files under it are already
    // represented as individual is_file pseudo-folders (one card per file).
    if (mediaRoots.has(folder.path.replace(/\/$/, ""))) continue;
    const parts = folder.path.split("/").filter(Boolean);
    let titleIdx = -1;
    for (let i = 0; i < parts.length; i++) {
      if (/\[(?:tvdb-\d+|tt\d+)\]/.test(parts[i])) { titleIdx = i; break; }
    }
    let groupPath: string, name: string;
    if (titleIdx >= 0) {
      groupPath = "/" + parts.slice(0, titleIdx + 1).join("/");
      name = parts[titleIdx];
    } else {
      groupPath = folder.path;
      name = parts[parts.length - 1] || folder.path;
    }
    if (!groups.has(groupPath)) {
      const yearMatch = name.match(/\((\d{4})\)/);
      let title = name.replace(/\s*\[(?:tt\d+|tvdb-\d+)\]/, "").replace(/\s*\(\d{4}\)/, "").trim();
      groups.set(groupPath, { key: groupPath, title: title || name, year: yearMatch ? yearMatch[1] : null, folders: [], fileCount: 0, totalSize: 0, isFile: folder.is_file });
    }
    const g = groups.get(groupPath)!;
    g.folders.push(folder);
    g.fileCount += folder.file_count;
    g.totalSize += folder.total_size;
  }
  return Array.from(groups.values());
}

// Selection path for a group — folders use a trailing "/" (prefix match),
// stray-file groups use the exact file path.
function groupSelectPath(g: TitleGroup): string {
  return g.isFile ? g.key : g.key + "/";
}

function sortGroups(groups: TitleGroup[], sortBy: string, sortDir: string): TitleGroup[] {
  const sorted = [...groups].sort((a, b) => {
    if (sortBy === "size") return a.totalSize - b.totalSize;
    if (sortBy === "files") return a.fileCount - b.fileCount;
    if (sortBy === "date") return Math.max(0, ...a.folders.map(f => f.newest_mtime)) - Math.max(0, ...b.folders.map(f => f.newest_mtime));
    return a.title.localeCompare(b.title);
  });
  return sortDir === "desc" ? sorted.reverse() : sorted;
}

function parseFile(row: any): ScannedFile {
  return {
    ...row, file_name: row.file_path.split("/").pop(),
    folder_name: row.file_path.split("/").slice(-2, -1)[0],
    file_size_gb: +(row.file_size / (1024 ** 3)).toFixed(2),
    audio_tracks: row.audio_tracks || [], subtitle_tracks: row.subtitle_tracks || [],
    has_removable_tracks: row.has_removable_tracks || false, has_removable_subs: row.has_removable_subs || false,
    estimated_savings_bytes: 0, estimated_savings_gb: 0,
    language_source: row.language_source || "heuristic",
    ignored: row.ignored || false, is_new: row.is_new || false,
    queued: row.queued || false, converted: row.converted || false,
    low_bitrate: row.low_bitrate || false, has_lossless_audio: row.has_lossless_audio || false,
    duration: row.duration || 0, file_mtime: row.file_mtime || null,
  };
}

// Card dimensions
const CARD_MIN_W = 160;
const CARD_GAP = 16;
const CARD_H = 330; // poster (240) + info (90)
const OVERSCAN = 3; // extra rows above/below viewport

export default function PosterGrid({
  folders, filter = "all", search, allowedPaths,
  isSelected, onToggleSelect, onToggleTrack, onToggleSubTrack, onRemoveFile,
  onIgnoreFile, onUnignoreFile, onDeleteFile,
  onFolderFilesLoaded,
  mediaDirs,
  sortBy = "name", sortDir = "asc",
}: PosterGridProps) {
  const mediaRoots = useMemo(
    () => new Set((mediaDirs || []).map(d => d.replace(/\/$/, ""))),
    [mediaDirs],
  );
  const [posterMeta, setPosterMeta] = useState<Map<string, PosterMeta>>(new Map());
  const [expandedKey, setExpandedKey] = useState<string | null>(null);
  const [fixModalGroup, setFixModalGroup] = useState<{ key: string; title: string; year: string | null } | null>(null);
  const [expandedFiles, setExpandedFiles] = useState<ScannedFile[]>([]);
  const [loadingFiles, setLoadingFiles] = useState(false);
  const [expandedFileDetails, setExpandedFileDetails] = useState<Set<string>>(new Set());
  const [accordionHeight, setAccordionHeight] = useState(0);
  const accordionRef = useRef<HTMLDivElement>(null);
  const [scrollTop, setScrollTop] = useState(0);
  const [containerWidth, setContainerWidth] = useState(1200);
  const containerRef = useRef<HTMLDivElement>(null);
  const pendingPaths = useRef<Set<string>>(new Set());
  const resolving = useRef(false);
  const resolveQueue = useRef<string[]>([]);
  const lastClickedGroup = useRef<string | null>(null);

  const groups = useMemo(() => sortGroups(groupFolders(folders, mediaRoots), sortBy, sortDir), [folders, mediaRoots, sortBy, sortDir]);

  // Wrap the track toggle handlers so we also update PosterGrid's local
  // `expandedFiles` copy. Without this, the parent's loadedFiles gets updated
  // but the local list stays stale, so the checkbox visually doesn't change.
  const toggleAudioTrack = useCallback((filePath: string, streamIndex: number) => {
    setExpandedFiles(prev => prev.map(f => {
      if (f.file_path !== filePath) return f;
      return {
        ...f,
        audio_tracks: f.audio_tracks.map(t =>
          t.stream_index === streamIndex ? { ...t, keep: !t.keep } : t
        ),
      };
    }));
    onToggleTrack(filePath, streamIndex);
  }, [onToggleTrack]);

  const toggleSubTrack = useCallback((filePath: string, streamIndex: number) => {
    setExpandedFiles(prev => prev.map(f => {
      if (f.file_path !== filePath) return f;
      return {
        ...f,
        subtitle_tracks: (f.subtitle_tracks || []).map(t =>
          t.stream_index === streamIndex ? { ...t, keep: !t.keep } : t
        ),
      };
    }));
    onToggleSubTrack?.(filePath, streamIndex);
  }, [onToggleSubTrack]);

  // Handle poster card selection with shift-select using group visual order
  const handlePosterSelect = useCallback((groupKey: string, shiftKey?: boolean) => {
    if (shiftKey && lastClickedGroup.current) {
      const lastIdx = groups.findIndex(g => g.key === lastClickedGroup.current);
      const curIdx = groups.findIndex(g => g.key === groupKey);
      if (lastIdx !== -1 && curIdx !== -1) {
        const start = Math.min(lastIdx, curIdx);
        const end = Math.max(lastIdx, curIdx);
        for (let i = start; i <= end; i++) {
          const path = groupSelectPath(groups[i]);
          if (!isSelected(path)) {
            onToggleSelect(path);
          }
        }
        lastClickedGroup.current = groupKey;
        return;
      }
    }
    lastClickedGroup.current = groupKey;
    const g = groups.find(x => x.key === groupKey);
    onToggleSelect(g ? groupSelectPath(g) : groupKey + "/");
  }, [groups, isSelected, onToggleSelect]);

  // Reset pending set when folders change (e.g. after rescan)
  // When the folder set changes (e.g. search narrowing), clear pending requests
  // so we don't keep processing stale batches for folders no longer visible
  useEffect(() => {
    pendingPaths.current.clear();
    resolveQueue.current = [];
    // Reset the resolving gate so the effect can start a fresh batch for the new set
    resolving.current = false;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [folders.length]);

  // When the filter changes, close any expanded card and cancel in-flight file loads
  // so we don't show stale file lists against a new filter.
  useEffect(() => {
    if (expandAbortCtrl.current) { try { expandAbortCtrl.current.abort(); } catch {} }
    setExpandedKey(null);
    setExpandedFiles([]);
    setLoadingFiles(false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filter]);

  // Measure container width
  useEffect(() => {
    if (!containerRef.current) return;
    const ro = new ResizeObserver(entries => {
      for (const e of entries) setContainerWidth(e.contentRect.width);
    });
    ro.observe(containerRef.current);
    setContainerWidth(containerRef.current.clientWidth);
    return () => ro.disconnect();
  }, []);

  // Track scroll from .main-content
  useEffect(() => {
    const scrollParent = containerRef.current?.closest(".main-content") as HTMLElement | null;
    if (!scrollParent) return;
    const handler = () => {
      if (containerRef.current) {
        setScrollTop(Math.max(0, scrollParent.scrollTop - containerRef.current.offsetTop));
      }
    };
    scrollParent.addEventListener("scroll", handler, { passive: true });
    handler();
    return () => scrollParent.removeEventListener("scroll", handler);
  }, [groups.length]);

  // Virtual scroll calculations
  const colCount = Math.max(1, Math.floor((containerWidth + CARD_GAP) / (CARD_MIN_W + CARD_GAP)));
  const rowCount = Math.ceil(groups.length / colCount);
  const rowH = CARD_H + CARD_GAP;
  const viewportH = typeof window !== "undefined" ? window.innerHeight : 800;

  // Find which row has the expanded card (if any)
  const expandedIdx = expandedKey ? groups.findIndex(g => g.key === expandedKey) : -1;
  const expandedRow = expandedIdx >= 0 ? Math.floor(expandedIdx / colCount) : -1;

  // Measure accordion height dynamically and force scroll recalc
  useEffect(() => {
    if (!accordionRef.current) { setAccordionHeight(0); return; }
    const ro = new ResizeObserver(entries => {
      for (const e of entries) {
        const h = e.contentRect.height;
        setAccordionHeight(prev => {
          if (Math.abs(prev - h) > 2) {
            // Force scroll position re-read on next frame
            requestAnimationFrame(() => {
              const sp = containerRef.current?.closest(".main-content") as HTMLElement | null;
              if (sp && containerRef.current) {
                setScrollTop(Math.max(0, sp.scrollTop - containerRef.current.offsetTop));
              }
            });
          }
          return h;
        });
      }
    });
    ro.observe(accordionRef.current);
    return () => ro.disconnect();
  }, [expandedKey, expandedFiles.length, expandedFileDetails.size]);

  // Compute total height accounting for accordion
  const totalHeight = rowCount * rowH + (expandedRow >= 0 ? accordionHeight : 0);

  // Visible row range — account for accordion shifting rows
  let startRow = Math.max(0, Math.floor(scrollTop / rowH) - OVERSCAN);
  let endRow = Math.min(rowCount, Math.ceil((scrollTop + viewportH + accordionHeight) / rowH) + OVERSCAN);
  // When accordion is open, ensure we render enough rows above and below it
  if (expandedRow >= 0) {
    startRow = Math.max(0, Math.min(startRow, expandedRow - OVERSCAN));
    endRow = Math.min(rowCount, Math.max(endRow, expandedRow + OVERSCAN + 2));
  }

  // Resolve posters for visible groups — queue what we don't have
  const expandAbortCtrl = useRef<AbortController | null>(null);
  const expandRequestGen = useRef(0);

  useEffect(() => {
    const startIdx = startRow * colCount;
    const endIdx = Math.min(groups.length, (endRow + 1) * colCount);
    for (let i = startIdx; i < endIdx; i++) {
      const k = groups[i].key;
      if (!posterMeta.has(k) && !pendingPaths.current.has(k)) {
        pendingPaths.current.add(k);
        resolveQueue.current.push(k);
      }
    }

    if (resolveQueue.current.length === 0 || resolving.current) return;

    resolving.current = true;
    const batch = [...resolveQueue.current];
    resolveQueue.current = [];

    (async () => {
      for (let i = 0; i < batch.length; i += 20) {
        const chunk = batch.slice(i, i + 20);
        try {
          const data = await resolvePosterMetadata(chunk);
          setPosterMeta(prev => {
            const next = new Map(prev);
            for (const [k, v] of Object.entries(data)) next.set(k, v);
            return next;
          });
        } catch (err) { /* ignore */ }
        for (const k of chunk) pendingPaths.current.delete(k);
        // Short breather between batches (cache hits are fast; this mainly paces uncached TMDB calls)
        if (i + 20 < batch.length) await new Promise(r => setTimeout(r, 50));
      }
      resolving.current = false;
      // If more items queued while resolving, trigger another round
      if (resolveQueue.current.length > 0) {
        setPosterMeta(prev => new Map(prev)); // Force re-render to trigger effect
      }
    })();
  }, [startRow, endRow, groups, posterMeta.size]);

  const handleExpand = useCallback(async (group: TitleGroup) => {
    if (expandedKey === group.key) {
      setExpandedKey(null);
      setExpandedFiles([]);
      if (expandAbortCtrl.current) { try { expandAbortCtrl.current.abort(); } catch {} }
      return;
    }
    // Cancel any in-flight expand so a prior click's result can't land on top
    if (expandAbortCtrl.current) { try { expandAbortCtrl.current.abort(); } catch {} }
    const ctrl = new AbortController();
    expandAbortCtrl.current = ctrl;
    const myGen = ++expandRequestGen.current;

    setExpandedKey(group.key);
    setExpandedFiles([]);
    setLoadingFiles(true);
    setExpandedFileDetails(new Set());
    try {
      const result = await getFilesByTitle(group.key, filter, ctrl.signal);
      if (myGen !== expandRequestGen.current) return;
      const allFiles = (Array.isArray(result) ? result : []).map(parseFile);

      const byFolder = new Map<string, ScannedFile[]>();
      for (const f of allFiles) {
        const folder = f.file_path.split("/").slice(0, -1).join("/");
        if (!byFolder.has(folder)) byFolder.set(folder, []);
        byFolder.get(folder)!.push(f);
      }
      for (const [folder, files] of byFolder) {
        onFolderFilesLoaded?.(folder, files);
      }

      setExpandedFiles(allFiles);
    } catch (err: any) {
      if (err?.name === "AbortError") return;
    }
    if (myGen === expandRequestGen.current) {
      setLoadingFiles(false);
    }
  }, [expandedKey, filter, onFolderFilesLoaded]);

  // Render only visible rows
  const renderedRows: React.ReactNode[] = [];
  for (let row = startRow; row < endRow; row++) {
    const yOffset = row * rowH + (expandedRow >= 0 && row > expandedRow ? accordionHeight : 0);
    const startIdx = row * colCount;
    const endIdx = Math.min(startIdx + colCount, groups.length);
    const cards: React.ReactNode[] = [];

    for (let i = startIdx; i < endIdx; i++) {
      const group = groups[i];
      const meta = posterMeta.get(group.key);
      cards.push(
        <PosterCard
          key={group.key}
          title={meta?.title || group.title}
          year={meta?.year || group.year}
          posterUrl={meta?.poster_url}
          fileCount={group.fileCount}
          totalSize={group.totalSize}
          isSelected={isSelected(groupSelectPath(group))}
          onSelect={(shiftKey) => handlePosterSelect(group.key, shiftKey)}
          onClick={() => handleExpand(group)}
          isExpanded={expandedKey === group.key}
          mediaType={meta?.media_type}
          onEditClick={() => setFixModalGroup({ key: group.key, title: meta?.title || group.title, year: meta?.year || group.year })}
        />
      );
    }

    renderedRows.push(
      <div
        key={`row-${row}`}
        style={{
          position: "absolute",
          top: yOffset,
          left: 0,
          right: 0,
          display: "grid",
          gridTemplateColumns: `repeat(${colCount}, 1fr)`,
          gap: CARD_GAP,
          height: CARD_H,
        }}
      >
        {cards}
      </div>
    );

    // Render accordion after the expanded row — connected to the expanded card
    if (row === expandedRow) {

      // Calculate card left position (percentage-based to match grid)

      renderedRows.push(
        <div
          key="accordion"
          ref={accordionRef}
          style={{
            position: "absolute",
            top: yOffset + CARD_H, // right after card bottom
            left: 0,
            right: 0,
            fontSize: 12,
          }}
        >
          <div className="poster-accordion" style={{ marginTop: CARD_GAP }}>
            {/* Metadata bar */}
            {(() => {
              const meta = expandedKey ? posterMeta.get(expandedKey) : null;
              // Extract parent library folder from the group key path
              const expGroup = expandedKey ? groups.find(g => g.key === expandedKey) : null;
              const parentFolder = expGroup ? (() => {
                const parts = expGroup.key.split("/").filter(Boolean);
                // Find the part just before the title folder (the library folder)
                for (let i = parts.length - 1; i >= 0; i--) {
                  if (/\[(?:tvdb-\d+|tt\d+)\]/.test(parts[i]) && i > 0) return parts[i - 1];
                }
                return parts.length > 1 ? parts[parts.length - 2] : null;
              })() : null;

              return (
              <div style={{ display: "flex", gap: 10, padding: "8px 12px", fontSize: 11, color: "var(--text-muted)", borderBottom: "1px solid var(--border)", flexWrap: "wrap", alignItems: "center" }}>
                {meta?.rating != null && meta.rating > 0 && (
                  <span style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
                    <span style={{ color: "#f5c518", fontWeight: 700 }}>IMDb</span>
                    <span style={{ color: meta.rating >= 7 ? "var(--success)" : meta.rating >= 5 ? "#ffa94d" : "#e94560", fontWeight: 600 }}>
                      ★ {meta.rating}
                    </span>
                  </span>
                )}
                {meta?.media_type && (
                  <span style={{ background: "var(--bg-primary)", padding: "1px 6px", borderRadius: 3, fontSize: 10 }}>
                    {meta.media_type === "tv" ? "TV" : "Movie"}
                  </span>
                )}
                {meta?.genres && <span>{meta.genres}</span>}
                {meta?.country && <span>{meta.country}</span>}
                {parentFolder && (
                  <span style={{ marginLeft: "auto", background: "rgba(104,96,254,0.25)", color: "#c4a8ff", padding: "1px 6px", borderRadius: 3, fontSize: 10 }}>
                    {parentFolder}
                  </span>
                )}
              </div>
            ); })()}
            {loadingFiles ? (
              <div style={{ display: "flex", alignItems: "center", gap: 8, padding: 16 }}>
                <div className="spinner" style={{ width: 16, height: 16 }} />
                <span style={{ fontSize: 12, color: "var(--text-muted)" }}>Loading files...</span>
              </div>
            ) : expandedFiles.length > 0 ? (
              <div style={{ padding: "4px 12px 8px" }}>
                {(() => {
                  const byFolder = new Map<string, ScannedFile[]>();
                  for (const f of expandedFiles) {
                    const parent = f.file_path.substring(0, f.file_path.lastIndexOf("/"));
                    const folderName = parent.split("/").pop() || parent;
                    if (!byFolder.has(folderName)) byFolder.set(folderName, []);
                    byFolder.get(folderName)!.push(f);
                  }
                  const sections = Array.from(byFolder.entries()).sort(([a], [b]) => a.localeCompare(b));
                  const showHeaders = sections.length > 1;
                  return sections.map(([folderName, files], sectionIdx) => (
                    <div key={folderName}>
                      {showHeaders && (
                        <div style={{ fontSize: 12, fontWeight: 600, color: "var(--text-muted)", padding: "8px 0 4px", ...(sectionIdx > 0 ? { borderTop: "1px solid var(--border)", marginTop: 4 } : {}) }}>{folderName}</div>
                      )}
                      {files.map((file) => (
                        <div key={file.file_path}>
                          <div className="tree-row" onClick={() => setExpandedFileDetails(prev => { const n = new Set(prev); if (n.has(file.file_path)) n.delete(file.file_path); else n.add(file.file_path); return n; })} style={{ cursor: "pointer", padding: "3px 0", fontSize: 12 }}>
                            <input type="checkbox" checked={isSelected(file.file_path)} readOnly onClick={(e) => { e.stopPropagation(); onToggleSelect(file.file_path, e.shiftKey); }} style={{ marginRight: 6 }} />
                            <span style={{ flex: 1 }}>{expandedFileDetails.has(file.file_path) ? "\u25BC" : "\u25B6"} {file.file_name}</span>
                            <span className="tree-file-size" style={{ marginLeft: "auto", flexShrink: 0 }}>{file.file_size_gb} GB</span>
                            <span className={`codec-badge ${file.needs_conversion ? "x264" : "x265"}`} style={{ flexShrink: 0 }}>{getCodecLabel(file.video_codec, file.needs_conversion)}</span>
                            {file.converted && <span style={{ color: "var(--success)", fontSize: 14, flexShrink: 0 }}>&#x2713;</span>}
                            {file.is_new && <span style={{ fontSize: 9, fontWeight: "bold", color: "white", background: "var(--accent)", padding: "2px 6px", borderRadius: 3, flexShrink: 0 }}>NEW</span>}
                            {file.ignored && onUnignoreFile && <button onClick={(e) => { e.stopPropagation(); onUnignoreFile(file.file_path); }} style={{ fontSize: 9, color: "var(--text-muted)", background: "var(--border)", padding: "2px 6px", borderRadius: 3, border: "none", cursor: "pointer", flexShrink: 0 }}>ignored ✕</button>}
                            <div style={{ display: "inline-flex", alignItems: "center", gap: 2, flexShrink: 0, marginLeft: 4 }}>
                              {!file.ignored && onIgnoreFile && <button onClick={(e) => { e.stopPropagation(); onIgnoreFile(file.file_path); }} style={{ background: "none", border: "none", color: "var(--text-muted)", cursor: "pointer", padding: 4, fontSize: 14 }}>&#x2298;</button>}
                              <button onClick={(e) => { e.stopPropagation(); onRemoveFile(file.file_path); }} style={{ background: "none", border: "none", color: "var(--text-muted)", cursor: "pointer", padding: 4, fontSize: 16 }}>&times;</button>
                              {onDeleteFile && <button onClick={(e) => { e.stopPropagation(); onDeleteFile(file.file_path); }} style={{ background: "none", border: "none", color: "#e94560", cursor: "pointer", padding: 4, opacity: 0.6 }}><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 01-2 2H7a2 2 0 01-2-2V6m3 0V4a2 2 0 012-2h4a2 2 0 012 2v2"/></svg></button>}
                            </div>
                          </div>
                          {expandedFileDetails.has(file.file_path) && <FileDetail file={file} onToggleTrack={toggleAudioTrack} onToggleSubTrack={toggleSubTrack} />}
                        </div>
                      ))}
                    </div>
                  ));
                })()}
              </div>
            ) : (
              <div style={{ padding: 16, fontSize: 12, color: "var(--text-muted)" }}>No files found</div>
            )}
          </div>
        </div>
      );
    }
  }

  return (
    <div ref={containerRef} style={{ position: "relative", minHeight: totalHeight || 100 }}>
      {renderedRows}
      {folders.length === 0 && (
        <div style={{ textAlign: "center", padding: 40, opacity: 0.5 }}>
          {search || allowedPaths || (filter && filter !== "all") ? (
            <div style={{ fontSize: 13 }}>No files match the current filter.</div>
          ) : (
            <div style={{ fontSize: 13 }}>No files scanned yet. Run a scan to get started.</div>
          )}
        </div>
      )}
      {fixModalGroup && (
        <PosterFixModal
          folderPath={fixModalGroup.key}
          currentTitle={fixModalGroup.title}
          currentYear={fixModalGroup.year}
          onClose={() => setFixModalGroup(null)}
          onFixed={() => {
            // Clear cached metadata for this group so it re-fetches
            setPosterMeta(prev => {
              const next = new Map(prev);
              next.delete(fixModalGroup.key);
              return next;
            });
            pendingPaths.current.delete(fixModalGroup.key);
            resolveQueue.current.push(fixModalGroup.key);
            setFixModalGroup(null);
          }}
        />
      )}
    </div>
  );
}
