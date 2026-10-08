"""Independent, bounded TCP-connect and HTTPS response checks."""

import asyncio
import ipaddress
import re
import socket
import ssl
from dataclasses import dataclass, field
from time import perf_counter


HTTP_STATUS = re.compile(rb"HTTP/1\.[01] ([1-5][0-9]{2})(?:[ \r\n]|$)")


def validate_https_path(value: str) -> str:
    if (not value.startswith("/") or value.startswith("//") or "#" in value
            or len(value) > 1024 or any(ord(char) < 33 or ord(char) > 126 for char in value)):
        raise ValueError("HTTPS path must start with / and contain no spaces, fragments or control characters")
    return value


@dataclass(slots=True)
class ServiceResult:
    success: bool
    latency_ms: float | None = None
    resolved_ip: str | None = None
    http_status: int | None = None
    error: str | None = None
    phases: list[dict] = field(default_factory=list)
    failed_phase: str | None = None
    resolved_addresses: tuple[str, ...] = ()


async def check_service(address: str, port: int, *, https: bool = False, path: str = "/",
                        timeout: float = 5.0, ssl_context: ssl.SSLContext | None = None) -> ServiceResult:
    if not 1 <= port <= 65535:
        raise ValueError("Service port must be between 1 and 65535")
    if https:
        validate_https_path(path)
    context = (ssl_context or ssl.create_default_context()) if https else None
    writer = None
    connected_socket = None
    resolved_ip = None
    start = perf_counter()
    names = ["dns", "tcp", "tls", "http"] if https else ["dns", "tcp"]
    phases = [{"phase": name, "status": "not-run", "duration_ms": None} for name in names]
    addresses = ()
    phase_index = 0
    phase_start = start
    status = None

    def finish_phase(status="ok", error=None):
        phases[phase_index].update(status=status, duration_ms=round((perf_counter() - phase_start) * 1000, 3))
        if error:
            phases[phase_index]["error"] = error

    def failed(error):
        finish_phase("failed", error)
        return ServiceResult(False, resolved_ip=resolved_ip, http_status=status if status and status >= 200 else None, error=error,
                             phases=phases, failed_phase=names[phase_index], resolved_addresses=addresses)

    try:
        # Covers DNS, connection, TLS and the HTTP response together.
        async with asyncio.timeout(timeout):
            loop = asyncio.get_running_loop()
            try:
                literal = ipaddress.ip_address(address)
            except ValueError:
                endpoints = await loop.getaddrinfo(address, port, type=socket.SOCK_STREAM)
                if not endpoints:
                    raise OSError("DNS returned no addresses")
                finish_phase()
            else:
                family = socket.AF_INET6 if literal.version == 6 else socket.AF_INET
                endpoint = (str(literal), port, 0, 0) if literal.version == 6 else (str(literal), port)
                endpoints = [(family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", endpoint)]
                finish_phase("skipped")
                phases[0]["reason"] = "IP address: DNS lookup not needed"
            addresses = tuple(dict.fromkeys(str(endpoint[4][0]) for endpoint in endpoints))
            phase_index, phase_start = 1, perf_counter()
            last_error = None
            attempted = set()
            for family, kind, protocol, _, endpoint in endpoints:
                if (family, endpoint) in attempted:
                    continue
                attempted.add((family, endpoint))
                connected_socket = socket.socket(family, kind, protocol)
                connected_socket.setblocking(False)
                try:
                    await loop.sock_connect(connected_socket, endpoint)
                    break
                except OSError as exc:
                    last_error = exc
                    connected_socket.close()
                    connected_socket = None
            if connected_socket is None:
                raise last_error or OSError("No usable TCP address")
            resolved_ip = str(connected_socket.getpeername()[0])
            reader, writer = await asyncio.open_connection(sock=connected_socket, limit=4096)
            connected_socket = None  # Stream writer now owns the socket.
            finish_phase()
            if https:
                phase_index, phase_start = 2, perf_counter()
                await writer.start_tls(context, server_hostname=address)
                finish_phase()
                phase_index, phase_start = 3, perf_counter()
                try:
                    host = f"[{address}]" if ipaddress.ip_address(address).version == 6 else address
                except ValueError:
                    host = address
                if port != 443:
                    host += f":{port}"
                writer.write((f"GET {path} HTTP/1.1\r\nHost: {host}\r\n"
                              "User-Agent: multipathNMS\r\nConnection: close\r\n\r\n").encode("ascii"))
                await writer.drain()
                # Do not download bodies or follow redirects. Bound interim responses too.
                for _ in range(5):
                    match = HTTP_STATUS.match(await reader.readline())
                    if not match:
                        raise ValueError("Invalid HTTP response status")
                    status = int(match[1])
                    if status >= 200:
                        break
                    header_bytes = 0
                    while True:
                        line = await reader.readline()
                        header_bytes += len(line)
                        if header_bytes > 16384 or not line:
                            raise ValueError("Invalid interim HTTP response")
                        if line in (b"\r\n", b"\n"):
                            break
                else:
                    raise ValueError("Too many interim HTTP responses")
            latency = round((perf_counter() - start) * 1000, 3)
            success = status is None or 200 <= status < 400
            if https:
                finish_phase("ok" if success else "failed", None if success else f"HTTP {status}")
            return ServiceResult(success, latency, resolved_ip, status,
                                 None if success else f"HTTP {status}", phases,
                                 None if success else "http", addresses)
    except TimeoutError:
        return failed(f"Timed out after {timeout:g} seconds")
    except (OSError, ValueError) as exc:
        return failed(str(exc)[:300])
    finally:
        if connected_socket is not None:
            connected_socket.close()
        if writer is not None:
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), 0.5)
            except (OSError, TimeoutError):
                writer.transport.abort()
