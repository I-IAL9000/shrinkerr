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

  const icon = (children: React.ReactNode) => (
    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      {children}
    </svg>
  );

  // The Scanner's selection bar, in look (v0.10.0): pills, the count in the accent.
  return (
    <div
      className="queue-control-panel"
      style={{
        display: "flex", flexWrap: "wrap", gap: 6, alignItems: "center",
        padding: "8px 12px", marginBottom: 12,
        background: "var(--bg-card)",
        border: "1px solid var(--border)",
        borderRadius: 6,
        position: "sticky", top: 0, zIndex: 10,
      }}
    >
      <button className="btn btn-sm btn-pill btn-secondary" onClick={onSelectAll}>
        {t("common:actions.selectAll")}
      </button>
      <button className="btn btn-sm btn-pill btn-secondary" onClick={onDeselectAll}>
        {t("common:actions.deselectAll")}
      </button>
      <span style={{ fontSize: 11, color: "var(--accent-text)", fontWeight: 600 }}>{t("queue:panel.selected", { count: selectedCount })}</span>
      <span style={{ width: 1, height: 14, background: "var(--border)" }} />

      <button className="btn btn-sm btn-pill btn-secondary btn-icon" onClick={onMoveTop} title={t("queue:panel.moveTop")} aria-label={t("queue:panel.moveTop")}>
        {icon(<><line x1="4" y1="4" x2="20" y2="4"/><polyline points="12 10 6 16"/><polyline points="12 10 18 16"/><line x1="12" y1="10" x2="12" y2="20"/></>)}
      </button>
      <button className="btn btn-sm btn-pill btn-secondary btn-icon" onClick={onMoveUp} title={t("queue:panel.moveUp")} aria-label={t("queue:panel.moveUp")}>
        {icon(<><polyline points="12 5 6 11"/><polyline points="12 5 18 11"/><line x1="12" y1="5" x2="12" y2="19"/></>)}
      </button>
      <button className="btn btn-sm btn-pill btn-secondary btn-icon" onClick={onMoveDown} title={t("queue:panel.moveDown")} aria-label={t("queue:panel.moveDown")}>
        {icon(<><polyline points="12 19 6 13"/><polyline points="12 19 18 13"/><line x1="12" y1="19" x2="12" y2="5"/></>)}
      </button>
      <button className="btn btn-sm btn-pill btn-secondary btn-icon" onClick={onMoveBottom} title={t("queue:panel.moveBottom")} aria-label={t("queue:panel.moveBottom")}>
        {icon(<><line x1="4" y1="20" x2="20" y2="20"/><polyline points="12 14 6 8"/><polyline points="12 14 18 8"/><line x1="12" y1="14" x2="12" y2="4"/></>)}
      </button>
      <span style={{ width: 1, height: 14, background: "var(--border)" }} />

      {/* Add to Queue's dialog, as the Scanner's Add to queue: the primary. */}
      <button className="btn btn-sm btn-pill btn-primary" onClick={onEdit}>
        {icon(<path d="M17 3a2.83 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5L17 3z" />)}
        {t("queue:panel.editSettings")}
      </button>

      {/* Priority: a menu, not a select (FE#28) */}
      <ActionMenu
        label={t("queue:panel.priorityPlaceholder")}
        buttonClassName="btn btn-sm btn-pill btn-secondary"
        items={PRIORITIES.map(p => ({
          label: t(`queue:priority.${p.labelKey}`),
          onSelect: () => onChangePriority(p.value),
        }))}
      />

      <button className="btn btn-sm btn-pill btn-secondary" onClick={onIgnore}>
        {icon(<><circle cx="12" cy="12" r="10"/><line x1="4.93" y1="4.93" x2="19.07" y2="19.07"/></>)}
        {t("common:actions.ignore")}
      </button>
      <button className="btn btn-sm btn-pill btn-secondary" onClick={onRemove}>
        {icon(<><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></>)}
        {t("common:actions.remove")}
      </button>
    </div>
  );
}
