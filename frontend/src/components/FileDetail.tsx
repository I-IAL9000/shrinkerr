import { useState, useEffect } from "react";
import type { ScannedFile, AudioTrack, SubtitleTrack } from "../types";
import { getTracksByPath, getFileHistory, researchFile, arrAction, type FileEvent } from "../api";
import AudioTrackRow from "./AudioTrackRow";
import EventTimeline from "./EventTimeline";
import { useToast } from "../useToast";
import { useConfirm } from "./ConfirmModal";

interface FileDetailProps {
  file: ScannedFile;
  onToggleTrack: (filePath: string, streamIndex: number) => void;
  onToggleSubTrack?: (filePath: string, streamIndex: number) => void;
}

type Tab = "tracks" | "history";

export default function FileDetail({ file, onToggleTrack, onToggleSubTrack }: FileDetailProps) {
  const [fetchedAudio, setFetchedAudio] = useState<AudioTrack[]>([]);
  const [fetchedSubs, setFetchedSubs] = useState<SubtitleTrack[]>([]);
  const [loading, setLoading] = useState(!file.audio_tracks?.length);
  const [tab, setTab] = useState<Tab>("tracks");
  const [history, setHistory] = useState<FileEvent[] | null>(null);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [researching, setResearching] = useState(false);
  const [upgrading, setUpgrading] = useState(false);
  const toast = useToast();
  const confirm = useConfirm();

  const isCorrupt = file.probe_status === "corrupt" || file.health_status === "corrupt";

  const handleUpgradeSearch = async () => {
    setUpgrading(true);
    try {
      const r: any = await arrAction(file.file_path, "upgrade");
      if (r?.success) {
        const label = r.service === "sonarr"
          ? `${r.series} — ${(r.episode_ids || []).length} ep(s)`
          : `${r.movie}`;
        toast(`Upgrade search triggered (${r.service}): ${label}`, "success");
      } else {
        toast(`Upgrade search failed: ${r?.error || "unknown error"}`, "error");
      }
    } catch (exc: any) {
      toast(`Upgrade search error: ${exc?.message || exc}`, "error");
    } finally {
      setUpgrading(false);
    }
  };

  const handleResearch = async () => {
    const label = isCorrupt ? "Re-download (file is corrupt)" : "Re-download (replace with different release)";
    const ok = await confirm({
      message: `${label}\n\nThis will blocklist the current release, delete the file, and ask Sonarr/Radarr to grab a replacement.\n\nContinue?`,
      confirmLabel: "Re-download",
      danger: true,
    });
    if (!ok) return;
    setResearching(true);
    try {
      const r = await researchFile(file.file_path, true);
      if (r.success) {
        const parts = [
          r.service === "sonarr" ? `Sonarr: ${r.series || ""}` : `Radarr: ${r.movie || ""}`,
          r.blocklisted ? "blocklisted" : "NOT blocklisted",
          r.deleted ? "deleted" : "not deleted",
          r.searched ? "search triggered" : "search NOT triggered",
        ];
        toast(`Re-download requested — ${parts.join(", ")}`, "success");
      } else {
        toast(`Re-download failed: ${r.error || "unknown error"}`, "error");
      }
    } catch (exc: any) {
      toast(`Re-download error: ${exc?.message || exc}`, "error");
    } finally {
      setResearching(false);
    }
  };

  useEffect(() => {
    if (file.audio_tracks?.length) return;
    setLoading(true);
    getTracksByPath(file.file_path).then((data) => {
      setFetchedAudio(data.audio_tracks || []);
      setFetchedSubs(data.subtitle_tracks || []);
      setLoading(false);
    }).catch(() => setLoading(false));
  }, [file.file_path]);

  useEffect(() => {
    if (tab !== "history" || history) return;
    setHistoryLoading(true);
    getFileHistory(file.file_path)
      .then(d => setHistory(d.events))
      .catch(() => setHistory([]))
      .finally(() => setHistoryLoading(false));
  }, [tab, file.file_path]);

  const audioTracks = file.audio_tracks?.length ? file.audio_tracks : fetchedAudio;
  const subtitleTracks = file.subtitle_tracks?.length ? file.subtitle_tracks : fetchedSubs;

  const convSavings = file.needs_conversion ? file.file_size * 0.3 : 0;

  const tabBtnStyle = (active: boolean) => ({
    background: "none",
    border: "none",
    borderBottom: `2px solid ${active ? "var(--accent)" : "transparent"}`,
    color: active ? "var(--text-primary)" : "var(--text-muted)",
    fontSize: 12,
    fontWeight: 600,
    padding: "4px 10px",
    cursor: "pointer",
  });

  return (
    <div className="file-detail">
      <div style={{ color: "var(--text-muted)", marginBottom: 4 }}>
        {file.video_codec} &middot; {file.file_size_gb} GB
      </div>
      <div style={{ display: "flex", gap: 4, marginBottom: 8, borderBottom: "1px solid var(--border)" }}>
        <button style={tabBtnStyle(tab === "tracks")} onClick={() => setTab("tracks")}>Tracks</button>
        <button style={tabBtnStyle(tab === "history")} onClick={() => setTab("history")}>History</button>
      </div>

      {tab === "tracks" && (
        <>
          <div style={{ fontSize: 11, color: "var(--text-muted)", marginBottom: 6 }}>
            Native language: <strong style={{ color: file.language_source === "api" ? "var(--success)" : "var(--text-secondary)" }}>
              {file.native_language.toUpperCase()}
            </strong>
            <span style={{
              fontSize: 9, marginLeft: 4, padding: "1px 4px", borderRadius: 3,
              background: file.language_source === "api" ? "rgba(0,200,100,0.15)" : "var(--border)",
              color: file.language_source === "api" ? "var(--success)" : "var(--text-muted)",
            }}>
              {file.language_source === "api" ? "from API" : "heuristic"}
            </span>
          </div>
          {file.needs_conversion && (
            <div style={{ color: "var(--success)", marginBottom: 6 }}>
              Convert to x265 10-bit (est. save ~{(convSavings / (1024**3)).toFixed(1)} GB)
            </div>
          )}
          {loading ? (
            <div style={{ display: "flex", alignItems: "center", gap: 8, padding: "8px 0" }}>
              <div className="spinner" style={{ width: 14, height: 14 }} />
              <span style={{ fontSize: 12, color: "var(--text-muted)" }}>Loading tracks...</span>
            </div>
          ) : (
            <>
              {audioTracks.length > 0 ? (
                <>
                  <div style={{ marginBottom: 2 }}>Audio tracks:</div>
                  <div style={{ paddingLeft: 12 }}>
                    {[...audioTracks].sort((a, b) => a.stream_index - b.stream_index).map((track) => (
                      <AudioTrackRow
                        key={track.stream_index}
                        track={track}
                        onToggle={(idx) => onToggleTrack(file.file_path, idx)}
                      />
                    ))}
                  </div>
                </>
              ) : (
                <div style={{ fontSize: 12, color: "var(--danger)", fontStyle: "italic", marginBottom: 6 }}>No audio tracks detected</div>
              )}
              {(() => {
                const embedded = subtitleTracks.filter(t => !t.external);
                const external = subtitleTracks.filter(t => t.external);
                return (
                  <>
                    {embedded.length > 0 ? (
                      <>
                        <div style={{ marginTop: 8, marginBottom: 2 }}>Subtitle tracks:</div>
                        <div style={{ paddingLeft: 12 }}>
                          {[...embedded].sort((a, b) => a.stream_index - b.stream_index).map((track) => (
                            <SubTrackRow key={track.stream_index} track={track} filePath={file.file_path} onToggle={onToggleSubTrack} />
                          ))}
                        </div>
                      </>
                    ) : (
                      <div style={{ fontSize: 12, color: "var(--text-muted)", fontStyle: "italic", marginTop: 6 }}>No embedded subtitle tracks</div>
                    )}
                    {external.length > 0 && (
                      <>
                        <div style={{ marginTop: 8, marginBottom: 2, display: "flex", alignItems: "center", gap: 6 }}>
                          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="var(--text-muted)" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                            <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/>
                          </svg>
                          External subtitles:
                        </div>
                        <div style={{ paddingLeft: 12 }}>
                          {external.map((track) => (
                            <SubTrackRow key={`ext-${track.stream_index}`} track={track} filePath={file.file_path} onToggle={onToggleSubTrack} isExternal />
                          ))}
                        </div>
                      </>
                    )}
                  </>
                );
              })()}
            </>
          )}
          <div style={{ color: "var(--success)", marginTop: 6, fontSize: 11 }}>
            Total est. savings: ~{file.estimated_savings_gb} GB
          </div>

          {/* *arr actions — Replace (red when corrupt) + Search upgrade (quiet) */}
          <div style={{ marginTop: 10, paddingTop: 8, borderTop: "1px solid var(--border)", display: "flex", flexWrap: "wrap", gap: 6 }}>
            <button
              type="button"
              onClick={handleResearch}
              disabled={researching || upgrading}
              title={isCorrupt
                ? "This file appears corrupt — blocklist the release and ask Sonarr/Radarr for a replacement"
                : "Replace this file with a different release (blocklists current, triggers new search)"}
              style={{
                background: isCorrupt ? "#e94560" : "transparent",
                color: isCorrupt ? "#fff" : "var(--text-muted)",
                border: `1px solid ${isCorrupt ? "#e94560" : "var(--border)"}`,
                borderRadius: 4,
                padding: "4px 10px",
                fontSize: 11,
                fontWeight: isCorrupt ? 600 : 400,
                cursor: researching ? "wait" : "pointer",
                opacity: researching ? 0.6 : 1,
                display: "inline-flex",
                alignItems: "center",
                gap: 6,
              }}
            >
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M21 12a9 9 0 11-3-6.7L21 8"/><path d="M21 3v5h-5"/>
              </svg>
              {researching
                ? "Requesting…"
                : isCorrupt
                  ? "Re-download (corrupt file)"
                  : "Request replacement"}
            </button>

            <button
              type="button"
              onClick={handleUpgradeSearch}
              disabled={researching || upgrading}
              title="Ask Sonarr/Radarr to search for a better release per your quality profile. Does NOT blocklist or delete the current file."
              style={{
                background: "transparent",
                color: "var(--text-muted)",
                border: "1px solid var(--border)",
                borderRadius: 4,
                padding: "4px 10px",
                fontSize: 11,
                cursor: upgrading ? "wait" : "pointer",
                opacity: upgrading ? 0.6 : 1,
                display: "inline-flex",
                alignItems: "center",
                gap: 6,
              }}
            >
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <polyline points="17 11 12 6 7 11"/><polyline points="17 18 12 13 7 18"/>
              </svg>
              {upgrading ? "Searching…" : "Search for upgrade"}
            </button>
          </div>
          {isCorrupt && (
            <div style={{ fontSize: 10, color: "var(--text-muted)", marginTop: 4 }}>
              ffprobe couldn't read a video stream in this file. Blocklists the release and requests a fresh download from Sonarr/Radarr.
            </div>
          )}
        </>
      )}

      {tab === "history" && (
        <div style={{ paddingTop: 4 }}>
          {historyLoading ? (
            <div style={{ display: "flex", alignItems: "center", gap: 8, padding: "8px 0" }}>
              <div className="spinner" style={{ width: 14, height: 14 }} />
              <span style={{ fontSize: 12, color: "var(--text-muted)" }}>Loading history...</span>
            </div>
          ) : (
            <EventTimeline events={history || []} compact />
          )}
        </div>
      )}
    </div>
  );
}

function SubTrackRow({ track, filePath, onToggle, isExternal }: {
  track: SubtitleTrack;
  filePath: string;
  onToggle?: (filePath: string, streamIndex: number) => void;
  isExternal?: boolean;
}) {
  const basename = isExternal && track.external_path
    ? track.external_path.split("/").pop() || track.title
    : null;

  return (
    <div style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 12, padding: "2px 0" }}>
      {track.locked ? (
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="var(--text-muted)" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={{ flexShrink: 0, opacity: 0.5 }}>
          <rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0110 0v4"/>
        </svg>
      ) : (
        <input
          type="checkbox"
          checked={!track.keep}
          onChange={() => onToggle?.(filePath, track.stream_index)}
          onClick={(e) => e.stopPropagation()}
          style={{ accentColor: "var(--accent)", cursor: "pointer" }}
        />
      )}
      <span style={{ color: track.keep ? "var(--text-secondary)" : "var(--text-muted)", textDecoration: track.keep ? "none" : "line-through" }}>
        {track.language.toUpperCase()} — {track.codec}
        {track.title && !isExternal && ` — ${track.title}`}
        {track.forced && <span style={{ fontSize: 9, color: "var(--warning)", marginLeft: 4 }}>FORCED</span>}
      </span>
      {isExternal && basename && (
        <span style={{ fontSize: 10, color: "var(--text-muted)", opacity: 0.7 }}>{basename}</span>
      )}
      {track.locked && <span style={{ opacity: 0.4, fontSize: 10 }}>(always keep)</span>}
    </div>
  );
}
