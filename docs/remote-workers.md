# Remote workers

Shrinkerr can offload encoding to other machines on your network. A common
setup: a lightweight server (NAS, NUC) keeps the database and UI, while a
beefier box (gaming PC, second GPU machine, ARM server) does the actual
encoding.

## Contents
- [When it's worth it](#when-its-worth-it)
- [Architecture in one paragraph](#architecture-in-one-paragraph)
- [Setting up a worker](#setting-up-a-worker)
- [Authentication (per-node tokens)](#authentication-per-node-tokens)
- [Path mappings](#path-mappings)
- [Capability-based job routing](#capability-based-job-routing)
- [Encoder translation (NVENC ↔ libx265)](#encoder-translation-nvenc--libx265)
- [Per-node controls](#per-node-controls)
- [Pause / schedule / affinity](#pause--schedule--affinity)
- [Circuit breaker](#circuit-breaker)
- [Verifying the setup](#verifying-the-setup)

## When it's worth it

- You have a second host with a faster CPU or a GPU and the primary server
  is CPU-only.
- You want encoding to happen on a gaming PC only during off-hours.
- You want two GPU hosts running different driver versions (e.g. a
  `:nvenc` box on driver 535 and an `:edge-nvenc` box on driver 570)
  sharing the same queue.

When it's **not** worth it:
- You have one GPU host. Just run Shrinkerr directly on it.
- Your "workers" would access the library over a slow network share. See
  the "path mappings" and "slow workers" sections — the I/O cost can
  dwarf the compute gain.

## Architecture in one paragraph

The primary Shrinkerr instance owns the database, queue, UI, and
WebSocket. Workers are the same image in `SHRINKERR_MODE=worker` — they
poll the primary for jobs over HTTP, run ffmpeg locally, and report
progress back. The primary never pushes to the worker; the worker always
initiates. That means workers on NAT'd / dynamic-IP networks Just Work as
long as they can reach the primary.

## Setting up a worker

On the primary, go to **Settings → System → Authentication** and copy the
API key. Then on the worker host:

```bash
docker run -d \
  --name shrinkerr-worker \
  -e SHRINKERR_MODE=worker \
  -e SERVER_URL=http://<primary-ip>:6680 \
  -e API_KEY=<primary-api-key> \
  -e WORKER_NAME="gaming-pc" \
  -v /mnt/media:/media \
  -v shrinkerr-worker-data:/app/data \
  --runtime=nvidia --gpus all \
  ghcr.io/i-ial9000/shrinkerr:nvenc     # or :latest for CPU-only worker
```

For CPU-only workers drop `--runtime=nvidia --gpus all` and use the
`:latest` image. Shrinkerr also offers a copy-paste snippet under
**Nodes → "Add a remote worker"** with the current server URL
pre-filled.

Within ~30 seconds the worker appears on the Nodes page with its
detected capabilities (`libx265`, `nvenc`, both).

**Environment variables**

| Variable | Required | Purpose |
|---|---|---|
| `SHRINKERR_MODE` | yes | `worker` to run as a worker, absent / `server` for the primary |
| `SERVER_URL` | yes | `http(s)://host:port` of the primary |
| `API_KEY` | yes | From Settings → System → Authentication on the primary |
| `WORKER_NAME` | no | Display name on the Nodes page. Defaults to the container hostname. |
| `CAPABILITIES` | no | Comma-separated override (e.g. `libx265`). If unset, the worker auto-detects. Useful to force a GPU host into CPU-only mode. |

## Authentication (per-node tokens)

From v0.3.30 onwards every remote worker carries a per-node auth token
*in addition* to the shared `API_KEY`. You don't configure it — the
handshake is automatic — but it's worth understanding what it does and
when you'd rotate it.

**Bootstrap.** The first time a worker with no cached token heartbeats
against the primary:

1. Worker sends heartbeat with only `X-Api-Key`.
2. Server sees no stored token for this `node_id`, generates one
   (`secrets.token_hex(24)`), persists it in the `worker_nodes` table,
   and returns it in the response body (`{"token": "..."}`).
3. Worker writes it to `/app/data/worker_token` (mode 0600) and sends
   `X-Node-Token: <token>` on every subsequent call.

From that point on the server rejects any call to `/api/nodes/request-job`
or `/api/nodes/report-*` that doesn't present the matching token, with
`hmac.compare_digest` on the compare. Even if an attacker steals the
shared `API_KEY`, they can't impersonate an existing registered node
without also getting the per-node token from that node's data volume.

**Rotation.** Go to **Nodes → [node] → Settings → Rotate token**. The
server NULLs the stored token immediately. On the worker's next call
it receives a 401, drops its cached copy (`/app/data/worker_token`),
and re-bootstraps on its next heartbeat. The worker doesn't need a
restart — the 401 handler is self-healing.

Rotating a node that's currently running a job will cause that job to
fail and be requeued (the worker's `report-progress` POST gets the 401
before the 200 it's expecting). Rotate during quiet hours, or pause
the node first, if that matters to you.

**Resetting a worker.** If you ever rebuild a worker host from scratch,
delete its data volume, or otherwise lose the cached token, just let it
reconnect — it will look like a brand-new node on the primary (new
heartbeat, empty stored token, fresh handshake). If you want the old
`node_id` back: **Nodes → [node] → Settings → Rotate token** before
starting the new worker, and the server will re-bootstrap against the
reinstalled worker on its first heartbeat.

**Local node.** The local node (`id=local`) runs in-process and doesn't
authenticate over HTTP, so it has no token. Rotating a local node is a
no-op.

**Upgrading server before workers.** v0.3.30+ servers require
`X-Node-Token` on every `/api/nodes/*` call from remote workers. A
pre-v0.3.30 worker against a v0.3.30 server will silently lock itself
out after its first heartbeat: the server bootstraps a token and stores
it, the old worker ignores the `"token"` field in the response, and
every subsequent call 401s. Server logs will print a diagnostic hint
(`[NODES] 401 for node 'X': server has a stored token but the request
sent no X-Node-Token...`) so you'll see it without tailing worker logs.

Two ways out:

1. **Upgrade the worker container** to v0.3.30+. Then clear the stale
   server-side token either by clicking "Rotate token" in the Node
   Settings modal, or by running
   `UPDATE worker_nodes SET token=NULL WHERE id!='local'` on the DB.
   The worker will re-bootstrap on its next heartbeat.

2. **Temporary escape hatch** — set
   `SHRINKERR_DISABLE_NODE_TOKENS=true` on the *server* container. This
   bypasses the token check entirely for heterogeneous fleets that
   can't all be upgraded simultaneously. The server prints a loud
   `[SECURITY] WARNING` on every startup while this is active. Unset
   it once every worker is on v0.3.30+.

## Path mappings

If the primary sees the library at `/media/TV/Foo (2020)/ep.mkv` but the
worker sees it at `/mnt/library/TV/Foo (2020)/ep.mkv`, you need a mapping
so the worker can find the file. Translation happens server-side: the
server rewrites its dispatched path using the node's mappings before the
worker receives it.

Two sources for mappings, in priority order (v0.3.31+):

**1. Admin override — Nodes → [node] → Settings → Path mappings.** Tick
"Override worker's env-var mappings" and add one row per prefix. Takes
effect immediately on save without touching the worker container. This
is the right choice when you want to tweak mappings without editing
`docker run` args.

**2. Worker's `PATH_MAPPINGS` env var** (fallback). JSON array of
`{server, worker}` pairs, set when you start the container:

```bash
-e PATH_MAPPINGS='[{"server":"/media","worker":"/mnt/library"}]'
```

The worker reports its env-var mappings to the server on every heartbeat,
and the server uses them whenever no UI override is set. Clearing the
admin override (via the checkbox in the UI) reverts to whatever the
worker reports.

Example:

```
Server path:  /media
Worker path:  /mnt/library
```

When the server dispatches `/media/TV/Foo/ep.mkv` to this worker, it
rewrites to `/mnt/library/TV/Foo/ep.mkv` before sending. Any written
output (encoded file, backup) is rewritten in the opposite direction on
report-complete, so the server's view of the result lands back under
`/media/`.

Multiple mappings are allowed — add one row per prefix.

**Important caveat — network mounts.** If the worker sees the library via
SMB / NFS / AFP, encoding writes go back over the network for every
chunk. On AFP over TCP we've measured a ~5× throughput penalty vs local
disk. Fine if the worker is on a fast LAN, rough on Wi-Fi. See
Troubleshooting for "Mac worker is slow".

## Capability-based job routing

Each node advertises what it can do. Job assignment considers:
- Job's requested encoder (`nvenc` / `libx265`)
- Node's capabilities
- Node's `translate_encoder` setting (Nodes → [node] → Settings)
- Node's `job_affinity` setting

If `translate_encoder` is on (default), a CPU-only node will accept NVENC
jobs and run them as libx265 (translating preset + CQ → preset + CRF, see
next section). With it off, incompatible jobs are rejected and stay in
the queue for another node.

If `job_affinity` is `nvenc_only`, the node only pulls jobs whose
requested encoder is nvenc. Useful for dedicating your GPU host to
GPU-appropriate work.

## Encoder translation (NVENC ↔ libx265)

When a job's requested encoder doesn't match the worker's capabilities,
Shrinkerr picks an effective preset and quality setting using this
priority chain (v0.3.18+):

1. **Per-job settings from an encoding rule** — if a rule set
   `libx265_preset` / `libx265_crf` (or the NVENC equivalents)
   explicitly, those are used verbatim.
2. **Cross-encoder fallback settings** — the "CPU fallback" pair
   (`nvenc_cpu_fallback_preset` + `nvenc_cpu_fallback_crf`) for NVENC →
   libx265, or "GPU fallback" pair for libx265 → NVENC, set in
   Settings → Video.
3. **Server's main settings for the target encoder** — only if that
   encoder is the server's `default_encoder`. Prevents leaking shipped
   defaults (e.g. `libx265 medium/CRF 20`) on NVENC-first servers.
4. **Translation table** — the final fallback.

**Translation table** (from `backend/worker_mode.py`):

| NVENC preset | libx265 preset | Rationale |
|---|---|---|
| p1 | ultrafast | Fastest end |
| p2 | superfast | |
| p3 | veryfast | |
| p4, p5 | fast | Default-ish |
| p6 | medium | |
| p7 | slow | Capped — libx265 `slower`/`veryslow` are exponentially slower on CPU |

CRF uses `nvenc_cq` 1:1 (similar perceptual quality, libx265's extra
efficiency shows up as a smaller file).

**Symmetric for libx265 → NVENC** (see Settings → Video → libx265
section → "GPU fallback"): pin specific NVENC preset + CQ for when a
GPU worker picks up a libx265 job.

## Per-node controls

Each node has its own settings (Nodes → [node] → Settings):

- **Max jobs** — how many concurrent jobs this node runs. Override the
  global `parallel_jobs`.
- **Pause** — node stops accepting new jobs. A running job finishes
  cleanly; no new work starts until unpaused.
- **Job affinity** — `any` / `cpu_only` / `nvenc_only`.
- **Translate encoder** — whether this node accepts jobs meant for the
  other encoder (with translation) or rejects them.
- **Schedule** — node-specific hours. Combines with global quiet hours.
- **Path mappings** — as above.

## Pause / schedule / affinity

Use cases:
- "Only encode on my gaming PC overnight": set Schedule on that node to
  `22:00 – 06:00`.
- "Primary GPU host handles NVENC; Mac mini handles CPU jobs": set
  `job_affinity=nvenc_only` on the GPU node and `job_affinity=cpu_only` on
  the Mac, `translate_encoder=false` on both.
- "Pause a noisy machine during movie night": toggle Pause on the node.
  Running job finishes cleanly; queue doesn't drain during the pause.

## Circuit breaker

If a node fails N consecutive jobs (tracked in
`worker_nodes.consecutive_failures`), Shrinkerr auto-pauses it and
flags it in the UI. Common cause: an unreachable network mount or a
failing GPU. Once you've fixed the underlying issue, unpause it.

## Verifying the setup

From the primary, Nodes page:
- Node's status is `online` or `working`
- Capabilities list what you expect
- `nvenc_unavailable_reason` is blank on GPU nodes

In the worker container's logs (`docker logs shrinkerr-worker`):
```
[NODES] Local node registered: capabilities=['libx265', 'nvenc'], ...
```
or for a remote worker started via `SHRINKERR_MODE=worker`:
```
[WORKER] Registered as '<name>' (id <uuid>) with server <url>
[WORKER] Heartbeat OK
```

First job dispatched to the worker will log:
```
[WORKER] Processing job 12345: /path/to/file.mkv
[CONVERT] Starting: /path/to/file.mkv (encoder=nvenc, duration=...)
[CONVERT] Settings: encoder=nvenc, preset=p6, cq=20, ...
```

If you see `[WORKER] Translated nvenc p6/CQ20 → libx265 medium/CRF20`,
you're on a CPU worker and the NVENC→libx265 translation ran; verify the
preset/CRF match what you expected via the priority chain above.
