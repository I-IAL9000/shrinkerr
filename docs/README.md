# Shrinkerr documentation

The top-level [README](../README.md) is the short version — image variants, a
docker-compose quick start, requirements. Everything in this folder goes
deeper.

## Guides

- [**Installation**](installation.md) — every installation scenario:
  bare docker-compose, Portainer, reverse proxy (Traefik / Nginx / Caddy),
  NVIDIA Container Toolkit setup, Windows + WSL2, first-run walkthrough.
- [**Remote workers**](remote-workers.md) — distributed encoding across
  machines. Registering a worker, path mappings, capability-based routing,
  per-node schedules, affinity, pausing, and the NVENC ↔ libx265 fallback
  chain.
- [**Encoding guide**](encoding-guide.md) — NVENC vs libx265, preset and
  quality tuning for each, CPU/GPU fallback settings, VMAF quality
  validation, and resolution-aware CQ.
- [**Rules and automation**](rules-and-automation.md) — encoding rules
  (conditions, actions, overrides), watch folders, Sonarr / Radarr /
  NZBGet / SABnzbd integration, scheduling (quiet hours, per-node hours),
  Plex / Jellyfin integration, batch rename.
- [**Best practices**](best-practices.md) — when to use which encoder,
  preset/CRF recommendations for common sources, how to size a worker
  fleet, backup strategy, what to set before your first batch.
- [**Troubleshooting**](troubleshooting.md) — symptoms and fixes, covering
  the VMAF and worker-performance saga from v0.3.10 through v0.3.19 plus
  classic issues (spinner on first launch, NVENC unavailable, AFP / NFS
  quirks).
- [**Security**](security.md) — threat model, in-app defences, and
  hardening checklist for production deployments.
- [**FAQ**](faq.md) — quick answers to recurring questions.

## Something missing?

Open an issue or PR. Docs live in `docs/` and any `.md` file on `main` is
served by GitHub as-is — no build step.
