import { useTranslation } from "react-i18next";
import { fmtNum } from "../fmt";

interface StatsCardsProps {
  filesToConvert: number;
  audioCleanup: number;
  ignoredCount?: number;
  corruptCount?: number;
  estimatedSavingsGB: number;
  totalScannedGB: number;
  settingsLabel?: string;
}

export default function StatsCards({ filesToConvert, audioCleanup, ignoredCount, corruptCount, estimatedSavingsGB, totalScannedGB, settingsLabel }: StatsCardsProps) {
  const { t } = useTranslation(["scanner", "common"]);
  return (
    <div className="stats-grid">
      <div className="stat-card">
        <div className="stat-value">{fmtNum(filesToConvert)}</div>
        <div className="stat-label">{t("scanner:stats.filesToConvert")}</div>
      </div>
      <div className="stat-card">
        <div className="stat-value">{fmtNum(audioCleanup)}</div>
        <div className="stat-label">{t("scanner:stats.audioCleanup")}</div>
      </div>
      {ignoredCount != null && ignoredCount > 0 && (
        <div className="stat-card">
          <div className="stat-value" style={{ color: "var(--text-secondary)" }}>{fmtNum(ignoredCount)}</div>
          <div className="stat-label">{t("scanner:stats.ignored")}</div>
        </div>
      )}
      {corruptCount != null && corruptCount > 0 && (
        <div className="stat-card">
          <div className="stat-value" style={{ color: "var(--danger)" }}>{fmtNum(corruptCount)}</div>
          <div className="stat-label">{t("scanner:stats.corrupt")}</div>
        </div>
      )}
      <div className="stat-card">
        <div className="stat-value success">~{estimatedSavingsGB >= 1024 ? `${(estimatedSavingsGB / 1024).toFixed(1)} TB` : `${estimatedSavingsGB.toFixed(0)} GB`}</div>
        <div className="stat-label">{settingsLabel ? t("scanner:stats.estSavingsWith", { settings: settingsLabel }) : t("scanner:stats.estSavings")}</div>
      </div>
      <div className="stat-card">
        <div className="stat-value" style={{ color: "white" }}>{totalScannedGB >= 1024 ? `${(totalScannedGB / 1024).toFixed(1)} TB` : `${totalScannedGB.toFixed(1)} GB`}</div>
        <div className="stat-label">{t("scanner:stats.totalScanned")}</div>
      </div>
    </div>
  );
}
