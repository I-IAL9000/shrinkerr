import { useState, type CSSProperties } from "react";
import { useTranslation } from "react-i18next";
import { testEncodingRule, type RuleTestResult } from "../api";

/**
 * Settings → Rules → "Test a file" (v0.10.0): which rule applies to a file,
 * and for every rule which of its conditions matched — rules are checked
 * top to bottom and the first match wins.
 */
export default function RuleTester({ inputStyle }: { inputStyle: CSSProperties }) {
  const { t } = useTranslation(["settingsIntegrations", "common"]);
  const [path, setPath] = useState("");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<RuleTestResult | null>(null);
  const [error, setError] = useState("");

  const run = async () => {
    if (!path.trim() || busy) return;
    setBusy(true);
    setError("");
    try {
      setResult(await testEncodingRule(path));
    } catch (e) {
      setResult(null);
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const ct = (key: string, fallback: string) => t(`settingsIntegrations:conditions.${key}`, { defaultValue: fallback });
  const actionLabel = (action: string) =>
    t(`settingsIntegrations:rules.actionBadge.${action === "encode" ? "encode" : action === "skip" ? "skip" : "ignore"}`);
  const applied = result?.matched_rule;

  return (
    <div style={{ marginTop: 14, paddingTop: 12, borderTop: "1px solid var(--border)" }}>
      <div style={{ color: "var(--text-primary)", fontSize: 13, fontWeight: 600 }}>{t("settingsIntegrations:rules.tester.title")}</div>
      <div style={{ color: "var(--text-muted)", fontSize: 12, margin: "2px 0 8px" }}>{t("settingsIntegrations:rules.tester.hint")}</div>
      <div style={{ display: "flex", gap: 8 }}>
        <input value={path} onChange={e => setPath(e.target.value)} onKeyDown={e => { if (e.key === "Enter") run(); }}
          aria-label={t("settingsIntegrations:rules.tester.title")} placeholder={t("settingsIntegrations:rules.tester.placeholder")}
          style={{ ...inputStyle, flex: 1, minWidth: 0 }} />
        <button className="btn btn-secondary" onClick={run} disabled={!path.trim() || busy}
          style={{ fontSize: 12, padding: "4px 12px", whiteSpace: "nowrap" }}>
          {busy ? t("settingsIntegrations:rules.tester.testing") : t("settingsIntegrations:rules.tester.test")}
        </button>
      </div>
      {error && <div role="alert" style={{ color: "var(--danger)", fontSize: 12, marginTop: 8 }}>{error}</div>}
      {result && (
        <div role="status" style={{ marginTop: 10, fontSize: 12 }}>
          <div style={{ fontWeight: 600, color: applied ? "var(--success)" : "var(--text-secondary)" }}>
            {applied
              ? t("settingsIntegrations:rules.tester.applies", { name: applied.rule_name, action: actionLabel(applied.action) })
              : t("settingsIntegrations:rules.tester.none")}
          </div>
          {result.scanned === false && (
            <div style={{ color: "var(--caution)", marginTop: 4 }}>{t("settingsIntegrations:rules.tester.notScanned")}</div>
          )}
          <ul style={{ listStyle: "none", padding: 0, margin: "8px 0 0", display: "flex", flexDirection: "column", gap: 4 }}>
            {result.rules.map(r => {
              const wins = r.rule_id === applied?.rule_id;
              return (
                <li key={r.rule_id} style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 6 }}>
                  <span aria-hidden="true" style={{ color: r.matched ? "var(--success)" : "var(--text-muted)", width: 12 }}>{r.matched ? "✓" : "✗"}</span>
                  <span style={{ color: "var(--text-primary)", fontWeight: wins ? 600 : 400 }}>{r.rule_name}</span>
                  <span style={{ color: "var(--text-muted)" }}>
                    {wins ? t("settingsIntegrations:rules.tester.wins")
                      : r.matched ? t("settingsIntegrations:rules.tester.earlierWins")
                      : t("settingsIntegrations:rules.tester.noMatch")}
                  </span>
                  {r.conditions.map((c, i) => (
                    <span key={i} style={{
                      fontSize: 11, padding: "1px 6px", borderRadius: 8, whiteSpace: "nowrap",
                      border: `1px solid ${c.matched ? "var(--success)" : "var(--border)"}`,
                      color: c.matched ? "var(--success)" : "var(--text-muted)",
                    }}>
                      {ct(`types.${c.type}`, c.type)} {ct(`operators.${c.operator || "is"}`, c.operator || "is")} {String(c.value ?? "")}
                    </span>
                  ))}
                </li>
              );
            })}
          </ul>
          {result.rules.length === 0 && <div style={{ color: "var(--text-muted)", marginTop: 6 }}>{t("settingsIntegrations:rules.tester.noRules")}</div>}
        </div>
      )}
    </div>
  );
}
