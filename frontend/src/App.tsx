import { BrowserRouter, Routes, Route, NavLink, useLocation, useNavigate } from "react-router-dom";
import React, { useCallback, useState, useEffect } from "react";
import { useWebSocket, getNewFileCount, clearNewFileCount, getFailedJobCount, getVersion, checkAuth, login, setStoredApiKey, startQueue, pauseQueue, getJobStats } from "./api";
import { useVisibleInterval } from "./useVisibleInterval";
import DashboardPage from "./pages/DashboardPage";
import ScannerPage from "./pages/ScannerPage";
import QueuePage from "./pages/QueuePage";
import LogsPage from "./pages/LogsPage";
import ActivityPage from "./pages/ActivityPage";
import NodesPage from "./pages/NodesPage";
import SchedulePage from "./pages/SchedulePage";
import SettingsPage from "./pages/SettingsPage";
import MonitorPage from "./pages/MonitorPage";
import DesignPage from "./pages/DesignPage";
import { useToastState, ToastProvider, ToastContainer } from "./useToast";
import { ConfirmProvider } from "./components/ConfirmModal";
import type { WSMessage, JobProgress, ScanProgress } from "./types";
import "./theme.css";

function VersionBadge() {
  const [version, setVersion] = useState<{ current: string; latest: string | null; update_available: boolean } | null>(null);

  useEffect(() => {
    getVersion().then(setVersion).catch(() => {});
  }, []);

  if (!version) return null;
  return (
    <div style={{ padding: "12px 0 24px", marginTop: "auto", display: "flex", alignItems: "center", justifyContent: "center", gap: 8 }} className="version-badge">
      <img src="/favicon.svg" alt="" width="16" height="17" />
      <span style={{ fontSize: 10, color: "#5c6778" }}>
        Shrinkerr v{version.current}
      </span>
    </div>
  );
}

function NewFileBadge() {
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
      {count} new
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
      background: "#e94560", color: "white", fontSize: 9, fontWeight: "bold",
      padding: "1px 5px", borderRadius: 8, marginLeft: 6, verticalAlign: "middle",
    }}>
      {count}
    </span>
  );
}

const SETTINGS_SECTIONS = [
  { id: "directories", label: "Directories" },
  { id: "video", label: "Video" },
  { id: "audio", label: "Audio" },
  { id: "subtitles", label: "Subtitles" },
  { id: "connections", label: "Connections" },
  { id: "rules", label: "Rules" },
  { id: "renaming", label: "Renaming" },
  { id: "automation", label: "Automation" },
  { id: "system", label: "System" },
];

interface NavItem {
  to: string;
  label: string;
  end?: boolean;
  icon: string;
  badge?: boolean;
  failedBadge?: boolean;
  section: string;
}

const NAV_SECTIONS: { label: string; items: NavItem[] }[] = [
  {
    label: "ENCODE",
    items: [
      { to: "/", label: "Dashboard", end: true, icon: "/icons/dashboard.svg", section: "ENCODE" },
      { to: "/scanner", label: "Scanner", icon: "/icons/search.svg", badge: true, section: "ENCODE" },
      { to: "/queue", label: "Queue", icon: "/icons/queue.svg", failedBadge: true, section: "ENCODE" },
      { to: "/nodes", label: "Nodes", icon: "/icons/nodes.svg", section: "ENCODE" },
    ],
  },
  {
    label: "SYSTEM",
    items: [
      { to: "/monitor", label: "Monitor", icon: "/icons/monitor.svg", section: "SYSTEM" },
      { to: "/activity", label: "Activity", icon: "/icons/activity.svg", section: "SYSTEM" },
      { to: "/logs", label: "Logs", icon: "/icons/terminal.svg", section: "SYSTEM" },
      { to: "/schedule", label: "Schedule", icon: "/icons/clock.svg", section: "SYSTEM" },
    ],
  },
  {
    label: "CONFIG",
    items: [
      { to: "/settings", label: "Settings", icon: "/icons/settings.svg", section: "CONFIG" },
    ],
  },
];

function SidebarNavItems() {
  const location = useLocation();
  return (
    <>
      {NAV_SECTIONS.map(section => (
        <div key={section.label}>
          <div className="sidebar-section-label">{section.label}</div>
          {section.items.map(item => (
            <React.Fragment key={item.to}>
              <NavLink to={item.to} end={item.end} className={({isActive}) => `sidebar-link ${isActive ? "active" : ""}`}>
                <img src={item.icon} alt="" width="18" height="18" />
                {item.label}
                {item.badge && <NewFileBadge />}
                {item.failedBadge && <FailedJobBadge />}
              </NavLink>
              {item.to === "/settings" && location.pathname === "/settings" && (
                <div className="settings-subnav">
                  {SETTINGS_SECTIONS.map(s => (
                    <a
                      key={s.id}
                      href={`#${s.id}`}
                      className="settings-subnav-link"
                      onClick={(e) => {
                        e.preventDefault();
                        document.getElementById(s.id)?.scrollIntoView({ behavior: "smooth" });
                      }}
                    >
                      {s.label}
                    </a>
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
  const [open, setOpen] = useState(false);
  const location = useLocation();

  // Close on navigation
  useEffect(() => { setOpen(false); }, [location.pathname]);

  return (
    <>
      <button className="hamburger-btn" onClick={() => setOpen(!open)} aria-label="Menu">
        <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
          {open ? <><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></> : <><line x1="3" y1="6" x2="21" y2="6"/><line x1="3" y1="12" x2="21" y2="12"/><line x1="3" y1="18" x2="21" y2="18"/></>}
        </svg>
      </button>
      {open && (
        <div className="mobile-menu-overlay" onClick={() => setOpen(false)}>
          <div className="mobile-menu" onClick={e => e.stopPropagation()}>
            <nav style={{ display: "flex", flexDirection: "column", gap: 2 }}>
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
        case " ": // Space = toggle queue start/pause
          e.preventDefault();
          onToggleQueue();
          break;
        default: break;
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [navigate, onToggleQueue]);

  return null;
}

function LoginScreen({ onLogin }: { onLogin: () => void }) {
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

      setError("Invalid credentials");
    } catch {
      setError("Connection failed");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div style={{ display: "flex", alignItems: "center", justifyContent: "center", minHeight: "100vh", background: "var(--bg-primary)" }}>
      <div style={{ background: "var(--bg-card)", padding: 32, borderRadius: 8, textAlign: "center", maxWidth: 360, width: "100%" }}>
        <img src="/shrinkerr-logo.svg" alt="Shrinkerr" height="32" style={{ marginBottom: 16 }} />
        <div style={{ color: "var(--text-muted)", fontSize: 12, marginBottom: 20 }}>Sign in to continue</div>
        <input
          type="text"
          placeholder="Username"
          value={username}
          onChange={e => { setUsername(e.target.value); setError(""); }}
          onKeyDown={e => { if (e.key === "Enter") document.getElementById("sq-pw")?.focus(); }}
          style={{
            width: "100%", padding: "10px 14px", marginBottom: 8,
            backgroundColor: "var(--bg-primary)", color: "var(--text-secondary)",
            border: error ? "1px solid #e94560" : "1px solid var(--border)", borderRadius: 6,
            fontSize: 14, outline: "none", boxSizing: "border-box",
          }}
          autoFocus
        />
        <input
          id="sq-pw"
          type="password"
          placeholder="Password"
          value={password}
          onChange={e => { setPassword(e.target.value); setError(""); }}
          onKeyDown={e => { if (e.key === "Enter") handleLogin(); }}
          style={{
            width: "100%", padding: "10px 14px", marginBottom: 12,
            backgroundColor: "var(--bg-primary)", color: "var(--text-secondary)",
            border: error ? "1px solid #e94560" : "1px solid var(--border)", borderRadius: 6,
            fontSize: 14, outline: "none", boxSizing: "border-box",
          }}
        />
        {error && <div style={{ color: "#e94560", fontSize: 12, marginBottom: 8 }}>{error}</div>}
        <button className="btn btn-primary" style={{ width: "100%", opacity: loading ? 0.6 : 1 }}
          onClick={handleLogin} disabled={loading}>
          {loading ? "Signing in..." : "Sign In"}
        </button>
      </div>
    </div>
  );
}

export default function App() {
  const [authChecked, setAuthChecked] = useState(false);
  const [authenticated, setAuthenticated] = useState(false);
  const [scanProgress, setScanProgress] = useState<ScanProgress | null>(null);
  const [jobProgressMap, setJobProgressMap] = useState<Map<number, JobProgress>>(new Map());
  const { toasts, addToast } = useToastState();
  // Read new key first, fall back to the legacy squeezarr_theme for users
  // upgrading from the old app name so they don't lose their theme pick.
  const [theme, setTheme] = useState<"dark" | "light">(() =>
    (localStorage.getItem("shrinkerr_theme") as "dark" | "light") ||
    (localStorage.getItem("squeezarr_theme") as "dark" | "light") ||
    "dark"
  );

  useEffect(() => {
    document.documentElement.setAttribute("data-theme", theme);
    localStorage.setItem("shrinkerr_theme", theme);
    localStorage.removeItem("squeezarr_theme");  // clean up the legacy copy
  }, [theme]);

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
      el.style.background = `linear-gradient(to right, #6860fe ${pct}%, #212533 ${pct}%)`;
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

  const toggleTheme = () => setTheme(t => t === "dark" ? "light" : "dark");

  const handleWS = useCallback((msg: WSMessage) => {
    if (msg.type === "scan_progress") setScanProgress(msg as ScanProgress);
    if (msg.type === "job_progress") {
      const jp = msg as JobProgress;
      setJobProgressMap(prev => {
        const next = new Map(prev);
        next.set(jp.job_id, jp);
        return next;
      });
    }
    if (msg.type === "job_complete") {
      const jc = msg as any;
      setJobProgressMap(prev => {
        const next = new Map(prev);
        next.delete(jc.job_id);
        return next;
      });
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
    <BrowserRouter>
      <KeyboardShortcuts onToggleQueue={async () => {
        try {
          const stats = await getJobStats();
          if (stats.running > 0 || stats.pending > 0) {
            if (stats.running > 0) { await pauseQueue(); addToast("Queue paused"); }
            else { await startQueue(); addToast("Queue started", "success"); }
          } else {
            await startQueue();
            addToast("Queue started", "success");
          }
        } catch { /* ignore */ }
      }} />
      <ToastContainer toasts={toasts} />
      <div className="app-layout">
        {/* Desktop sidebar */}
        <aside className="sidebar sidebar-desktop">
          <div className="sidebar-logo">
            <img src="/shrinkerr-logo.svg" alt="Shrinkerr" width="160" style={{ flexShrink: 0 }} />
          </div>
          <nav className="sidebar-nav">
            <SidebarNavItems />
          </nav>
          <VersionBadge />
        </aside>

        {/* Mobile header */}
        <header className="mobile-header">
          <div className="sidebar-logo" style={{ margin: 0, padding: 0 }}>
            <img src="/shrinkerr-logo.svg" alt="Shrinkerr" height="22" style={{ flexShrink: 0 }} />
          </div>
          <MobileMenu />
        </header>

        <main className="main-content">
          <Routes>
            <Route path="/" element={<DashboardPage jobProgressMap={jobProgressMap} />} />
            <Route path="/scanner" element={<ScannerPage scanProgress={scanProgress} onClearScanProgress={() => setScanProgress(null)} />} />
            <Route path="/queue" element={<QueuePage jobProgressMap={jobProgressMap} />} />
            <Route path="/logs" element={<LogsPage />} />
            <Route path="/nodes" element={<NodesPage />} />
            <Route path="/activity" element={<ActivityPage />} />
            <Route path="/schedule" element={<SchedulePage />} />
            <Route path="/monitor" element={<MonitorPage />} />
            <Route path="/settings" element={<SettingsPage theme={theme} onToggleTheme={toggleTheme} />} />
            {/* Design-system reference page — no sidebar link; reach it directly at /design */}
            <Route path="/design" element={<DesignPage />} />
          </Routes>
        </main>
      </div>
    </BrowserRouter>
    </ConfirmProvider>
    </ToastProvider>
  );
}
