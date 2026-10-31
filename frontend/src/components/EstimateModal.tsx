import { useState, useEffect, useRef } from "react";
import { triedQuality } from "../utils/cameOutLarger";
import { useDialog } from "../useDialog";
import { useTranslation, Trans } from "react-i18next";
import i18n from "../i18n";
import { estimateJobs, startTestEncode, getEncodingSettings, getEncoderCaps, wsUrl, type EncoderCaps } from "../api";
import { useRangeFill } from "../useRangeFill";
import { fmtNum, fmtBytes } from "../fmt";
import { vmafColor, vmafTintBg } from "../utils/vmaf";
import { trackLanguageList } from "../utils/languages";

function formatTime(seconds: number): string {
  if (seconds < 60) return i18n.t("scannerModals:time.seconds", { n: seconds });
  if (seconds < 3600) return i18n.t("scannerModals:time.minutes", { n: Math.round(seconds / 60) });
  const h = Math.floor(seconds / 3600);
  const m = Math.round((seconds % 3600) / 60);
  return h > 24
    ? i18n.t("scannerModals:time.days", { n: (seconds / 86400).toFixed(1) })
    : i18n.t("scannerModals:time.hoursMinutes", { h, m });
}

// → scannerModals:estimate.priorities.*
const PRIORITY_KEYS = ["normal", "high", "highest"];
const PRIORITY_COLORS = ["var(--text-muted)", "var(--caution)", "var(--danger)"];

export interface EncodingOverrides {
  encoder?: string;
  nvenc_preset?: string;
  nvenc_cq?: number;
  libx265_crf?: number;
  libx265_preset?: string;
  audio_codec?: string;
  audio_bitrate?: number;
  target_resolution?: string;
  force_reencode?: boolean;
  // "No video conversion" — only run the audio/sub cleanup pass (track
  // removal + native-language reorder + audio codec transcode). v0.3.80+.
  cleanup_only?: boolean;
  // Fix for direct play (v0.10.0), ticked or not, and the fixes it makes
  direct_play?: boolean;
  direct_play_fixes?: string[];
}

// Fix for direct play's fixes (backend/direct_play.py's problems), in the
// dialog's order.
const DP_FIXES = ["container", "video", "subs", "audio"] as const;

// label is the x265 preset name (not translated); suffixKey → scannerModals:overrides.*
const CPU_PRESETS: { value: string; label: string; suffixKey?: string }[] = [
  { value: "ultrafast", label: "Ultrafast" },
  { value: "superfast", label: "Superfast" },
  { value: "veryfast", label: "Very Fast" },
  { value: "faster", label: "Faster" },
  { value: "fast", label: "Fast" },
  { value: "medium", label: "Medium", suffixKey: "cpuPresetDefault" },
  { value: "slow", label: "Slow" },
  { value: "slower", label: "Slower" },
  { value: "veryslow", label: "Very Slow", suffixKey: "cpuPresetBest" },
];

const NVENC_PRESET_IDS = ["p1", "p2", "p3", "p4", "p5", "p6", "p7"];

interface EstimateModalProps {
  filePaths: string[];
  hasIgnoredFiles?: boolean;
  activeFilter?: string;
  onConfirm: (priority: number, overrideRules: boolean, encodingOverrides?: EncodingOverrides, start?: boolean) => void;
  onCancel: () => void;
}

export default function EstimateModal({ filePaths, hasIgnoredFiles, activeFilter, onConfirm, onCancel }: EstimateModalProps) {
  const { t } = useTranslation(["scannerModals", "common"]);
  const [estimate, setEstimate] = useState<any>(null);
  const [loading, setLoading] = useState(true);
  const [priority, setPriority] = useState(0);
  // Off (v0.10.0): ignored files get only their clean-up, as rules have it.
  const [overrideRules, setOverrideRules] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);

  // Encoding overrides — null = auto (falls back to the user's configured
  // default encoder, fetched below)
  const [encoder, setEncoder] = useState<string | null>(null);
  const [defaultEncoder, setDefaultEncoder] = useState<string>("nvenc");
  // Caps drive which options the encoder dropdown shows. Without this we'd
  // either always show all 4 (and have qsv/vaapi fail at encode time on
  // hosts without /dev/dri) or always hide them. v0.3.69+.
  const [encoderCaps, setEncoderCaps] = useState<EncoderCaps | null>(null);
  // Fix for direct play (v0.10.0): ticked when Settings' "Fix for direct
  // play when converting" is on; every fix ticked to start with.
  const [directPlay, setDirectPlay] = useState(false);
  const [dpFixes, setDpFixes] = useState<string[]>([...DP_FIXES]);
  // The estimate waits for the settings: one ticking Fix for direct play
  // made it run twice on opening.
  const [settingsRead, setSettingsRead] = useState(false);

  useEffect(() => {
    getEncodingSettings().then(s => {
      if (s?.default_encoder) setDefaultEncoder(s.default_encoder);
      if (s?.direct_play_on_convert) setDirectPlay(true);
    }).catch(() => {}).finally(() => setSettingsRead(true));
    getEncoderCaps().then(setEncoderCaps).catch(() => {});
  }, []);
  const [preset, setPreset] = useState<string | null>(null);
  const [cq, setCq] = useState<number | null>(null);
  const [cqSlider, setCqSlider] = useState<number | null>(null); // visual only, committed on release
  const [audioCdc, setAudioCdc] = useState<string | null>(null);
  // Test encode state
  const [testRunning, setTestRunning] = useState(false);
  const [testResult, setTestResult] = useState<any>(null);
  const [testStep, setTestStep] = useState("");
  const [testProgress, setTestProgress] = useState(0);
  const wsRef = useRef<WebSocket | null>(null);

  // Paint range-slider fill gradient for sliders mounted inside this modal.
  // The global observer was removed for perf, so each container that owns
  // a slider has to opt in. The CQ slider (line ~333) needs this to show
  // correctly on first open.
  const modalRef = useRef<HTMLDivElement>(null);
  useRangeFill(modalRef);
  const dialog = useDialog(modalRef, onCancel, t("scannerModals:estimate.title"));

  // Connect WebSocket on mount for test encode progress (must be open before test starts)
  useEffect(() => {
    let cancelled = false;
    wsUrl("/ws").then(url => {
      if (cancelled) return;
      const ws = new WebSocket(url);
      wsRef.current = ws;
      ws.onmessage = (e) => {
        try {
          const msg = JSON.parse(e.data);
          if (msg.type === "test_encode_progress") {
            setTestProgress(msg.progress || 0);
            if (msg.step) setTestStep(msg.step);
          }
        } catch {}
      };
    }).catch(() => {});
    return () => { cancelled = true; wsRef.current?.close(); wsRef.current = null; };
  }, []);
  const [audioBr, setAudioBr] = useState<number | null>(null);
  const [resolution, setResolution] = useState<string | null>(null);
  const [forceReencode, setForceReencode] = useState(false);
  // "No video conversion" — runs only the audio/sub cleanup pass.
  // Mutually exclusive with forceReencode: the two are direct opposites
  // so checking one auto-clears the other. v0.3.80+.
  const [cleanupOnly, setCleanupOnly] = useState(false);

  // Effective encoder for UI branching — explicit override wins, otherwise
  // follow the user's configured default. Previously we compared against
  // "libx265" literally, which made auto always fall into the NVENC branch
  // even on CPU-only installs.
  const effectiveEncoder = encoder ?? defaultEncoder;
  const isCpu = effectiveEncoder === "libx265";
  // QSV / VAAPI don't share the NVENC CQ/preset slider or the libx265
  // CRF/preset dropdown — they have their own quality/preset settings
  // managed centrally in Settings → Encoding. For per-batch overrides
  // we'd need new sliders; for v0.3.69 we just hide the legacy controls
  // and let the global QSV/VAAPI settings apply. v0.3.69+.
  const isHwHevc = effectiveEncoder === "qsv" || effectiveEncoder === "vaapi" || effectiveEncoder === "videotoolbox";

  // Debounced estimate refresh
  useEffect(() => {
    if (!settingsRead) return;
    setLoading(true);
    const overrides: Record<string, any> = {};
    if (cq !== null) {
      if (isCpu) overrides.libx265_crf_override = cq;
      else overrides.nvenc_cq_override = cq;
    }
    if (forceReencode) overrides.force_reencode = true;
    if (cleanupOnly) overrides.cleanup_only = true;  // the estimate converts nothing either
    if (activeFilter && activeFilter !== "all") overrides.filter = activeFilter;
    if (encoder) overrides.encoder = encoder;
    if (resolution !== null) overrides.target_resolution_override = resolution;  // the estimate resizes too
    if (directPlay) {
      overrides.direct_play = true;
      overrides.direct_play_fixes = dpFixes;
    }
    estimateJobs(filePaths, overrideRules, overrides).then(data => {
      setEstimate(data);
      setLoading(false);
    }).catch(err => {
      console.error("Estimate failed:", err);
      setEstimate({ total_files: 0, error: err?.message || t("scannerModals:estimate.unknownError") });
      setLoading(false);
    });
  }, [settingsRead, overrideRules, cq, forceReencode, cleanupOnly, isCpu, encoder, directPlay, dpFixes, resolution]);
  const buildOverrides = (): EncodingOverrides | undefined => {
    const o: EncodingOverrides = {};
    if (encoder !== null) o.encoder = encoder;
    if (preset !== null) {
      if (isCpu) o.libx265_preset = preset;
      else o.nvenc_preset = preset;
    }
    if (cq !== null) {
      if (isCpu) o.libx265_crf = cq;
      else o.nvenc_cq = cq;
    }
    if (audioCdc !== null) o.audio_codec = audioCdc;
    if (audioBr !== null) o.audio_bitrate = audioBr;
    if (resolution !== null) o.target_resolution = resolution;
    if (forceReencode) o.force_reencode = true;
    if (cleanupOnly) o.cleanup_only = true;
    o.direct_play = directPlay;  // either way: unticked overrides the setting
    if (directPlay) o.direct_play_fixes = dpFixes;
    return o;
  };
  // A clean-up's Dolby Vision (v0.10.0): removed under "Convert to HDR10";
  // with Cleanup only, also why it's kept (files it leaves out included).
  const dolbyVisionRows = (dv: Record<string, number> = {}) =>
    ["removed", "kept", "ffmpeg", "profile5"]
      .filter(outcome => (dv[outcome] || 0) > 0 && (outcome === "removed" || cleanupOnly))
      .map(outcome => t(`scannerModals:estimate.whatHappens.dolbyVision.${outcome}`,
        { count: dv[outcome], num: fmtNum(dv[outcome]) }));
  // The VMAF target's files (v0.10.0): how many convert with a search, per
  // target, and how many a rule's Off keeps at a fixed quality. Both keys are
  // missing from the estimate's error fallback.
  const vmafTargets: [string, number][] = Object.entries((estimate?.vmaf_target?.targets || {}) as Record<string, number>)
    .filter(([, n]) => n > 0).sort(([a], [b]) => Number(a) - Number(b));
  const vmafFixed: number = estimate?.vmaf_target?.fixed_by_rule || 0;
  // One file's search: the median of the recorded ones, else 3 minutes.
  const searchTime = formatTime(estimate?.vmaf_target_search_seconds ?? 180);

  return (
    <div style={{
      position: "fixed", inset: 0, background: "rgba(0,0,0,0.7)", display: "flex",
      alignItems: "center", justifyContent: "center", zIndex: 1000,
    }} onClick={e => { if (e.target === e.currentTarget) onCancel(); }}>
      <div ref={modalRef} {...dialog} style={{
        background: "var(--bg-card)", borderRadius: 8, padding: "20px", width: "95%", maxWidth: 620,
        maxHeight: "90vh", overflowY: "auto", boxSizing: "border-box" as const,
        border: "1px solid var(--border)",
      }}>
        <h3 style={{ color: "white", margin: "0 0 16px", fontSize: 16 }}>{t("scannerModals:estimate.title")}</h3>

        {/* Override rules toggle */}
        {hasIgnoredFiles && (
          <div style={{
            background: "rgba(255,169,77,0.1)", border: "1px solid rgba(255,169,77,0.2)",
            borderRadius: 4, padding: "10px 14px", marginBottom: 14,
            display: "flex", alignItems: "center", gap: 10,
          }}>
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="var(--caution)" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={{ flexShrink: 0 }}>
              <path d="M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/>
            </svg>
            <div style={{ flex: 1 }}>
              <div style={{ fontSize: 12, color: "var(--caution)", fontWeight: 500 }}>{t("scannerModals:estimate.rulesWarningTitle")}</div>
              <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 2 }}>
                {t("scannerModals:estimate.rulesWarningHelp")}
              </div>
            </div>
            <label style={{ display: "flex", alignItems: "center", gap: 6, cursor: "pointer", flexShrink: 0 }}>
              <input type="checkbox" checked={overrideRules} onChange={() => setOverrideRules(!overrideRules)}
                style={{ accentColor: "var(--accent)" }} />
              <span style={{ fontSize: 11, color: "var(--text-secondary)", whiteSpace: "nowrap" }}>{t("scannerModals:estimate.overrideRules")}</span>
            </label>
          </div>
        )}

        {loading ? (
          <div style={{ display: "flex", alignItems: "center", gap: 12, padding: 20, justifyContent: "center" }}>
            <div className="spinner" style={{ width: 20, height: 20 }} />
            <span style={{ color: "var(--text-muted)", fontSize: 13 }}>{t("scannerModals:estimate.estimating")}</span>
          </div>
        ) : estimate ? (
          <>
            {/* Summary */}
            <div className="estimate-summary-grid" style={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: 12, marginBottom: 16 }}>
              <div style={{ background: "var(--bg-primary)", padding: 12, borderRadius: 4, textAlign: "center" }}>
                <div style={{ fontSize: 22, fontWeight: "bold", color: "white" }}>{fmtNum(estimate.total_files)}</div>
                <div style={{ fontSize: 11, color: "var(--text-muted)" }}>{t("scannerModals:estimate.filesToProcess")}</div>
              </div>
              <div style={{ background: "var(--bg-primary)", padding: 12, borderRadius: 4, textAlign: "center" }}>
                <div style={{ fontSize: 22, fontWeight: "bold", color: "var(--success)" }}>~{fmtBytes(estimate.estimated_savings)}</div>
                <div style={{ fontSize: 11, color: "var(--text-muted)" }}>{t("scannerModals:estimate.estSavings", { pct: estimate.savings_pct })}</div>
                {estimate.learned_from != null && (
                  <div style={{ fontSize: 10, color: "var(--text-muted)", marginTop: 2 }}>
                    {estimate.learned_from > 0 ? t("scannerModals:estimate.learnedFrom", { count: estimate.learned_from })
                      : t("scannerModals:estimate.typicalSavings")}
                  </div>
                )}
                {/* The search picks each target file's quality: its savings are at the start. */}
                {vmafTargets.length > 0 && (
                  <div style={{ fontSize: 10, color: "var(--text-muted)", marginTop: 2 }}>
                    {t("scannerModals:estimate.savingsAtStartingQuality")}
                  </div>
                )}
              </div>
              <div style={{ background: "var(--bg-primary)", padding: 12, borderRadius: 4, textAlign: "center" }}>
                <div style={{ fontSize: 22, fontWeight: "bold", color: "var(--accent-text)" }}>~{formatTime(estimate.estimated_time_seconds)}</div>
                <div style={{ fontSize: 11, color: "var(--text-muted)" }}>{t("scannerModals:estimate.estTime")}</div>
              </div>
            </div>

            {/* Breakdown */}
            {(estimate.total_files > 0) && (
              <div className="estimate-breakdown-grid" style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12, marginBottom: 16 }}>
                <div style={{ background: "var(--bg-primary)", padding: 12, borderRadius: 4 }}>
                  <div style={{ fontSize: 11, color: "var(--text-muted)", marginBottom: 6, fontWeight: 600 }}>{t("scannerModals:estimate.byJobType")}</div>
                  {(() => {
                    const bt = estimate.by_type || {};
                    const conv = (bt.convert || 0) + (bt.combined || 0);
                    const cleanup = (bt.audio || 0) + (bt.combined || 0);
                    const items: [string, number][] = [];
                    if (conv > 0) items.push([t("scannerModals:estimate.conversions"), conv]);
                    if (cleanup > 0) items.push([t("scannerModals:estimate.cleanups"), cleanup]);
                    if (items.length === 0) {
                      Object.entries(bt).filter(([, v]) => (v as number) > 0).forEach(([k, v]) => items.push([k, v as number]));
                    }
                    return items.map(([label, count]) => (
                      <div key={label} style={{ display: "flex", justifyContent: "space-between", fontSize: 12, marginTop: 3 }}>
                        <span style={{ color: "var(--text-muted)" }}>{label}</span>
                        <span style={{ color: "var(--text-secondary)", fontWeight: 500 }}>{fmtNum(count)}</span>
                      </div>
                    ));
                  })()}
                </div>
                <div style={{ background: "var(--bg-primary)", padding: 12, borderRadius: 4 }}>
                  <div style={{ fontSize: 11, color: "var(--text-muted)", marginBottom: 6, fontWeight: 600 }}>{t("scannerModals:estimate.bySource")}</div>
                  {Object.entries(estimate.by_source || {}).sort(([,a], [,b]) => (b as number) - (a as number)).map(([k, v]) => (
                    <div key={k} style={{ display: "flex", justifyContent: "space-between", fontSize: 12, marginTop: 3 }}>
                      <span style={{ color: "var(--text-muted)" }}>{k}</span>
                      <span style={{ color: "var(--text-secondary)", fontWeight: 500 }}>{fmtNum(v as number)}</span>
                    </div>
                  ))}
                </div>
              </div>
            )}

            {/* Resolution breakdown */}
            {estimate.resolution_breakdown && Object.values(estimate.resolution_breakdown).some((v: any) => v > 0) && (
              <div className="estimate-breakdown-grid" style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12, marginBottom: 16 }}>
                {estimate.resolution_breakdown && (
                  <div style={{ background: "var(--bg-primary)", padding: 12, borderRadius: 4 }}>
                    <div style={{ fontSize: 11, color: "var(--text-muted)", marginBottom: 6, fontWeight: 600 }}>{t("scannerModals:estimate.byResolution")}</div>
                    {(["4k", "1080p", "720p", "sd"] as const).map(res => {
                      const count = estimate.resolution_breakdown[res] || 0;
                      if (!count) return null;
                      return (
                        <div key={res} style={{ display: "flex", justifyContent: "space-between", fontSize: 12, marginTop: 3 }}>
                          <span style={{ color: "var(--text-muted)" }}>{res === "sd" ? "SD" : res.toUpperCase()}</span>
                          <span style={{ color: "var(--text-secondary)", fontWeight: 500 }}>{fmtNum(count)}</span>
                        </div>
                      );
                    })}
                  </div>
                )}
              </div>
            )}

            {estimate.total_files === 0 && (
              <div style={{ textAlign: "center", padding: 16, color: "var(--text-muted)", fontSize: 13 }}>
                {t("scannerModals:estimate.noActionable")}
                {(estimate.ignored_files > 0 || estimate.skipped_by_rules > 0) && (
                  <div style={{ marginTop: 8 }}>
                    {estimate.ignored_files > 0 && (
                      <span><Trans
                        i18nKey="scannerModals:estimate.ignoredFiles"
                        count={estimate.ignored_files}
                        values={{ num: fmtNum(estimate.ignored_files) }}
                        components={{ badge: <span style={{ background: "var(--bg-tertiary)", color: "var(--text-secondary)", padding: "1px 6px", borderRadius: 3, fontSize: 11 }} /> }}
                      /></span>
                    )}
                    {estimate.ignored_files > 0 && estimate.skipped_by_rules > 0 && <span> · </span>}
                    {estimate.skipped_by_rules > 0 && (
                      <span style={{ color: "var(--caution)" }}>{t("scannerModals:estimate.skippedByRules", { num: fmtNum(estimate.skipped_by_rules) })}</span>
                    )}
                  </div>
                )}
                {dolbyVisionRows(estimate.dolby_vision).map((row, i) => (
                  <div key={i} style={{ marginTop: 8 }}>{row}</div>
                ))}
              </div>
            )}
            {estimate.total_files === 0 && (estimate.ignored_files > 0 || estimate.skipped_by_rules > 0 || estimate.hardlinked > 0 || estimate.leaving_soon > 0) && (
              <div style={{ textAlign: "center", marginBottom: 12 }}>
                <label style={{ display: "inline-flex", alignItems: "center", gap: 8, cursor: "pointer", fontSize: 12, color: "var(--text-secondary)" }}>
                  <input type="checkbox" checked={overrideRules}
                    onChange={() => setOverrideRules(!overrideRules)}
                    style={{ accentColor: "var(--accent)" }} />
                  {t("scannerModals:estimate.includeIgnored")}
                </label>
              </div>
            )}

            {/* What will happen (v0.10.0): which encoder runs, what becomes of
                the originals, which tracks are removed — before anything is
                queued. */}
            {estimate.total_files > 0 && estimate.encoder && (() => {
              const audio = estimate.removals?.audio || {};
              const subs = estimate.removals?.subtitles || {};
              const enc = estimate.encoder;
              const encName = (e: string) => t(`settingsMedia:video.encoderNames.${e}`, { defaultValue: e });
              const orig = estimate.originals || {};
              // Nothing converts (v0.10.0) — Cleanup only, or only cleanups to do:
              // it said "Encodes with NVENC" regardless.
              const bt = estimate.by_type || {};
              const converts = cleanupOnly ? 0 : (bt.convert || 0) + (bt.combined || 0);
              const rows: React.ReactNode[] = [
                converts === 0
                  ? t("scannerModals:estimate.whatHappens.noEncode")
                  : enc.runs_here !== enc.requested
                    ? t("scannerModals:estimate.whatHappens.encoderSwapped", { runs: encName(enc.runs_here), requested: encName(enc.requested) })
                    : t("scannerModals:estimate.whatHappens.encoder", { encoder: encName(enc.runs_here) }),
                orig.action === "keep"
                  ? t("scannerModals:estimate.whatHappens.originalsKeep", { count: orig.days })
                  : orig.action === "trash"
                    ? t("scannerModals:estimate.whatHappens.originalsTrash")
                    : t("scannerModals:estimate.whatHappens.originalsDelete"),
              ];
              for (const [target, n] of vmafTargets) rows.push(t("scannerModals:estimate.whatHappens.vmafTarget", { target, count: n, num: fmtNum(n), time: searchTime }));
              if (vmafFixed > 0) rows.push(t("scannerModals:estimate.whatHappens.vmafFixed", { count: vmafFixed, num: fmtNum(vmafFixed) }));
              // SRT copies take the place of the image subtitles they're read
              // from, unless those are kept (v0.10.0: "no tracks are removed"
              // read beside them).
              const srtReplaces = directPlay && dpFixes.includes("subs") && (estimate.direct_play?.subs || 0) > 0
                && estimate.image_subs_keep_original === false;
              if (Object.keys(audio).length) rows.push(t("scannerModals:estimate.whatHappens.audioRemoved", { list: trackLanguageList(audio) }));
              if (Object.keys(subs).length) rows.push(t("scannerModals:estimate.whatHappens.subsRemoved", { list: trackLanguageList(subs) }));
              if (!Object.keys(audio).length && !Object.keys(subs).length && !srtReplaces) rows.push(t("scannerModals:estimate.whatHappens.nothingRemoved"));
              let fixing = false;
              if (directPlay) {
                // An old container is remuxed, or becomes an MKV by converting
                // (Cleanup only converts none).
                const box = estimate.direct_play_container || { remux: 0, convert: 0 };
                const counts: Record<string, number> = {
                  ...estimate.direct_play,
                  container: cleanupOnly ? box.remux + box.convert : box.remux,
                  containerConverted: cleanupOnly ? 0 : box.convert,
                };
                for (const fix of ["container", "containerConverted", "video", "subs", "audio"]) {
                  const n = counts[fix] || 0;
                  const ticked = dpFixes.includes(fix === "containerConverted" ? "container" : fix);
                  if (n > 0 && ticked && !(fix === "video" && cleanupOnly)) {
                    rows.push(t(`scannerModals:estimate.whatHappens.directPlay.${fix === "subs" && srtReplaces ? "subsReplace" : fix}`,
                      { count: n, num: fmtNum(n) }));
                    fixing = true;
                  }
                }
              }
              rows.push(...dolbyVisionRows(estimate.dolby_vision));
              // (a file with a fix to make is queued for it)
              if (cleanupOnly) rows.push(t(`scannerModals:estimate.whatHappens.${fixing ? "cleanupSkipsFixes" : "cleanupSkips"}`));
              if (estimate.hardlinked > 0) rows.push(t("scannerModals:estimate.whatHappens.hardlinked", { count: estimate.hardlinked, num: fmtNum(estimate.hardlinked) }));
              if (estimate.leaving_soon > 0) rows.push(t("scannerModals:estimate.whatHappens.leavingSoon", { count: estimate.leaving_soon, num: fmtNum(estimate.leaving_soon) }));
              // Converted before and came out larger (v0.10.0): converted again now
              const larger = estimate.came_out_larger;
              if (larger?.files > 0) {
                const qualities = (larger.qualities || []).map(triedQuality).join(", ");
                rows.push(t(qualities ? "scannerModals:estimate.whatHappens.cameOutLargerAt" : "scannerModals:estimate.whatHappens.cameOutLarger",
                  { count: larger.files, num: fmtNum(larger.files), qualities }));
              }
              return (
                <div style={{ background: "var(--bg-primary)", borderRadius: 6, padding: "10px 12px", marginBottom: 16 }}>
                  <div style={{ fontSize: 11, color: "var(--text-muted)", marginBottom: 6, fontWeight: 600 }}>{t("scannerModals:estimate.whatHappens.title")}</div>
                  <ul style={{ margin: 0, paddingLeft: 16, display: "flex", flexDirection: "column", gap: 3 }}>
                    {rows.map((r, i) => (
                      <li key={i} style={{ fontSize: 12, color: orig.action === "delete" && i === 1 ? "var(--warning)" : "var(--text-secondary)" }}>{r}</li>
                    ))}
                  </ul>
                </div>
              );
            })()}

            {/* Fix for direct play (v0.10.0): what makes media servers
                transcode, fixed where the files need it — each fix with the
                files it touches; one none of them need is greyed out. */}
            {estimate.direct_play && (() => {
              const dp: Record<string, number> = estimate.direct_play;
              const unavailable = (fix: string) => !dp[fix] || (fix === "video" && cleanupOnly);
              const files = (n: number) => t("scannerModals:estimate.directPlay.files", { count: n, num: fmtNum(n) });
              return (
                <div style={{ border: "1px solid var(--border)", borderRadius: 6, padding: "10px 12px", marginBottom: 16 }}>
                  <label style={{ display: "flex", alignItems: "center", gap: 8, cursor: "pointer" }}>
                    <input type="checkbox" checked={directPlay} onChange={e => setDirectPlay(e.target.checked)}
                      style={{ accentColor: "var(--accent)", margin: 0 }} />
                    <span style={{ fontSize: 12, fontWeight: 600, color: "var(--text-primary)" }}>{t("scannerModals:estimate.directPlay.title")}</span>
                    <span style={{ marginLeft: "auto", fontSize: 11, color: "var(--text-muted)", fontVariantNumeric: "tabular-nums" }}>
                      {files(estimate.direct_play_files || 0)}
                    </span>
                  </label>
                  <div style={{ fontSize: 11, color: "var(--text-muted)", margin: "2px 0 0 21px" }}>{t("scannerModals:estimate.directPlay.hint")}</div>
                  {directPlay && (
                    <div style={{ display: "flex", flexDirection: "column", gap: 7, padding: "9px 0 2px 21px" }}>
                      {DP_FIXES.map(fix => {
                        const off = unavailable(fix);
                        return (
                          <label key={fix} title={fix === "video" && cleanupOnly ? t("scannerModals:estimate.directPlay.videoCleanupOnly") : undefined}
                            style={{ display: "flex", alignItems: "center", gap: 8, cursor: off ? "default" : "pointer", opacity: off ? 0.45 : 1 }}>
                            <input type="checkbox" disabled={off} checked={!off && dpFixes.includes(fix)}
                              onChange={e => setDpFixes(f => e.target.checked ? [...f, fix] : f.filter(x => x !== fix))}
                              style={{ accentColor: "var(--accent)", margin: 0 }} />
                            <span style={{ fontSize: 12, color: "var(--text-secondary)" }}>{t(`scannerModals:estimate.directPlay.${fix}`)}</span>
                            <span style={{ marginLeft: "auto", fontSize: 11, color: "var(--text-muted)", fontVariantNumeric: "tabular-nums" }}>
                              {files(dp[fix] || 0)}
                            </span>
                          </label>
                        );
                      })}
                    </div>
                  )}
                </div>
              );
            })()}

            {/* Priority selector */}
            <div style={{ marginBottom: 16 }}>
              <div style={{ fontSize: 12, color: "var(--text-muted)", marginBottom: 6 }}>{t("scannerModals:estimate.priority")}</div>
              <div style={{ display: "flex", gap: 8 }}>
                {PRIORITY_KEYS.map((labelKey, i) => (
                  <button key={i}
                    style={{
                      padding: "5px 14px", borderRadius: 4, fontSize: 12, cursor: "pointer",
                      border: priority === i ? `1px solid ${PRIORITY_COLORS[i]}` : "1px solid var(--border)",
                      background: priority === i ? `color-mix(in srgb, ${PRIORITY_COLORS[i]} 13%, transparent)` : "var(--bg-primary)",
                      color: priority === i ? PRIORITY_COLORS[i] : "var(--text-secondary)",
                      fontWeight: priority === i ? 600 : 400,
                    }}
                    onClick={() => setPriority(i)}
                  >{t(`scannerModals:estimate.priorities.${labelKey}`)}</button>
                ))}
              </div>
            </div>

            {/* Encoding Settings (expandable) */}
            <div style={{ marginBottom: 16 }}>
              <button
                onClick={() => setSettingsOpen(!settingsOpen)}
                style={{
                  background: "none", border: "1px solid var(--border)", color: "var(--text-muted)",
                  padding: "6px 12px", borderRadius: 4, fontSize: 12, cursor: "pointer", width: "100%",
                  textAlign: "left", display: "flex", alignItems: "center", gap: 6,
                }}
              >
                <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" style={{ transform: settingsOpen ? "rotate(90deg)" : "none", transition: "transform 0.15s" }}>
                  <polyline points="9 18 15 12 9 6"/>
                </svg>
                {t("scannerModals:overrides.encodingSettings")}
                <span style={{ marginLeft: "auto", color: "var(--text-muted)", fontSize: 11 }}>
                  {cq !== null || encoder !== null || preset !== null || audioCdc !== null ? t("scannerModals:overrides.custom") : t("scannerModals:overrides.defaultSettings")}
                </span>
              </button>

              {settingsOpen && (
                <div style={{ background: "var(--bg-primary)", borderRadius: "0 0 4px 4px", padding: 14, display: "flex", flexDirection: "column", gap: 10 }}>
                  {/* Encoder */}
                  <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
                    <label style={{ fontSize: 12, color: "var(--text-muted)", width: 80, flexShrink: 0 }}>{t("scannerModals:overrides.encoder")}</label>
                    <select aria-label={t("scannerModals:overrides.encoder")} value={encoder ?? ""} onChange={e => { setEncoder(e.target.value || null); setPreset(null); }}
                      style={{ flex: 1, backgroundColor: "var(--bg-card)", color: "var(--text-secondary)", border: "1px solid var(--border)", padding: "4px 8px", borderRadius: 4, fontSize: 12 }}>
                      <option value="">{t("scannerModals:overrides.auto")}</option>
                      {(encoderCaps?.nvenc ?? true) && <option value="nvenc">NVENC (NVIDIA GPU)</option>}
                      {encoderCaps?.qsv && <option value="qsv">Intel QSV</option>}
                      {encoderCaps?.vaapi && <option value="vaapi">VAAPI (Intel/AMD)</option>}
                      {encoderCaps?.videotoolbox && <option value="videotoolbox">VideoToolbox (Mac)</option>}
                      <option value="libx265">libx265 (CPU)</option>
                    </select>
                  </div>

                  {/* Quality CQ/CRF — hidden for QSV/VAAPI which use their own
                      global settings (Settings → Encoding). Per-batch QSV/VAAPI
                      overrides aren't surfaced in v0.3.69; could add later if
                      requested. */}
                  {!isHwHevc && <>
                  <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
                    <label style={{ fontSize: 12, color: "var(--text-muted)", width: 80, flexShrink: 0 }}>{t("scannerModals:overrides.quality", { mode: isCpu ? "CRF" : "CQ" })}</label>
                    <div style={{ flex: 1, display: "flex", alignItems: "center", gap: 8 }}>
                      <input type="range" min={15} max={40} value={cqSlider ?? cq ?? (estimate?.cq || 20)}
                        aria-label={t("scannerModals:overrides.quality", { mode: isCpu ? "CRF" : "CQ" })}
                        onChange={e => setCqSlider(Number(e.target.value))}
                        onMouseUp={e => { const v = Number((e.target as HTMLInputElement).value); setCq(v); setCqSlider(null); }}
                        onTouchEnd={e => { const v = Number((e.target as HTMLInputElement).value); setCq(v); setCqSlider(null); }}
                        // FE#23: arrow-key changes were shown but never applied.
                        onKeyUp={e => { const v = Number((e.target as HTMLInputElement).value); setCq(v); setCqSlider(null); }}
                        style={{ flex: 1, accentColor: "var(--accent)" }} />
                      <span style={{ fontSize: 12, color: (cqSlider ?? cq) !== null ? "white" : "var(--text-muted)", fontWeight: 600, width: 24, textAlign: "center" }}>
                        {cqSlider ?? cq ?? (estimate?.cq || 20)}
                      </span>
                      {cq !== null && (
                        <button onClick={() => setCq(null)} className="btn btn-xs btn-ghost">
                          {t("scannerModals:overrides.reset")}
                        </button>
                      )}
                    </div>
                  </div>
                  {/* With a VMAF target the quality only starts the search (v0.10.0). */}
                  {vmafTargets.length > 0 && (
                    <div style={{ fontSize: 11, color: "var(--text-muted)", padding: "0 0 0 90px" }}>
                      {t("scannerModals:overrides.qualityStartsSearch")}
                    </div>
                  )}
                  </>}

                  {/* Preset — hidden for QSV/VAAPI for the same reason as
                      Quality above. */}
                  {!isHwHevc && (
                  <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
                    <label style={{ fontSize: 12, color: "var(--text-muted)", width: 80, flexShrink: 0 }}>{t("scannerModals:overrides.preset")}</label>
                    <select aria-label={t("scannerModals:overrides.preset")} value={preset ?? ""} onChange={e => setPreset(e.target.value || null)}
                      style={{ flex: 1, backgroundColor: "var(--bg-card)", color: "var(--text-secondary)", border: "1px solid var(--border)", padding: "4px 8px", borderRadius: 4, fontSize: 12 }}>
                      <option value="">{t("scannerModals:overrides.auto")}</option>
                      {isCpu ? (
                        CPU_PRESETS.map(p => <option key={p.value} value={p.value}>{p.suffixKey ? `${p.label} ${t(`scannerModals:overrides.${p.suffixKey}`)}` : p.label}</option>)
                      ) : (
                        <>
                          {NVENC_PRESET_IDS.map(id => (
                            <option key={id} value={id}>{`${id.toUpperCase()} — ${t(`scannerModals:overrides.nvencPresets.${id}`)}`}</option>
                          ))}
                        </>
                      )}
                    </select>
                  </div>
                  )}
                  {isHwHevc && (
                    <div style={{ fontSize: 11, color: "var(--text-muted)", padding: "4px 0 0 90px" }}>
                      {t("scannerModals:overrides.hwHevcNote")}
                    </div>
                  )}

                  {/* Audio Codec */}
                  <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
                    <label style={{ fontSize: 12, color: "var(--text-muted)", width: 80, flexShrink: 0 }}>{t("scannerModals:overrides.audio")}</label>
                    <select aria-label={t("scannerModals:overrides.audio")} value={audioCdc ?? ""} onChange={e => setAudioCdc(e.target.value || null)}
                      style={{ flex: 1, backgroundColor: "var(--bg-card)", color: "var(--text-secondary)", border: "1px solid var(--border)", padding: "4px 8px", borderRadius: 4, fontSize: 12 }}>
                      <option value="">{t("scannerModals:overrides.auto")}</option>
                      <option value="copy">{t("scannerModals:overrides.audioCopy")}</option>
                      <option value="eac3">EAC3 (Dolby Digital+)</option>
                      <option value="ac3">AC3 (Dolby Digital)</option>
                      <option value="aac">AAC</option>
                      <option value="opus">Opus</option>
                    </select>
                    {audioCdc && audioCdc !== "copy" && (
                      <select aria-label={t("common:labels.bitrate")} value={audioBr ?? ""} onChange={e => setAudioBr(e.target.value ? Number(e.target.value) : null)}
                        style={{ width: 80, backgroundColor: "var(--bg-card)", color: "var(--text-secondary)", border: "1px solid var(--border)", padding: "4px 8px", borderRadius: 4, fontSize: 12 }}>
                        <option value="">{t("scannerModals:overrides.auto")}</option>
                        <option value="96">96k</option>
                        <option value="128">128k</option>
                        <option value="192">192k</option>
                        <option value="256">256k</option>
                        <option value="320">320k</option>
                        <option value="640">640k</option>
                      </select>
                    )}
                  </div>

                  {/* Resolution */}
                  <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
                    <label style={{ fontSize: 12, color: "var(--text-muted)", width: 80, flexShrink: 0 }}>{t("scannerModals:overrides.resolution")}</label>
                    <select aria-label={t("scannerModals:overrides.resolution")} value={resolution ?? ""} onChange={e => setResolution(e.target.value || null)}
                      style={{ flex: 1, backgroundColor: "var(--bg-card)", color: "var(--text-secondary)", border: "1px solid var(--border)", padding: "4px 8px", borderRadius: 4, fontSize: 12 }}>
                      <option value="">{t("scannerModals:overrides.autoKeepOriginal")}</option>
                      <option value="1080p">1080p</option>
                      <option value="720p">720p</option>
                      <option value="480p">480p</option>
                    </select>
                  </div>

                  {/* Force re-encode — mutually exclusive with cleanupOnly
                      below. Checking force_reencode auto-clears cleanup_only
                      and vice versa. v0.3.80+. */}
                  <div style={{ display: "flex", alignItems: "center", gap: 10, marginTop: 6 }}>
                    <label style={{ fontSize: 12, color: "var(--text-muted)", width: 80, flexShrink: 0 }}>{t("scannerModals:overrides.reencode")}</label>
                    <label style={{ display: "flex", alignItems: "center", gap: 6, cursor: "pointer" }}>
                      <input type="checkbox" checked={forceReencode} onChange={(e) => {
                        setForceReencode(e.target.checked);
                        if (e.target.checked) setCleanupOnly(false);
                      }}
                        style={{ accentColor: "var(--accent)" }} />
                      <span style={{ fontSize: 11, color: forceReencode ? "var(--text-secondary)" : "var(--text-muted)" }}>
                        {t("scannerModals:overrides.forceReencode")}
                      </span>
                    </label>
                  </div>

                  {/* Cleanup-only toggle — runs the audio/sub cleanup pass
                      but skips video re-encode entirely. Same effect as a
                      rule with action="ignore", expressed per-batch. v0.3.80+. */}
                  <div style={{ display: "flex", alignItems: "center", gap: 10, marginTop: 4 }}>
                    <label style={{ fontSize: 12, color: "var(--text-muted)", width: 80, flexShrink: 0 }}>{t("scannerModals:overrides.audioSub")}</label>
                    <label style={{ display: "flex", alignItems: "center", gap: 6, cursor: "pointer" }}>
                      <input type="checkbox" checked={cleanupOnly} onChange={(e) => {
                        setCleanupOnly(e.target.checked);
                        if (e.target.checked) setForceReencode(false);
                      }}
                        style={{ accentColor: "var(--accent)" }} />
                      <span style={{ fontSize: 11, color: cleanupOnly ? "var(--text-secondary)" : "var(--text-muted)" }}>
                        {t("scannerModals:overrides.cleanupOnly")}
                      </span>
                    </label>
                  </div>

                  {/* Test Encode */}
                  <div style={{ borderTop: "1px solid var(--border)", paddingTop: 10, marginTop: 4 }}>
                    <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
                      <button
                        className="btn btn-xs btn-secondary" style={{ whiteSpace: "nowrap" }}
                        disabled={testRunning || filePaths.length === 0}
                        onClick={async () => {
                          setTestRunning(true);
                          setTestResult(null);
                          setTestStep("encoding");
                          setTestProgress(0);
                          try {
                            let testFile = filePaths.find(p => !p.endsWith("/")) || filePaths[0];
                            const result = await startTestEncode(
                              testFile,
                              encoder ?? undefined,
                              cq ?? (estimate?.cq || 20),
                              preset ?? undefined,
                            );
                            setTestResult(result);
                          } catch (err: any) {
                            setTestResult({ status: "failed", error: err.message });
                          }
                          setTestRunning(false);
                        }}
                      >
                        {testRunning ? (
                          <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                            <div className="spinner" style={{ width: 12, height: 12 }} />
                            {testStep === "extracting" ? t("scannerModals:test.extracting") :
                             testStep === "encoding" ? t("scannerModals:test.encoding") :
                             testStep === "analyzing" ? t("scannerModals:test.analyzing") : t("scannerModals:test.starting")}
                          </span>
                        ) : t("scannerModals:test.button")}
                      </button>
                      {testRunning && (
                        <div style={{ flex: 1, display: "flex", alignItems: "center", gap: 8 }}>
                          <div className="progress-bar-track" style={{ flex: 1, height: 4 }}>
                            <div className="progress-bar-fill" style={{ width: `${testProgress}%` }} />
                          </div>
                          <span style={{ fontSize: 10, color: "var(--accent-text)", fontWeight: 600, whiteSpace: "nowrap" }}>
                            {testProgress.toFixed(0)}%
                          </span>
                        </div>
                      )}
                      {testResult && testResult.status === "complete" && (
                        <div style={{ fontSize: 11, display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
                          <span style={{ color: "var(--text-secondary)" }}>
                            {fmtBytes(testResult.original_size)} → {fmtBytes(testResult.encoded_size)}
                          </span>
                          <span style={{
                            fontWeight: 600,
                            color: testResult.ratio > 50 ? "var(--success)" : testResult.ratio > 30 ? "var(--accent-text)" : "var(--text-muted)",
                          }}>
                            {t("scannerModals:test.savings", { pct: testResult.ratio })}
                          </span>
                          {testResult.vmaf_score != null && (
                            <span style={{
                              padding: "1px 6px", borderRadius: 3, fontSize: 10, fontWeight: 600,
                              background: vmafTintBg(testResult.vmaf_score),
                              color: vmafColor(testResult.vmaf_score),
                            }}>
                              VMAF {testResult.vmaf_score} ({testResult.vmaf_label})
                            </span>
                          )}
                          {testResult.encoding_fps > 0 && (
                            <span style={{ color: "var(--text-muted)", fontSize: 10 }}>
                              {testResult.encoding_fps} fps
                            </span>
                          )}
                        </div>
                      )}
                      {testResult && testResult.status === "failed" && (
                        <span style={{ fontSize: 11, color: "var(--danger)" }}>
                          {t("scannerModals:test.failed", { error: testResult.error?.slice(0, 80) })}
                        </span>
                      )}
                    </div>
                  </div>
                </div>
              )}
            </div>
          </>
        ) : (
          <div style={{ color: "var(--text-muted)", padding: 20, textAlign: "center" }}>
            {t("scannerModals:estimate.failed")}
            {estimate?.error && <div style={{ fontSize: 11, color: "var(--danger)", marginTop: 8 }}>{estimate.error}</div>}
          </div>
        )}

        {/* Actions */}
        <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
          <button className="btn btn-sm btn-secondary" onClick={onCancel}>
            {t("common:actions.cancel")}
          </button>
          <button className="btn btn-sm btn-secondary"
            disabled={loading || !estimate || (estimate.total_files === 0 && !overrideRules)}
            onClick={() => onConfirm(priority, overrideRules, buildOverrides(), true)}
          >
            {t("scannerModals:estimate.addAndStart")}
          </button>
          <button className="btn btn-sm btn-primary"
            disabled={loading || !estimate || (estimate.total_files === 0 && !overrideRules)}
            onClick={() => onConfirm(priority, overrideRules, buildOverrides())}
          >
            {t("scannerModals:estimate.addToQueue", { num: fmtNum(estimate?.total_files || filePaths.length) })}
          </button>
        </div>
      </div>
    </div>
  );
}
