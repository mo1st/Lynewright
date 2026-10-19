"""Typed URL and DNS validation outcomes used by outbound fetchers."""
from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit


class URLSecurityError(ValueError):
    """A URL is unsafe or unsupported."""


class PrivateAddressError(URLSecurityError):
    """A hostname resolves to at least one disallowed address."""


class UnsupportedURLScheme(URLSecurityError):
    """Only HTTP and HTTPS URLs may be fetched."""


class DNSResolutionError(Exception):
    """A hostname could not be resolved; this is not an SSRF verdict."""


class TemporaryDNSResolutionError(DNSResolutionError):
    pass


class PermanentDNSResolutionError(DNSResolutionError):
    pass


@dataclass(frozen=True)
class ResolvedPublicTarget:
    scheme: str
    hostname: str
    port: int
    path_and_query: str
    addresses: tuple[str, ...]
    selected_address: str


def resolve_hostname_addresses(hostname: str) -> list[str]:
    try:
        infos = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        code = getattr(exc, "errno", None)
        message = str(exc).lower()
        temporary_codes = {value for value in (getattr(socket, "EAI_AGAIN", None),) if value is not None}
        if code in temporary_codes or "temporary" in message or "try again" in message:
            raise TemporaryDNSResolutionError("DNS resolution temporarily failed.") from exc
        raise PermanentDNSResolutionError("Hostname could not be resolved.") from exc

    addresses = list(dict.fromkeys(str(info[4][0]) for info in infos if info[4]))
    if not addresses:
        raise TemporaryDNSResolutionError("DNS resolution returned no addresses.")
    return addresses


_NAT64 = ipaddress.ip_network("64:ff9b::/96")


def is_disallowed_address(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    if isinstance(ip, ipaddress.IPv6Address):
        # An IPv4 address wrapped in IPv6 reaches the IPv4 host; judge that host.
        if ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        elif ip in _NAT64:
            ip = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return any(
        (
            # "Not global" also covers shared address space (100.64.0.0/10)
            # and other special-purpose ranges that is_private omits.
            not ip.is_global,
            ip.is_private,
            ip.is_loopback,
            ip.is_link_local,
            ip.is_multicast,
            ip.is_reserved,
            ip.is_unspecified,
        )
    )


def reject_disallowed_addresses(addresses: list[str]) -> None:
    """Fail closed when any answer is private, internal, or reserved."""
    try:
        blocked = any(is_disallowed_address(address) for address in addresses)
    except ValueError as exc:
        raise PermanentDNSResolutionError("DNS returned an invalid address.") from exc
    if blocked:
        raise PrivateAddressError(
            "URL resolves to a private or internal IP address (including reserved addresses)."
        )


def resolve_public_target(url: str) -> ResolvedPublicTarget:
    """Resolve and approve one URL hop for an IP-pinned connection."""
    try:
        parsed = urlsplit(url)
        hostname = (parsed.hostname or "").strip().rstrip(".").lower()
        port = parsed.port
    except ValueError as exc:
        raise URLSecurityError("URL hostname or port is invalid.") from exc
    if parsed.scheme not in {"http", "https"}:
        raise UnsupportedURLScheme("Only HTTP and HTTPS URLs are allowed.")
    if parsed.username is not None or parsed.password is not None:
        raise URLSecurityError("Credentials in outbound URLs are not allowed.")
    if not hostname:
        raise URLSecurityError("URL hostname is required.")
    try:
        normalized_hostname = hostname.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise URLSecurityError("URL hostname is invalid.") from exc
    if normalized_hostname in {"localhost", "metadata.google.internal"}:
        raise PrivateAddressError("Local/internal hostnames are not allowed.")
    effective_port = port or (443 if parsed.scheme == "https" else 80)
    if effective_port not in {80, 443}:
        raise URLSecurityError("Outbound URL port is not allowed.")
    addresses = resolve_hostname_addresses(normalized_hostname)
    reject_disallowed_addresses(addresses)
    # Prefer IPv4 where available while preserving resolver order within each
    # address family. The transport may fail over only within this approved set.
    ordered = tuple(
        sorted(addresses, key=lambda address: 1 if ipaddress.ip_address(address).version == 6 else 0)
    )
    path_and_query = parsed.path or "/"
    if parsed.query:
        path_and_query += f"?{parsed.query}"
    return ResolvedPublicTarget(
        scheme=parsed.scheme,
        hostname=normalized_hostname,
        port=effective_port,
        path_and_query=path_and_query,
        addresses=ordered,
        selected_address=ordered[0],
    )
