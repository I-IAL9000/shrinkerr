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
  can execute code (e.g. the post-conversion script). Choosing "Not from
  local addresses" trusts local addresses with all of that.
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

- **Sign-in is required** (v0.10.0, as in Sonarr v4). Until it's set
  up — on a fresh install, and on an install upgraded from before 0.10
  without password login — the web UI shows a setup screen before
  anything else. Two ways to sign in:
  - **Sign-in page**: a username and password (8 characters or more).
    *Who signs in*: everyone, or **not from local addresses** (loopback,
    192.168.x.x, 10.x.x.x, 172.16–31.x.x, link-local, IPv6 unique-local).
    A request that carries `X-Forwarded-For`, `X-Real-IP` or `Forwarded`
    came through a proxy and always signs in, so a reverse proxy on the
    same host doesn't make internet traffic "local". Tailscale and other
    CGNAT addresses (100.64.0.0/10) aren't local, and neither is Docker's
    gateway (the host's side of the container's network): a request from
    it was passed on by something on the host, such as Tailscale or
    Docker's userland proxy, and could have come from anywhere. Under
    *Who signs in*, Settings shows the address Shrinkerr sees for your
    browser and whether it counts as local. If Docker rewrites addresses
    some other way (Docker Desktop), every request can still look local —
    so choose **Everyone** if Shrinkerr's port can be reached from outside
    your network.
  - **External**: your reverse proxy (Authelia, Authentik, …) signs
    people in and Shrinkerr asks nobody. Use it only when Shrinkerr can't
    be reached except through the proxy.

  Sign-in can't be turned off afterwards; switch to External instead.
  Until it's set up the API stays as open as before (so integrations keep
  working), and a loud `[SECURITY]` banner prints at startup. If an API
  key is set, setting up sign-in needs that key — nobody else can pick
  the password.
- **API keys keep working** everywhere, whatever the sign-in method — in
  the `X-Api-Key` header only (v0.10.0). A key in the URL (`?api_key=`)
  no longer works: it ended up in proxy logs and browser history.
  Prometheus can send it as a bearer token to `/api/metrics`, and
  webhooks as the Basic-auth password. The key reaches the whole API
  except the settings that can run code, so treat it like an admin
  password.
- **Live updates** (the WebSocket) connect with a one-time ticket the UI
  fetches first; each ticket works once, within a minute, so no key goes
  in a URL.
- **Passwords are stored as bcrypt hashes** (cost 12). Legacy SHA-256
  hashes from older installs are transparently upgraded on the next
  successful login.
- **Login is rate-limited** to 8 attempts per minute per IP.
- **API key and session comparisons are constant-time** (`hmac.compare_digest`).
- **Session signing uses a per-install secret** generated on first
  launch; sessions fail closed if the secret is ever missing.
- **Integration endpoints** (`/api/webhooks/*`, the remote-worker
  endpoints under `/api/nodes/`, backup download/restore, NZBGet/SABnzbd
  config + scripts) always require the API key in `X-Api-Key` (webhooks
  also take it as the Basic-auth password) or a password session — also
  from local addresses and with External sign-in.
- **Remote worker nodes carry a per-node auth token** on top of the
  shared `X-Api-Key`. The server issues a fresh token on the node's
  first heartbeat, the worker persists it in `/app/data/worker_token`
  (mode 0600), and every subsequent call sends it as `X-Node-Token`.
  Even if the shared API key leaks, an attacker who registers a new
  node can't impersonate an existing one: the server compares the
  supplied token with the stored value via `hmac.compare_digest` and
  returns 401 on mismatch. Admins can force a fresh handshake any time
  from **Nodes → Node settings → Rotate token**; the worker drops its
  cached copy on the next 401 and re-bootstraps automatically.
- **A remote worker can only report on its own job**: one that lost its
  job can't mark it failed or completed under the node that runs it now.
- **Demo mode** (`SHRINKERR_DEMO=1`, see [demo.md](demo.md)) is read-only
  and nobody signs in. Never set it on a real install: anyone who can
  reach the port sees your library and settings.

### Secrets handling

- **API key is masked** (`****xxxx`) in the bulk `/api/settings/encoding`
  response. A dedicated `GET /api/settings/api-key` returns the raw
  value on demand for the UI copy button.
- **Integration keys and tokens** (TMDB, Plex, Jellyfin, Emby, Sonarr,
  Radarr, Bazarr, SMTP password, Telegram bot token, ntfy and Gotify tokens)
  are all masked the same way; the MQTT password is masked in full.
- **Settings export** strips every secret row: `api_key`,
  `session_secret`, `auth_password_hash`, each integration key and
  token (MQTT password, ntfy and Gotify tokens included), SMTP password,
  and path-tokened URLs (Discord webhook, generic webhook, Telegram bot
  token, ntfy topic URL, Apprise URLs).
- **Settings import** never imports credentials or sign-in settings, and
  runs the same checks as saving in Settings.
- **Download diagnostics** (Doctor page) takes passwords, API keys and
  tokens out of the file it makes for bug reports.
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
- **Backup folder setting** validated the same way, and it can't overlap
  a media folder. Backup expiry and deleting backups (Settings →
  Automation) only ever delete backups.
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

User-configured outbound URLs (Plex, Jellyfin, Emby, Sonarr, Radarr,
Bazarr, Maintainerr, the MQTT broker, Discord webhook, generic webhook,
ntfy, Gotify) are validated at save-time against an IP block-list
covering:

- IPv4 link-local `169.254.0.0/16` — includes AWS / Azure / GCP / Alibaba
  cloud metadata endpoints
- IPv6 link-local `fe80::/10`
- IPv6 site-local `fec0::/10`
- the IPv6 cloud metadata addresses `fd00:ec2::254` (AWS) and `fd20:ce::254`
  (Google Cloud) — the rest of `fd00::/8` is allowed, as home networks use it
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

- **ffmpeg / ffprobe subprocess calls** use list args
  (`asyncio.create_subprocess_exec` or `subprocess.run`) — never
  `shell=True`, no string interpolation into a
  single command line. Paths with spaces / apostrophes / special chars
  are safe as individual argv entries.
- **VMAF filter-complex path** is sanitized (alphanumeric + `._-`)
  before interpolation into the filter string, so filenames with
  apostrophes don't break the filter parser.
- **LIKE query user input** is escaped with `ESCAPE '\\'` so `%` and
  `_` in user-supplied search strings don't act as wildcards (stops
  enumeration).
- **Custom ffmpeg flags** can't add inputs or outputs or name files, and
  encodes run in an empty scratch folder. A backup restored with just
  the API key comes back without its post-conversion script and custom
  flags; re-enter them signed in.
- **Links in the in-app changelog** only open http(s) addresses.

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
- [ ] **Use a strong password** (Settings → System → Authentication), and
      "Not from local addresses" only on a network you trust.
- [ ] **Rotate the API key if it may have leaked** — e.g. an install
      from before v0.3.65 printed its key to the log at first launch.
      Settings → System → Authentication → API Key → Regenerate makes a
      new, cryptographically random one.
- [ ] **Bind the container port to `127.0.0.1`** unless you actually
      want LAN access directly. Put the reverse proxy in front.
- [ ] **Set up sign-in** before configuring `post_conversion_script` or
      custom ffmpeg flags — the app refuses to save them until it's set
      up, and never with just the API key.
- [ ] **Run as your own user.** Set `PUID` / `PGID` (and optionally
      `UMASK`) and the container runs Shrinkerr as that user instead of
      root.
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
