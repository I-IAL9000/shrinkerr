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
  return (
    <div className="stats-grid">
      <div className="stat-card">
        <div className="stat-value">{fmtNum(filesToConvert)}</div>
        <div className="stat-label">Files to convert</div>
      </div>
      <div className="stat-card">
        <div className="stat-value">{fmtNum(audioCleanup)}</div>
        <div className="stat-label">Audio cleanup</div>
      </div>
      {ignoredCount != null && ignoredCount > 0 && (
        <div className="stat-card">
          <div className="stat-value" style={{ color: "var(--text-secondary)" }}>{fmtNum(ignoredCount)}</div>
          <div className="stat-label">Ignored</div>
        </div>
      )}
      {corruptCount != null && corruptCount > 0 && (
        <div className="stat-card">
          <div className="stat-value" style={{ color: "var(--danger)" }}>{fmtNum(corruptCount)}</div>
          <div className="stat-label">Corrupt</div>
        </div>
      )}
      <div className="stat-card">
        <div className="stat-value success">~{estimatedSavingsGB >= 1024 ? `${(estimatedSavingsGB / 1024).toFixed(1)} TB` : `${estimatedSavingsGB.toFixed(0)} GB`}</div>
        <div className="stat-label">Est. savings{settingsLabel ? ` (${settingsLabel})` : ""}</div>
      </div>
      <div className="stat-card">
        <div className="stat-value" style={{ color: "white" }}>{totalScannedGB >= 1024 ? `${(totalScannedGB / 1024).toFixed(1)} TB` : `${totalScannedGB.toFixed(1)} GB`}</div>
        <div className="stat-label">Total scanned</div>
      </div>
    </div>
  );
}
