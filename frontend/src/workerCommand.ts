// The Nodes page's copy-ready worker command (v0.10.0): docker run or
// compose, for an NVIDIA, Intel/AMD (VA-API / QSV) or CPU-only machine.

export type WorkerGpu = "nvidia" | "intel_amd" | "cpu";
export type WorkerFormat = "run" | "compose";

export interface WorkerCommandOptions {
  gpu: WorkerGpu;
  format: WorkerFormat;
  image: string;           // ghcr.io/i-ial9000/shrinkerr
  tag: string;             // matches the server: v0.10.0-nvenc, develop-nvenc...
  serverUrl: string;
  apiKey: string;
  workerName: string;      // "" = the container's hostname
  hostMediaPath: string;   // the library on the worker machine
  mediaRoot: string;       // where the server sees it — mounted there, no path mappings
}

/** A shell word, single-quoted when it needs to be (nothing expands inside). */
function sh(s: string): string {
  return /^[\w@%+=:,./-]+$/.test(s) ? s : `'${s.replace(/'/g, "'\\''")}'`;
}

/** A YAML list item, quoted when it needs to be. */
function yaml(s: string): string {
  return /^[\w@%+=:,./ -]+$/.test(s) && !/^[ -]|[ :]$/.test(s) && !s.includes(": ") ? s : JSON.stringify(s);
}

export function workerCommand(o: WorkerCommandOptions): string {
  const env = [
    "SHRINKERR_MODE=worker",
    `SERVER_URL=${o.serverUrl}`,
    `API_KEY=${o.apiKey}`,
    ...(o.workerName.trim() ? [`WORKER_NAME=${o.workerName.trim()}`] : []),
  ];
  const media = `${o.hostMediaPath.trim() || "/path/to/media"}:${o.mediaRoot}`;
  const image = `${o.image}:${o.tag}`;

  if (o.format === "run") {
    const lines = [
      "docker run -d",
      "--name shrinkerr-worker",
      "--restart unless-stopped",
      ...env.map(e => `-e ${sh(e)}`),
      `-v ${sh(media)}`,
      "-v shrinkerr-worker-data:/app/data",
      ...(o.gpu === "nvidia" ? ["--runtime=nvidia --gpus all"] : []),
      ...(o.gpu === "intel_amd" ? ["--device /dev/dri:/dev/dri", "--group-add video", "--group-add render"] : []),
      image,
    ];
    return lines.join(" \\\n  ");
  }

  const out = [
    "services:",
    "  shrinkerr-worker:",
    `    image: ${image}`,
    "    container_name: shrinkerr-worker",
    "    restart: unless-stopped",
    "    environment:",
    ...env.map(e => `      - ${yaml(e)}`),
    "    volumes:",
    `      - ${yaml(media)}`,
    "      - shrinkerr-worker-data:/app/data",
  ];
  if (o.gpu === "nvidia") {
    out.push("    deploy:", "      resources:", "        reservations:", "          devices:",
             "            - driver: nvidia", "              count: all", "              capabilities: [gpu]");
  }
  if (o.gpu === "intel_amd") {
    out.push("    devices:", "      - /dev/dri:/dev/dri", "    group_add:", "      - video",
             "      - render  # or its number: stat -c '%g' /dev/dri/renderD128");
  }
  out.push("", "volumes:", "  shrinkerr-worker-data:");
  return out.join("\n");
}
