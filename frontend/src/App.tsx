import { BrowserRouter, Routes, Route, NavLink, useLocation, useNavigate } from "react-router-dom";
import { useCallback, useState, useEffect } from "react";
import { useWebSocket, getNewFileCount, clearNewFileCount, getVersion, checkAuth, setStoredApiKey, startQueue, pauseQueue, getJobStats } from "./api";
import DashboardPage from "./pages/DashboardPage";
import ScannerPage from "./pages/ScannerPage";
import QueuePage from "./pages/QueuePage";
import SavedSpacePage from "./pages/SavedSpacePage";
import LogsPage from "./pages/LogsPage";
import SchedulePage from "./pages/SchedulePage";
import SettingsPage from "./pages/SettingsPage";
import MonitorPage from "./pages/MonitorPage";
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
  const isBeta = version.current.includes("beta");

  return (
    <div style={{ padding: "12px 20px", marginTop: "auto" }} className="version-badge">
      {version.update_available && (
        <div style={{
          fontSize: 11, padding: "4px 8px", marginBottom: 8, borderRadius: 4,
          background: "rgba(145,53,255,0.15)", color: "var(--accent)",
          display: "flex", alignItems: "center", gap: 6,
        }}>
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
            <path d="M12 2v10m0 0l3-3m-3 3l-3-3M5 12v7a2 2 0 002 2h10a2 2 0 002-2v-7"/>
          </svg>
          v{version.latest} available
        </div>
      )}
      <div style={{ display: "flex", alignItems: "flex-start", gap: 8 }}>
        <img src="/squeezarr-logo.svg" alt="" width="14" height="14" style={{ opacity: 0.4, marginTop: 2 }} />
        <div>
          <div style={{ fontSize: 12, color: "var(--text-muted)", fontWeight: 500 }}>
            Squeezarr{isBeta ? " Beta" : ""}
          </div>
          <div style={{ fontSize: 10, color: "var(--text-muted)", opacity: 0.5 }}>
            v{version.current}
          </div>
        </div>
      </div>
    </div>
  );
}

function NewFileBadge() {
  const [count, setCount] = useState(0);
  const location = useLocation();

  useEffect(() => {
    const check = () => getNewFileCount().then(r => setCount(r.count)).catch(() => {});
    check();
    const interval = setInterval(check, 30000);
    return () => clearInterval(interval);
  }, []);

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

const NAV_ITEMS = [
  { to: "/", label: "Dashboard", end: true, icon: <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/></svg> },
  { to: "/scanner", label: "Scanner", icon: <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/></svg>, badge: true },
  { to: "/queue", label: "Queue", icon: <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><rect x="3" y="3" width="18" height="18" rx="2"/><path d="M8 7h8M8 12h8M8 17h8"/></svg> },
  { to: "/monitor", label: "Monitor", icon: <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20z"/><path d="M12 12l4-4"/><path d="M8 16h.01"/><path d="M12 16h.01"/><path d="M16 16h.01"/><path d="M6 12h.01"/><path d="M18 12h.01"/></svg> },
  { to: "/saved", label: "Statistics", icon: <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><rect x="3" y="12" width="4" height="9"/><rect x="10" y="7" width="4" height="14"/><rect x="17" y="3" width="4" height="18"/></svg> },
  { to: "/logs", label: "Logs", icon: <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><polyline points="4 17 10 11 4 5"/><line x1="12" y1="19" x2="20" y2="19"/></svg> },
  { to: "/schedule", label: "Schedule", icon: <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg> },
  { to: "/settings", label: "Settings", icon: <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><line x1="4" y1="21" x2="4" y2="14"/><line x1="4" y1="10" x2="4" y2="3"/><line x1="12" y1="21" x2="12" y2="12"/><line x1="12" y1="8" x2="12" y2="3"/><line x1="20" y1="21" x2="20" y2="16"/><line x1="20" y1="12" x2="20" y2="3"/><line x1="1" y1="14" x2="7" y2="14"/><line x1="9" y1="8" x2="15" y2="8"/><line x1="17" y1="16" x2="23" y2="16"/></svg> },
];

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
              {NAV_ITEMS.map(item => (
                <NavLink key={item.to} to={item.to} end={item.end} className={({isActive}) => `sidebar-link ${isActive ? "active" : ""}`}>
                  {item.icon} {item.label}{item.badge && <NewFileBadge />}
                </NavLink>
              ))}
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
        case "t": navigate("/saved"); break;
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
  const [key, setKey] = useState("");
  const [error, setError] = useState(false);
  return (
    <div style={{ display: "flex", alignItems: "center", justifyContent: "center", minHeight: "100vh", background: "var(--bg-primary)" }}>
      <div style={{ background: "var(--bg-card)", padding: 32, borderRadius: 8, textAlign: "center", maxWidth: 360, width: "100%" }}>
        <img src="/squeezarr-logo.svg" alt="" width="40" height="40" style={{ marginBottom: 16 }} />
        <h2 style={{ color: "white", margin: "0 0 4px", fontSize: 20 }}>Squeezarr</h2>
        <div style={{ color: "var(--text-muted)", fontSize: 12, marginBottom: 20 }}>Enter your API key to continue</div>
        <input
          type="password"
          placeholder="API Key"
          value={key}
          onChange={e => { setKey(e.target.value); setError(false); }}
          onKeyDown={e => { if (e.key === "Enter") { setStoredApiKey(key); checkAuth().then(r => { if (r.authenticated) onLogin(); else setError(true); }); } }}
          style={{
            width: "100%", padding: "10px 12px", fontSize: 14, borderRadius: 4, border: error ? "1px solid #e94560" : "1px solid var(--border)",
            background: "var(--bg-primary)", color: "var(--text-secondary)", marginBottom: 12, boxSizing: "border-box",
          }}
          autoFocus
        />
        {error && <div style={{ color: "#e94560", fontSize: 12, marginBottom: 8 }}>Invalid API key</div>}
        <button className="btn btn-primary" style={{ width: "100%" }}
          onClick={() => { setStoredApiKey(key); checkAuth().then(r => { if (r.authenticated) onLogin(); else setError(true); }); }}
        >Sign In</button>
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
  const [theme, setTheme] = useState<"dark" | "light">(() => (localStorage.getItem("squeezarr_theme") as "dark" | "light") || "dark");

  useEffect(() => {
    document.documentElement.setAttribute("data-theme", theme);
    localStorage.setItem("squeezarr_theme", theme);
  }, [theme]);

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
            <img src="/squeezarr-logo.svg" alt="Squeezarr" width="24" height="24" style={{ flexShrink: 0 }} />
            <span style={{
              background: "linear-gradient(90deg, #5942DE 0%, #5564FB 100%)",
              WebkitBackgroundClip: "text",
              WebkitTextFillColor: "transparent",
              backgroundClip: "text",
              fontWeight: "bold",
              fontSize: 19,
            }}>Squeezarr</span>
          </div>
          <nav className="sidebar-nav">
            {NAV_ITEMS.map(item => (
              <NavLink key={item.to} to={item.to} end={item.end} className={({isActive}) => `sidebar-link ${isActive ? "active" : ""}`}>
                {item.icon} {item.label}{item.badge && <NewFileBadge />}
              </NavLink>
            ))}
          </nav>
          <VersionBadge />
          <button
            onClick={toggleTheme}
            style={{
              background: "none", border: "none", color: "var(--text-muted)",
              cursor: "pointer", padding: "4px 20px 12px", margin: 0,
              display: "flex", alignItems: "center", gap: 8, fontSize: 11,
              transition: "color 0.15s", opacity: 0.7,
            }}
            title={`Switch to ${theme === "dark" ? "light" : "dark"} mode`}
          >
            {theme === "dark" ? (
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="5"/><line x1="12" y1="1" x2="12" y2="3"/><line x1="12" y1="21" x2="12" y2="23"/><line x1="4.22" y1="4.22" x2="5.64" y2="5.64"/><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"/><line x1="1" y1="12" x2="3" y2="12"/><line x1="21" y1="12" x2="23" y2="12"/><line x1="4.22" y1="19.78" x2="5.64" y2="18.36"/><line x1="18.36" y1="5.64" x2="19.78" y2="4.22"/></svg>
            ) : (
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>
            )}
            {theme === "dark" ? "Light mode" : "Dark mode"}
          </button>
        </aside>

        {/* Mobile header */}
        <header className="mobile-header">
          <div className="sidebar-logo" style={{ margin: 0 }}>
            <img src="/squeezarr-logo.svg" alt="Squeezarr" width="20" height="20" style={{ flexShrink: 0 }} />
            <span style={{
              background: "linear-gradient(90deg, #5942DE 0%, #5564FB 100%)",
              WebkitBackgroundClip: "text",
              WebkitTextFillColor: "transparent",
              backgroundClip: "text",
              fontWeight: "bold",
              fontSize: 16,
            }}>Squeezarr</span>
          </div>
          <MobileMenu />
        </header>

        <main className="main-content">
          <Routes>
            <Route path="/" element={<DashboardPage jobProgressMap={jobProgressMap} />} />
            <Route path="/scanner" element={<ScannerPage scanProgress={scanProgress} onClearScanProgress={() => setScanProgress(null)} />} />
            <Route path="/queue" element={<QueuePage jobProgressMap={jobProgressMap} />} />
            <Route path="/saved" element={<SavedSpacePage />} />
            <Route path="/logs" element={<LogsPage />} />
            <Route path="/schedule" element={<SchedulePage />} />
            <Route path="/monitor" element={<MonitorPage />} />
            <Route path="/settings" element={<SettingsPage />} />
          </Routes>
        </main>
      </div>
    </BrowserRouter>
    </ConfirmProvider>
    </ToastProvider>
  );
}
