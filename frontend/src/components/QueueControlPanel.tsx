import { useState } from "react";
import { useTranslation } from "react-i18next";

interface QueueControlPanelProps {
  selectedCount: number;
  onMoveTop: () => void;
  onMoveUp: () => void;
  onMoveDown: () => void;
  onMoveBottom: () => void;
  onChangeVideoPreset: (preset: string, cq: number) => void;
  onChangeAudioPreset: (codec: string, bitrate: number) => void;
  onChangePriority: (priority: number) => void;
  onIgnore: () => void;
  onRemove: () => void;
  onSelectAll: () => void;
  onDeselectAll: () => void;
  defaultEncoder?: string;
}

const PRIORITIES = [
  { labelKey: "normal", value: 0 },
  { labelKey: "high", value: 1 },
  { labelKey: "highest", value: 2 },
];

// nameKey → queue:panel.videoPresets.*; detail is technical and not translated.
const NVENC_VIDEO_PRESETS = [
  { nameKey: "maxQuality", detail: "p7 / CQ 20", preset: "p7", cq: 20 },
  { nameKey: "qualityFirst", detail: "p6 / CQ 21", preset: "p6", cq: 21 },
  { nameKey: "balanced", detail: "p5 / CQ 23", preset: "p5", cq: 23 },
  { nameKey: "spaceSaver", detail: "p4 / CQ 25", preset: "p4", cq: 25 },
  { nameKey: "maxCompression", detail: "p3 / CQ 27", preset: "p3", cq: 27 },
  { nameKey: "potato", detail: "p1 / CQ 30", preset: "p1", cq: 30 },
];

const LIBX265_VIDEO_PRESETS = [
  { nameKey: "maxQuality", detail: "veryslow / CRF 20", preset: "veryslow", cq: 20 },
  { nameKey: "qualityFirst", detail: "slower / CRF 21", preset: "slower", cq: 21 },
  { nameKey: "balanced", detail: "medium / CRF 23", preset: "medium", cq: 23 },
  { nameKey: "spaceSaver", detail: "fast / CRF 25", preset: "fast", cq: 25 },
  { nameKey: "maxCompression", detail: "veryfast / CRF 27", preset: "veryfast", cq: 27 },
  { nameKey: "potato", detail: "ultrafast / CRF 30", preset: "ultrafast", cq: 30 },
];

// labelKey (whole label) / noteKey (parenthetical) → queue:panel.audioPresets.*;
// plain `label` is technical and not translated.
const AUDIO_PRESETS = [
  { labelKey: "copy", codec: "copy", bitrate: 0 },
  { label: "EAC3 640k", noteKey: "bluray", codec: "eac3", bitrate: 640 },
  { label: "EAC3 448k", noteKey: "streaming", codec: "eac3", bitrate: 448 },
  { label: "EAC3 256k", noteKey: "compact", codec: "eac3", bitrate: 256 },
  { label: "AC3 640k", codec: "ac3", bitrate: 640 },
  { label: "AC3 448k", codec: "ac3", bitrate: 448 },
  { label: "AAC 256k", codec: "aac", bitrate: 256 },
  { label: "AAC 128k", codec: "aac", bitrate: 128 },
];

const moveBtnStyle: React.CSSProperties = {
  background: "none",
  border: "none",
  cursor: "pointer",
  borderRadius: 4,
  padding: "5px 8px",
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
  transition: "background 0.15s",
};

const smallBtnStyle: React.CSSProperties = {
  background: "none",
  border: "1px solid var(--border)",
  color: "var(--text-muted)",
  cursor: "pointer",
  borderRadius: 4,
  padding: "3px 7px",
  fontSize: 13,
  lineHeight: 1,
};

// `backgroundColor` (not the `background` shorthand) preserves the global
// `select { background-image: <chevron-svg> }` rule from theme.css. Using
// the shorthand wipes background-image and the dropdown arrow disappears.
const selectStyle: React.CSSProperties = {
  backgroundColor: "var(--bg-primary)",
  color: "var(--text-secondary)",
  border: "1px solid var(--border)",
  borderRadius: 4,
  padding: "3px 6px",
  fontSize: 11,
  cursor: "pointer",
  outline: "none",
};

export default function QueueControlPanel({
  selectedCount,
  onMoveTop,
  onMoveUp,
  onMoveDown,
  onMoveBottom,
  onChangeVideoPreset,
  onChangeAudioPreset,
  onChangePriority,
  onIgnore,
  onRemove,
  onSelectAll,
  onDeselectAll,
  defaultEncoder = "nvenc",
}: QueueControlPanelProps) {
  const { t } = useTranslation(["queue", "common"]);
  const [videoValue, setVideoValue] = useState("");
  const [audioValue, setAudioValue] = useState("");
  const VIDEO_PRESETS = defaultEncoder === "libx265" ? LIBX265_VIDEO_PRESETS : NVENC_VIDEO_PRESETS;

  return (
    <div
      className="queue-control-panel"
      style={{
        position: "sticky",
        top: 0,
        zIndex: 10,
        background: "var(--bg-secondary)",
        backdropFilter: "blur(8px)",
        WebkitBackdropFilter: "blur(8px)",
        border: "1px solid var(--border)",
        borderRadius: 6,
        padding: "8px 14px",
        marginBottom: 10,
        display: "flex",
        alignItems: "center",
        gap: 12,
        flexWrap: "wrap",
      }}
    >
      {/* Selection count */}
      <span style={{ fontSize: 12, color: "var(--accent)", fontWeight: "bold", whiteSpace: "nowrap" }}>
        {t("queue:panel.selected", { count: selectedCount })}
      </span>
      <button
        onClick={onSelectAll}
        style={{
          background: "none",
          border: "none",
          color: "var(--text-muted)",
          cursor: "pointer",
          fontSize: 11,
          textDecoration: "underline",
          padding: 0,
        }}
      >
        {t("common:actions.selectAll")}
      </button>
      <button
        onClick={onDeselectAll}
        style={{
          background: "none",
          border: "none",
          color: "var(--text-muted)",
          cursor: "pointer",
          fontSize: 11,
          textDecoration: "underline",
          padding: 0,
        }}
      >
        {t("queue:panel.deselect")}
      </button>

      {/* Divider */}
      <span style={{ width: 1, height: 18, background: "var(--border)" }} />

      {/* Move buttons */}
      <div style={{ display: "flex", gap: 2, alignItems: "center", background: "var(--bg-primary)", borderRadius: 6, padding: 2 }}>
        <button onClick={onMoveTop} style={{ ...moveBtnStyle }} title={t("queue:panel.moveTop")}>
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#7a6f99" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
            <line x1="4" y1="4" x2="20" y2="4"/><polyline points="12 10 6 16"/><polyline points="12 10 18 16"/><line x1="12" y1="10" x2="12" y2="20"/>
          </svg>
        </button>
        <button onClick={onMoveUp} style={{ ...moveBtnStyle }} title={t("queue:panel.moveUp")}>
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#7a6f99" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
            <polyline points="12 5 6 11"/><polyline points="12 5 18 11"/><line x1="12" y1="5" x2="12" y2="19"/>
          </svg>
        </button>
        <button onClick={onMoveDown} style={{ ...moveBtnStyle }} title={t("queue:panel.moveDown")}>
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#7a6f99" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
            <polyline points="12 19 6 13"/><polyline points="12 19 18 13"/><line x1="12" y1="19" x2="12" y2="5"/>
          </svg>
        </button>
        <button onClick={onMoveBottom} style={{ ...moveBtnStyle }} title={t("queue:panel.moveBottom")}>
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#7a6f99" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
            <line x1="4" y1="20" x2="20" y2="20"/><polyline points="12 14 6 8"/><polyline points="12 14 18 8"/><line x1="12" y1="14" x2="12" y2="4"/>
          </svg>
        </button>
      </div>

      {/* Divider */}
      <span style={{ width: 1, height: 18, background: "var(--border)" }} />

      {/* Video preset */}
      <select
        value={videoValue}
        onChange={(e) => {
          const idx = Number(e.target.value);
          if (isNaN(idx)) return;
          const p = VIDEO_PRESETS[idx];
          onChangeVideoPreset(p.preset, p.cq);
          setVideoValue("");
        }}
        style={selectStyle}
      >
        <option value="">{t("queue:panel.videoPresetPlaceholder")}</option>
        {VIDEO_PRESETS.map((p, i) => (
          <option key={i} value={i}>
            {`${t(`queue:panel.videoPresets.${p.nameKey}`)} — ${p.detail}`}
          </option>
        ))}
      </select>

      {/* Audio preset */}
      <select
        value={audioValue}
        onChange={(e) => {
          const idx = Number(e.target.value);
          if (isNaN(idx)) return;
          const p = AUDIO_PRESETS[idx];
          onChangeAudioPreset(p.codec, p.bitrate);
          setAudioValue("");
        }}
        style={selectStyle}
      >
        <option value="">{t("queue:panel.audioPresetPlaceholder")}</option>
        {AUDIO_PRESETS.map((p, i) => (
          <option key={i} value={i}>
            {(p.labelKey ? t(`queue:panel.audioPresets.${p.labelKey}`) : p.label)
              + (p.noteKey ? ` (${t(`queue:panel.audioPresets.${p.noteKey}`)})` : "")}
          </option>
        ))}
      </select>

      {/* Priority */}
      <select
        onChange={(e) => {
          const val = Number(e.target.value);
          if (!isNaN(val)) onChangePriority(val);
          (e.target as HTMLSelectElement).value = "";
        }}
        defaultValue=""
        style={selectStyle}
      >
        <option value="">{t("queue:panel.priorityPlaceholder")}</option>
        {PRIORITIES.map(p => (
          <option key={p.value} value={p.value}>{t(`queue:priority.${p.labelKey}`)}</option>
        ))}
      </select>

      {/* Divider */}
      <span style={{ width: 1, height: 18, background: "var(--border)" }} />

      {/* Ignore */}
      <button
        onClick={onIgnore}
        style={{
          ...smallBtnStyle,
          color: "#e94560",
          borderColor: "rgba(233, 69, 96, 0.3)",
          fontSize: 11,
        }}
      >
        &#x2298; {t("common:actions.ignore")}
      </button>

      {/* Remove */}
      <button
        onClick={onRemove}
        style={{
          ...smallBtnStyle,
          color: "#e94560",
          borderColor: "rgba(233, 69, 96, 0.3)",
          fontSize: 11,
        }}
      >
        &times; {t("common:actions.remove")}
      </button>
    </div>
  );
}
