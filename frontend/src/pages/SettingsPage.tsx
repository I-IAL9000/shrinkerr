import { useState, useEffect } from "react";
import {
  getMediaDirs, addMediaDir, removeMediaDir,
  getEncodingSettings, updateEncodingSettings,
} from "../api";

export default function SettingsPage() {
  const [dirs, setDirs] = useState<any[]>([]);
  const [newPath, setNewPath] = useState("");
  const [newLabel, setNewLabel] = useState("");
  const [encoding, setEncoding] = useState<any>(null);

  useEffect(() => {
    loadDirs();
    getEncodingSettings().then(setEncoding);
  }, []);

  const loadDirs = () => getMediaDirs().then((r: any) => setDirs(Array.isArray(r) ? r : r.dirs || []));

  const handleAddDir = async () => {
    if (!newPath) return;
    await addMediaDir(newPath, newLabel);
    setNewPath("");
    setNewLabel("");
    loadDirs();
  };

  const handleRemoveDir = async (id: number) => {
    await removeMediaDir(id);
    loadDirs();
  };

  const handleSaveEncoding = async () => {
    if (!encoding) return;
    await updateEncodingSettings(encoding);
  };

  return (
    <div>
      <h2 style={{ color: "white", fontSize: 20, marginBottom: 20 }}>Settings</h2>

      <div style={{ background: "var(--bg-card)", padding: 20, borderRadius: 6, marginBottom: 20 }}>
        <h3 style={{ color: "white", marginBottom: 12 }}>Media Directories</h3>
        <div style={{
          background: "var(--bg-primary)", borderRadius: 4, padding: 8,
          fontFamily: "var(--font-mono)", fontSize: 12, marginBottom: 8,
        }}>
          {dirs.map((d: any) => (
            <div key={d.id} style={{ display: "flex", justifyContent: "space-between", padding: "4px 0" }}>
              <span>{d.path}</span>
              <button onClick={() => handleRemoveDir(d.id)}
                style={{ background: "none", border: "none", color: "var(--text-muted)", cursor: "pointer" }}>&times;</button>
            </div>
          ))}
          {dirs.length === 0 && <div style={{ opacity: 0.5 }}>No directories configured</div>}
        </div>
        <div style={{ display: "flex", gap: 8 }}>
          <input
            placeholder="Path (e.g., /media/Movies/HD 2020)"
            value={newPath} onChange={(e) => setNewPath(e.target.value)}
            style={{
              flex: 1, background: "var(--bg-primary)", color: "var(--text-secondary)",
              border: "1px solid var(--border)", padding: "6px 10px", borderRadius: 4, fontSize: 13,
            }}
          />
          <input
            placeholder="Label (optional)"
            value={newLabel} onChange={(e) => setNewLabel(e.target.value)}
            style={{
              width: 160, background: "var(--bg-primary)", color: "var(--text-secondary)",
              border: "1px solid var(--border)", padding: "6px 10px", borderRadius: 4, fontSize: 13,
            }}
          />
          <button className="btn btn-secondary" onClick={handleAddDir}>+ Add</button>
        </div>
      </div>

      {encoding && (
        <div style={{ background: "var(--bg-card)", padding: 20, borderRadius: 6, marginBottom: 20 }}>
          <h3 style={{ color: "white", marginBottom: 12 }}>Encoding Defaults</h3>
          <div style={{ display: "flex", flexDirection: "column", gap: 12, maxWidth: 400 }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
              <span style={{ opacity: 0.5 }}>Default encoder:</span>
              <select
                value={encoding.default_encoder}
                onChange={(e) => setEncoding({ ...encoding, default_encoder: e.target.value })}
                style={{
                  background: "var(--bg-primary)", color: "var(--text-secondary)",
                  border: "1px solid var(--border)", padding: "4px 8px", borderRadius: 4,
                }}
              >
                <option value="nvenc">NVENC (GPU)</option>
                <option value="libx265">libx265 (CPU)</option>
              </select>
            </div>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
              <span style={{ opacity: 0.5 }}>NVENC CQ:</span>
              <input type="number" value={encoding.nvenc_cq}
                onChange={(e) => setEncoding({ ...encoding, nvenc_cq: parseInt(e.target.value) })}
                style={{
                  width: 60, background: "var(--bg-primary)", color: "var(--text-secondary)",
                  border: "1px solid var(--border)", padding: "4px 8px", borderRadius: 4, textAlign: "center",
                }}
              />
            </div>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
              <span style={{ opacity: 0.5 }}>libx265 CRF:</span>
              <input type="number" value={encoding.libx265_crf}
                onChange={(e) => setEncoding({ ...encoding, libx265_crf: parseInt(e.target.value) })}
                style={{
                  width: 60, background: "var(--bg-primary)", color: "var(--text-secondary)",
                  border: "1px solid var(--border)", padding: "4px 8px", borderRadius: 4, textAlign: "center",
                }}
              />
            </div>
            <button className="btn btn-primary" onClick={handleSaveEncoding} style={{ alignSelf: "flex-start" }}>
              Save
            </button>
          </div>
        </div>
      )}

      {encoding && (
        <div style={{ background: "var(--bg-card)", padding: 20, borderRadius: 6 }}>
          <h3 style={{ color: "white", marginBottom: 12 }}>Audio Track Rules</h3>
          <div style={{ display: "flex", flexDirection: "column", gap: 12, maxWidth: 400 }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
              <span style={{ opacity: 0.5 }}>Always keep:</span>
              <input
                value={(encoding.always_keep_languages || []).join(", ")}
                onChange={(e) => setEncoding({
                  ...encoding,
                  always_keep_languages: e.target.value.split(",").map((s: string) => s.trim()).filter(Boolean),
                })}
                style={{
                  width: 200, background: "var(--bg-primary)", color: "var(--success)",
                  border: "1px solid var(--border)", padding: "4px 8px", borderRadius: 4,
                }}
              />
            </div>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
              <span style={{ opacity: 0.5 }}>Ignore unknown tracks:</span>
              <input
                type="checkbox"
                checked={encoding.ignore_unknown_tracks}
                onChange={(e) => setEncoding({ ...encoding, ignore_unknown_tracks: e.target.checked })}
                style={{ accentColor: "var(--accent)" }}
              />
            </div>
            <button className="btn btn-primary" onClick={handleSaveEncoding} style={{ alignSelf: "flex-start" }}>
              Save Audio Rules
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
