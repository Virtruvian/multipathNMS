import asyncio
import ipaddress
import socket
from dataclasses import dataclass

from ..config import settings


@dataclass(slots=True)
class VoyageResult:
    target: str
    resolved_ip: str
    return_code: int
    stdout: str
    stderr: str


async def resolve_target(target: str) -> str:
    try:
        return str(ipaddress.ip_address(target))
    except ValueError:
        loop = asyncio.get_running_loop()
        infos = await loop.getaddrinfo(
            target,
            None,
            family=socket.AF_INET,
            type=socket.SOCK_DGRAM,
        )
        if not infos:
            raise ValueError(f"Unable to resolve target: {target}")
        return infos[0][4][0]


async def run_voyage(
    target: str,
    *,
    protocol: str = "icmp",
    max_ttl: int = 32,
) -> VoyageResult:
    if protocol not in {"icmp", "udp"}:
        raise ValueError("protocol must be icmp or udp")

    resolved_ip = await resolve_target(target)
    max_ttl = max(1, min(max_ttl, 64))

    process = await asyncio.create_subprocess_exec(
        "voyage",
        "--dst-addr",
        resolved_ip,
        "--protocol",
        protocol,
        "--max-ttl",
        str(max_ttl),
        "--confidence",
        str(settings.voyage_confidence),
        "--output-format",
        "flat",
        "--probing-rate",
        str(settings.voyage_probing_rate),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(),
            timeout=settings.voyage_timeout_seconds,
        )
    except TimeoutError:
        process.kill()
        await process.communicate()
        raise RuntimeError(
            f"Voyage timed out after {settings.voyage_timeout_seconds} seconds"
        )

    return VoyageResult(
        target=target,
        resolved_ip=resolved_ip,
        return_code=process.returncode,
        stdout=stdout.decode(errors="replace"),
        stderr=stderr.decode(errors="replace"),
    )
