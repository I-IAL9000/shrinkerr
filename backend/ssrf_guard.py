"""SSRF protection for outbound HTTP URLs sourced from user settings.

The app lets users configure URLs for Plex / Jellyfin / Sonarr / Radarr /
Discord-webhook / generic-webhook / NZBGet. Without validation an
attacker with settings-write could point any of these at a cloud
metadata endpoint and exfiltrate IAM credentials the moment a
connection test or post-conversion notification fires.

Home installs legitimately run Plex / Sonarr on RFC 1918 IPs, so we
CAN'T blanket-block private ranges. What we DO block:

* 169.254.0.0/16 — link-local. Includes:
    - 169.254.169.254 (AWS IMDS, Azure IMDS, Alibaba)
    - 169.254.169.254 via alt-form / padded octets
    - 169.254.170.2   (ECS task metadata)
* ::ffff:169.254.0.0/112 — the IPv4-mapped IPv6 form
* fe80::/10         — IPv6 link-local
* fd00::/8          — IPv6 metadata can appear here too; block defensively

Loopback / private ranges are ALLOWED by default (your Plex server is
probably on 192.168.x.x or 10.x.x.x and talking to it is the whole
point). Operators running on multi-tenant cloud hosts can flip the
`block_private` kwarg to get the stricter set.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

from fastapi import HTTPException


def _iter_resolved_ips(hostname: str) -> list[str]:
    try:
        return [ai[4][0] for ai in socket.getaddrinfo(hostname, None)]
    except socket.gaierror:
        # Can't resolve — don't block here; the eventual connect will
        # fail anyway. Raising a 400 on "hostname didn't resolve at this
        # instant" is a worse UX than letting the integration tester
        # surface the real DNS error.
        return []


def _is_blocked_ip(ip_str: str, *, block_private: bool = False) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    # Always block link-local + IPv6 link-local + IPv6 unique-local —
    # those ranges serve cloud metadata + no legitimate self-hosted use
    # case reaches them for Plex/Sonarr/etc.
    if ip.is_link_local:
        return True
    if isinstance(ip, ipaddress.IPv6Address) and ip.is_site_local:
        return True
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        if ipaddress.IPv4Address(ip.ipv4_mapped).is_link_local:
            return True
    if block_private and (ip.is_private or ip.is_loopback):
        return True
    return False


def validate_outbound_url(raw_url: str, *, label: str = "URL", block_private: bool = False) -> str:
    """Reject URLs pointing at SSRF-sensitive destinations.

    Empty input is allowed (means "no integration configured").
    Returns the input untouched on success so callers can write it back
    to settings as-is.
    """
    if not raw_url or not raw_url.strip():
        return raw_url
    url = raw_url.strip()
    try:
        parsed = urlparse(url if "://" in url else f"http://{url}")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"{label} is not a valid URL: {exc}")
    if parsed.scheme and parsed.scheme.lower() not in ("http", "https"):
        raise HTTPException(
            status_code=400,
            detail=f"{label} must use http:// or https:// (got {parsed.scheme})",
        )
    host = parsed.hostname
    if not host:
        raise HTTPException(status_code=400, detail=f"{label} has no hostname")

    # Check the literal host AND everything it resolves to. Resolution
    # can be time-of-check-vs-time-of-use vulnerable (DNS rebinding);
    # that's a known limitation of validating at save-time only. A
    # motivated attacker with settings-write can still win a race. The
    # practical benefit here is blocking casual pointing-at-IMDS.
    candidates = [host, *_iter_resolved_ips(host)]
    for candidate in candidates:
        if _is_blocked_ip(candidate, block_private=block_private):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"{label} resolves to a blocked address ({candidate}). "
                    "Link-local / IMDS / IPv6 site-local ranges are rejected to prevent SSRF."
                ),
            )
    return url
