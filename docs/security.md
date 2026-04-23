# Security

This page covers Shrinkerr's threat model, the security measures the app
takes by default, and a hardening checklist for production deployments.

## Threat model

**What this is:** a self-hosted media transcoder. It runs ffmpeg against
your library, exposes a web UI over HTTP, and integrates with other
services on the network (Plex, Sonarr, NZBGet, etc.). The operator is
the user; there's no multi-tenant model.

**What we protect against:**

- **Unauth network neighbours.** Anyone who can reach the port shouldn't
  be able to read settings, queue transcodes, or change anything that
  can execute code (e.g. the post-conversion script).
- **Session hijack / XSS.** A leaked cookie or in-browser code-injection
  attack on the SPA shouldn't immediately hand out the raw API key or
  stored integration secrets.
- **Malicious downloads.** The NZBGet / SABnzbd post-processing hook
  feeds filenames from the downloader — a poisoned filename or path
  shouldn't allow RCE or filesystem traversal.
- **Compromised integration target.** A hostile Plex / Sonarr instance
  shouldn't be able to push Shrinkerr into making requests against
  cloud metadata endpoints (169.254.169.254) or localhost admin panels.

**What we don't protect against:**

- The user running the container has read access to their library. If
  you run Shrinkerr with `--privileged` or as root, a compromised app
  process can reach anything the container user can reach.
- Physical / filesystem access to the Shrinkerr data volume (`./data`).
  Anyone who can read `shrinkerr.db` has every stored secret.
- Full DNS rebinding against configured integration URLs — the SSRF
  guard validates at save time, not on every request.

## What the app does by default

### Authentication

- **Fresh installs auto-generate a strong API key** on first startup and
  enable password auth. The generated key is printed once to the
  container logs; the operator reads it from there to configure
  workers / NZBGet / SABnzbd.
- **Existing installs with no auth configured** see a loud
  `[SECURITY]` warning banner on every startup and continue to work
  unauthenticated — the existing deployment isn't broken mid-upgrade,
  but the operator is told to fix it.
- **Passwords are stored as bcrypt hashes** (cost 12). Legacy SHA-256
  hashes from older installs are transparently upgraded on the next
  successful login.
- **Login is rate-limited** to 8 attempts per minute per IP.
- **API key and session comparisons are constant-time** (`hmac.compare_digest`).
- **Session signing uses a per-install secret** generated on first
  launch; sessions fail closed if the secret is ever missing.
- **Integration endpoints** (`/api/webhooks/*`, `/api/nodes/*`, backup
  download/restore, NZBGet/SABnzbd config + scripts) always require
  `X-Api-Key`, regardless of whether password auth is enabled.

### Secrets handling

- **API key is masked** (`****xxxx`) in the bulk `/api/settings/encoding`
  response. A dedicated `GET /api/settings/api-key` returns the raw
  value on demand for the UI copy button.
- **Integration keys and tokens** (TMDB, Plex, Jellyfin, Sonarr,
  Radarr, SMTP password) are all masked the same way.
- **Settings export** strips every secret row: `api_key`,
  `session_secret`, `auth_password_hash`, each integration key, SMTP
  password, and path-tokened URLs (Discord webhook, generic webhook,
  Telegram bot token).
- **TMDB API key** is sent as a `params=` arg rather than interpolated
  into URL strings, so it doesn't leak into httpx exception messages
  that may end up in logs.
- **Docker `.env` files** are excluded from the build context via
  `.dockerignore` so the only secret baked into images is the
  TMDB build-arg (which is TMDB-sanctioned for distribution).

### Path / filesystem safety

- **Media directories** must be absolute, existing directories. Paths
  under `/etc`, `/root`, `/proc`, `/sys`, `/boot`, `/dev`, `/app/data`,
  and the filesystem root itself are rejected.
- **Backup folder setting** validated the same way.
- **All user-supplied file paths** for delete / queue / probe operations
  are resolved (symlinks followed) and checked against configured media
  dirs with `os.path.commonpath`, not string `startswith` (which the
  old code used and was trivially defeatable by
  `"/media/../etc/hostname"`).
- **Webhook queue / scan endpoints** validate every supplied path
  before running ffprobe/ffmpeg — stops NZBGet post-processing from
  being used as an ffmpeg-against-arbitrary-files primitive.
- **Rename / backup write destinations** refuse to overwrite symlinks.

### SSRF protection

All user-configured outbound URLs (Plex, Sonarr, Radarr, Discord
webhook, generic webhook) are validated at save-time against an IP
block-list covering:

- IPv4 link-local `169.254.0.0/16` — includes AWS / Azure / GCP / Alibaba
  cloud metadata endpoints
- IPv6 link-local `fe80::/10`
- IPv6 site-local `fd00::/8`
- IPv4-mapped IPv6 variants of the above

Loopback and RFC 1918 private ranges are **allowed by default** —
home setups legitimately run integrations on LAN IPs. See
`backend/ssrf_guard.py` if you want to enable the stricter
`block_private=True` mode for a cloud deployment.

### Directory browser

`/api/settings/browse` (used when picking a new media directory) is
restricted: refuses to list the filesystem root, any system directory
(`_DISALLOWED_MEDIA_DIR_PREFIXES`), or non-directory targets.

### Injection surface

- **ffmpeg / ffprobe subprocess calls** use `asyncio.create_subprocess_exec`
  with list args — never `shell=True`, no string interpolation into a
  single command line. Paths with spaces / apostrophes / special chars
  are safe as individual argv entries.
- **VMAF filter-complex path** is sanitized (alphanumeric + `._-`)
  before interpolation into the filter string, so filenames with
  apostrophes don't break the filter parser.
- **LIKE query user input** is escaped with `ESCAPE '\\'` so `%` and
  `_` in user-supplied search strings don't act as wildcards (stops
  enumeration).

### Response headers

Every response carries:

- `X-Content-Type-Options: nosniff`
- `X-Frame-Options: DENY`
- `Content-Security-Policy: frame-ancestors 'none'`
- `Referrer-Policy: strict-origin-when-cross-origin`

Session cookies are `HttpOnly`, `SameSite=Lax`, and `Secure` when the
request arrived over HTTPS (detected via scheme + `X-Forwarded-Proto`).

## Hardening checklist

For a production / internet-exposed deployment, on top of the defaults:

- [ ] **Front with HTTPS.** Run Shrinkerr behind Traefik / Caddy / Nginx
      — see [installation.md § Reverse proxy](installation.md#reverse-proxy-setups).
      The app doesn't terminate TLS itself.
- [ ] **Set a strong password** in Settings → System → Authentication
      (if you prefer password auth over raw API keys).
- [ ] **Rotate the auto-generated API key.** The one printed at first
      launch is cryptographically random, but if you suspect the log
      was copied somewhere untrusted, Settings → System → API Key →
      Regenerate.
- [ ] **Bind the container port to `127.0.0.1`** unless you actually
      want LAN access directly. Put the reverse proxy in front.
- [ ] **Enable password auth** (`auth_enabled=true`) before configuring
      `post_conversion_script` — the app refuses to save that setting
      with password auth off, but configure it in the intended order.
- [ ] **Keep the `./data` volume private.** It's the single source of
      truth for every stored secret.
- [ ] **Don't expose port 6680 to the public internet** without at
      least password auth + TLS in front. A shared API key over
      cleartext HTTP on a public IP is not sufficient.
- [ ] **Update regularly.** Security fixes are documented in
      [CHANGELOG.md](../CHANGELOG.md) and tagged with `Security:` /
      `release: …security…` commit messages.

## Reporting vulnerabilities

Please email issues privately to the repo owner rather than opening
public GitHub issues — new-style GitHub Security Advisories also work:
<https://github.com/I-IAL9000/shrinkerr/security/advisories/new>.

## Known limitations / roadmap

### Deferred to the next release

- **Per-node worker tokens.** Currently remote workers authenticate
  with the shared `api_key` and identify themselves via a `node_id` in
  the request body. Anyone who holds the shared key can impersonate
  any registered node (send heartbeats, claim jobs, report completion)
  by sending the target's `node_id`. Fix in-flight: per-node tokens
  issued at registration, required on every subsequent call. Tracked
  for the next security-hardening release.

### Accepted

- **CSRF.** Session cookies use `SameSite=Lax`, which stops cross-site
  form POSTs. Self-hosted deployments with the UI behind a reverse
  proxy on the same origin as nothing else are the primary target;
  there's no embedded OAuth flow or third-party origin to generate
  cross-site tokens from. We haven't added double-submit CSRF tokens
  because they'd add friction without meaningful gain in this threat
  model. Cookie hijack via XSS is a larger risk and the CSP
  `frame-ancestors 'none'` header + `HttpOnly` cookie are the
  mitigations there.
- **Long session lifetime (30 days).** A stolen cookie is valid for 30
  days; `/api/auth/logout` drops the client copy but doesn't revoke
  server-side (the HMAC signature still verifies). This is standard
  for single-admin self-hosted apps; server-side revocation with a
  session table is on the roadmap but not urgent.
- **DNS rebinding against configured integration URLs.** SSRF
  validation runs at save time. A motivated attacker with settings-
  write can race the DNS resolution. In practice they already have
  settings-write, which has a bigger blast radius.
