"""Read-only web target policy, adapted from IWANTGOHOME's web/program guards.

This module makes policy decisions. The guarded browser transport calls
``resolve_target`` for *every* request and connects to the returned numeric IP;
checking a URL and then letting another client resolve it would reintroduce a
DNS rebinding window.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import ipaddress
import re
import socket
from typing import Callable
from urllib.parse import parse_qsl, quote, unquote, urljoin, urlsplit, urlunsplit


SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_FORBIDDEN_QUERY_KEY = re.compile(
    r"token|secret|pass|auth|session|cookie|action|delete|redirect|callback|url", re.I
)


class PolicyError(ValueError):
    """A stable reason code suitable for job results and tests."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _host(value: str) -> str:
    if not value or len(value) > 253 or value.endswith(".") or "%" in value:
        raise PolicyError("INVALID_HOST")
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        try:
            ascii_host = value.encode("idna").decode("ascii").lower()
        except UnicodeError as exc:
            raise PolicyError("INVALID_HOST") from exc
        labels = ascii_host.split(".")
        if (
            not all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in labels)
            or re.fullmatch(r"[0-9.]+", ascii_host)
            or ascii_host.startswith("0x")
        ):
            raise PolicyError("INVALID_HOST")
        return ascii_host
    return address.compressed


def canonical_url(value: str) -> str:
    """Return a conservative, unambiguous HTTP(S) URL."""
    if (
        not isinstance(value, str)
        or len(value) > 2048
        or re.search(r"[\x00-\x20\x7f\\]", value)
        or "#" in value
    ):
        raise PolicyError("INVALID_URL")
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.netloc.endswith(":")
        ):
            raise PolicyError("INVALID_URL")
        host = _host(parsed.hostname)
        port = parsed.port if parsed.port is not None else (443 if parsed.scheme == "https" else 80)
        if not 1 <= port <= 65535:
            raise PolicyError("INVALID_PORT")
        path = unquote(parsed.path or "/", errors="strict")
        if (
            "%" in path
            or "\\" in path
            or "//" in path
            or re.search(r"[\x00-\x20\x7f]", path)
            or any(segment in {".", ".."} for segment in path.split("/"))
        ):
            raise PolicyError("AMBIGUOUS_PATH")
        if parsed.query:
            for key, _ in parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True):
                if _FORBIDDEN_QUERY_KEY.search(key):
                    raise PolicyError("SENSITIVE_QUERY_KEY")
        authority = f"[{host}]" if ":" in host else host
        if port != (443 if parsed.scheme == "https" else 80):
            authority += f":{port}"
        return urlunsplit((parsed.scheme, authority, quote(path, safe="/~._-"), parsed.query, ""))
    except (ValueError, UnicodeError) as exc:
        if isinstance(exc, PolicyError):
            raise
        raise PolicyError("INVALID_URL") from exc


def _path_prefix(value: str) -> str:
    if not isinstance(value, str) or not value.startswith("/") or "?" in value or "#" in value:
        raise PolicyError("INVALID_PATH_PREFIX")
    path = urlsplit(canonical_url("https://scope.invalid" + value)).path
    return path.rstrip("/") or "/"


def _path_matches(path: str, prefix: str) -> bool:
    return prefix == "/" or path == prefix or path.startswith(prefix + "/")


@dataclass(frozen=True)
class ResolvedTarget:
    url: str
    hostname: str
    port: int
    address: str


@dataclass(frozen=True)
class RunPolicy:
    """Approved scope snapshot supplied by a trusted backend, never by a reporter."""

    program_id: str
    scope_revision: int
    base_url: str
    allowed_path_prefixes: tuple[str, ...]
    excluded_path_prefixes: tuple[str, ...] = ()
    allowed_methods: tuple[str, ...] = ("GET", "HEAD", "OPTIONS")
    allow_private_targets: bool = False
    private_cidrs: tuple[str, ...] = ()
    _origin: tuple[str, str, int] = field(init=False, repr=False)
    _private_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = field(
        init=False, repr=False
    )

    def __post_init__(self) -> None:
        if (
            not isinstance(self.program_id, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", self.program_id)
            or type(self.scope_revision) is not int
            or self.scope_revision < 1
        ):
            raise PolicyError("INVALID_SCOPE_ID")
        base = canonical_url(self.base_url)
        parsed = urlsplit(base)
        if parsed.path != "/" or parsed.query:
            raise PolicyError("BASE_URL_MUST_BE_ORIGIN")
        if not self.allowed_path_prefixes:
            raise PolicyError("EMPTY_SCOPE")
        allowed = tuple(sorted({_path_prefix(p) for p in self.allowed_path_prefixes}))
        excluded = tuple(sorted({_path_prefix(p) for p in self.excluded_path_prefixes}))
        methods = tuple(sorted(set(self.allowed_methods)))
        if not methods or not set(methods) <= SAFE_METHODS:
            raise PolicyError("METHOD_NOT_ALLOWED")
        if type(self.allow_private_targets) is not bool or (self.private_cidrs and not self.allow_private_targets):
            raise PolicyError("INVALID_PRIVATE_POLICY")
        try:
            networks = tuple(ipaddress.ip_network(cidr, strict=True) for cidr in self.private_cidrs)
        except (TypeError, ValueError) as exc:
            raise PolicyError("INVALID_PRIVATE_CIDR") from exc
        if any(network.prefixlen == 0 for network in networks):
            raise PolicyError("PRIVATE_CIDR_TOO_BROAD")
        port = parsed.port if parsed.port is not None else (443 if parsed.scheme == "https" else 80)
        object.__setattr__(self, "base_url", base)
        object.__setattr__(self, "allowed_path_prefixes", allowed)
        object.__setattr__(self, "excluded_path_prefixes", excluded)
        object.__setattr__(self, "allowed_methods", methods)
        object.__setattr__(self, "_origin", (parsed.scheme, parsed.hostname or "", port))
        object.__setattr__(self, "_private_networks", networks)

    def check_url(self, value: str, method: str = "GET") -> str:
        if method not in self.allowed_methods:
            raise PolicyError("METHOD_NOT_ALLOWED")
        url = canonical_url(value)
        parsed = urlsplit(url)
        origin = (
            parsed.scheme,
            parsed.hostname or "",
            parsed.port if parsed.port is not None else (443 if parsed.scheme == "https" else 80),
        )
        if origin != self._origin:
            raise PolicyError("ORIGIN_OUT_OF_SCOPE")
        path = parsed.path
        if any(_path_matches(path, p) for p in self.excluded_path_prefixes):
            raise PolicyError("PATH_EXCLUDED")
        if re.search(r"(?:^|/)(?:logout|signout|delete|remove|purchase|checkout|unsubscribe)(?:/|$)", path, re.I):
            raise PolicyError("STATE_CHANGING_PATH")
        if not any(_path_matches(path, p) for p in self.allowed_path_prefixes):
            raise PolicyError("PATH_OUT_OF_SCOPE")
        return url

    def resolve_target(
        self,
        value: str,
        method: str = "GET",
        *,
        resolver: Callable[..., list[tuple]] = socket.getaddrinfo,
    ) -> ResolvedTarget:
        """Validate every DNS answer and return an IP for a pinned connection."""
        url = self.check_url(value, method)
        parsed = urlsplit(url)
        hostname = parsed.hostname or ""
        port = parsed.port if parsed.port is not None else (443 if parsed.scheme == "https" else 80)
        try:
            answers = resolver(hostname, port, type=socket.SOCK_STREAM)
            addresses = sorted({answer[4][0] for answer in answers})
        except (OSError, IndexError, TypeError, ValueError) as exc:
            raise PolicyError("DNS_FAILED") from exc
        if not addresses:
            raise PolicyError("DNS_FAILED")
        for address in addresses:
            try:
                ip = ipaddress.ip_address(address)
            except ValueError as exc:
                raise PolicyError("DNS_FAILED") from exc
            if ip.is_multicast or ip.is_unspecified or ip.is_link_local:
                raise PolicyError("DESTINATION_BLOCKED")
            if not ip.is_global and not (
                self.allow_private_targets
                and any(ip.version == network.version and ip in network for network in self._private_networks)
            ):
                raise PolicyError("PRIVATE_DESTINATION_BLOCKED")
        return ResolvedTarget(url, hostname, port, addresses[0])

    def check_redirect(self, current_url: str, location: str, method: str = "GET") -> str:
        """Check the next URL; the sender must resolve it again before connecting."""
        return self.check_url(urljoin(self.check_url(current_url, method), location), method)
