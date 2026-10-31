import { useState, type CSSProperties, type ReactNode } from "react";
import { useTranslation } from "react-i18next";

/**
 * A setting's help (v0.10.0): one line saying what it does, always shown;
 * the full explanation behind "More", opened in place — not a tooltip, so
 * it reads on a phone and by keyboard, and stays next to its setting.
 * `style` places it as the plain help text it replaces did. The explanation
 * is in the page while closed, so Settings search can find it and open it
 * (SettingsPage's jumpTo).
 */
export default function SettingHelp({ summary, children, style }: {
  summary: ReactNode;
  children?: ReactNode;
  style?: CSSProperties;
}) {
  const { t } = useTranslation("common");
  const [open, setOpen] = useState(false);
  return (
    <div style={{ fontSize: 12, color: "var(--text-muted)", marginTop: 4, lineHeight: 1.5, ...style }}>
      {summary}
      {children && (
        <>
          {" "}
          <button type="button" aria-expanded={open} onClick={() => setOpen(o => !o)} data-setting-help-toggle
            style={{ background: "none", border: "none", padding: 0, font: "inherit", color: "var(--accent-text)",
                     cursor: "pointer", whiteSpace: "nowrap" }}>
            {open ? t("common:settingHelp.less") : t("common:settingHelp.more")}
          </button>
          <div hidden={!open} data-setting-help-more style={{ marginTop: 6 }}>{children}</div>
        </>
      )}
    </div>
  );
}
