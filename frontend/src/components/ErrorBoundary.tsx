import React from "react";
import { useTranslation } from "react-i18next";
import { useLocation } from "react-router-dom";

/**
 * A page that crashes while rendering shows an error card instead of blanking
 * the whole app (v0.10.0). Navigating to another page clears it.
 */
class Boundary extends React.Component<{ resetKey: string; children: React.ReactNode }, { error: Error | null }> {
  state = { error: null as Error | null };

  static getDerivedStateFromError(error: Error) {
    return { error };
  }

  componentDidCatch(error: Error, info: React.ErrorInfo) {
    console.error("[ErrorBoundary]", error, info.componentStack);
  }

  componentDidUpdate(prev: { resetKey: string }) {
    if (prev.resetKey !== this.props.resetKey && this.state.error) this.setState({ error: null });
  }

  render() {
    return this.state.error ? <Fallback error={this.state.error} /> : this.props.children;
  }
}

function Fallback({ error }: { error: Error }) {
  const { t } = useTranslation("common");
  return (
    <div style={{ background: "var(--bg-card)", border: "1px solid var(--border)", borderRadius: 6, padding: 20, maxWidth: 640 }}>
      <div style={{ color: "var(--text-primary)", fontWeight: 600, marginBottom: 8 }}>{t("errorBoundary.title")}</div>
      <div style={{ color: "var(--text-muted)", fontSize: 12, fontFamily: "monospace", marginBottom: 14, wordBreak: "break-word" }}>
        {error.message}
      </div>
      <button className="btn btn-primary" onClick={() => window.location.reload()}>{t("errorBoundary.reload")}</button>
    </div>
  );
}

export default function RouteErrorBoundary({ children }: { children: React.ReactNode }) {
  const location = useLocation();
  return <Boundary resetKey={location.pathname}>{children}</Boundary>;
}
