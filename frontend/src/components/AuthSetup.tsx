import { useState } from "react";
import { useTranslation } from "react-i18next";
import { setupAuth, type AuthMethod, type AuthRequired } from "../api";

const FIELD = {
  width: "100%", padding: "10px 14px", marginBottom: 8, backgroundColor: "var(--bg-primary)",
  color: "var(--text-secondary)", border: "1px solid var(--border)", borderRadius: 6, fontSize: 14,
  outline: "none", boxSizing: "border-box",
} as const;

/**
 * Sign-in is required (v0.10.0), as in Sonarr v4: an install without it
 * shows this before anything else — the sign-in page (username and
 * password) or External (a reverse proxy signs people in), for everyone or
 * not for local addresses.
 */
export default function AuthSetup({ onDone }: { onDone: () => void }) {
  const { t } = useTranslation(["nav", "common"]);
  const [method, setMethod] = useState<AuthMethod>("forms");
  const [required, setRequired] = useState<AuthRequired>("enabled");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const submit = async () => {
    setError("");
    if (method === "forms" && password !== confirm) {
      setError(t("nav:authSetup.mismatch"));
      return;
    }
    setBusy(true);
    try {
      await setupAuth({ method, required, username, password });
      onDone();
    } catch (err: any) {
      setError(err?.message || String(err));
    } finally {
      setBusy(false);
    }
  };

  const option = (value: AuthMethod) => (
    <label style={{ display: "flex", gap: 10, alignItems: "flex-start", textAlign: "left", cursor: "pointer", padding: "10px 12px",
                    borderRadius: 6, marginBottom: 8, border: `1px solid ${method === value ? "var(--accent)" : "var(--border)"}` }}>
      <input type="radio" name="auth-method" checked={method === value} onChange={() => setMethod(value)} style={{ marginTop: 3 }} />
      <span>
        <span style={{ display: "block", fontSize: 14, color: "var(--text-primary)", fontWeight: 600 }}>
          {t(`nav:authSetup.${value}.title`)}
        </span>
        <span style={{ display: "block", fontSize: 12, color: "var(--text-muted)", lineHeight: 1.5 }}>
          {t(`nav:authSetup.${value}.help`)}
        </span>
      </span>
    </label>
  );

  return (
    <div style={{ display: "flex", alignItems: "center", justifyContent: "center", minHeight: "100vh", background: "var(--bg-primary)", padding: 16 }}>
      <form onSubmit={(e) => { e.preventDefault(); submit(); }} aria-labelledby="auth-setup-title"
        style={{ background: "var(--bg-card)", padding: 28, borderRadius: 8, maxWidth: 440, width: "100%" }}>
        <div style={{ textAlign: "center" }}>
          <img src="/shrinkerr-logo.svg" alt="Shrinkerr" height="32" style={{ marginBottom: 12 }} />
          <h1 id="auth-setup-title" style={{ fontSize: 18, color: "var(--text-primary)", margin: "0 0 6px" }}>{t("nav:authSetup.title")}</h1>
          <p style={{ fontSize: 13, color: "var(--text-muted)", lineHeight: 1.5, margin: "0 0 16px" }}>{t("nav:authSetup.intro")}</p>
        </div>
        {option("forms")}
        {option("external")}
        {method === "forms" && (
          <>
            <label style={{ display: "block", fontSize: 12, color: "var(--text-secondary)", margin: "12px 0 4px" }} htmlFor="auth-required">
              {t("nav:authSetup.required")}
            </label>
            <select id="auth-required" value={required} onChange={(e) => setRequired(e.target.value as AuthRequired)} style={FIELD}>
              <option value="enabled">{t("nav:authSetup.requiredEnabled")}</option>
              <option value="local_disabled">{t("nav:authSetup.requiredLocalDisabled")}</option>
            </select>
            {required === "local_disabled" && (
              <div style={{ fontSize: 12, color: "var(--text-muted)", lineHeight: 1.5, margin: "-2px 0 8px" }}>{t("nav:authSetup.localHelp")}</div>
            )}
            <input type="text" autoComplete="username" value={username} onChange={(e) => setUsername(e.target.value)} style={FIELD}
              aria-label={t("nav:login.username")} placeholder={t("nav:login.username")} />
            <input type="password" autoComplete="new-password" value={password} onChange={(e) => setPassword(e.target.value)} style={FIELD}
              aria-label={t("nav:login.password")} placeholder={t("nav:authSetup.passwordPlaceholder")} />
            <input type="password" autoComplete="new-password" value={confirm} onChange={(e) => setConfirm(e.target.value)} style={FIELD}
              aria-label={t("nav:authSetup.confirm")} placeholder={t("nav:authSetup.confirm")} />
          </>
        )}
        <div style={{ fontSize: 12, color: "var(--text-muted)", lineHeight: 1.5, margin: "4px 0 12px" }}>{t("nav:authSetup.apiKeys")}</div>
        {error && <div role="alert" style={{ color: "var(--danger)", fontSize: 12, marginBottom: 8 }}>{error}</div>}
        <button type="submit" className="btn btn-primary" style={{ width: "100%" }}
          disabled={busy || (method === "forms" && (!username.trim() || !password))}>
          {busy ? t("nav:authSetup.saving") : t("nav:authSetup.save")}
        </button>
      </form>
    </div>
  );
}
