import { useRef, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { addLibraryRule, downloadRulesFile, getRuleLibrary, importRulesFile,
  type LibraryRule, type RuleImportResult } from "../api";
import { serverText } from "../i18n/server";
import { useDialog } from "../useDialog";
import { useToast } from "../useToast";

/**
 * Settings → Automation → Rules (v0.10.0): export the rules as a YAML file,
 * import one (rules with an existing rule's name replace it), and add
 * ready-made rules from the library.
 */
export default function RuleSharing({ ruleNames, onChanged }: { ruleNames: string[]; onChanged: () => void }) {
  const { t } = useTranslation(["settingsIntegrations", "common"]);
  const toast = useToast();
  const fileRef = useRef<HTMLInputElement>(null);
  const [problems, setProblems] = useState<RuleImportResult | null>(null);
  const [library, setLibrary] = useState<LibraryRule[] | null>(null);

  const report = (res: RuleImportResult) => {
    onChanged();
    if (res.errors.length) setProblems(res);
    else toast(t("settingsIntegrations:rules.sharing.imported", { added: res.added, replaced: res.replaced }), "success");
  };

  const importFile = async (file: File | undefined) => {
    if (!file) return;
    try {
      report(await importRulesFile(await file.text()));
    } catch (err: any) {
      toast(err.message || t("settingsIntegrations:rules.sharing.importFailed"), "error");
    }
  };

  return (
    <>
      <button className="btn btn-xs btn-secondary" onClick={() => {
        getRuleLibrary().then(setLibrary).catch((err) => toast(err.message, "error"));
      }}>{t("settingsIntegrations:rules.sharing.library")}</button>
      <button className="btn btn-xs btn-secondary" onClick={() => fileRef.current?.click()}>
        {t("settingsIntegrations:rules.sharing.import")}
      </button>
      <input ref={fileRef} type="file" accept=".yaml,.yml,application/yaml,text/yaml" hidden
        aria-label={t("settingsIntegrations:rules.sharing.import")}
        onChange={(e) => { importFile(e.target.files?.[0]); e.target.value = ""; }} />
      <button className="btn btn-xs btn-secondary" disabled={!ruleNames.length}
        onClick={() => downloadRulesFile().catch((err) => toast(err.message, "error"))}>
        {t("settingsIntegrations:rules.sharing.export")}
      </button>
      {problems && (
        <Dialog label={t("settingsIntegrations:rules.sharing.problemsTitle")} onClose={() => setProblems(null)}>
          <p style={{ margin: "0 0 10px", fontSize: 13, color: "var(--text-secondary)" }}>
            {t("settingsIntegrations:rules.sharing.imported", { added: problems.added, replaced: problems.replaced })}{" "}
            {t("settingsIntegrations:rules.sharing.notImported", { count: problems.errors.length })}
          </p>
          <ul style={{ margin: 0, paddingLeft: 18, fontSize: 12, lineHeight: 1.6, color: "var(--text-secondary)" }}>
            {problems.errors.map((e, i) => (
              <li key={i}>
                <b>{e.name || t("settingsIntegrations:rules.sharing.unnamed")}</b>: {serverText("serverApi", e.code, e.params, e.error)}
              </li>
            ))}
          </ul>
        </Dialog>
      )}
      {library && (
        <Dialog label={t("settingsIntegrations:rules.library.title")} onClose={() => setLibrary(null)}>
          <p style={{ margin: "0 0 12px", fontSize: 12, color: "var(--text-muted)", lineHeight: 1.5 }}>
            {t("settingsIntegrations:rules.library.intro")}
          </p>
          <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            {library.map(p => {
              const name = t(`settingsIntegrations:rules.library.presets.${p.id}.name`);
              const added = ruleNames.includes(name);
              return (
                <div key={p.id} style={{ display: "flex", gap: 12, alignItems: "center", background: "var(--bg-primary)",
                  borderRadius: 4, padding: "8px 10px" }}>
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div style={{ fontSize: 13, fontWeight: 600, color: "var(--text-primary)" }}>{name}</div>
                    <div style={{ fontSize: 12, color: "var(--text-muted)", lineHeight: 1.5 }}>
                      {t(`settingsIntegrations:rules.library.presets.${p.id}.description`)}
                    </div>
                  </div>
                  <button className={added ? "btn btn-xs btn-secondary" : "btn btn-xs btn-primary"}
                    onClick={async () => {
                      try {
                        const res = await addLibraryRule(p.id, name);
                        onChanged();
                        if (res.errors.length) setProblems(res);
                        else toast(t("settingsIntegrations:rules.library.added", { name }), "success");
                      } catch (err: any) { toast(err.message, "error"); }
                    }}>
                    {added ? t("settingsIntegrations:rules.library.reset") : t("settingsIntegrations:rules.library.add")}
                  </button>
                </div>
              );
            })}
          </div>
        </Dialog>
      )}
    </>
  );
}

function Dialog({ label, onClose, children }: { label: string; onClose: () => void; children: ReactNode }) {
  const { t } = useTranslation("common");
  const panelRef = useRef<HTMLDivElement>(null);
  const dialog = useDialog(panelRef, onClose, label);
  return (
    <div onClick={onClose} style={{ position: "fixed", inset: 0, zIndex: 9000, background: "rgba(0, 0, 0, 0.6)",
      display: "flex", alignItems: "center", justifyContent: "center" }}>
      <div ref={panelRef} {...dialog} onClick={(e) => e.stopPropagation()} style={{
        background: "var(--bg-card)", border: "1px solid var(--border)", borderRadius: 8, padding: "20px 22px",
        maxWidth: 560, width: "92%", maxHeight: "85vh", overflowY: "auto", boxShadow: "0 8px 32px rgba(0, 0, 0, 0.5)" }}>
        <h3 style={{ margin: "0 0 10px", fontSize: 16, color: "var(--text-primary)" }}>{label}</h3>
        {children}
        <div style={{ display: "flex", justifyContent: "flex-end", marginTop: 16 }}>
          <button className="btn btn-secondary" onClick={onClose}>
            {t("common:actions.close")}
          </button>
        </div>
      </div>
    </div>
  );
}
