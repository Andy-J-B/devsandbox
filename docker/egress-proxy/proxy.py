#!/usr/bin/env python3
"""Allowlist HTTP/HTTPS proxy for DevSandbox sandbox attempts.

The agent container is attached to an *internal* Docker network and has no
route off it.  The only reachable service is this proxy, which forwards plain
HTTP requests and HTTPS ``CONNECT`` tunnels to hosts in
``DEVSANDBOX_ALLOWED_HOSTS``.

Entries are ``host`` (ports 80/443 only) or ``host:port``.  A leading ``*.``
matches subdomains but not the apex domain.  The proxy is deliberately small
and stdlib-only: it never logs request paths, query strings, headers, or
bodies, so credentials cannot leak through its output.

The proxy runs as a non-root user with a read-only root filesystem, no
capabilities, and no Docker socket.  It is *not* a general-purpose proxy:
DNS names that resolve to loopback, link-local, private, multicast, or
reserved addresses are rejected unless the operator allowlisted a literal IP
address, which keeps DNS rebinding from turning an allowed name into a route
to the Docker host or another internal service.
"""

from __future__ import annotations

import contextlib
import ipaddress
import os
import re
import select
import socket
import sys
from http.client import HTTPResponse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

_LISTEN_HOST = "0.0.0.0"
_DEFAULT_PORT = 8888
_CONNECT_TIMEOUT = 15.0
_TUNNEL_IDLE_TIMEOUT = 120.0
_BUFFER = 64 * 1024
_MAX_REQUEST_BODY = 32 * 1024 * 1024

# Same shape as devsandbox.common.types.normalize_allowed_host.  The image is
# standalone, so the validation is duplicated rather than imported.
_ALLOWED_HOST_RE = re.compile(
    r"^(?:\*\.)?"
    r"(?:[a-z0-9_](?:[a-z0-9_-]*[a-z0-9_])?)"
    r"(?:\.[a-z0-9_](?:[a-z0-9_-]*[a-z0-9_])?)*"
    r"(?::\d{1,5})?$"
)

_HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "proxy-connection",
    "te",
    "trailer",
    "trailers",
    "transfer-encoding",
    "upgrade",
}


def _log(decision: str, host: str, port: int, detail: str) -> None:
    """Log a decision without ever logging the request target or headers."""
    print(f"egress {decision} {host}:{port} {detail}", flush=True)


def _normalize_entry(entry: str) -> str:
    value = entry.strip().lower()
    if value.endswith("."):
        value = value[:-1]
    if not value or not _ALLOWED_HOST_RE.match(value):
        raise ValueError(f"invalid allowlist host entry: {entry!r}")
    _host, port = _split_entry(value)
    if port is not None and not 1 <= port <= 65535:
        raise ValueError(f"allowlist port out of range: {entry!r}")
    return value


def _split_entry(entry: str) -> tuple[str, int | None]:
    host, separator, port_text = entry.rpartition(":")
    if separator and port_text.isdigit() and ":" not in host:
        return host, int(port_text)
    return entry, None


def _split_authority(authority: str, default_port: int) -> tuple[str, int] | None:
    value = authority.strip()
    if not value:
        return None
    try:
        parsed = urlsplit("//" + value)
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        return None
    if not host or parsed.username or parsed.password:
        return None
    return host.lower().rstrip("."), port or default_port


def _host_matches(host: str, port: int, entries: tuple[str, ...]) -> bool:
    normalized_host = host.strip().lower().rstrip(".")
    for entry in entries:
        allowed_host, allowed_port = _split_entry(entry)
        if allowed_port is not None:
            if port != allowed_port:
                continue
        elif port not in (80, 443):
            continue
        if allowed_host.startswith("*."):
            if normalized_host.endswith("." + allowed_host[2:]):
                return True
        elif normalized_host == allowed_host:
            return True
    return False


def _is_public_address(address: str) -> bool:
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return False
    return not (
        parsed.is_loopback
        or parsed.is_link_local
        or parsed.is_multicast
        or parsed.is_unspecified
        or parsed.is_private
        or parsed.is_reserved
    )


def _target_allowed(host: str, port: int, entries: tuple[str, ...]) -> tuple[bool, str]:
    if not _host_matches(host, port, entries):
        return False, "not in allowlist"
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        # The operator explicitly allowlisted a literal address.
        return True, "literal-address"
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        return False, f"dns-error:{exc.errno}"
    addresses = {info[4][0] for info in infos}
    if not addresses:
        return False, "dns-no-addresses"
    for address in addresses:
        if not _is_public_address(address):
            return False, "resolves-to-non-public"
    return True, "resolved"


def _connect(host: str, port: int) -> socket.socket:
    last_error: OSError | None = None
    for family, socktype, proto, _canonname, sockaddr in socket.getaddrinfo(
        host, port, type=socket.SOCK_STREAM
    ):
        candidate = socket.socket(family, socktype, proto)
        candidate.settimeout(_CONNECT_TIMEOUT)
        try:
            candidate.connect(sockaddr)
        except OSError as exc:
            last_error = exc
            candidate.close()
            continue
        candidate.settimeout(None)
        return candidate
    raise OSError(last_error or "no addresses to connect to")


def _tunnel(client: socket.socket, upstream: socket.socket) -> None:
    sockets = [client, upstream]
    while True:
        try:
            readable, _writable, errored = select.select(sockets, [], sockets, _TUNNEL_IDLE_TIMEOUT)
        except (OSError, ValueError):
            return
        if errored or not readable:
            return
        for source in readable:
            try:
                data = source.recv(_BUFFER)
            except OSError:
                return
            if not data:
                return
            target = upstream if source is client else client
            try:
                target.sendall(data)
            except OSError:
                return


class _ProxyServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        handler: type[BaseHTTPRequestHandler],
        entries: tuple[str, ...],
    ) -> None:
        self.entries = entries
        super().__init__(server_address, handler)

    def authorize(self, host: str, port: int) -> tuple[bool, str]:
        return _target_allowed(host, port, self.entries)


class _ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "devsandbox-egress/1"
    sys_version = ""
    rbufsize = 0
    wbufsize = 0
    disable_nagle_algorithm = True

    server: _ProxyServer

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        """Suppress BaseHTTPRequestHandler's default request logging."""
        return

    # -- request entry points ---------------------------------------------

    def do_CONNECT(self) -> None:
        self._handle_connect()

    def do_GET(self) -> None:
        self._handle_http()

    def do_HEAD(self) -> None:
        self._handle_http()

    def do_POST(self) -> None:
        self._handle_http()

    def do_PUT(self) -> None:
        self._handle_http()

    def do_PATCH(self) -> None:
        self._handle_http()

    def do_DELETE(self) -> None:
        self._handle_http()

    def do_OPTIONS(self) -> None:
        self._handle_http()

    # -- health -----------------------------------------------------------

    def _is_health_request(self) -> bool:
        return self.command in ("GET", "HEAD") and self.path.split("?", 1)[0] == "/healthz"

    def _send_health(self) -> None:
        body = b"ok\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            with contextlib.suppress(OSError):
                self.wfile.write(body)
        self.close_connection = True

    # -- responses --------------------------------------------------------

    def _deny(self, status: int, message: str) -> None:
        body = (message + "\n").encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        with contextlib.suppress(OSError):
            self.wfile.write(body)
        self.close_connection = True

    # -- CONNECT ----------------------------------------------------------

    def _handle_connect(self) -> None:
        split = _split_authority(self.path, 443)
        if split is None:
            self._deny(400, "invalid CONNECT target")
            return
        host, port = split
        allowed, reason = self.server.authorize(host, port)
        if not allowed:
            _log("deny", host, port, reason)
            self._deny(403, "host not allowed")
            return
        try:
            upstream = _connect(host, port)
        except OSError:
            _log("error", host, port, "connect-failed")
            self._deny(502, "upstream connection failed")
            return
        _log("allow", host, port, "CONNECT")
        self.send_response_only(200, "Connection Established")
        self.end_headers()
        try:
            _tunnel(self.connection, upstream)
        finally:
            upstream.close()
        self.close_connection = True

    # -- plain HTTP -------------------------------------------------------

    def _handle_http(self) -> None:
        if self._is_health_request():
            self._send_health()
            return

        target = self._parse_http_target()
        if target is None:
            self._deny(400, "invalid proxy request")
            return
        host, port, path = target
        allowed, reason = self.server.authorize(host, port)
        if not allowed:
            _log("deny", host, port, reason)
            self._deny(403, "host not allowed")
            return

        try:
            body = self._read_request_body()
        except ValueError:
            self._deny(413, "request body too large or malformed")
            return

        try:
            upstream = _connect(host, port)
        except OSError:
            _log("error", host, port, "connect-failed")
            self._deny(502, "upstream connection failed")
            return
        try:
            self._forward_http(upstream, host, port, path, body)
            _log("allow", host, port, self.command)
        except (OSError, ValueError):
            _log("error", host, port, "forward-failed")
        finally:
            upstream.close()
            self.close_connection = True

    def _parse_http_target(self) -> tuple[str, int, str] | None:
        try:
            parts = urlsplit(self.path)
        except ValueError:
            return None
        if parts.scheme == "http" and parts.hostname:
            try:
                port = parts.port or 80
            except ValueError:
                return None
            path = parts.path or "/"
            if parts.query:
                path += "?" + parts.query
            return parts.hostname.lower().rstrip("."), port, path
        if parts.scheme:
            return None
        split = _split_authority(self.headers.get("Host", ""), 80)
        if split is None:
            return None
        host, port = split
        return host, port, self.path

    def _read_request_body(self) -> bytes | None:
        transfer_encoding = self.headers.get("Transfer-Encoding", "").lower()
        if "chunked" in transfer_encoding:
            return self._read_chunked_body()
        length = self.headers.get("Content-Length")
        if length is None:
            return None
        try:
            size = int(length)
        except ValueError as exc:
            raise ValueError("invalid Content-Length") from exc
        if size < 0 or size > _MAX_REQUEST_BODY:
            raise ValueError("request body too large")
        data = self._read_exact(size)
        if len(data) != size:
            raise ValueError("truncated request body")
        return data

    def _read_exact(self, size: int) -> bytes:
        """Read exactly ``size`` bytes from an unbuffered client socket."""
        chunks: list[bytes] = []
        remaining = size
        while remaining > 0:
            chunk = self.rfile.read(remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def _read_chunked_body(self) -> bytes:
        chunks: list[bytes] = []
        total = 0
        while True:
            line = self.rfile.readline(128)
            if not line:
                raise ValueError("truncated chunked body")
            size_text = line.split(b";", 1)[0].strip()
            try:
                size = int(size_text, 16)
            except ValueError as exc:
                raise ValueError("invalid chunk size") from exc
            if size == 0:
                while True:
                    trailer = self.rfile.readline(8 * 1024)
                    if trailer in (b"\r\n", b"\n", b""):
                        break
                break
            total += size
            if total > _MAX_REQUEST_BODY:
                raise ValueError("request body too large")
            data = self._read_exact(size)
            if len(data) != size:
                raise ValueError("truncated chunked body")
            chunks.append(data)
            if self._read_exact(2) != b"\r\n":
                raise ValueError("invalid chunk terminator")
        return b"".join(chunks)

    def _forward_http(
        self,
        upstream: socket.socket,
        host: str,
        port: int,
        path: str,
        body: bytes | None,
    ) -> None:
        host_header = host if port == 80 else f"{host}:{port}"
        lines = [f"{self.command} {path} HTTP/1.1"]
        for key, value in self.headers.items():
            lower = key.lower()
            if lower in _HOP_BY_HOP or lower in ("host", "content-length"):
                continue
            if "\r" in key or "\n" in key or "\r" in value or "\n" in value:
                raise ValueError("invalid header")
            lines.append(f"{key}: {value}")
        lines.append(f"Host: {host_header}")
        if body is not None:
            lines.append(f"Content-Length: {len(body)}")
        request = ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1", "replace")
        if body:
            request += body
        upstream.sendall(request)

        response = HTTPResponse(upstream, method=self.command)
        response.begin()
        try:
            self._write_response(response)
        finally:
            response.close()

    def _write_response(self, response: HTTPResponse) -> None:
        status = response.status
        self.send_response_only(status, response.reason)

        content_length: str | None = None
        for key, value in response.getheaders():
            lower = key.lower()
            if lower in _HOP_BY_HOP:
                continue
            if lower == "content-length":
                content_length = value
                continue
            self.send_header(key, value)

        body_allowed = (
            self.command != "HEAD" and status not in (204, 304) and not 100 <= status < 200
        )
        framing: str | None = None
        length = 0
        if body_allowed:
            if content_length is not None and content_length.isdigit():
                length = int(content_length)
                framing = "length"
                self.send_header("Content-Length", str(length))
            else:
                framing = "chunked"
                self.send_header("Transfer-Encoding", "chunked")
        elif content_length is not None:
            # HEAD and 304 responses have no body but may still advertise the
            # entity length.
            self.send_header("Content-Length", content_length)
        self.send_header("Connection", "close")
        self.end_headers()

        if not body_allowed:
            return
        if framing == "length":
            remaining = length
            while remaining > 0:
                chunk = response.read(min(_BUFFER, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)
        elif framing == "chunked":
            while True:
                chunk = response.read(_BUFFER)
                if not chunk:
                    break
                self.wfile.write(f"{len(chunk):x}\r\n".encode("ascii"))
                self.wfile.write(chunk)
                self.wfile.write(b"\r\n")
            self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()


def main() -> int:
    raw = os.environ.get("DEVSANDBOX_ALLOWED_HOSTS", "")
    try:
        entries = tuple(_normalize_entry(part) for part in raw.split(",") if part.strip())
    except ValueError as exc:
        print(f"egress proxy: {exc}", file=sys.stderr, flush=True)
        return 2
    if not entries:
        print(
            "egress proxy: DEVSANDBOX_ALLOWED_HOSTS is empty; refusing to start",
            file=sys.stderr,
            flush=True,
        )
        return 2

    raw_port = os.environ.get("DEVSANDBOX_PROXY_PORT", str(_DEFAULT_PORT))
    try:
        port = int(raw_port)
    except ValueError:
        print(f"egress proxy: invalid DEVSANDBOX_PROXY_PORT {raw_port!r}", file=sys.stderr)
        return 2
    if not 1 <= port <= 65535:
        print(f"egress proxy: port out of range {port}", file=sys.stderr)
        return 2

    server = _ProxyServer((_LISTEN_HOST, port), _ProxyHandler, entries)
    print(
        f"egress proxy listening on {_LISTEN_HOST}:{port} with {len(entries)} allowlist entries",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
