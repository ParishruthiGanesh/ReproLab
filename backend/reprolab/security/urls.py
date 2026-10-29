"""SSRF-resistant URL validation and fetching.

Paper URLs, repository URLs and anything a model or a web page suggests are
untrusted input. Every outbound fetch on behalf of a run goes through
``validate_public_url`` first, and every redirect hop is re-validated, because
a public hostname can redirect to ``169.254.169.254`` or ``127.0.0.1``.
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from typing import Iterable
from urllib.parse import urlparse, urlunparse

import httpx

ALLOWED_SCHEMES = frozenset({"http", "https"})
ALLOWED_PORTS = frozenset({80, 443})
MAX_REDIRECTS = 5

# Hostnames used by cloud metadata services. Blocked by name as well as by IP,
# since some resolve through public DNS to a link-local address.
BLOCKED_HOSTNAMES = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "metadata",
        "metadata.google.internal",
        "metadata.goog",
        "instance-data",
        "instance-data.ec2.internal",
    }
)

# Explicit metadata endpoints across providers.
BLOCKED_IPS = frozenset(
    {
        ipaddress.ip_address("169.254.169.254"),  # AWS / GCP / Azure / OpenStack
        ipaddress.ip_address("169.254.170.2"),  # ECS task metadata
        ipaddress.ip_address("100.100.100.200"),  # Alibaba Cloud
        ipaddress.ip_address("fd00:ec2::254"),  # AWS IMDSv2 over IPv6
    }
)


class UrlNotAllowed(ValueError):
    """Raised when a URL is rejected by the SSRF policy."""


@dataclass(frozen=True)
class ValidatedUrl:
    url: str
    hostname: str
    resolved_ips: tuple[str, ...]


def _is_disallowed_ip(ip: ipaddress._BaseAddress) -> str | None:
    """Return a human-readable reason if ``ip`` must not be contacted."""
    if ip in BLOCKED_IPS:
        return f"{ip} is a cloud instance metadata address"
    if ip.is_loopback:
        return f"{ip} is a loopback address"
    if ip.is_link_local:
        return f"{ip} is a link-local address"
    if ip.is_private:
        return f"{ip} is a private address"
    if ip.is_multicast:
        return f"{ip} is a multicast address"
    if ip.is_reserved:
        return f"{ip} is a reserved address"
    if ip.is_unspecified:
        return f"{ip} is the unspecified address"
    # IPv4-mapped / translated IPv6 can smuggle a private v4 address through.
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        inner = _is_disallowed_ip(mapped)
        if inner:
            return f"{ip} maps to {inner}"
    sixtofour = getattr(ip, "sixtofour", None)
    if sixtofour is not None:
        inner = _is_disallowed_ip(sixtofour)
        if inner:
            return f"{ip} encapsulates {inner}"
    if not ip.is_global:
        return f"{ip} is not a globally routable address"
    return None


def _resolve(hostname: str) -> list[ipaddress._BaseAddress]:
    try:
        infos = socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:  # pragma: no cover - network dependent
        raise UrlNotAllowed(f"cannot resolve host {hostname!r}: {exc}") from exc
    addresses: list[ipaddress._BaseAddress] = []
    for info in infos:
        sockaddr = info[4]
        try:
            addresses.append(ipaddress.ip_address(sockaddr[0]))
        except ValueError:
            continue
    if not addresses:
        raise UrlNotAllowed(f"host {hostname!r} resolved to no usable address")
    return addresses


def validate_public_url(raw_url: str, *, resolve: bool = True) -> ValidatedUrl:
    """Validate that ``raw_url`` points at a public internet host.

    ``resolve=False`` skips DNS (used by unit tests and by callers that only
    need syntax checks); the fetch path always resolves.
    """
    if not isinstance(raw_url, str) or not raw_url.strip():
        raise UrlNotAllowed("URL must be a non-empty string")
    raw_url = raw_url.strip()
    if len(raw_url) > 2048:
        raise UrlNotAllowed("URL is longer than 2048 characters")

    parsed = urlparse(raw_url)
    if parsed.scheme.lower() not in ALLOWED_SCHEMES:
        raise UrlNotAllowed(
            f"scheme {parsed.scheme!r} is not allowed; use http or https"
        )
    if parsed.username or parsed.password:
        raise UrlNotAllowed("URLs with embedded credentials are not allowed")
    hostname = (parsed.hostname or "").lower().rstrip(".")
    if not hostname:
        raise UrlNotAllowed("URL has no hostname")
    if hostname in BLOCKED_HOSTNAMES:
        raise UrlNotAllowed(f"host {hostname!r} is blocked")
    if hostname.endswith((".localhost", ".internal", ".local")):
        raise UrlNotAllowed(f"host {hostname!r} is a private-network name")

    port = parsed.port
    if port is None:
        port = 443 if parsed.scheme.lower() == "https" else 80
    if port not in ALLOWED_PORTS:
        raise UrlNotAllowed(f"port {port} is not allowed; use 80 or 443")

    # A literal IP in the URL is checked directly.
    try:
        literal = ipaddress.ip_address(hostname.strip("[]"))
    except ValueError:
        literal = None
    if literal is not None:
        reason = _is_disallowed_ip(literal)
        if reason:
            raise UrlNotAllowed(f"blocked: {reason}")
        return ValidatedUrl(raw_url, hostname, (str(literal),))

    if not resolve:
        return ValidatedUrl(raw_url, hostname, ())

    addresses = _resolve(hostname)
    for address in addresses:
        reason = _is_disallowed_ip(address)
        if reason:
            raise UrlNotAllowed(f"host {hostname!r} blocked: {reason}")
    return ValidatedUrl(raw_url, hostname, tuple(str(a) for a in addresses))


def _join_redirect(base: str, location: str) -> str:
    from urllib.parse import urljoin

    return urljoin(base, location)


def fetch_text(
    url: str,
    *,
    max_bytes: int,
    timeout: float = 30.0,
    accept: str = "text/html,application/pdf,text/plain;q=0.9,*/*;q=0.5",
    allowed_content_types: Iterable[str] | None = None,
) -> tuple[str, bytes, str]:
    """Fetch ``url`` safely, following redirects with re-validation.

    Returns ``(final_url, body, content_type)``. The body is truncated at
    ``max_bytes``; download limits are enforced here rather than trusted to the
    remote server's ``Content-Length``.
    """
    current = validate_public_url(url).url
    with httpx.Client(follow_redirects=False, timeout=timeout) as client:
        for _ in range(MAX_REDIRECTS + 1):
            with client.stream(
                "GET", current, headers={"Accept": accept, "User-Agent": "ReproLab/1.0"}
            ) as response:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        raise UrlNotAllowed("redirect without a Location header")
                    nxt = _join_redirect(current, location)
                    # Re-validate every hop: this is the redirect SSRF check.
                    current = validate_public_url(nxt).url
                    continue
                response.raise_for_status()
                content_type = response.headers.get("content-type", "").split(";")[0]
                if allowed_content_types is not None:
                    allowed = {c.lower() for c in allowed_content_types}
                    if content_type.lower() not in allowed:
                        raise UrlNotAllowed(
                            f"content type {content_type!r} is not allowed"
                        )
                chunks: list[bytes] = []
                total = 0
                for chunk in response.iter_bytes(65536):
                    remaining = max_bytes - total
                    if remaining <= 0:
                        break
                    chunks.append(chunk[:remaining])
                    total += len(chunk[:remaining])
                return current, b"".join(chunks), content_type
    raise UrlNotAllowed(f"too many redirects (>{MAX_REDIRECTS})")


def normalize_github_repo(raw_url: str) -> tuple[str, str, str]:
    """Validate a public GitHub repository URL.

    Returns ``(owner, repo, canonical_https_url)``.
    """
    validated = validate_public_url(raw_url, resolve=False)
    parsed = urlparse(validated.url)
    if parsed.hostname is None or parsed.hostname.lower() not in {
        "github.com",
        "www.github.com",
    }:
        raise UrlNotAllowed("only public github.com repository URLs are supported")
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) < 2:
        raise UrlNotAllowed("repository URL must look like https://github.com/owner/repo")
    owner, repo = parts[0], parts[1]
    if repo.endswith(".git"):
        repo = repo[:-4]
    for token, label in ((owner, "owner"), (repo, "repository")):
        if not token or len(token) > 100:
            raise UrlNotAllowed(f"{label} name is empty or too long")
        if not all(c.isalnum() or c in "-._" for c in token):
            raise UrlNotAllowed(f"{label} name {token!r} has unexpected characters")
        if token.startswith(".") or token in {".", ".."}:
            raise UrlNotAllowed(f"{label} name {token!r} is not allowed")
    canonical = urlunparse(("https", "github.com", f"/{owner}/{repo}", "", "", ""))
    return owner, repo, canonical
