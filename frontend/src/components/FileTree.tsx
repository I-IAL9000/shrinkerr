import { useState } from "react";
import type { ScannedFile } from "../types";
import FileDetail from "./FileDetail";

interface FileTreeProps {
  files: ScannedFile[];
  selectedPaths: Set<string>;
  onToggleSelect: (path: string) => void;
  onToggleTrack: (filePath: string, streamIndex: number) => void;
  onRemoveFile: (filePath: string) => void;
}

interface TreeNode {
  name: string;
  path: string;
  children: Map<string, TreeNode>;
  files: ScannedFile[];
  totalSize: number;
  fileCount: number;
}

function buildTree(files: ScannedFile[]): TreeNode {
  const root: TreeNode = { name: "root", path: "", children: new Map(), files: [], totalSize: 0, fileCount: 0 };

  for (const file of files) {
    const parts = file.file_path.split("/").filter(Boolean);
    let node = root;
    for (let i = 0; i < parts.length - 1; i++) {
      const part = parts[i];
      if (!node.children.has(part)) {
        node.children.set(part, {
          name: part,
          path: "/" + parts.slice(0, i + 1).join("/"),
          children: new Map(),
          files: [],
          totalSize: 0,
          fileCount: 0,
        });
      }
      node = node.children.get(part)!;
      node.totalSize += file.file_size;
      node.fileCount += 1;
    }
    node.files.push(file);
  }

  return root;
}

function getAllFiles(node: TreeNode): ScannedFile[] {
  let files = [...node.files];
  for (const child of node.children.values()) {
    files = files.concat(getAllFiles(child));
  }
  return files;
}

function FolderNode({
  node, depth, selectedPaths, onToggleSelect, onToggleTrack, onRemoveFile,
}: {
  node: TreeNode; depth: number;
  selectedPaths: Set<string>;
  onToggleSelect: (path: string) => void;
  onToggleTrack: (filePath: string, streamIndex: number) => void;
  onRemoveFile: (filePath: string) => void;
}) {
  const [expanded, setExpanded] = useState(depth < 2);
  const allFiles = getAllFiles(node);
  const allSelected = allFiles.length > 0 && allFiles.every((f) => selectedPaths.has(f.file_path));

  const colorClass = depth === 0 ? "tree-folder" : depth === 1 ? "tree-subfolder" : "tree-season";

  return (
    <div style={{ paddingLeft: depth > 0 ? 16 : 0 }}>
      <div className="tree-row" onClick={() => setExpanded(!expanded)}>
        <input
          type="checkbox"
          checked={allSelected}
          onChange={(e) => {
            e.stopPropagation();
            allFiles.forEach((f) => onToggleSelect(f.file_path));
          }}
          style={{ accentColor: "var(--accent)" }}
        />
        <span className={colorClass}>
          {expanded ? "\u25BC" : "\u25B6"} {node.name}/
        </span>
        <span className="tree-file-size">
          {node.fileCount} files &middot; {(node.totalSize / (1024 ** 3)).toFixed(1)} GB
        </span>
      </div>
      {expanded && (
        <>
          {Array.from(node.children.values()).map((child) => (
            <FolderNode
              key={child.path} node={child} depth={depth + 1}
              selectedPaths={selectedPaths} onToggleSelect={onToggleSelect}
              onToggleTrack={onToggleTrack} onRemoveFile={onRemoveFile}
            />
          ))}
          {node.files.map((file) => (
            <FileNode
              key={file.file_path} file={file} depth={depth + 1}
              selected={selectedPaths.has(file.file_path)}
              onToggleSelect={onToggleSelect}
              onToggleTrack={onToggleTrack}
              onRemoveFile={onRemoveFile}
            />
          ))}
        </>
      )}
    </div>
  );
}

function FileNode({
  file, depth, selected, onToggleSelect, onToggleTrack, onRemoveFile,
}: {
  file: ScannedFile; depth: number; selected: boolean;
  onToggleSelect: (path: string) => void;
  onToggleTrack: (filePath: string, streamIndex: number) => void;
  onRemoveFile: (filePath: string) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const codecClass = file.needs_conversion ? "x264" : "x265";

  return (
    <div style={{ paddingLeft: depth * 16 }}>
      <div className="tree-row" onClick={() => setExpanded(!expanded)}>
        <input
          type="checkbox"
          checked={selected}
          onChange={(e) => { e.stopPropagation(); onToggleSelect(file.file_path); }}
          style={{ accentColor: "var(--accent)" }}
        />
        <span>{file.file_name}</span>
        <span className="tree-file-size">{file.file_size_gb} GB</span>
        <span className={`codec-badge ${codecClass}`}>
          {file.needs_conversion ? "x264" : "x265"}
        </span>
        <button
          onClick={(e) => { e.stopPropagation(); onRemoveFile(file.file_path); }}
          style={{ background: "none", border: "none", color: "var(--text-muted)", cursor: "pointer", marginLeft: 4 }}
        >
          &times;
        </button>
      </div>
      {expanded && (
        <FileDetail file={file} onToggleTrack={onToggleTrack} />
      )}
    </div>
  );
}

export default function FileTree({ files, selectedPaths, onToggleSelect, onToggleTrack, onRemoveFile }: FileTreeProps) {
  const tree = buildTree(files);

  return (
    <div className="tree-container">
      {Array.from(tree.children.values()).map((node) => (
        <FolderNode
          key={node.path} node={node} depth={0}
          selectedPaths={selectedPaths} onToggleSelect={onToggleSelect}
          onToggleTrack={onToggleTrack} onRemoveFile={onRemoveFile}
        />
      ))}
      {files.length === 0 && (
        <div style={{ textAlign: "center", padding: 40, opacity: 0.5 }}>
          No scan results. Select directories and click Scan to start.
        </div>
      )}
    </div>
  );
}
