import { createBrowserRouter, RouterProvider, Routes, Route, NavLink, useLocation, useNavigate } from "react-router-dom";
import React, { Suspense, lazy, useCallback, useState, useEffect } from "react";
import { useTranslation, Trans } from "react-i18next";
import { ApiRequestError, useWebSocket, getNewFileCount, clearNewFileCount, getFailedJobCount, getVersion, checkAuth, login, setStoredApiKey, startQueue, pauseQueue, getJobStats, getTmdbStatus } from "./api";
import { useVisibleInterval } from "./useVisibleInterval";
// Pages load on first visit (FE#30, v0.10.0): everything was one 1.5 MB
// script, charts and all. The design-system page is for development only.
const DashboardPage = lazy(() => import("./pages/DashboardPage"));
const ScannerPage = lazy(() => import("./pages/ScannerPage"));
const QueuePage = lazy(() => import("./pages/QueuePage"));
const LogsPage = lazy(() => import("./pages/LogsPage"));
const DoctorPage = lazy(() => import("./pages/DoctorPage"));
const ActivityPage = lazy(() => import("./pages/ActivityPage"));
const NodesPage = lazy(() => import("./pages/NodesPage"));
const SchedulePage = lazy(() => import("./pages/SchedulePage"));
const SettingsPage = lazy(() => import("./pages/SettingsPage"));
const MonitorPage = lazy(() => import("./pages/MonitorPage"));
const DesignPage = import.meta.env.DEV ? lazy(() => import("./pages/DesignPage")) : null;
import { useToastState, ToastProvider, ToastContainer } from "./useToast";
import { ConfirmProvider } from "./components/ConfirmModal";
import ChangelogModal from "./components/ChangelogModal";
import WhatsNewModal from "./components/WhatsNewModal";
import RouteErrorBoundary from "./components/ErrorBoundary";
import GiftIcon from "./components/GiftIcon";
import type { WSMessage, JobProgress, ScanProgress } from "./types";
import { jobProgressStore } from "./jobProgressStore";
import { SETTINGS_SECTIONS } from "./settingsSections";
import { dialogOpen } from "./useDialog";
import { shortcutsEnabled } from "./shortcuts";

export type ThemePref = "system" | "light" | "dark";
import "./theme.css";

function VersionBadge() {
  const { t } = useTranslation(["nav", "common"]);
  const [version, setVersion] = useState<{ current: string; latest: string | null; update_available: boolean } | null>(null);
  const [modalOpen, setModalOpen] = useState(false);

  useEffect(() => {
    getVersion().then(setVersion).catch(() => {});
  }, []);

  if (!version) return null;

  // When a newer release exists on GitHub, swap the plain version line for
  // a clickable "Update available" pill that opens the changelog modal.
  // Matches the Figma design at sidebar bottom (gift icon + accent pill).
  if (version.update_available) {
    return (
      <>
        <div style={{ padding: "12px 12px 24px", marginTop: "auto", display: "flex", flexDirection: "column", alignItems: "center", gap: 6 }} className="version-badge">
          <button
            onClick={() => setModalOpen(true)}
            title={t("nav:version.updateTooltip", { latest: version.latest, current: version.current })}
            style={{
              display: "inline-flex", alignItems: "center", gap: 8,
              padding: "7px 14px", borderRadius: 20,
              background: "linear-gradient(90deg, #4920F0, #6A64FF)",
              color: "#ffffff", border: "none", cursor: "pointer",
              fontSize: 12, fontWeight: 600,
              boxShadow: "0 2px 8px rgba(73,32,240,0.35)",
            }}
          >
            {/* Gift icon lifted from the Figma design-system file (node 91:31). */}
            <GiftIcon size={14} />
            {t("nav:version.updateAvailable")}
          </button>
          <span style={{ fontSize: 10, color: "var(--text-muted)" }}>
            v{version.current} → v{version.latest}
          </span>
        </div>
        <ChangelogModal
          open={modalOpen}
          onClose={() => setModalOpen(false)}
          latestVersion={version.latest}
          showLatestOnly
        />
      </>
    );
  }

  // Up-to-date: plain version line. A development build's version
  // ("0.10.0-dev.844+c55fcd3") doesn't fit the sidebar on one line, so the
  // part after the release number goes on a second line.
  const dash = version.current.indexOf("-");
  const release = dash === -1 ? version.current : version.current.slice(0, dash);
  const prerelease = dash === -1 ? null : version.current.slice(dash + 1);
  return (
    <div style={{ padding: "12px 0 24px", marginTop: "auto", display: "flex", alignItems: "center", justifyContent: "center", gap: 8 }} className="version-badge">
      <img src="/favicon.svg" alt="" width="16" height="17" />
      <span style={{ fontSize: 10, color: "var(--text-muted)" }}>
        Shrinkerr v{release}
        {prerelease && <><br />{prerelease}</>}
      </span>
    </div>
  );
}

function NewFileBadge() {
  const { t } = useTranslation(["nav", "common"]);
  const [count, setCount] = useState(0);
  const location = useLocation();

  const check = useCallback(() => {
    getNewFileCount().then(r => setCount(r.count)).catch(() => {});
  }, []);
  useEffect(() => { check(); }, [check]);
  useVisibleInterval(check, 30000);

  useEffect(() => {
    if (location.pathname === "/scanner") {
      if (count > 0) {
        clearNewFileCount().catch(() => {});
        setCount(0);
      }
    }
  }, [location.pathname]);

  if (count <= 0 || location.pathname === "/scanner") return null;
  return (
    <span style={{
      background: "var(--accent)", color: "white", fontSize: 9, fontWeight: "bold",
      padding: "1px 5px", borderRadius: 8, marginLeft: 6, verticalAlign: "middle",
    }}>
      {t("nav:sidebar.newFiles", { count })}
    </span>
  );
}

function FailedJobBadge() {
  const [count, setCount] = useState(0);

  const check = useCallback(() => {
    getFailedJobCount().then(r => setCount(r.count)).catch(() => {});
  }, []);
  useEffect(() => { check(); }, [check]);
  useVisibleInterval(check, 30000);

  if (count <= 0) return null;
  return (
    <span style={{
      background: "var(--danger)", color: "white", fontSize: 9, fontWeight: "bold",
      padding: "1px 5px", borderRadius: 8, marginLeft: 6, verticalAlign: "middle",
    }}>
      {count}
    </span>
  );
}

interface NavItem {
  to: string;
  labelKey: string;
  end?: boolean;
  icon: string;
  badge?: boolean;
  failedBadge?: boolean;
  section: string;
}

const NAV_SECTIONS: { labelKey: string; items: NavItem[] }[] = [
  {
    labelKey: "nav:sidebar.sections.encode",
    items: [
      { to: "/", labelKey: "nav:sidebar.dashboard", end: true, icon: "/icons/dashboard.svg", section: "ENCODE" },
      { to: "/scanner", labelKey: "nav:sidebar.scanner", icon: "/icons/search.svg", badge: true, section: "ENCODE" },
      { to: "/queue", labelKey: "nav:sidebar.queue", icon: "/icons/queue.svg", failedBadge: true, section: "ENCODE" },
      { to: "/nodes", labelKey: "nav:sidebar.nodes", icon: "/icons/nodes.svg", section: "ENCODE" },
    ],
  },
  {
    labelKey: "nav:sidebar.sections.system",
    items: [
      { to: "/monitor", labelKey: "nav:sidebar.monitor", icon: "/icons/monitor.svg", section: "SYSTEM" },
      { to: "/activity", labelKey: "nav:sidebar.activity", icon: "/icons/activity.svg", section: "SYSTEM" },
      { to: "/logs", labelKey: "nav:sidebar.logs", icon: "/icons/terminal.svg", section: "SYSTEM" },
      { to: "/doctor", labelKey: "nav:sidebar.doctor", icon: "/icons/doctor.svg", section: "SYSTEM" },
      { to: "/schedule", labelKey: "nav:sidebar.schedule", icon: "/icons/clock.svg", section: "SYSTEM" },
    ],
  },
  {
    labelKey: "nav:sidebar.sections.config",
    items: [
      { to: "/settings", labelKey: "nav:sidebar.settings", icon: "/icons/settings.svg", section: "CONFIG" },
    ],
  },
];

function SidebarNavItems() {
  const { t } = useTranslation(["nav", "common"]);
  const location = useLocation();
  return (
    <>
      {NAV_SECTIONS.map(section => (
        <div key={section.labelKey}>
          <div className="sidebar-section-label">{t(section.labelKey)}</div>
          {section.items.map(item => (
            <React.Fragment key={item.to}>
              <NavLink to={item.to} end={item.end} className={({isActive}) => `sidebar-link ${isActive ? "active" : ""}`}>
                <img src={item.icon} alt="" width="18" height="18" />
                {t(item.labelKey)}
                {item.badge && <NewFileBadge />}
                {item.failedBadge && <FailedJobBadge />}
              </NavLink>
              {item.to === "/settings" && location.pathname.startsWith("/settings") && (
                <div className="settings-subnav">
                  {SETTINGS_SECTIONS.map(s => (
                    <NavLink
                      key={s.id}
                      to={`/settings/${s.id}`}
                      className={({ isActive }) => `settings-subnav-link ${isActive ? "active" : ""}`}
                    >
                      {t(s.labelKey)}
                    </NavLink>
                  ))}
                </div>
              )}
            </React.Fragment>
          ))}
        </div>
      ))}
    </>
  );
}

function MobileMenu() {
  const { t } = useTranslation(["nav", "common"]);
  const [open, setOpen] = useState(false);
  const location = useLocation();

  // Close on navigation
  useEffect(() => { setOpen(false); }, [location.pathname]);

  return (
    <>
      <button className="hamburger-btn" onClick={() => setOpen(!open)} aria-label={t("nav:sidebar.menu")}>
        <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
          {open ? <><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></> : <><line x1="3" y1="6" x2="21" y2="6"/><line x1="3" y1="12" x2="21" y2="12"/><line x1="3" y1="18" x2="21" y2="18"/></>}
        </svg>
      </button>
      {open && (
        <div className="mobile-menu-overlay" onClick={() => setOpen(false)}>
          <div className="mobile-menu" onClick={e => e.stopPropagation()}>
            {/* Close on a link too: a navigation held back (Settings' unsaved
                changes) leaves the path as it was. */}
            <nav style={{ display: "flex", flexDirection: "column", gap: 2 }}
              onClick={e => { if ((e.target as HTMLElement).closest("a")) setOpen(false); }}>
              <SidebarNavItems />
            </nav>
            <VersionBadge />
          </div>
        </div>
      )}
    </>
  );
}

function KeyboardShortcuts({ onToggleQueue }: { onToggleQueue: () => void }) {
  const navigate = useNavigate();

  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      // Off when switched off, and while a dialog is open: a letter navigated
      // away underneath it, and its action then ran for a page that had
      // unmounted (FE#8, v0.10.0).
      if (!shortcutsEnabled() || dialogOpen()) return;
      // Ignore when typing in inputs, textareas, selects, or contentEditable
      const tag = (e.target as HTMLElement)?.tagName;
      if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || (e.target as HTMLElement)?.isContentEditable) return;
      // Ignore with modifier keys (Ctrl/Cmd/Alt) to avoid conflicts with browser shortcuts
      if (e.ctrlKey || e.metaKey || e.altKey) return;

      switch (e.key.toLowerCase()) {
        case "d": navigate("/"); break;
        case "s": navigate("/scanner"); break;
        case "q": navigate("/queue"); break;
        case "l": navigate("/logs"); break;
        case "h": navigate("/schedule"); break;
        case "m": navigate("/monitor"); break;
        case "e": navigate("/settings"); break;
        case " ": { // Space = toggle queue start/pause
          // Not while a button, link or other control has focus: Space
          // presses that control (v0.10.0 — it toggled the queue instead).
          const el = e.target as HTMLElement;
          if (el?.closest?.("button, a, summary, [role=button], [role=checkbox], [role=switch], [role=tab], [role=menuitem], [role=option], [role=radio]")) return;
          e.preventDefault();
          onToggleQueue();
          break;
        }
        default: break;
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [navigate, onToggleQueue]);

  return null;
}

function LoginScreen({ onLogin }: { onLogin: () => void }) {
  const { t } = useTranslation(["nav", "common"]);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  const handleLogin = async () => {
    setError("");
    setLoading(true);
    try {
      // Try username/password login first
      if (username && password) {
        try {
          await login(username, password);
          const check = await checkAuth();
          if (check.authenticated) {
            onLogin();
            return;
          }
        } catch {
          // Fall through to API key attempt
        }
      }

      // Try as API key (for backward compat — user might enter just a key in the password field)
      const keyToTry = password || username;
      if (keyToTry) {
        setStoredApiKey(keyToTry);
        const check = await checkAuth();
        if (check.authenticated) {
          onLogin();
          return;
        }
        setStoredApiKey("");
      }

      setError(t("nav:login.invalidCredentials"));
    } catch {
      setError(t("nav:login.connectionFailed"));
    } finally {
      setLoading(false);
    }
  };

  return (
    <div style={{ display: "flex", alignItems: "center", justifyContent: "center", minHeight: "100vh", background: "var(--bg-primary)" }}>
      <div style={{ background: "var(--bg-card)", padding: 32, borderRadius: 8, textAlign: "center", maxWidth: 360, width: "100%" }}>
        <img src="/shrinkerr-logo.svg" alt="Shrinkerr" height="32" style={{ marginBottom: 16 }} />
        <div style={{ color: "var(--text-muted)", fontSize: 12, marginBottom: 20 }}>{t("nav:login.subtitle")}</div>
        <input
          type="text"
          aria-label={t("nav:login.username")} placeholder={t("nav:login.username")}
          value={username}
          onChange={e => { setUsername(e.target.value); setError(""); }}
          onKeyDown={e => { if (e.key === "Enter") document.getElementById("sq-pw")?.focus(); }}
          style={{
            width: "100%", padding: "10px 14px", marginBottom: 8,
            backgroundColor: "var(--bg-primary)", color: "var(--text-secondary)",
            border: error ? "1px solid var(--danger)" : "1px solid var(--border)", borderRadius: 6,
            fontSize: 14, outline: "none", boxSizing: "border-box",
          }}
          autoFocus
        />
        <input
          id="sq-pw"
          type="password"
          aria-label={t("nav:login.password")} placeholder={t("nav:login.password")}
          value={password}
          onChange={e => { setPassword(e.target.value); setError(""); }}
          onKeyDown={e => { if (e.key === "Enter") handleLogin(); }}
          style={{
            width: "100%", padding: "10px 14px", marginBottom: 12,
            backgroundColor: "var(--bg-primary)", color: "var(--text-secondary)",
            border: error ? "1px solid var(--danger)" : "1px solid var(--border)", borderRadius: 6,
            fontSize: 14, outline: "none", boxSizing: "border-box",
          }}
        />
        {error && <div style={{ color: "var(--danger)", fontSize: 12, marginBottom: 8 }}>{error}</div>}
        <button className="btn btn-primary" style={{ width: "100%", opacity: loading ? 0.6 : 1 }}
          onClick={handleLogin} disabled={loading}>
          {loading ? t("nav:login.signingIn") : t("nav:login.signIn")}
        </button>
      </div>
    </div>
  );
}

// VMAF re-measure progress shape. The pass runs as a detached async
// task in the FastAPI process and broadcasts per-file progress +
// a final complete event. App-level banner shows progress only;
// completion summary is rendered inline next to the Settings →
// VMAF button (see VmafRemeasureRow in SettingsPage). v0.3.107+.
type VmafRemeasureState = { phase: "running"; done: number; total: number; current: string };

function AppContent() {
  const { t } = useTranslation(["nav", "common"]);
  const [authChecked, setAuthChecked] = useState(false);
  const [authenticated, setAuthenticated] = useState(false);
  const [scanProgress, setScanProgress] = useState<ScanProgress | null>(null);
  const [vmafRemeasure, setVmafRemeasure] = useState<VmafRemeasureState | null>(null);
  const [tmdbConfigured, setTmdbConfigured] = useState(true);  // default true → no banner flash before the check
  const { toasts, addToast, dismiss: dismissToast } = useToastState();
  // Theme: System (follows the OS), Light or Dark (v0.10.0). A browser with
  // no saved choice follows the OS; a saved Light / Dark is kept (the legacy
  // squeezarr_theme key too). Saved only when chosen — it used to be written
  // on every load, so "dark" is also what browsers that never chose have.
  const [themePref, setThemePref] = useState<ThemePref>(() => {
    try {
      const saved = localStorage.getItem("shrinkerr_theme") || localStorage.getItem("squeezarr_theme");
      if (saved === "dark" || saved === "light" || saved === "system") return saved;
    } catch { /* storage unavailable */ }
    return "system";
  });
  const osLight = () => !!window.matchMedia?.("(prefers-color-scheme: light)").matches;
  const [systemLight, setSystemLight] = useState(osLight);
  useEffect(() => {
    const mq = window.matchMedia?.("(prefers-color-scheme: light)");
    if (!mq) return;
    const sync = () => setSystemLight(mq.matches);
    mq.addEventListener("change", sync);
    // Not every browser / OS reports the change while the tab is hidden.
    document.addEventListener("visibilitychange", sync);
    return () => {
      mq.removeEventListener("change", sync);
      document.removeEventListener("visibilitychange", sync);
    };
  }, []);
  const theme: "dark" | "light" = themePref === "system" ? (systemLight ? "light" : "dark") : themePref;

  useEffect(() => {
    document.documentElement.setAttribute("data-theme", theme);
  }, [theme]);

  const chooseTheme = (pref: ThemePref) => {
    setThemePref(pref);
    setSystemLight(osLight());
    try {
      localStorage.setItem("shrinkerr_theme", pref);
      localStorage.removeItem("squeezarr_theme");  // clean up the legacy copy
    } catch { /* not saved: fine for this session */ }
  };

  // Update range slider fill color via inline background gradient.
  // NOTE: We used to run a MutationObserver on document.body subtree here to
  // catch sliders as they mount. That fired on every DOM mutation anywhere in
  // the app (every progress tick, every text-node update) and ran a full
  // querySelectorAll on the page — it was the primary CPU hog during encoding.
  // Now we only listen for `input` events globally (cheap, event-delegated)
  // and let SettingsPage initialize its own sliders via a ref on mount.
  useEffect(() => {
    const updateRange = (el: HTMLInputElement) => {
      const min = parseFloat(el.min) || 0;
      const max = parseFloat(el.max) || 100;
      const val = parseFloat(el.value) || 0;
      const pct = ((val - min) / (max - min)) * 100;
      el.style.background = `linear-gradient(to right, var(--accent) ${pct}%, var(--border) ${pct}%)`;
      el.style.borderRadius = "3px";
    };
    const handler = (e: Event) => {
      const tgt = e.target as HTMLElement;
      if (tgt?.tagName === "INPUT" && (tgt as HTMLInputElement).type === "range") {
        updateRange(tgt as HTMLInputElement);
      }
    };
    document.addEventListener("input", handler);
    return () => { document.removeEventListener("input", handler); };
  }, []);

  // Drop progress entries that stopped updating (see handleWS).
  useEffect(() => {
    const timer = setInterval(() => jobProgressStore.dropStale(120_000), 30_000);
    return () => clearInterval(timer);
  }, []);

  // A click whose request failed and that nobody handled (most Save buttons)
  // used to fail silently (v0.10.0). GET failures are left to the pages —
  // background polls fail while the server restarts.
  useEffect(() => {
    let last = { message: "", at: 0 };
    const onRejection = (e: PromiseRejectionEvent) => {
      const err = e.reason;
      if (!(err instanceof ApiRequestError) || err.method === "GET") return;
      const now = Date.now();
      if (err.message === last.message && now - last.at < 5000) return;
      last = { message: err.message, at: now };
      addToast(err.message, "error");
    };
    window.addEventListener("unhandledrejection", onRejection);
    return () => window.removeEventListener("unhandledrejection", onRejection);
  }, [addToast]);


  const handleWS = useCallback((msg: WSMessage) => {
    if (msg.type === "scan_progress") setScanProgress(msg as ScanProgress);
    if (msg.type === "job_progress") jobProgressStore.set(msg as JobProgress);
    // A job can end without a job_complete reaching us (node pause requeue,
    // the orphan reaper, a WebSocket gap): its entry kept the Queue
    // "running" with a phantom card (v0.10.0). Live jobs resend progress
    // within seconds, so a reconnect starts from an empty map.
    if (msg.type === "ws_reconnected") jobProgressStore.clear();
    if (msg.type === "job_complete") jobProgressStore.delete((msg as any).job_id);
    if (msg.type === "vmaf_remeasure_progress") {
      const m = msg as any;
      setVmafRemeasure({
        phase: "running",
        done: m.done ?? 0,
        total: m.total ?? 0,
        current: m.current_file || "",
      });
    }
    if (msg.type === "vmaf_remeasure_complete") {
      // Banner is progress-only. The inline outcome on Settings →
      // VMAF (VmafRemeasureRow) shows the rescued/unchanged/skipped
      // breakdown next to the button where the user clicked, which
      // is where they're looking. v0.3.107+.
      setVmafRemeasure(null);
    }
  }, []);

  useWebSocket(handleWS);

  // Check auth on mount
  useEffect(() => {
    checkAuth().then(r => {
      setAuthenticated(!r.auth_required || r.authenticated);
      setAuthChecked(true);
    }).catch(() => { setAuthenticated(true); setAuthChecked(true); });
  }, []);

  // v0.9.110: check whether a TMDB key is available (drives the banner below).
  useEffect(() => {
    if (!authenticated) return;
    getTmdbStatus().then(s => setTmdbConfigured(s.configured)).catch(() => {});
  }, [authenticated]);

  if (!authChecked) {
    return <div style={{ display: "flex", alignItems: "center", justifyContent: "center", minHeight: "100vh", background: "var(--bg-primary)" }}>
      <div className="spinner" />
    </div>;
  }

  if (!authenticated) {
    return <LoginScreen onLogin={() => setAuthenticated(true)} />;
  }

  return (
    <ToastProvider value={addToast}>
    <ConfirmProvider>
    <>
      <KeyboardShortcuts onToggleQueue={async () => {
        try {
          const stats = await getJobStats();
          if (stats.running > 0 || stats.pending > 0) {
            if (stats.running > 0) { await pauseQueue(); addToast(t("nav:toast.queuePaused")); }
            else { await startQueue(); addToast(t("nav:toast.queueStarted"), "success"); }
          } else {
            await startQueue();
            addToast(t("nav:toast.queueStarted"), "success");
          }
        } catch { /* ignore */ }
      }} />
      <ToastContainer toasts={toasts} onDismiss={dismissToast} />
      <div className="app-layout">
        {/* Desktop sidebar */}
        <aside className="sidebar sidebar-desktop">
          <div className="sidebar-logo">
            <img src={theme === "light" ? "/shrinkerr-logo-light.svg" : "/shrinkerr-logo.svg"} alt="Shrinkerr" width="160" style={{ flexShrink: 0 }} />
          </div>
          <nav className="sidebar-nav">
            <SidebarNavItems />
          </nav>
          <VersionBadge />
        </aside>

        {/* Mobile header */}
        <header className="mobile-header">
          <div className="sidebar-logo" style={{ margin: 0, padding: 0 }}>
            <img src={theme === "light" ? "/shrinkerr-logo-light.svg" : "/shrinkerr-logo.svg"} alt="Shrinkerr" height="22" style={{ flexShrink: 0 }} />
          </div>
          <MobileMenu />
        </header>

        <main className="main-content">
          {/* Global VMAF re-measure progress banner. Visible from any
              page so users know a remeasure pass started from Settings
              is still running. Disappears once the pass completes —
              the outcome summary lives inline next to the button on
              Settings → VMAF instead of duplicating up here. */}
          {vmafRemeasure && (
            <div style={{
              background: "var(--bg-card)",
              border: "1px solid var(--border)",
              borderRadius: 6,
              padding: "10px 14px",
              marginBottom: 12,
              display: "flex",
              alignItems: "center",
              gap: 12,
              fontSize: 12,
            }}>
              <div className="spinner" style={{ width: 14, height: 14, flexShrink: 0 }} />
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ color: "var(--text-secondary)", fontWeight: 600, marginBottom: 2 }}>
                  {t("nav:banner.vmafRemeasuring", { done: vmafRemeasure.done, total: vmafRemeasure.total })}
                </div>
                <div style={{
                  color: "var(--text-muted)",
                  whiteSpace: "nowrap",
                  overflow: "hidden",
                  textOverflow: "ellipsis",
                }} title={vmafRemeasure.current}>
                  {vmafRemeasure.current || t("nav:banner.vmafStarting")}
                </div>
              </div>
              <div style={{
                width: 120,
                height: 4,
                background: "var(--bg-primary)",
                borderRadius: 2,
                overflow: "hidden",
                flexShrink: 0,
              }}>
                <div style={{
                  width: `${vmafRemeasure.total > 0 ? (vmafRemeasure.done / vmafRemeasure.total) * 100 : 0}%`,
                  height: "100%",
                  background: "var(--accent)",
                  transition: "width 0.3s",
                }} />
              </div>
            </div>
          )}
          {/* v0.9.110: surface a missing TMDB key so self-built images (which
              ship without the bundled key) make it obvious why nothing matches. */}
          {!tmdbConfigured && (
            <div style={{
              background: "var(--bg-card)",
              border: "1px solid var(--warning)",
              borderRadius: 6,
              padding: "10px 14px",
              marginBottom: 12,
              display: "flex",
              alignItems: "center",
              gap: 10,
              fontSize: 12,
            }}>
              <span style={{ color: "var(--warning)", fontWeight: 700, flexShrink: 0 }}>⚠</span>
              <div style={{ flex: 1, minWidth: 0, color: "var(--text-secondary)" }}>
                <Trans i18nKey="nav:banner.tmdbMissing" components={{ b: <strong />, link: <NavLink to="/settings" style={{ color: "var(--accent-text)" }} /> }} />
              </div>
            </div>
          )}
          <RouteErrorBoundary>
          <Suspense fallback={<div className="spinner" style={{ width: 24, height: 24, margin: 40 }} />}>
          <Routes>
            <Route path="/" element={<DashboardPage />} />
            <Route path="/scanner" element={<ScannerPage scanProgress={scanProgress} onClearScanProgress={() => setScanProgress(null)} />} />
            <Route path="/queue" element={<QueuePage />} />
            <Route path="/logs" element={<LogsPage />} />
            <Route path="/doctor" element={<DoctorPage />} />
            <Route path="/nodes" element={<NodesPage />} />
            <Route path="/activity" element={<ActivityPage />} />
            <Route path="/schedule" element={<SchedulePage />} />
            <Route path="/monitor" element={<MonitorPage />} />
            <Route path="/settings/*" element={<SettingsPage themePref={themePref} onThemeChange={chooseTheme} />} />
            {/* Design-system reference page — no sidebar link; reach it directly at /design (dev builds) */}
            {DesignPage && <Route path="/design" element={<DesignPage />} />}
          </Routes>
          </Suspense>
          </RouteErrorBoundary>
        </main>
        <WhatsNewModal />
      </div>
    </>
    </ConfirmProvider>
    </ToastProvider>
  );
}

// A data router (v0.10.0): Settings warns before you leave it with unsaved
// changes (useBlocker), which needs one. The app's own <Routes> still do
// the routing, under this single catch-all.
const router = createBrowserRouter([{ path: "*", element: <AppContent /> }]);

export default function App() {
  return <RouterProvider router={router} />;
}
