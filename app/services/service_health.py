"""Independent, bounded TCP-connect and HTTPS response checks."""

import asyncio
import ipaddress
import re
import ssl
from dataclasses import dataclass
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


async def check_service(address: str, port: int, *, https: bool = False, path: str = "/",
                        timeout: float = 5.0, ssl_context: ssl.SSLContext | None = None) -> ServiceResult:
    if not 1 <= port <= 65535:
        raise ValueError("Service port must be between 1 and 65535")
    if https:
        validate_https_path(path)
    context = (ssl_context or ssl.create_default_context()) if https else None
    writer = None
    resolved_ip = None
    start = perf_counter()
    try:
        # Covers DNS, connection, TLS and the HTTP response together.
        async with asyncio.timeout(timeout):
            reader, writer = await asyncio.open_connection(
                address, port, ssl=context, server_hostname=address if https else None,
                limit=4096,
            )
            peer = writer.get_extra_info("peername")
            resolved_ip = str(peer[0]) if peer else None
            status = None
            if https:
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
            return ServiceResult(success, latency, resolved_ip, status,
                                 None if success else f"HTTP {status}")
    except TimeoutError:
        return ServiceResult(False, resolved_ip=resolved_ip, error=f"Timed out after {timeout:g} seconds")
    except (OSError, ValueError) as exc:
        return ServiceResult(False, resolved_ip=resolved_ip, error=str(exc)[:300])
    finally:
        if writer is not None:
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), 0.5)
            except (OSError, TimeoutError):
                writer.transport.abort()
