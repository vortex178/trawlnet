"""Public-URL guard for fetches whose URL a model or a posting chose (stdlib only; nothing at import time).

check_url vets one URL: http(s), port 80/443, no credentials, and (with resolve) every address its host resolves to
is global. opener() adds the same check to every redirect hop. DNS rebinding between the check and the connection
is not covered.
"""
from __future__ import annotations

import ipaddress
import re
import socket
import urllib.error
import urllib.parse
import urllib.request

_6TO4 = ipaddress.ip_network("2002::/16")
_NAT64_EMBEDS = tuple(ipaddress.ip_network(n) for n in ("64:ff9b::/96", "::/96", "::ffff:0:0:0/96"))  # IPv4 in the last 32 bits
_SITE_LOCAL = ipaddress.ip_network("fec0::/10")  # deprecated, but is_global is True for it
_NAT64_LOCAL = ipaddress.ip_network("64:ff9b:1::/48")  # local-use NAT64: a gateway to private IPv4 ranges


def public(ip) -> bool:
    """A globally routable unicast address; IPv6 forms that embed an IPv4 address are judged by that address, the
    same on every Python version (6to4, NAT64, IPv4-mapped, -compatible and -translated). Other ranges follow
    ipaddress.is_global, which differs slightly between Python versions."""
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped:
        return public(mapped)
    if ip.version == 6:
        if ip in _6TO4:
            return public(ip.sixtofour)
        if ip in _NAT64_LOCAL or ip in _SITE_LOCAL:
            return False
        if any(ip in net for net in _NAT64_EMBEDS):
            return public(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
    return ip.is_global and not ip.is_multicast


def check_url(url: str, name: str = "url", resolve: bool = True) -> None:
    """Raise ValueError unless `url` is a public http(s) URL. `resolve=False` skips the lookup for URLs that are only
    stored, never fetched (a literal address is still checked)."""
    if re.search(r"[\x00-\x20\x7f]", url):  # urlsplit drops tabs/newlines, so the checked URL would differ
        raise ValueError(f"{name} must not contain control characters")
    parts = urllib.parse.urlsplit(url)
    host = (parts.hostname or "").lower().rstrip(".")
    try:
        port = parts.port
    except ValueError:
        port = -1
    if parts.scheme not in ("http", "https") or not host or parts.username or parts.password:
        raise ValueError(f"{name} must be an http(s) URL without credentials")
    if port not in (None, 80, 443):
        raise ValueError(f"{name} must use port 80 or 443")
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        raise ValueError(f"{name} must be a public address")
    try:  # a literal address needs no lookup, so it is checked even when resolve=False
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None and not public(literal):
        raise ValueError(f"{name} must be a public address")
    if not resolve:
        return
    try:
        addrs = socket.getaddrinfo(host, port or (80 if parts.scheme == "http" else 443), proto=socket.IPPROTO_TCP)
    except OSError:
        raise ValueError(f"{name}: cannot resolve {host}")
    for info in addrs:
        if not public(ipaddress.ip_address(info[4][0].split("%")[0])):
            raise ValueError(f"{name} must be a public address")


class _CheckedRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        try:
            check_url(newurl, "redirect")
        except ValueError as e:  # an OSError subclass, so callers treat it like any failed fetch
            raise urllib.error.URLError(f"refused redirect to {newurl[:200]}: {e}") from e
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(_CheckedRedirects)
