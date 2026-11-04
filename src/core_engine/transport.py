"""One-hop HTTP transport that connects only to policy-validated numeric IPs."""

from __future__ import annotations

from dataclasses import dataclass
import http.client
import socket
import ssl
import threading
from typing import Callable
from urllib.parse import urlsplit

from .budget import RequestBudget
from .policy import PolicyError, RunPolicy


@dataclass(frozen=True)
class Response:
    status: int
    headers: dict[str, str]
    body: bytes


class GuardedTransport:
    def __init__(
        self, policy: RunPolicy, budget: RequestBudget,
        is_authorized: Callable[[], bool], cancelled: threading.Event,
        *, resolver: Callable = socket.getaddrinfo,
    ):
        self.policy, self.budget = policy, budget
        self.is_authorized, self.cancelled = is_authorized, cancelled
        self.resolver = resolver
        self.closed = threading.Event()
        self.connections: set[http.client.HTTPConnection] = set()
        self.lock = threading.Lock()

    def check(self) -> None:
        if self.closed.is_set() or self.cancelled.is_set():
            raise PolicyError("CANCELLED")
        self.budget.check_runtime()
        try:
            allowed = self.is_authorized()
        except Exception:
            raise PolicyError("AUTHORIZATION_UNAVAILABLE") from None
        if allowed is not True:
            raise PolicyError("AUTHORIZATION_REVOKED")

    def close(self) -> None:
        self.closed.set()
        with self.lock:
            for connection in tuple(self.connections):
                connection.close()

    def fetch(self, url: str, method: str = "GET") -> Response:
        self.check()
        with self.budget.request():
            target = self.policy.resolve_target(url, method, resolver=self.resolver)
            self.check()
            parsed = urlsplit(target.url)
            # HTTPConnection retains the original Host. Supplying its socket prevents
            # a second DNS lookup; TLS still verifies the original hostname.
            connection = http.client.HTTPConnection(target.hostname, target.port, timeout=1)
            raw = None
            response = None
            with self.lock:
                self.connections.add(connection)
            try:
                raw = socket.create_connection((target.address, target.port), timeout=1)
                connection.sock = raw
                if parsed.scheme == "https":
                    connection.sock = ssl.create_default_context().wrap_socket(raw, server_hostname=target.hostname)
                self.check()
                path = parsed.path + ("?" + parsed.query if parsed.query else "")
                connection.request(method, path, headers={
                    "Accept-Encoding": "identity", "Connection": "close",
                    "User-Agent": "METAPHOR-Core/0.1", "Accept": "*/*",
                })
                response = connection.getresponse()
                pairs = response.getheaders()
                headers = {key.lower(): value for key, value in pairs}
                if response.status in {301, 302, 303, 307, 308}:
                    if "location" not in headers:
                        raise PolicyError("REDIRECT_WITHOUT_LOCATION")
                    headers["location"] = self.policy.check_redirect(target.url, headers["location"], method)
                if headers.get("content-encoding", "identity").lower() not in {"", "identity"}:
                    raise PolicyError("ENCODED_RESPONSE_UNSUPPORTED")
                if headers.get("content-type", "").split(";", 1)[0].strip() == "text/event-stream":
                    raise PolicyError("EVENT_STREAM_UNSUPPORTED")
                declared = headers.get("content-length")
                if declared is not None:
                    if not declared.isdigit():
                        raise PolicyError("INVALID_CONTENT_LENGTH")
                    self.budget.check_response_size(int(declared))
                chunks, size = [], 0
                while method != "HEAD":
                    self.check()
                    chunk = response.read1(min(16384, self.budget.limits.max_response_bytes - size + 1))
                    if not chunk:
                        break
                    size += len(chunk)
                    self.budget.check_response_size(size)
                    self.budget.received(len(chunk))
                    chunks.append(chunk)
                if method != "HEAD" and declared is not None and int(declared) != size:
                    raise PolicyError("TRUNCATED_RESPONSE")
                self.check()
                # Anonymous runs: do not import server credentials into the browser.
                blocked = {
                    "content-length", "transfer-encoding", "connection", "content-encoding",
                    "set-cookie", "alt-svc", "report-to", "nel", "proxy-authenticate",
                }
                blocked.update(part.strip().lower() for part in headers.get("connection", "").split(","))
                return Response(response.status, {k: v for k, v in headers.items() if k not in blocked}, b"".join(chunks))
            except (OSError, http.client.HTTPException):
                raise PolicyError("TRANSPORT_ERROR") from None
            finally:
                if response is not None:
                    response.close()
                connection.close()
                if raw is not None:
                    raw.close()
                with self.lock:
                    self.connections.discard(connection)
