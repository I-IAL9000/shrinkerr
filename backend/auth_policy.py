"""Who has to sign in (v0.10.0): sign-in is required, as in Sonarr v4.

  auth_method    "forms"     the sign-in page (username and password)
                 "external"  a reverse proxy in front signs people in;
                             Shrinkerr asks nobody
                 ""          not set up yet: the UI shows the setup screen
                             (installs from before 0.10 with password login
                             on count as "forms")
  auth_required  "enabled"         everyone signs in
                 "local_disabled"  not from local addresses (loopback and
                                   private ranges, not proxied requests)

The API key works throughout, and the integration endpoints
(main._AUTH_ALWAYS_REQUIRED_PREFIXES) need it whatever these say.
"""
from __future__ import annotations

import ipaddress

METHODS = ("forms", "external")
REQUIRED = ("enabled", "local_disabled")
MIN_PASSWORD = 8
# A request that came through a proxy carries one of these: its client
# address is the proxy's, so it isn't counted as local.
_PROXY_HEADERS = ("x-forwarded-for", "x-real-ip", "forwarded")
# Local: loopback, the private ranges (RFC 1918), link-local and IPv6
# unique-local. Not CGNAT / Tailscale (100.64.0.0/10).
_LOCAL_NETS = tuple(ipaddress.ip_network(n) for n in (
    "127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16",
    "::1/128", "fc00::/7", "fe80::/10"))


def method(settings: dict) -> str:
    from backend import demo
    if demo.ENABLED:  # read-only and open to all (backend/demo.py)
        return "external"
    m = (settings.get("auth_method") or "").strip()
    if m in METHODS:
        return m
    return "forms" if settings.get("auth_enabled") else ""


def is_local(request) -> bool:
    """Loopback or a private address, connecting directly."""
    if any(request.headers.get(h) for h in _PROXY_HEADERS):
        return False
    host = getattr(getattr(request, "client", None), "host", None) or ""
    try:
        ip = ipaddress.ip_address(host.split("%")[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return any(ip in net for net in _LOCAL_NETS)


def describe(request) -> dict:
    """What is_local goes by, for Settings: the address this request comes
    from and whether a proxy header came with it."""
    return {"address": getattr(getattr(request, "client", None), "host", None) or "",
            "proxied": any(bool(request.headers.get(h)) for h in _PROXY_HEADERS),
            "local": is_local(request)}


def bypass(settings: dict, request) -> str | None:
    """"external" or "local" when this request needs no sign-in (outside
    the integration endpoints), else None."""
    m = method(settings)
    if m == "external":
        return "external"
    if m == "forms" and settings.get("auth_required") == "local_disabled" and is_local(request):
        return "local"
    return None
