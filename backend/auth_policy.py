"""Who has to sign in (v0.10.0): sign-in is required, as in Sonarr v4.

  auth_method    "forms"     the sign-in page (username and password)
                 "external"  a reverse proxy in front signs people in;
                             Shrinkerr asks nobody
                 ""          not set up yet: the UI shows the setup screen
                             (installs from before 0.10 with password login
                             on count as "forms")
  auth_required  "enabled"         everyone signs in
                 "local_disabled"  not from local addresses (loopback and
                                   private ranges, not proxied requests,
                                   not Docker's gateway)

The API key works throughout, and the integration endpoints
(main._AUTH_ALWAYS_REQUIRED_PREFIXES) need it whatever these say.
"""
from __future__ import annotations

import functools
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
# Docker's gateway is the host's side of the container's network: a request
# from it was passed on by something on the host (Tailscale, Docker's
# userland proxy), so it could have come from anywhere. Its address is
# private all the same; Tailscale and mobile-data visits got in as local.
_ROUTE_TABLE = "/proc/net/route"


def _read_gateway(path: str):
    """The default route's gateway in a Linux route table, or None."""
    try:
        with open(path) as f:
            for line in f.readlines()[1:]:
                fields = line.split()
                if len(fields) > 2 and fields[1] == "00000000" and fields[2] != "00000000":
                    return ipaddress.IPv4Address(int(fields[2], 16).to_bytes(4, "little"))
    except (OSError, ValueError):
        pass
    return None


@functools.lru_cache(maxsize=1)
def _gateway():
    return _read_gateway(_ROUTE_TABLE)


def method(settings: dict) -> str:
    from backend import demo
    if demo.ENABLED:  # read-only and open to all (backend/demo.py)
        return "external"
    m = (settings.get("auth_method") or "").strip()
    if m in METHODS:
        return m
    return "forms" if settings.get("auth_enabled") else ""


def _client_ip(request):
    host = getattr(getattr(request, "client", None), "host", None) or ""
    try:
        ip = ipaddress.ip_address(host.split("%")[0])
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip


def is_local(request) -> bool:
    """Loopback or a private address, connecting directly."""
    if any(request.headers.get(h) for h in _PROXY_HEADERS):
        return False
    ip = _client_ip(request)
    if ip is None or ip == _gateway():
        return False
    return any(ip in net for net in _LOCAL_NETS)


def describe(request) -> dict:
    """What is_local goes by, for Settings: the address this request comes
    from, whether a proxy header came with it, and whether the host passed
    it on (Docker's gateway)."""
    ip = _client_ip(request)
    return {"address": getattr(getattr(request, "client", None), "host", None) or "",
            "proxied": any(bool(request.headers.get(h)) for h in _PROXY_HEADERS),
            "relayed": ip is not None and ip == _gateway(),
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
