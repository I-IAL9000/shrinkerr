import { useTranslation } from "react-i18next";
import ActionMenu from "./ActionMenu";

interface QueueControlPanelProps {
  selectedCount: number;
  onMoveTop: () => void;
  onMoveUp: () => void;
  onMoveDown: () => void;
  onMoveBottom: () => void;
  /** Add to Queue's settings for the selected jobs (v0.10.0): any encoder,
   *  quality, Force re-encode, the rules. It replaced the Video preset,
   *  Audio preset and VMAF target menus. */
  onEdit: () => void;
  onChangePriority: (priority: number) => void;
  onIgnore: () => void;
  onRemove: () => void;
  onSelectAll: () => void;
  onDeselectAll: () => void;
}

const PRIORITIES = [
  { labelKey: "normal", value: 0 },
  { labelKey: "high", value: 1 },
  { labelKey: "highest", value: 2 },
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
  onEdit,
  onChangePriority,
  onIgnore,
  onRemove,
  onSelectAll,
  onDeselectAll,
}: QueueControlPanelProps) {
  const { t } = useTranslation(["queue", "common"]);

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
      <span style={{ fontSize: 12, color: "var(--accent-text)", fontWeight: "bold", whiteSpace: "nowrap" }}>
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
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="var(--text-muted)" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
            <line x1="4" y1="4" x2="20" y2="4"/><polyline points="12 10 6 16"/><polyline points="12 10 18 16"/><line x1="12" y1="10" x2="12" y2="20"/>
          </svg>
        </button>
        <button onClick={onMoveUp} style={{ ...moveBtnStyle }} title={t("queue:panel.moveUp")}>
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="var(--text-muted)" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
            <polyline points="12 5 6 11"/><polyline points="12 5 18 11"/><line x1="12" y1="5" x2="12" y2="19"/>
          </svg>
        </button>
        <button onClick={onMoveDown} style={{ ...moveBtnStyle }} title={t("queue:panel.moveDown")}>
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="var(--text-muted)" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
            <polyline points="12 19 6 13"/><polyline points="12 19 18 13"/><line x1="12" y1="19" x2="12" y2="5"/>
          </svg>
        </button>
        <button onClick={onMoveBottom} style={{ ...moveBtnStyle }} title={t("queue:panel.moveBottom")}>
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="var(--text-muted)" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
            <line x1="4" y1="20" x2="20" y2="20"/><polyline points="12 14 6 8"/><polyline points="12 14 18 8"/><line x1="12" y1="14" x2="12" y2="4"/>
          </svg>
        </button>
      </div>

      {/* Divider */}
      <span style={{ width: 1, height: 18, background: "var(--border)" }} />

      <button onClick={onEdit} style={{ ...smallBtnStyle, color: "var(--text-secondary)", fontSize: 11, display: "inline-flex", alignItems: "center", gap: 4 }}>
        <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <path d="M17 3a2.83 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5L17 3z" />
        </svg>
        {t("queue:panel.editSettings")}
      </button>

      {/* Priority: a menu, not a select (FE#28) */}
      <ActionMenu
        label={t("queue:panel.priorityPlaceholder")}
        buttonStyle={selectStyle}
        items={PRIORITIES.map(p => ({
          label: t(`queue:priority.${p.labelKey}`),
          onSelect: () => onChangePriority(p.value),
        }))}
      />

      {/* Divider */}
      <span style={{ width: 1, height: 18, background: "var(--border)" }} />

      {/* Ignore */}
      <button
        onClick={onIgnore}
        style={{
          ...smallBtnStyle,
          color: "var(--danger)",
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
          color: "var(--danger)",
          borderColor: "rgba(233, 69, 96, 0.3)",
          fontSize: 11,
        }}
      >
        &times; {t("common:actions.remove")}
      </button>
    </div>
  );
}
