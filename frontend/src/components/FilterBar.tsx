import { useTranslation } from "react-i18next";
import { decodeAdvanced } from "../advancedSearch";

interface FilterBarProps {
  /** Filter ids; "!id" excludes. */
  activeFilters: string[];
  onFilterToggle: (filter: string, exclude: boolean) => void;
  newCount?: number;
  counts?: Record<string, number>;
}

// `labelKey` is a translation key resolved at render time; `label` is a
// literal kept for pure tech tokens (codecs, resolutions, source tags).
const FILTERS: { key: string; label?: string; labelKey?: string; group?: string }[] = [
  { key: "all", labelKey: "scanner:filters.all" },
  { key: "new", labelKey: "scanner:filters.new" },
  { key: "needs_conversion", labelKey: "scanner:filters.needsConversion" },
  { key: "disc_iso", labelKey: "scanner:filters.discIso" },
  { key: "high_bitrate", labelKey: "scanner:filters.highBitrate" },
  { key: "low_bitrate", labelKey: "scanner:filters.lowBitrate" },
  { key: "bloated", labelKey: "scanner:filters.bloated" },
  { key: "sub_cleanup", labelKey: "scanner:filters.subCleanup" },
  { key: "unknown_language", labelKey: "scanner:filters.unknownLanguage" },
  { key: "ignored", labelKey: "scanner:filters.ignored" },
  { key: "duplicates", labelKey: "scanner:filters.duplicates" },
  { key: "corrupt", labelKey: "scanner:filters.corrupt" },
  { key: "converted", labelKey: "scanner:filters.converted" },
  { key: "queued", labelKey: "scanner:filters.queued" },
  { key: "extras", labelKey: "scanner:filters.extras" },
  { key: "hardlinked", labelKey: "scanner:filters.hardlinked" },
  // Added group (v0.10.0)
  { key: "_added", labelKey: "scanner:filters.groups.added", group: "divider" },
  { key: "added_7d", labelKey: "scanner:filters.added7d" },
  { key: "added_30d", labelKey: "scanner:filters.added30d" },
  { key: "added_90d", labelKey: "scanner:filters.added90d" },
  // Video group
  { key: "_video", labelKey: "scanner:filters.groups.video", group: "divider" },
  { key: "x264", label: "x264" },
  { key: "x265", label: "x265" },
  { key: "av1", label: "AV1" },
  { key: "codec_mpeg2", label: "MPEG-2" },
  { key: "codec_vc1", label: "VC-1 / WMV" },
  { key: "codec_mpeg4", label: "MPEG-4 / Xvid" },
  { key: "codec_vp9", label: "VP9" },
  { key: "misc_codec", labelKey: "scanner:filters.otherCodecs" },
  // Container group (v0.10.0)
  { key: "_container", labelKey: "scanner:filters.groups.container", group: "divider" },
  { key: "container_mkv", label: "MKV" },
  { key: "container_mp4", label: "MP4" },
  { key: "container_avi", label: "AVI" },
  { key: "container_other", labelKey: "scanner:filters.other" },
  // Resolution group
  { key: "_res", labelKey: "scanner:filters.groups.resolution", group: "divider" },
  { key: "res_4k", label: "4K" },
  { key: "res_1080p", label: "1080p" },
  { key: "res_720p", label: "720p" },
  { key: "res_sd", label: "SD" },
  // Picture group (v0.10.0)
  { key: "_picture", labelKey: "scanner:filters.groups.picture", group: "divider" },
  { key: "bit10", label: "10-bit" },
  { key: "hi10p", label: "Hi10P" },
  { key: "interlaced", labelKey: "scanner:filters.interlaced" },
  { key: "vfr", label: "VFR" },
  // HDR group (v0.10.0): the probe's format, else the name
  { key: "_hdr", label: "HDR:", group: "divider" },
  { key: "hdr_dv", label: "Dolby Vision" },
  { key: "hdr_hdr10", label: "HDR10" },
  { key: "hdr_hlg", label: "HLG" },
  // Size group
  { key: "_size", labelKey: "scanner:filters.groups.size", group: "divider" },
  { key: "size_small", labelKey: "scanner:filters.sizeSmall" },
  { key: "size_medium", labelKey: "scanner:filters.sizeMedium" },
  { key: "size_large", labelKey: "scanner:filters.sizeLarge" },
  // Audio group
  { key: "_audio", labelKey: "scanner:filters.groups.audio", group: "divider" },
  { key: "audio_cleanup", labelKey: "scanner:filters.audioCleanup" },
  { key: "lossless_audio", labelKey: "scanner:filters.losslessAudio" },
  { key: "lossy_audio", labelKey: "scanner:filters.lossyAudio" },
  { key: "object_audio", label: "Atmos / DTS:X" },
  { key: "audio_71", label: "7.1" },
  { key: "commentary", labelKey: "scanner:filters.commentary" },
  // Subtitles group (v0.10.0)
  { key: "_subs", labelKey: "scanner:filters.groups.subtitles", group: "divider" },
  { key: "image_subs", labelKey: "scanner:filters.imageSubs" },
  { key: "external_subs", labelKey: "scanner:filters.externalSubs" },
  { key: "forced_subs", labelKey: "scanner:filters.forcedSubs" },
  { key: "sdh_subs", label: "SDH" },
  // Language group
  { key: "_lang", labelKey: "scanner:filters.groups.language", group: "divider" },
  { key: "dubbed", labelKey: "scanner:filters.dubbed" },
  { key: "not_api_matched", labelKey: "scanner:filters.notApiMatched" },
  { key: "missing_language", labelKey: "scanner:filters.missingLanguage" },
  // Sonarr / Radarr group (v0.10.0)
  { key: "_arr", label: "Sonarr/Radarr:", group: "divider" },
  { key: "arr_cutoff_unmet", labelKey: "scanner:filters.arrCutoffUnmet" },
  { key: "arr_unmonitored", labelKey: "scanner:filters.arrUnmonitored" },
  // Plex group
  { key: "_plex", label: "Plex:", group: "divider" },
  { key: "plex_watched", labelKey: "scanner:filters.watched" },
  { key: "plex_unwatched", labelKey: "scanner:filters.unwatched" },
  // Type group
  { key: "_type", labelKey: "scanner:filters.groups.type", group: "divider" },
  { key: "type_movie", labelKey: "scanner:filters.movies" },
  { key: "type_tv", labelKey: "scanner:filters.tvShows" },
  { key: "type_other", labelKey: "scanner:filters.other" },
  // Source group
  { key: "_source", labelKey: "scanner:filters.groups.source", group: "divider" },
  { key: "src_remux", label: "Remux" },
  { key: "src_bluray", label: "Blu-ray" },
  { key: "src_webdl", label: "WEB-DL" },
  { key: "src_hdtv", label: "HDTV" },
  { key: "src_dvd", label: "DVD" },
  // VMAF group
  { key: "_vmaf", label: "VMAF:", group: "divider" },
  { key: "vmaf_excellent", labelKey: "scanner:filters.vmafExcellent" },
  { key: "vmaf_good", labelKey: "scanner:filters.vmafGood" },
  { key: "vmaf_poor", labelKey: "scanner:filters.vmafPoor" },
  { key: "vmaf_uncertain", labelKey: "scanner:filters.vmafUncertain" },
  // Health and outcome groups (v0.10.0)
  { key: "_health", labelKey: "scanner:filters.groups.health", group: "divider" },
  { key: "health_never", labelKey: "scanner:filters.healthNever" },
  { key: "health_warnings", labelKey: "scanner:filters.healthWarnings" },
  { key: "health_stale", labelKey: "scanner:filters.healthStale" },
  { key: "_outcome", labelKey: "scanner:filters.groups.outcome", group: "divider" },
  { key: "failed_before", labelKey: "scanner:filters.failedBefore" },
  { key: "vmaf_rejected", labelKey: "scanner:filters.vmafRejected" },
  { key: "undo_possible", labelKey: "scanner:filters.undoPossible" },
  { key: "no_savings", labelKey: "scanner:filters.noSavings" },
];

// Maps filter key → translation key (or literal tech-token label). Resolve
// with `filterLabel(key, t)` at render time.
const FILTER_LABELS: Record<string, { label?: string; labelKey?: string }> = {};
for (const f of FILTERS) {
  if (!f.group) FILTER_LABELS[f.key] = f;
}

export function filterLabel(key: string, t: (k: string, o?: any) => string): string | undefined {
  if (key.startsWith("adv:")) {
    return t("scanner:filters.advancedLabel", { count: decodeAdvanced(key)?.p.length ?? 0 });
  }
  if (key.startsWith("!")) {
    const label = filterLabel(key.slice(1), t);
    return label && t("scanner:filters.notLabel", { label });
  }
  const f = FILTER_LABELS[key];
  if (!f) return undefined;
  return f.labelKey ? t(f.labelKey) : f.label;
}

export default function FilterBar({ activeFilters, onFilterToggle, newCount, counts }: FilterBarProps) {
  const { t } = useTranslation(["scanner", "common"]);
  const isAll = activeFilters.length === 0 || (activeFilters.length === 1 && activeFilters[0] === "all");

  // v0.10.0: pills in a group match any of them, groups must all match, and
  // Alt-click excludes (the server's scan_filters.py decides; see there).
  return (
    <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginBottom: 24, alignItems: "center" }}>
      {FILTERS.map((f) => {
        if (f.key === "new" && (!newCount || newCount <= 0)) return null;
        if (f.group === "divider") {
          return (
            <span key={f.key} style={{ display: "inline-flex", alignItems: "center", gap: 4, marginLeft: 4 }}>
              <span style={{ width: 1, height: 16, background: "var(--border)" }} />
              <span style={{ color: "var(--text-muted)", fontSize: 12 }}>{f.labelKey ? t(f.labelKey) : f.label}</span>
            </span>
          );
        }
        const isActive = f.key === "all" ? isAll : activeFilters.includes(f.key);
        const isExcluded = f.key !== "all" && activeFilters.includes("!" + f.key);
        const count = f.key === "new" ? newCount : counts?.[f.key];
        return (
          <button
            key={f.key}
            className={`filter-pill ${isActive ? "active" : ""} ${isExcluded ? "excluded" : ""}`}
            aria-pressed={isActive || isExcluded}
            title={f.key === "all" ? undefined : t("scanner:filters.excludeHint")}
            onClick={(e) => onFilterToggle(f.key, e.altKey && f.key !== "all")}
            style={{ whiteSpace: "nowrap", display: "inline-flex", alignItems: "center", gap: 5 }}
          >
            <span className="filter-pill-label">{f.labelKey ? t(f.labelKey) : f.label}</span>
            {isExcluded && <span className="sr-only">{t("scanner:filters.excluded")}</span>}
            {count != null && (f.key !== "new" || count > 0) && (
              <span style={{
                background: f.key === "new" ? "var(--accent)" : "rgba(104,96,254,0.3)",
                color: f.key === "new" ? "white" : "var(--text-secondary)",
                fontSize: 10, fontWeight: "bold",
                padding: "1px 6px", borderRadius: 8,
                display: "inline-flex", alignItems: "center", lineHeight: 1.4,
              }}>
                {count > 99999 ? `${(count / 1000).toFixed(0)}k` : count.toLocaleString()}
              </span>
            )}
          </button>
        );
      })}
      <span className="filter-bar-hint">{t("scanner:filters.hint")}</span>
    </div>
  );
}
