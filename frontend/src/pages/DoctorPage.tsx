import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { downloadDiagnostics, getDoctor, type DoctorCheck, type DoctorReport } from "../api";
import { fmtBytes } from "../fmt";
import { useToast } from "../useToast";

// Doctor (v0.10.0): what Shrinkerr depends on, checked in one place, and a
// diagnostics file to attach to a bug report. The server says what it found
// (backend/doctor.py); this page words it.

const GROUPS: { key: string; ids: string[] }[] = [
  { key: "storage", ids: ["media_folder", "data_folder", "database"] },
  { key: "encoding", ids: ["ffmpeg", "ffprobe", "vmaf", "encoder"] },
  { key: "connections", ids: ["connection"] },
  { key: "workers", ids: ["node"] },
  { key: "security", ids: ["auth"] },
];
const SERVICE_NAMES: Record<string, string> = {
  plex: "Plex", jellyfin: "Jellyfin", emby: "Emby", sonarr: "Sonarr", radarr: "Radarr", tmdb: "TMDB",
};
const ICON: Record<DoctorCheck["status"], { mark: string; color: string }> = {
  ok: { mark: "✓", color: "var(--success)" },
  warn: { mark: "!", color: "var(--caution)" },
  error: { mark: "✕", color: "var(--danger)" },
  info: { mark: "i", color: "var(--info)" },
};

function ago(seconds: number | null | undefined, t: (k: string, o?: any) => string): string {
  if (seconds == null) return t("doctor:never");
  if (seconds < 3600) return t("doctor:minutesAgo", { count: Math.max(1, Math.round(seconds / 60)) });
  if (seconds < 86400 * 2) return t("doctor:hoursAgo", { count: Math.round(seconds / 3600) });
  return t("doctor:daysAgo", { count: Math.round(seconds / 86400) });
}

function checkText(c: DoctorCheck, t: (k: string, o?: any) => string): { title: string; detail: string } {
  const d = c.data || {};
  const sizes = {
    free: d.free != null ? fmtBytes(d.free) : "", total: d.total != null ? fmtBytes(d.total) : "",
    threshold: d.threshold != null ? fmtBytes(d.threshold) : "", size: d.size != null ? fmtBytes(d.size) : "",
  };
  switch (c.id) {
    case "media_folder":
    case "data_folder":
      return {
        title: c.target || t(`doctor:checks.${c.id}.title`),
        detail: t(`doctor:checks.${c.id}.${c.status === "ok" ? "ok" : d.problem}`, sizes),
      };
    case "connection": {
      const name = SERVICE_NAMES[c.target || ""] || c.target || "";
      const key = c.status === "ok" ? (d.bundled ? "bundled" : d.version ? "okVersion" : "ok")
        : d.problem === "no_key" ? "noKey" : "error";
      return { title: name, detail: t(`doctor:checks.connection.${key}`, { version: d.version, detail: d.detail }) };
    }
    case "node":
      return {
        title: c.target || "",
        detail: c.status === "ok" ? t("doctor:checks.node.ok") : t("doctor:checks.node.offline", { ago: ago(d.age, t) }),
      };
    case "encoder":
      return {
        title: t("doctor:checks.encoder.title"),
        detail: t(`doctor:checks.encoder.${c.status === "ok" ? "ok" : "missing"}`,
                  { encoder: c.target, available: (d.available || []).join(", ") }),
      };
    case "ffmpeg":
    case "ffprobe":
      return { title: c.id, detail: c.status === "ok" ? t("doctor:checks.tool.ok", { version: d.version }) : t("doctor:checks.tool.missing") };
    default:
      return {
        title: t(`doctor:checks.${c.id}.title`),
        detail: t(`doctor:checks.${c.id}.${c.status}`, { ...sizes, detail: d.detail }),
      };
  }
}

export default function DoctorPage() {
  const { t } = useTranslation(["doctor", "common"]);
  const toast = useToast();
  const [report, setReport] = useState<DoctorReport | null>(null);
  const [checking, setChecking] = useState(false);
  const [downloading, setDownloading] = useState(false);

  const run = useCallback(async () => {
    setChecking(true);
    try {
      setReport(await getDoctor());
    } catch (e) {
      toast((e as Error).message, "error");
    } finally {
      setChecking(false);
    }
  }, [toast]);
  useEffect(() => { run(); }, [run]);

  const download = async () => {
    setDownloading(true);
    try {
      await downloadDiagnostics();
    } catch (e) {
      toast((e as Error).message, "error");
    } finally {
      setDownloading(false);
    }
  };

  const problems = (report?.checks || []).filter(c => c.status === "error" || c.status === "warn").length;

  return (
    <div style={{ maxWidth: 900 }}>
      <h1 style={{ fontSize: 22, color: "var(--text-primary)", marginBottom: 6 }}>{t("doctor:title")}</h1>
      <div style={{ fontSize: 13, color: "var(--text-muted)", marginBottom: 16 }}>{t("doctor:intro")}</div>
      <div style={{ display: "flex", flexWrap: "wrap", gap: 8, alignItems: "center", marginBottom: 20 }}>
        <button className="btn btn-secondary" onClick={run} disabled={checking}>
          {checking ? t("doctor:checking") : t("doctor:run")}
        </button>
        <button className="btn btn-primary" onClick={download} disabled={downloading}>
          {downloading ? t("doctor:downloading") : t("doctor:download")}
        </button>
        <a href="https://github.com/I-IAL9000/shrinkerr/issues/new/choose" target="_blank" rel="noopener noreferrer"
          style={{ fontSize: 13, color: "var(--accent-text)" }}>{t("doctor:report")} ↗</a>
        {report && !checking && (
          <span role="status" style={{ fontSize: 13, marginLeft: "auto", color: problems ? "var(--caution)" : "var(--success)" }}>
            {problems ? t("doctor:problems", { count: problems }) : t("doctor:allGood")}
          </span>
        )}
      </div>
      {!report && checking && <div className="spinner" style={{ width: 24, height: 24, margin: 20 }} />}
      {report && GROUPS.map(group => {
        const checks = report.checks.filter(c => group.ids.includes(c.id));
        if (!checks.length) return null;
        return (
          <section key={group.key} style={{ marginBottom: 18 }}>
            <h2 style={{ fontSize: 14, color: "var(--text-secondary)", marginBottom: 8 }}>{t(`doctor:groups.${group.key}`)}</h2>
            <ul style={{ listStyle: "none", padding: 0, margin: 0, display: "flex", flexDirection: "column", gap: 6 }}>
              {checks.map((c, i) => {
                const { title, detail } = checkText(c, t);
                const icon = ICON[c.status];
                return (
                  <li key={`${c.id}-${c.target}-${i}`} style={{
                    display: "flex", gap: 12, alignItems: "flex-start", padding: "10px 12px", borderRadius: 6,
                    background: "var(--bg-card)", border: "1px solid var(--border)",
                  }}>
                    <span aria-label={t(`doctor:status.${c.status}`)} style={{
                      flexShrink: 0, width: 20, height: 20, borderRadius: 10, display: "inline-flex", alignItems: "center",
                      justifyContent: "center", fontSize: 12, fontWeight: 700, color: icon.color,
                      border: `1px solid ${icon.color}`,
                    }}>{icon.mark}</span>
                    <div style={{ minWidth: 0 }}>
                      <div style={{ fontSize: 13, color: "var(--text-primary)", fontWeight: 600, overflowWrap: "anywhere" }}>{title}</div>
                      <div style={{ fontSize: 12, color: "var(--text-muted)", marginTop: 2 }}>{detail}</div>
                    </div>
                  </li>
                );
              })}
            </ul>
          </section>
        );
      })}
    </div>
  );
}
