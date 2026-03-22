import { BrowserRouter, Routes, Route, NavLink } from "react-router-dom";
import { useCallback, useState } from "react";
import { useWebSocket } from "./api";
import ScannerPage from "./pages/ScannerPage";
import QueuePage from "./pages/QueuePage";
import SchedulePage from "./pages/SchedulePage";
import SettingsPage from "./pages/SettingsPage";
import type { WSMessage, JobProgress, ScanProgress } from "./types";
import "./theme.css";

export default function App() {
  const [scanProgress, setScanProgress] = useState<ScanProgress | null>(null);
  const [jobProgress, setJobProgress] = useState<JobProgress | null>(null);

  const handleWS = useCallback((msg: WSMessage) => {
    if (msg.type === "scan_progress") setScanProgress(msg as ScanProgress);
    if (msg.type === "job_progress") setJobProgress(msg as JobProgress);
    if (msg.type === "job_complete") setJobProgress(null);
  }, []);

  useWebSocket(handleWS);

  return (
    <BrowserRouter>
      <div className="app-layout">
        <aside className="sidebar">
          <div className="sidebar-logo">Shrinkarr</div>
          <nav className="sidebar-nav">
            <NavLink to="/" end className={({isActive}) => `sidebar-link ${isActive ? "active" : ""}`}>
              Scanner
            </NavLink>
            <NavLink to="/queue" className={({isActive}) => `sidebar-link ${isActive ? "active" : ""}`}>
              Queue
            </NavLink>
            <NavLink to="/schedule" className={({isActive}) => `sidebar-link ${isActive ? "active" : ""}`}>
              Schedule
            </NavLink>
            <NavLink to="/settings" className={({isActive}) => `sidebar-link ${isActive ? "active" : ""}`}>
              Settings
            </NavLink>
          </nav>
        </aside>
        <main className="main-content">
          <Routes>
            <Route path="/" element={<ScannerPage scanProgress={scanProgress} />} />
            <Route path="/queue" element={<QueuePage jobProgress={jobProgress} />} />
            <Route path="/schedule" element={<SchedulePage />} />
            <Route path="/settings" element={<SettingsPage />} />
          </Routes>
        </main>
      </div>
    </BrowserRouter>
  );
}
