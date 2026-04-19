/**
 * DesignPage — live component showcase / style reference.
 *
 * Not linked from the sidebar on purpose; this page is for developers and
 * designers to see every component family in one place so the app stays
 * visually consistent and variants don't drift over time.
 *
 * Mounted at /design. Pulls real CSS variables from the current theme, so
 * toggling dark/light in Settings updates this page live.
 */
import { useEffect, useState } from "react";
import { useToast } from "../useToast";


// ─── Primitives ──────────────────────────────────────────────────────────

function Section({ title, description, children }: { title: string; description?: string; children: React.ReactNode }) {
  return (
    <section style={{ marginBottom: 48, scrollMarginTop: 12 }} id={title.toLowerCase().replace(/\s+/g, "-")}>
      <h2 style={{ fontSize: 20, color: "var(--text-primary)", marginBottom: 4, fontWeight: 600 }}>{title}</h2>
      {description && <p style={{ fontSize: 12, color: "var(--text-muted)", marginBottom: 16, maxWidth: 640 }}>{description}</p>}
      <div style={{ background: "var(--bg-card)", borderRadius: 8, padding: 20, border: "1px solid var(--border)" }}>
        {children}
      </div>
    </section>
  );
}

function SubHead({ children }: { children: React.ReactNode }) {
  return <div style={{ fontSize: 11, textTransform: "uppercase", letterSpacing: 0.6, color: "var(--text-muted)", marginBottom: 10, marginTop: 16, fontWeight: 600 }}>{children}</div>;
}

function Row({ children, style }: { children: React.ReactNode; style?: React.CSSProperties }) {
  return <div style={{ display: "flex", gap: 12, flexWrap: "wrap", alignItems: "center", ...style }}>{children}</div>;
}


// ─── Color swatch ────────────────────────────────────────────────────────

function Swatch({ name, varName, description }: { name: string; varName: string; description?: string }) {
  const [hex, setHex] = useState("");
  useEffect(() => {
    // Resolve the CSS custom property to its current hex value for display.
    const v = getComputedStyle(document.documentElement).getPropertyValue(varName).trim();
    setHex(v);
  }, [varName]);
  return (
    <div style={{ width: 160, background: "var(--bg-primary)", borderRadius: 6, padding: 12, border: "1px solid var(--border)" }}>
      <div style={{ width: "100%", height: 56, borderRadius: 4, background: `var(${varName})`, border: "1px solid var(--border)", marginBottom: 8 }} />
      <div style={{ fontSize: 12, color: "var(--text-primary)", fontWeight: 600, marginBottom: 2 }}>{name}</div>
      <div style={{ fontSize: 10, color: "var(--text-muted)", fontFamily: "var(--font-mono)" }}>{varName}</div>
      {hex && <div style={{ fontSize: 10, color: "var(--text-muted)", fontFamily: "var(--font-mono)", marginTop: 2 }}>{hex}</div>}
      {description && <div style={{ fontSize: 10, color: "var(--text-muted)", marginTop: 4, lineHeight: 1.4 }}>{description}</div>}
    </div>
  );
}


// ─── Buttons ─────────────────────────────────────────────────────────────

function ButtonShowcase() {
  return (
    <>
      <SubHead>Primary actions</SubHead>
      <Row>
        <button className="btn btn-primary">Primary</button>
        <button className="btn btn-primary" disabled>Disabled</button>
      </Row>

      <SubHead>Secondary actions</SubHead>
      <Row>
        <button className="btn btn-secondary">Secondary</button>
        <button className="btn btn-secondary" disabled>Disabled</button>
      </Row>

      <SubHead>Pill buttons (toolbars)</SubHead>
      <Row>
        <button className="btn btn-secondary" style={{ fontSize: 12, padding: "6px 12px", borderRadius: 16, display: "inline-flex", alignItems: "center", gap: 4 }}>
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
          Add to queue
        </button>
        <button className="btn btn-secondary" style={{ fontSize: 12, padding: "6px 12px", borderRadius: 16, display: "inline-flex", alignItems: "center", gap: 4 }}>
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></svg>
          Rescan
        </button>
        <button className="btn btn-secondary" style={{ fontSize: 12, padding: "6px 12px", borderRadius: 16, display: "inline-flex", alignItems: "center", gap: 4, color: "#e94560", borderColor: "#e94560" }}>
          Destructive pill
        </button>
      </Row>

      <SubHead>Small destructive (re-download corrupt)</SubHead>
      <Row>
        <button style={{ background: "#e94560", color: "#fff", border: "1px solid #e94560", borderRadius: 4, padding: "4px 10px", fontSize: 11, fontWeight: 600, cursor: "pointer", display: "inline-flex", alignItems: "center", gap: 6 }}>
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M21 12a9 9 0 11-3-6.7L21 8"/><path d="M21 3v5h-5"/></svg>
          Re-download (corrupt file)
        </button>
        <button style={{ background: "transparent", color: "var(--text-muted)", border: "1px solid var(--border)", borderRadius: 4, padding: "4px 10px", fontSize: 11, cursor: "pointer", display: "inline-flex", alignItems: "center", gap: 6 }}>
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><polyline points="17 11 12 6 7 11"/><polyline points="17 18 12 13 7 18"/></svg>
          Search for upgrade
        </button>
      </Row>

      <SubHead>Plex Connect (branded)</SubHead>
      <Row>
        <button style={{ background: "#e5a00d", color: "#1f1f1f", border: "none", borderRadius: 6, padding: "10px 18px", fontSize: 14, fontWeight: 600, cursor: "pointer", display: "inline-flex", alignItems: "center", gap: 8 }}>
          <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><path d="M11.644 1.59a.9.9 0 0 1 .712 0l9 4.5a.9.9 0 0 1 .544.826v10.168a.9.9 0 0 1-.544.826l-9 4.5a.9.9 0 0 1-.712 0l-9-4.5a.9.9 0 0 1-.544-.826V6.916a.9.9 0 0 1 .544-.826l9-4.5Z"/></svg>
          Connect to Plex
        </button>
      </Row>

      <SubHead>Icon buttons</SubHead>
      <Row>
        <button style={{ background: "none", border: "none", color: "var(--text-muted)", cursor: "pointer", padding: 4 }} title="Delete">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-2 14a2 2 0 01-2 2H9a2 2 0 01-2-2L5 6m3 0V4a2 2 0 012-2h4a2 2 0 012 2v2"/></svg>
        </button>
        <button style={{ background: "none", border: "none", color: "var(--accent)", cursor: "pointer", padding: 4 }} title="Edit">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>
        </button>
      </Row>
    </>
  );
}


// ─── Badges & pills ──────────────────────────────────────────────────────

function BadgeShowcase() {
  const pill = (background: string, color: string, text: string) => (
    <span style={{ fontSize: 9, padding: "1px 5px", borderRadius: 3, background, color, fontWeight: 600, textTransform: "uppercase", letterSpacing: 0.3 }}>{text}</span>
  );
  return (
    <>
      <SubHead>Priority</SubHead>
      <Row>
        {pill("rgba(233,69,96,0.15)", "#e94560", "Highest")}
        {pill("rgba(255,169,77,0.15)", "#ffa94d", "High")}
        {pill("var(--border)", "var(--text-muted)", "Normal")}
      </Row>

      <SubHead>Plex server badges</SubHead>
      <Row>
        {pill("rgba(229,160,13,0.2)", "#e5a00d", "Owned")}
        {pill("rgba(0,200,100,0.15)", "var(--success)", "Local")}
        {pill("var(--border)", "var(--text-muted)", "Relay")}
        {pill("rgba(0,200,100,0.15)", "var(--success)", "Reachable")}
        {pill("rgba(231,76,60,0.2)", "var(--danger)", "Unreachable")}
      </Row>

      <SubHead>Health & status</SubHead>
      <Row>
        <span style={{ fontSize: 11, color: "#fff", background: "var(--danger)", padding: "1px 6px", borderRadius: 3, fontWeight: 600 }}>Corrupt</span>
        <span style={{ fontSize: 11, color: "#fff", background: "var(--success)", padding: "1px 6px", borderRadius: 3, fontWeight: 600 }}>Healthy</span>
        <span style={{ fontSize: 11, color: "var(--text-muted)", background: "var(--border)", padding: "1px 6px", borderRadius: 3 }}>Ignored</span>
        <span style={{ fontSize: 11, color: "var(--success)" }}>saved 1.2 GB</span>
      </Row>

      <SubHead>Language-source</SubHead>
      <Row>
        <span style={{ fontSize: 9, padding: "1px 4px", borderRadius: 3, background: "rgba(0,200,100,0.15)", color: "var(--success)" }}>from API</span>
        <span style={{ fontSize: 9, padding: "1px 4px", borderRadius: 3, background: "var(--border)", color: "var(--text-muted)" }}>heuristic</span>
        <span style={{ fontSize: 9, color: "var(--warning)" }}>FORCED</span>
      </Row>
    </>
  );
}


// ─── Status icons (completed row left-gutter) ───────────────────────────

function StatusIconShowcase() {
  const Tile = ({ children, label, hint }: { children: React.ReactNode; label: string; hint?: string }) => (
    <div style={{ width: 200, padding: 12, border: "1px solid var(--border)", borderRadius: 6, background: "var(--bg-primary)" }}>
      <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 6 }}>
        {children}
        <span style={{ fontSize: 13, fontWeight: 500, color: "var(--text-primary)" }}>{label}</span>
      </div>
      {hint && <div style={{ fontSize: 10, color: "var(--text-muted)" }}>{hint}</div>}
    </div>
  );
  return (
    <Row>
      <Tile label="Green check" hint="Healthy completion with real savings">
        <span style={{ color: "var(--success)", fontSize: 18 }}>&#x2713;</span>
      </Tile>
      <Tile label="Amber info-circle" hint="Warnings OR no-savings ignored">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#ffa94d" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
          <circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/>
        </svg>
      </Tile>
      <Tile label="Red warning triangle" hint="Corrupt — health check found errors">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="var(--danger)" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
          <path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/>
          <line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/>
        </svg>
      </Tile>
      <Tile label="Status dots" hint="Node online / working / error / offline">
        <div style={{ display: "flex", gap: 6 }}>
          <span style={{ width: 10, height: 10, borderRadius: "50%", background: "var(--success)" }} />
          <span style={{ width: 10, height: 10, borderRadius: "50%", background: "var(--accent)", boxShadow: "0 0 6px var(--accent)" }} />
          <span style={{ width: 10, height: 10, borderRadius: "50%", background: "var(--danger)" }} />
          <span style={{ width: 10, height: 10, borderRadius: "50%", background: "var(--text-muted)" }} />
        </div>
      </Tile>
    </Row>
  );
}


// ─── Inputs ──────────────────────────────────────────────────────────────

function InputShowcase() {
  const [v, setV] = useState("some value");
  const [c1, setC1] = useState(true);
  const [c2, setC2] = useState(false);
  return (
    <>
      <SubHead>Text input</SubHead>
      <Row>
        <input type="text" value={v} onChange={e => setV(e.target.value)} placeholder="Type here…" style={{ padding: "8px 12px", background: "var(--bg-primary)", color: "var(--text-secondary)", border: "1px solid var(--border)", borderRadius: 4, fontSize: 13, minWidth: 220 }} />
        <input type="password" value="mysecret" readOnly style={{ padding: "8px 12px", background: "var(--bg-primary)", color: "var(--text-secondary)", border: "1px solid var(--border)", borderRadius: 4, fontSize: 13, minWidth: 220 }} />
        <input type="text" disabled placeholder="Disabled" style={{ padding: "8px 12px", background: "var(--bg-tertiary)", color: "var(--text-muted)", border: "1px solid var(--border)", borderRadius: 4, fontSize: 13, minWidth: 220, opacity: 0.5 }} />
      </Row>

      <SubHead>Checkbox</SubHead>
      <Row>
        <label style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 13, color: "var(--text-secondary)" }}>
          <input type="checkbox" checked={c1} onChange={() => setC1(!c1)} style={{ accentColor: "var(--accent)" }} />
          Checked
        </label>
        <label style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 13, color: "var(--text-secondary)" }}>
          <input type="checkbox" checked={c2} onChange={() => setC2(!c2)} style={{ accentColor: "var(--accent)" }} />
          Unchecked
        </label>
        <label style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 13, color: "var(--text-muted)", opacity: 0.5 }}>
          <input type="checkbox" checked disabled />
          Disabled
        </label>
      </Row>

      <SubHead>Radio group</SubHead>
      <Row style={{ gap: 16 }}>
        <label style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 13, color: "var(--text-secondary)" }}>
          <input type="radio" name="demo" defaultChecked style={{ accentColor: "var(--accent)" }} />
          Option A
        </label>
        <label style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 13, color: "var(--text-secondary)" }}>
          <input type="radio" name="demo" style={{ accentColor: "var(--accent)" }} />
          Option B
        </label>
        <label style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 13, color: "var(--text-secondary)" }}>
          <input type="radio" name="demo" style={{ accentColor: "var(--accent)" }} />
          Option C
        </label>
      </Row>
    </>
  );
}


// ─── Toasts (static mocks — not fired through the real system) ──────────

function ToastShowcase() {
  const toast = useToast();
  const Mock = ({ kind, children }: { kind?: "" | "success" | "error"; children: React.ReactNode }) => (
    <div className={`toast ${kind || ""}`} style={{ position: "relative", top: 0, right: 0, minWidth: 220, animation: "none" }}>{children}</div>
  );
  return (
    <>
      <SubHead>Variants</SubHead>
      <Row>
        <Mock>Queue paused</Mock>
        <Mock kind="success">Jobs added to queue</Mock>
        <Mock kind="error">Re-download failed: Sonarr unreachable</Mock>
      </Row>
      <SubHead>Live fire</SubHead>
      <Row>
        <button className="btn btn-secondary" onClick={() => toast("Info toast", "info")}>Trigger info</button>
        <button className="btn btn-secondary" onClick={() => toast("Success toast", "success")}>Trigger success</button>
        <button className="btn btn-secondary" onClick={() => toast("Error toast", "error")}>Trigger error</button>
      </Row>
    </>
  );
}


// ─── Cards (real-ish mocks) ──────────────────────────────────────────────

function CardShowcase() {
  return (
    <>
      <SubHead>Job row (completed — healthy)</SubHead>
      <div style={{ background: "var(--bg-primary)", padding: "10px 12px", borderRadius: 6, display: "flex", alignItems: "center", gap: 12 }}>
        <span style={{ display: "inline-flex", alignItems: "center", gap: 6, width: 34 }}>
          <span style={{ color: "var(--success)", fontSize: 14 }}>&#x2713;</span>
          <span style={{ fontSize: 10, color: "var(--text-muted)", opacity: 0.5 }}>▶</span>
        </span>
        <span style={{ flex: 1, minWidth: 0, fontSize: 13, color: "var(--text-primary)" }}>
          Bluey (2018) - S01E01 - Magic Xylophone - 1080p WEB-DL DDP5.1 H.264-CAKES.mkv
          <span style={{ marginLeft: 8, fontSize: 11, opacity: 0.4 }}>2.1 GB</span>
          <span style={{ marginLeft: 8, fontSize: 9, padding: "1px 5px", borderRadius: 6, fontWeight: "bold", background: "rgba(255,169,77,0.15)", color: "#ffa94d" }}>HIGH</span>
        </span>
        <span style={{ color: "var(--success)", fontSize: 11 }}>saved 842 MB</span>
      </div>

      <SubHead>Job row (completed — ignored, no savings)</SubHead>
      <div style={{ background: "var(--bg-primary)", padding: "10px 12px", borderRadius: 6, display: "flex", alignItems: "center", gap: 12 }}>
        <span style={{ display: "inline-flex", alignItems: "center", gap: 6, width: 34 }}>
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#ffa94d" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
            <circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/>
          </svg>
          <span style={{ fontSize: 10, color: "var(--text-muted)", opacity: 0.5 }}>▶</span>
        </span>
        <span style={{ flex: 1, minWidth: 0, fontSize: 13, color: "var(--text-primary)" }}>The Voice Kids (UK) - S05E03 - Final - 1080p HDTV AAC 2.0 h264-DARKFLiX.mkv</span>
        <span style={{ fontSize: 11, color: "var(--text-muted)", background: "var(--border)", padding: "1px 6px", borderRadius: 3 }}>Ignored</span>
      </div>

      <SubHead>Job row (running, progress)</SubHead>
      <div style={{ background: "var(--bg-primary)", padding: "10px 12px", borderRadius: 6 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 6 }}>
          <div className="spinner" style={{ width: 14, height: 14 }} />
          <span style={{ flex: 1, minWidth: 0, fontSize: 13, color: "var(--text-primary)" }}>Converting: Encanto (2021).mkv</span>
          <span style={{ fontSize: 11, color: "var(--text-muted)" }}>ETA 4m · 78 fps</span>
        </div>
        <div style={{ height: 4, background: "var(--border)", borderRadius: 2, overflow: "hidden" }}>
          <div style={{ width: "62%", height: "100%", background: "var(--accent)" }} />
        </div>
      </div>

      <SubHead>Metric card (gauge summary)</SubHead>
      <div style={{ background: "var(--bg-primary)", padding: 16, borderRadius: 6, maxWidth: 340 }}>
        <div style={{ fontSize: 13, fontWeight: 600, color: "var(--text-primary)", marginBottom: 12 }}>GPU — Quadro P2200</div>
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 8, marginBottom: 8 }}>
          {[{ label: "GPU", val: 78, color: "var(--accent)" }, { label: "VRAM", val: 42, color: "#74c0fc" }, { label: "Power", val: 65, color: "#ffa94d" }].map(g => (
            <div key={g.label} style={{ textAlign: "center" }}>
              <div style={{ fontSize: 18, fontWeight: 700, color: g.color }}>{g.val}%</div>
              <div style={{ fontSize: 10, color: "var(--text-muted)" }}>{g.label}</div>
            </div>
          ))}
        </div>
        <div style={{ display: "flex", justifyContent: "space-between", fontSize: 12, marginBottom: 4 }}>
          <span style={{ color: "var(--text-muted)" }}>Temperature</span>
          <span style={{ color: "var(--success)", fontWeight: 600 }}>56°C</span>
        </div>
        <div style={{ display: "flex", justifyContent: "space-between", fontSize: 12 }}>
          <span style={{ color: "var(--text-muted)" }}>NVENC</span>
          <span style={{ color: "#74c0fc", fontWeight: 600 }}>34%</span>
        </div>
      </div>

      <SubHead>Node card</SubHead>
      <div style={{ background: "var(--bg-primary)", padding: 14, borderRadius: 6, border: "1px solid var(--accent)", maxWidth: 380 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 10 }}>
          <span style={{ width: 8, height: 8, borderRadius: "50%", background: "var(--accent)", boxShadow: "0 0 6px var(--accent)" }} />
          <div style={{ flex: 1 }}>
            <div style={{ fontSize: 13, fontWeight: 600, color: "var(--text-primary)" }}>Mac Studio</div>
            <div style={{ fontSize: 10, color: "var(--text-muted)" }}>192.168.1.42 · job #1423</div>
          </div>
        </div>
        <Row style={{ gap: 24, justifyContent: "space-evenly" }}>
          {[{ label: "CPU", val: 42 }, { label: "RAM", val: 58, color: "#74c0fc" }, { label: "GPU", val: 0, color: "var(--text-muted)" }].map(g => (
            <div key={g.label} style={{ textAlign: "center", flex: 1 }}>
              <div style={{ fontSize: 18, fontWeight: 700, color: (g as any).color || "var(--text-primary)" }}>{g.val}%</div>
              <div style={{ fontSize: 10, color: "var(--text-muted)" }}>{g.label}</div>
            </div>
          ))}
        </Row>
      </div>
    </>
  );
}


// ─── Progress / spinners ─────────────────────────────────────────────────

function ProgressShowcase() {
  return (
    <>
      <SubHead>Spinners</SubHead>
      <Row style={{ alignItems: "center", gap: 16 }}>
        <div className="spinner" style={{ width: 14, height: 14 }} />
        <div className="spinner" style={{ width: 22, height: 22 }} />
        <div className="spinner" style={{ width: 32, height: 32 }} />
      </Row>

      <SubHead>Progress bars</SubHead>
      <div style={{ maxWidth: 480 }}>
        {[25, 50, 75, 100].map(v => (
          <div key={v} style={{ marginBottom: 8 }}>
            <div style={{ fontSize: 11, color: "var(--text-muted)", marginBottom: 4 }}>{v}%</div>
            <div style={{ height: 6, background: "var(--border)", borderRadius: 3, overflow: "hidden" }}>
              <div style={{ width: `${v}%`, height: "100%", background: v === 100 ? "var(--success)" : "var(--accent)" }} />
            </div>
          </div>
        ))}
      </div>
    </>
  );
}


// ─── Event timeline icons ────────────────────────────────────────────────

const EVENT_KINDS: Array<{ type: string; label: string; color: string; svg: React.ReactNode }> = [
  { type: "scanned",      label: "Scanned",      color: "var(--text-muted)", svg: <><circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/></> },
  { type: "queued",       label: "Queued",       color: "var(--accent)",     svg: <polygon points="6 4 20 12 6 20 6 4"/> },
  { type: "completed",    label: "Completed",    color: "var(--success)",    svg: <polyline points="20 6 9 17 4 12"/> },
  { type: "failed",       label: "Failed",       color: "var(--danger)",     svg: <><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></> },
  { type: "skipped",      label: "Skipped",      color: "var(--text-muted)", svg: <><polygon points="5 4 15 12 5 20 5 4"/><line x1="19" y1="5" x2="19" y2="19"/></> },
  { type: "ignored",      label: "Ignored",      color: "var(--text-muted)", svg: <><circle cx="12" cy="12" r="10"/><line x1="4.93" y1="4.93" x2="19.07" y2="19.07"/></> },
  { type: "health_check", label: "Health check", color: "var(--success)",    svg: <path d="M22 12h-4l-3 9L9 3l-3 9H2"/> },
  { type: "vmaf",         label: "VMAF",         color: "var(--accent)",     svg: <><line x1="18" y1="20" x2="18" y2="10"/><line x1="12" y1="20" x2="12" y2="4"/><line x1="6" y1="20" x2="6" y2="14"/><line x1="3" y1="20" x2="21" y2="20"/></> },
  { type: "reverted",     label: "Reverted",     color: "var(--warning)",    svg: <><polyline points="1 4 1 10 7 10"/><path d="M3.51 15a9 9 0 1 0 2.13-9.36L1 10"/></> },
  { type: "arr_action",   label: "*arr action",  color: "#e5a00d",           svg: <><path d="M21 12a9 9 0 11-3-6.7L21 8"/><path d="M21 3v5h-5"/></> },
];

function TimelineShowcase() {
  return (
    <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(160px, 1fr))", gap: 10 }}>
      {EVENT_KINDS.map(k => (
        <div key={k.type} style={{ padding: 10, border: "1px solid var(--border)", borderRadius: 4, background: "var(--bg-primary)", display: "flex", alignItems: "center", gap: 10 }}>
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke={k.color} strokeWidth={2} strokeLinecap="round" strokeLinejoin="round">
            {k.svg}
          </svg>
          <div>
            <div style={{ fontSize: 12, color: "var(--text-primary)" }}>{k.label}</div>
            <div style={{ fontSize: 10, color: "var(--text-muted)", fontFamily: "var(--font-mono)" }}>{k.type}</div>
          </div>
        </div>
      ))}
    </div>
  );
}


// ─── Typography scale ───────────────────────────────────────────────────

function TypeShowcase() {
  return (
    <div style={{ lineHeight: 1.4 }}>
      <div style={{ fontSize: 24, fontWeight: 700, color: "var(--text-primary)", marginBottom: 4 }}>Display / 24 / 700</div>
      <div style={{ fontSize: 20, fontWeight: 600, color: "var(--text-primary)", marginBottom: 4 }}>Page title / 20 / 600</div>
      <div style={{ fontSize: 16, fontWeight: 600, color: "var(--text-primary)", marginBottom: 4 }}>Section heading / 16 / 600</div>
      <div style={{ fontSize: 14, fontWeight: 600, color: "var(--text-primary)", marginBottom: 4 }}>Card heading / 14 / 600</div>
      <div style={{ fontSize: 13, color: "var(--text-secondary)", marginBottom: 4 }}>Body / 13 / 400</div>
      <div style={{ fontSize: 12, color: "var(--text-muted)", marginBottom: 4 }}>Caption / 12 / 400</div>
      <div style={{ fontSize: 11, color: "var(--text-muted)", marginBottom: 4, textTransform: "uppercase", letterSpacing: 0.6, fontWeight: 600 }}>Eyebrow / 11 / 600</div>
      <div style={{ fontSize: 10, color: "var(--text-muted)", fontFamily: "var(--font-mono)" }}>Mono / 10 / JetBrains Mono</div>
    </div>
  );
}


// ─── Main page ───────────────────────────────────────────────────────────

export default function DesignPage() {
  const toc = [
    "Colors", "Typography", "Buttons", "Badges", "Status icons",
    "Inputs", "Toasts", "Cards", "Progress", "Event timeline",
  ];
  return (
    <div>
      <div style={{ marginBottom: 24 }}>
        <h1 style={{ fontSize: 24, color: "var(--text-primary)", fontWeight: 700, marginBottom: 6 }}>Design System</h1>
        <p style={{ fontSize: 13, color: "var(--text-muted)", maxWidth: 680 }}>
          Live reference for every component family in Shrinkerr. Use this page to verify consistency,
          spot drift, and hand off pixel references to Figma. All swatches resolve to the current theme's
          CSS variables — toggle dark/light in Settings and this page follows.
        </p>
      </div>

      {/* Table of contents */}
      <nav style={{ display: "flex", flexWrap: "wrap", gap: 6, marginBottom: 32 }}>
        {toc.map(t => (
          <a key={t} href={`#${t.toLowerCase().replace(/\s+/g, "-")}`} style={{
            fontSize: 11, padding: "4px 10px", borderRadius: 12, background: "var(--bg-card)",
            color: "var(--text-secondary)", textDecoration: "none", border: "1px solid var(--border)",
          }}>
            {t}
          </a>
        ))}
      </nav>

      <Section title="Colors" description="Core theme tokens. Use var(--name) in CSS/inline styles — never hard-code hex.">
        <SubHead>Surfaces</SubHead>
        <Row>
          <Swatch name="BG Primary"   varName="--bg-primary"   description="Main page background" />
          <Swatch name="BG Secondary" varName="--bg-secondary" description="Sidebar, panels" />
          <Swatch name="BG Tertiary"  varName="--bg-tertiary"  description="Hover, raised" />
          <Swatch name="BG Card"      varName="--bg-card"      description="Cards, stat blocks" />
          <Swatch name="Border"       varName="--border"       description="Dividers, outlines" />
        </Row>

        <SubHead>Brand / accent</SubHead>
        <Row>
          <Swatch name="Accent"       varName="--accent"       description="Primary brand purple" />
          <Swatch name="Accent hover" varName="--accent-hover" />
          <Swatch name="Accent bg"    varName="--accent-bg"    description="Subtle accent tint" />
          <Swatch name="Accent btn"   varName="--accent-btn"   description="Button deep-press" />
        </Row>

        <SubHead>Semantic</SubHead>
        <Row>
          <Swatch name="Success" varName="--success" description="Saved, healthy, online" />
          <Swatch name="Warning" varName="--warning" description="Attention, amber" />
          <Swatch name="Danger"  varName="--danger"  description="Error, corrupt, destructive" />
        </Row>

        <SubHead>Text</SubHead>
        <Row>
          <Swatch name="Primary"   varName="--text-primary"   description="Headings, emphasis" />
          <Swatch name="Secondary" varName="--text-secondary" description="Body text" />
          <Swatch name="Muted"     varName="--text-muted"     description="Labels, captions" />
        </Row>
      </Section>

      <Section title="Typography" description="Type scale — 24/20/16/14/13/12/11/10. Weights: 400 body, 600 emphasis, 700 display.">
        <TypeShowcase />
      </Section>

      <Section title="Buttons" description="Primary/secondary, pill-shaped toolbar buttons, small destructive actions, branded Plex button, icon-only.">
        <ButtonShowcase />
      </Section>

      <Section title="Badges" description="Pills for priority, Plex server attributes, health status, language source.">
        <BadgeShowcase />
      </Section>

      <Section title="Status icons" description="Row-gutter icons for completed jobs, plus node status dots.">
        <StatusIconShowcase />
      </Section>

      <Section title="Inputs" description="Text, password, disabled, checkbox, radio group.">
        <InputShowcase />
      </Section>

      <Section title="Toasts" description="Three variants: info (default), success (green), error (red). 3-second auto-dismiss.">
        <ToastShowcase />
      </Section>

      <Section title="Cards" description="Real-ish mocks of the main recurring layouts — job row, metric card, node card.">
        <CardShowcase />
      </Section>

      <Section title="Progress" description="Spinners and bars.">
        <ProgressShowcase />
      </Section>

      <Section title="Event timeline" description="Icons used in the History tab and Activity page.">
        <TimelineShowcase />
      </Section>
    </div>
  );
}
