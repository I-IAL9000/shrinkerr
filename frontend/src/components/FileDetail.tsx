import { useState, useEffect } from "react";
import type { ScannedFile, AudioTrack, SubtitleTrack } from "../types";
import { getTracksByPath, getFileHistory, researchFile, arrAction, detectLanguages, addJobsFromScan, setTrackLanguage, type FileEvent } from "../api";
import AudioTrackRow from "./AudioTrackRow";
import EventTimeline from "./EventTimeline";
import { LANGUAGES } from "../utils/languages";
import { vmafLabel } from "../utils/vmaf";
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
  const [detecting, setDetecting] = useState(false);
  const [detectStage, setDetectStage] = useState<string | null>(null);
  const [detected, setDetected] = useState(false);
  // v0.9.86: writing a language to an mp4 remuxes the whole file (mkvpropedit
  // can't tag mp4), which can take a while — show a spinner and lock the track
  // controls so users know it's working and don't retry.
  const [settingLang, setSettingLang] = useState(false);
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

  const handleDetect = async () => {
    setDetecting(true);
    setDetectStage(null);
    try {
      const r = await detectLanguages(file.file_path);
      // v0.9.45: apply the returned tracks whenever present — a notes-only run
      // (nothing resolved, just recorded WHY) returns changed=false but still
      // carries the per-track detect_note we want to show.
      if (r.audio_tracks || r.subtitle_tracks) {
        setFetchedAudio(r.audio_tracks || []);
        setFetchedSubs(r.subtitle_tracks || []);
        setDetected(true);
      }
      toast(r.changed ? "Languages detected — tracks updated"
        : "No new languages resolved — see the reason next to each track", "success");
    } catch (exc: any) {
      toast(`Language detection error: ${exc?.message || exc}`, "error");
    } finally {
      setDetecting(false);
      setDetectStage(null);
    }
  };

  // v0.9.50: the detail now renders the freshly-fetched tracks (fetchedAudio/
  // Subs), so a keep-toggle must flip them locally too — the parent handler
  // only updates the scanner cache, which this view no longer reads. Without
  // this the checkbox looked frozen.
  const handleToggleAudioLocal = (idx: number) => {
    setFetchedAudio(prev => prev.map(t => t.stream_index === idx ? { ...t, keep: !t.keep } : t));
    onToggleTrack?.(file.file_path, idx);
  };
  const handleToggleSubLocal = (fp: string, idx: number) => {
    setFetchedSubs(prev => prev.map(t => t.stream_index === idx ? { ...t, keep: !t.keep } : t));
    onToggleSubTrack?.(fp, idx);
  };

  // v0.9.43: manual language override for a track detection can't resolve.
  const handleSetTrackLanguage = async (trackType: "audio" | "subtitle", streamIndex: number, language: string) => {
    if (settingLang) return;  // ignore re-fires while a write is in flight
    setSettingLang(true);
    try {
      const r = await setTrackLanguage(file.file_path, trackType, streamIndex, language);
      setFetchedAudio(r.audio_tracks || []);
      setFetchedSubs(r.subtitle_tracks || []);
      setDetected(true);
      if (r.file_written) {
        toast("Language set", "success");
      } else if (r.pending_detected) {
        toast("Saved — remux/convert to MKV to apply it (this container can't be tagged in place)", "success");
      } else {
        toast("Couldn't set the language for this track", "error");
      }
    } catch (exc: any) {
      toast(`Failed to set language: ${exc?.message || exc}`, "error");
    } finally {
      setSettingLang(false);
    }
  };

  // v0.9.1: surface the coarse OCR/detection stages the backend broadcasts
  // during on-demand language detection (image-sub OCR can take minutes).
  // useWebSocket re-dispatches every WS message as a `ws-message` window
  // event (see api.ts), so we subscribe here without prop drilling —
  // mirrors ScannerPage's scan_results_changed listener. Only react to
  // progress for THIS file.
  useEffect(() => {
    const onWsMessage = (event: Event) => {
      const me = event as MessageEvent;
      try {
        const msg = JSON.parse(me.data);
        if (msg?.type !== "detect_progress" || msg.file_path !== file.file_path) return;
        setDetectStage(msg.stage || null);
      } catch { return; }
    };
    window.addEventListener("ws-message", onWsMessage);
    return () => window.removeEventListener("ws-message", onWsMessage);
  }, [file.file_path]);

  useEffect(() => {
    // v0.9.45: always refetch tracks from the DB (was skipped when the prop
    // already had tracks) so freshly-written fields — detect_note, a manual
    // language, detected_language — show without reloading the scanner list.
    // Only show the spinner when we have nothing to render yet.
    if (!file.audio_tracks?.length) setLoading(true);
    getTracksByPath(file.file_path).then((data) => {
      setFetchedAudio(data.audio_tracks || []);
      setFetchedSubs(data.subtitle_tracks || []);
    }).catch(() => {}).finally(() => setLoading(false));
  }, [file.file_path]);

  useEffect(() => {
    if (tab !== "history" || history) return;
    setHistoryLoading(true);
    getFileHistory(file.file_path)
      .then(d => setHistory(d.events))
      .catch(() => setHistory([]))
      .finally(() => setHistoryLoading(false));
  }, [tab, file.file_path]);

  // v0.9.45: prefer the freshly-fetched tracks (they carry detect_note /
  // manual edits); fall back to the prop only until the fetch resolves.
  const audioTracks = (detected || fetchedAudio.length) ? fetchedAudio : (file.audio_tracks || []);
  const subtitleTracks = (detected || fetchedSubs.length) ? fetchedSubs : (file.subtitle_tracks || []);

  const isUnd = (lang: string | undefined | null) => (lang || "und").toLowerCase() === "und";
  const hasUndTracks = audioTracks.some(t => isUnd(t.language)) || subtitleTracks.some(t => isUnd(t.language));

  // v0.9.35: a language detected in place couldn't be written to this file's
  // container. Untaggable flat files (AVI etc.) are fixed by a stream-copy
  // remux to mkv; disc structures need a full conversion (which detects +
  // stamps during the encode). Offer the appropriate one.
  const _ext = file.file_path.slice(file.file_path.lastIndexOf(".")).toLowerCase();
  // v0.9.54: .m4v/.m4a included — ffmpeg's ipod muxer can't write language in
  // place, so they take the same remux-to-mkv path as AVI (matches backend
  // _UNTAGGABLE_CONTAINERS).
  const isUntaggableFlat = [".avi", ".mpg", ".mpeg", ".wmv", ".flv", ".asf", ".vob", ".m4v", ".m4a"].includes(_ext);
  const isDisc = /\/VIDEO_TS\/|\/BDMV\/|VIDEO_TS\.IFO$|index\.bdmv$/i.test(file.file_path);
  const hasPendingDetected = audioTracks.some(t => (t as any).detected_language && isUnd(t.language))
    || subtitleTracks.some(t => (t as any).detected_language && isUnd(t.language));
  const showApplyViaConvert = (isUntaggableFlat && hasPendingDetected) || (isDisc && hasUndTracks);

  const handleApplyLanguage = async () => {
    const remux = isUntaggableFlat && !isDisc;
    const ok = await confirm({
      message: remux
        ? `Remux this file to MKV to apply the detected language?\n\nFast stream-copy — no re-encode, no quality loss, no size increase. The ${_ext} is replaced by an .mkv.`
        : "Convert this to MKV to apply the language?\n\nThe language is detected and stamped during the conversion.",
      confirmLabel: remux ? "Remux to MKV" : "Convert to MKV",
    });
    if (!ok) return;
    try {
      const r = await addJobsFromScan([file.file_path], 0, false, remux ? { language_remux: true } : {});
      if (r.added) {
        toast(`Queued ${remux ? "remux" : "conversion"} to apply language`, "success");
      } else if (r.skipped_existing) {
        toast("Already in the queue — check the Queue tab", "info");
      } else {
        toast("Nothing queued", "error");
      }
    } catch (exc: any) {
      toast(`Failed to queue: ${exc?.message || exc}`, "error");
    }
  };

  // v0.6.7: was `file.file_size * 0.3` — a stale flat 30% reduction default
  // that disagreed with the queue-estimate modal's CQ-calibrated curve.
  // Backend now stores the CQ-derived video-only savings on each scan row.
  const convSavings = file.needs_conversion ? (file.video_conv_savings_bytes || 0) : 0;

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
                        onToggle={(idx) => handleToggleAudioLocal(idx)}
                        onSetLanguage={(idx, lang) => handleSetTrackLanguage("audio", idx, lang)}
                        busy={settingLang}
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
                            <SubTrackRow key={track.stream_index} track={track} filePath={file.file_path} onToggle={handleToggleSubLocal} onSetLanguage={(idx, lang) => handleSetTrackLanguage("subtitle", idx, lang)} busy={settingLang} />
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
                            <SubTrackRow key={`ext-${track.stream_index}`} track={track} filePath={file.file_path} onToggle={handleToggleSubLocal} isExternal onSetLanguage={(idx, lang) => handleSetTrackLanguage("subtitle", idx, lang)} busy={settingLang} />
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
            {hasUndTracks && (
              <button
                type="button"
                onClick={handleDetect}
                disabled={detecting}
                title="Run language detection on this file's unknown (und) audio and text-subtitle tracks"
                style={{
                  background: "transparent",
                  color: "var(--text-muted)",
                  border: "1px solid var(--border)",
                  borderRadius: 4,
                  padding: "4px 10px",
                  fontSize: 11,
                  cursor: detecting ? "wait" : "pointer",
                  opacity: detecting ? 0.6 : 1,
                  display: "inline-flex",
                  alignItems: "center",
                  gap: 6,
                }}
              >
                <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <circle cx="12" cy="12" r="10"/><path d="M2 12h20"/><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/>
                </svg>
                {detecting ? "Detecting…" : "Detect languages"}
              </button>
            )}
            {showApplyViaConvert && (
              <button
                type="button"
                onClick={handleApplyLanguage}
                title={isUntaggableFlat
                  ? "Remux to MKV (stream copy, no re-encode) to apply the detected language — this container can't store it in place"
                  : "Convert this disc to MKV; the language is detected and applied during the conversion"}
                style={{
                  background: "transparent",
                  color: "var(--accent)",
                  border: "1px solid var(--accent)",
                  borderRadius: 4,
                  padding: "4px 10px",
                  fontSize: 11,
                  cursor: "pointer",
                  display: "inline-flex",
                  alignItems: "center",
                  gap: 6,
                }}
              >
                {isUntaggableFlat && !isDisc ? "Remux to MKV (apply language)" : "Convert to MKV (apply language)"}
              </button>
            )}
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
          {detecting && detectStage && (
            <div style={{ display: "flex", alignItems: "center", gap: 6, marginTop: 6, fontSize: 10, color: "var(--text-muted)" }}>
              <div className="spinner" style={{ width: 12, height: 12 }} />
              <span>{detectStage}</span>
            </div>
          )}
          {settingLang && (
            <div style={{ display: "flex", alignItems: "center", gap: 6, marginTop: 6, fontSize: 10, color: "var(--text-muted)" }}>
              <div className="spinner" style={{ width: 12, height: 12 }} />
              <span>Setting track language… (mp4 files are remuxed, this can take a moment)</span>
            </div>
          )}
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
            // If the file has a VMAF score in scan_results but no VMAF event
            // was logged for it (either pre-dates the event-logging feature,
            // or was logged against a pre-rename path), synthesize an entry
            // from scan_results so the score is always visible here. When
            // VMAF filters are what led you to this file, seeing the actual
            // number one click away is the whole point.
            <EventTimeline events={mergeVmafIntoEvents(history || [], file)} compact />
          )}
        </div>
      )}
    </div>
  );
}

/**
 * Ensure the file's current VMAF score always has a presence in the event
 * timeline. Behaviour:
 *   - If there's already a VMAF event in the fetched history, leave the
 *     history alone (the real event has the correct timestamp + metadata).
 *   - If there's a vmaf_score on the scan_results row but no VMAF event,
 *     synthesize a placeholder entry so the score is visible. Uses the
 *     file's health_checked_at timestamp as a rough chronological anchor
 *     when available, otherwise falls back to an empty string (the
 *     EventTimeline renders that gracefully as "just now"-ish).
 *   - If the file has no vmaf_score at all, return the events unchanged.
 *
 * The synthetic entry uses id = -1 to avoid colliding with real DB ids,
 * and event_type = "vmaf" so the existing timeline styling picks it up.
 */
function mergeVmafIntoEvents(events: FileEvent[], file: ScannedFile): FileEvent[] {
  const score = file.vmaf_score;
  if (score == null) return events;
  const hasRealVmafEvent = events.some(e => e.event_type === "vmaf");
  if (hasRealVmafEvent) return events;

  const tier = vmafLabel(score);
  const synthetic: FileEvent = {
    id: -1,
    file_path: file.file_path,
    event_type: "vmaf",
    summary: `VMAF: ${score} (${tier})`,
    occurred_at: file.health_checked_at || "",
    // Synthetic event carries the score so EventTimeline can colour it
    // correctly even though there's no real DB row.
    details: { vmaf_score: score },
  };
  // Prepend so the synthesized score sits at the top of a short history —
  // mirrors the normal reverse-chronological ordering (VMAF runs after
  // Convert, so the real event would sort newest-first too).
  return [synthetic, ...events];
}


function SubTrackRow({ track, filePath, onToggle, isExternal, onSetLanguage, busy }: {
  track: SubtitleTrack;
  filePath: string;
  onToggle?: (filePath: string, streamIndex: number) => void;
  isExternal?: boolean;
  onSetLanguage?: (streamIndex: number, language: string) => void;
  busy?: boolean;
}) {
  const [editing, setEditing] = useState(false);
  const basename = isExternal && track.external_path
    ? track.external_path.split("/").pop() || track.title
    : null;

  // v0.5.16: lock branch removed (issue #11). Every track renders as an
  // editable checkbox so users can override always-keep defaults. See
  // AudioTrackRow.tsx + scanner.classify_audio_tracks for the smart-
  // selection logic that picks defaults.
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 12, padding: "2px 0" }}>
      <input
        type="checkbox"
        checked={!track.keep}
        onChange={() => onToggle?.(filePath, track.stream_index)}
        onClick={(e) => e.stopPropagation()}
        style={{ accentColor: "var(--accent)", cursor: "pointer" }}
      />
      <span style={{ color: track.keep ? "var(--text-secondary)" : "var(--text-muted)", textDecoration: track.keep ? "none" : "line-through" }}>
        {track.language.toUpperCase()} — {track.codec}
        {track.title && !isExternal && ` — ${track.title}`}
        {track.forced && <span style={{ fontSize: 9, color: "var(--warning)", marginLeft: 4 }}>FORCED</span>}
      </span>
      {isExternal && basename && (
        <span style={{ fontSize: 10, color: "var(--text-muted)", opacity: 0.7 }}>{basename}</span>
      )}
      {track.detect_note && !track.detected_language && (track.language || "und").toLowerCase() === "und" && (
        <span style={{ fontSize: 10, color: "var(--warning)", opacity: 0.8 }} title="Why auto-detection didn't set a language">
          {track.detect_note}
        </span>
      )}
      {onSetLanguage && !editing && (
        <button
          type="button"
          onClick={(e) => { e.stopPropagation(); setEditing(true); }}
          disabled={busy}
          title="Set language manually"
          style={{ background: "none", border: "none", color: "var(--text-muted)", cursor: busy ? "wait" : "pointer", padding: 0, display: "inline-flex", alignItems: "center", opacity: busy ? 0.5 : 1 }}
        >
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <path d="M12 20h9"/><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4 12.5-12.5z"/>
          </svg>
        </button>
      )}
      {onSetLanguage && editing && (
        <select
          autoFocus
          defaultValue={(track.language || "und").toLowerCase()}
          onClick={(e) => e.stopPropagation()}
          onChange={(e) => { const v = e.target.value; setEditing(false); if (v && v !== (track.language || "und").toLowerCase()) onSetLanguage(track.stream_index, v); }}
          onBlur={() => setEditing(false)}
          style={{ background: "var(--bg-tertiary)", color: "var(--text-primary)", border: "1px solid var(--accent)", borderRadius: 4, fontSize: 11, padding: "1px 4px" }}
        >
          {LANGUAGES.map((l) => <option key={l.code} value={l.code}>{l.name} ({l.code})</option>)}
        </select>
      )}
    </div>
  );
}
