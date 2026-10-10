import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { getPendingSummary, type PendingSummary as Summary } from "../api";
import { fmtBytes, fmtNum } from "../fmt";
import { trackLanguageList } from "../utils/languages";

// What the whole pending queue will do (v0.10.0): its jobs by kind, the
// encoders, the tracks it removes by language, about what it saves and what
// becomes of the originals. `refreshKey` changes when the queue does.
export default function PendingSummary({ refreshKey }: { refreshKey: string }) {
  const { t } = useTranslation(["queue", "scannerModals", "settingsMedia"]);
  const [s, setS] = useState<Summary | null>(null);

  useEffect(() => {
    let cancelled = false;
    getPendingSummary().then(r => { if (!cancelled) setS(r); }).catch(() => {});
    return () => { cancelled = true; };
  }, [refreshKey]);

  if (!s || s.jobs === 0) return null;
  const n = (type: string) => s.by_type[type] || 0;
  const kinds = [
    [n("convert") + n("combined"), "queue:summary.conversions"],
    [n("audio"), "queue:summary.trackOnly"],
    [n("health_check"), "queue:summary.healthChecks"],
  ] as const;
  const encName = (e: string) => t(`settingsMedia:video.encoderNames.${e}`, { defaultValue: e });
  const encoders = Object.entries(s.by_encoder).sort((a, b) => b[1] - a[1]);
  const { audio, subtitles } = s.removals;
  const rows: { text: string; warn?: boolean }[] = [
    { text: kinds.filter(([count]) => count > 0).map(([count, key]) => t(key, { count })).join(", ") },
  ];
  if (encoders.length) {
    rows.push({ text: t("queue:summary.encoders", {
      list: encoders.length === 1 ? encName(encoders[0][0]) : encoders.map(([e, c]) => `${encName(e)} (${c})`).join(", "),
    }) });
  }
  if (Object.keys(audio).length) rows.push({ text: t("scannerModals:estimate.whatHappens.audioRemoved", { list: trackLanguageList(audio) }) });
  if (Object.keys(subtitles).length) rows.push({ text: t("scannerModals:estimate.whatHappens.subsRemoved", { list: trackLanguageList(subtitles) }) });
  if (!Object.keys(audio).length && !Object.keys(subtitles).length) rows.push({ text: t("scannerModals:estimate.whatHappens.nothingRemoved") });
  rows.push(s.originals.action === "keep"
    ? { text: t("scannerModals:estimate.whatHappens.originalsKeep", { count: s.originals.days }) }
    : s.originals.action === "trash"
      ? { text: t("scannerModals:estimate.whatHappens.originalsTrash") }
      : { text: t("scannerModals:estimate.whatHappens.originalsDelete"), warn: true });

  return (
    <details style={{ background: "var(--bg-primary)", borderRadius: 6, padding: "8px 12px", marginBottom: 8, fontSize: 12 }}>
      <summary style={{ cursor: "pointer", color: "var(--text-secondary)", userSelect: "none" }}>
        <span style={{ fontWeight: 600 }}>{t("queue:summary.title")}</span>
        <span style={{ color: "var(--text-muted)", marginLeft: 8 }}>
          {t("queue:summary.jobs", { count: s.jobs, formatted: fmtNum(s.jobs) })}
          {s.total_size > 0 && <> · {s.estimated_savings > 0
            ? t("queue:summary.size", { before: fmtBytes(s.total_size), after: fmtBytes(s.total_size - s.estimated_savings), saved: fmtBytes(s.estimated_savings) })
            : fmtBytes(s.total_size)}</>}
        </span>
      </summary>
      <ul style={{ margin: "6px 0 0", paddingLeft: 16, display: "flex", flexDirection: "column", gap: 3 }}>
        {rows.map((r, i) => (
          <li key={i} style={{ color: r.warn ? "var(--warning)" : "var(--text-secondary)" }}>{r.text}</li>
        ))}
      </ul>
      <div style={{ marginTop: 6, fontSize: 11, color: "var(--text-muted)" }}>{t("queue:summary.perJob")}</div>
    </details>
  );
}
