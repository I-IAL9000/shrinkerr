import { useEffect, useState } from "react";
import { Trans, useTranslation } from "react-i18next";
import { Link } from "react-router-dom";
import { getApiKey, getWorkerSetup, type WorkerSetupInfo } from "../api";
import { copyText } from "../utils/clipboard";
import { useToast } from "../useToast";
import { workerCommand, type WorkerFormat, type WorkerGpu } from "../workerCommand";

/**
 * Nodes → "Add a remote worker" (v0.10.0): a copy-ready command with this
 * server's address, its API key and the image matching its version, as
 * docker run or compose, for NVIDIA, Intel/AMD or CPU-only machines.
 */
export default function WorkerSetup() {
  const { t } = useTranslation(["nodes", "common"]);
  const toast = useToast();
  const [info, setInfo] = useState<WorkerSetupInfo | null>(null);
  const [apiKey, setApiKey] = useState<string | null>(null);
  const [gpu, setGpu] = useState<WorkerGpu>("nvidia");
  const [format, setFormat] = useState<WorkerFormat>("compose");
  const [workerName, setWorkerName] = useState("");
  const [hostMediaPath, setHostMediaPath] = useState("");
  const [showKey, setShowKey] = useState(false);

  useEffect(() => {
    getWorkerSetup().then(setInfo).catch(() => {});
    getApiKey().then(r => setApiKey(r.api_key || "")).catch(() => setApiKey(""));
  }, []);

  const local = ["localhost", "127.0.0.1", "[::1]"].includes(window.location.hostname);
  const build = (key: string) => info ? workerCommand({
    gpu, format, image: info.image, tag: info.tags[gpu], serverUrl: window.location.origin, apiKey: key,
    workerName, hostMediaPath, mediaRoot: info.media_root,
  }) : "";
  const shown = apiKey && !showKey ? `${"•".repeat(8)}${apiKey.slice(-4)}` : apiKey || "<api-key>";
  const copy = async () => {
    await copyText(build(apiKey || "<api-key>"));
    toast(t("nodes:setup.copied"), "success");
  };

  const pill = (active: boolean) => ({
    background: active ? "var(--accent-bg)" : undefined, color: active ? "var(--accent-text)" : undefined,
    borderColor: active ? "var(--accent-text)" : undefined,
  });
  const input = { padding: "5px 8px", fontSize: 12, background: "var(--bg-primary)", color: "var(--text-secondary)",
                  border: "1px solid var(--border)", borderRadius: 4, minWidth: 0 };

  return (
    <div style={{ marginTop: 32, padding: 16, background: "var(--bg-card)", borderRadius: 6, border: "1px solid var(--border)" }}>
      <div style={{ fontSize: 13, fontWeight: 600, color: "var(--text-primary)", marginBottom: 8 }}>{t("nodes:setup.title")}</div>
      <div style={{ fontSize: 12, color: "var(--text-muted)", lineHeight: 1.7 }}>{t("nodes:setup.intro")}</div>

      <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginTop: 10, alignItems: "center" }}>
        {(["nvidia", "intel_amd", "cpu"] as WorkerGpu[]).map(g => (
          <button key={g} className="sort-pill" aria-pressed={gpu === g} onClick={() => setGpu(g)} style={pill(gpu === g)}>
            {t(`nodes:setup.gpu.${g}`)}
          </button>
        ))}
        <span style={{ width: 1, height: 16, background: "var(--border)", margin: "0 4px" }} />
        {(["compose", "run"] as WorkerFormat[]).map(f => (
          <button key={f} className="sort-pill" aria-pressed={format === f} onClick={() => setFormat(f)} style={pill(format === f)}>
            {t(`nodes:setup.format.${f}`)}
          </button>
        ))}
      </div>
      <div style={{ display: "flex", flexWrap: "wrap", gap: 8, marginTop: 8 }}>
        <input value={workerName} onChange={e => setWorkerName(e.target.value)} style={{ ...input, flex: "1 1 160px" }}
          aria-label={t("nodes:setup.workerName")} placeholder={t("nodes:setup.workerName")} />
        <input value={hostMediaPath} onChange={e => setHostMediaPath(e.target.value)} style={{ ...input, flex: "2 1 240px" }}
          aria-label={t("nodes:setup.hostMedia")} placeholder={t("nodes:setup.hostMedia")} />
      </div>

      {apiKey === "" && (
        <div role="alert" style={{ fontSize: 12, color: "var(--caution)", marginTop: 10 }}>
          <Trans i18nKey="nodes:setup.noKey" components={{ a: <Link to="/settings/system#system" /> }} />
        </div>
      )}
      {local && <div style={{ fontSize: 12, color: "var(--caution)", marginTop: 10 }}>{t("nodes:setup.localhost")}</div>}

      <div style={{ position: "relative" }}>
        <pre style={{
          fontSize: 11, padding: 12, marginTop: 8, borderRadius: 4, background: "var(--bg-primary)",
          border: "1px solid var(--border)", color: "var(--text-secondary)", overflow: "auto", lineHeight: 1.6,
        }}>{build(shown)}</pre>
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <button className="btn btn-primary" onClick={copy} disabled={!info} style={{ fontSize: 12, padding: "4px 12px" }}>
            {t("nodes:setup.copy")}
          </button>
          {apiKey && (
            <label style={{ fontSize: 12, color: "var(--text-muted)", display: "inline-flex", gap: 6, alignItems: "center" }}>
              <input type="checkbox" checked={showKey} onChange={e => setShowKey(e.target.checked)} />
              {t("nodes:setup.showKey")}
            </label>
          )}
        </div>
      </div>
      <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 8, lineHeight: 1.6 }}>
        {t("nodes:setup.footer", { path: info?.media_root || "/media" })}
      </div>
    </div>
  );
}
