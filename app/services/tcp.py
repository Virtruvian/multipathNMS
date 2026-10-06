"""Bounded TCP SYN traces, with one fixed source/destination port per flow."""

import asyncio
import ipaddress
import json
import math
import re
import secrets
from dataclasses import dataclass, replace
from statistics import median

from ..config import settings
from .topology_parser import TopologyObservation, parse_voyage_flat
from .voyage import resolve_target

ROW = re.compile(r"^\s*(\d+)\s+(.+)$")
RTT = re.compile(r"(?<!\S)(\d+(?:\.\d+)?)\s+ms(?=\s|$)")
FLAGS = re.compile(r"<([^>]+)>")


@dataclass(slots=True)
class TcpResult:
    resolved_ip: str
    traces: tuple[str, ...]
    source_ports: tuple[int, ...]
    destination_port: int
    max_ttl: int
    stderr: str = ""


async def run_tcp_trace(target: str, destination_port: int = 443) -> TcpResult:
    if not 1 <= destination_port <= 65535:
        raise ValueError("TCP destination port must be between 1 and 65535")
    resolved_ip = await resolve_target(target)
    if ipaddress.ip_address(resolved_ip).version != 4:
        raise ValueError("Topology tracing currently supports IPv4 only")
    traces, errors, ports = [], [], []
    # Disjoint ports within this scan; each flow holds its port constant across TTLs.
    first_port = 40000 + secrets.randbelow(20000 - settings.tcp_flows)
    for offset in range(settings.tcp_flows):
        source_port = first_port + offset
        process = await asyncio.create_subprocess_exec(
            "traceroute", "-4", "-n", "-T", "-O", "info",
            f"--sport={source_port}", "-N", "1", "-q", "1",
            "-m", str(settings.voyage_max_ttl),
            "-w", str(settings.tcp_hop_timeout_seconds),
            "-z", str(settings.tcp_sendwait_seconds),
            "-p", str(destination_port), resolved_ip,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env={"LC_ALL": "C", "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"},
        )
        deadline = settings.voyage_max_ttl * (
            settings.tcp_hop_timeout_seconds + settings.tcp_sendwait_seconds
        ) + 5
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), deadline)
        except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
            if process.returncode is None:
                process.kill()
            await process.communicate()
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise RuntimeError(f"TCP trace timed out after {deadline:.1f} seconds") from exc
        output, error = stdout.decode(errors="replace"), stderr.decode(errors="replace")
        if process.returncode != 0:
            raise RuntimeError(f"TCP trace exited with code {process.returncode}: {(error or output)[:500]}")
        traces.append(output)
        errors.append(error)
        ports.append(source_port)
    return TcpResult(resolved_ip, tuple(traces), tuple(ports), destination_port,
                     settings.voyage_max_ttl, "\n".join(errors).strip())


def parse_tcp_traces(result: TcpResult) -> TopologyObservation:
    """Normalize engine output without joining TTLs from different TCP flows."""
    records = []
    responses: dict[tuple[str | None, ...], set[str]] = {}
    destination_rtts: dict[tuple[str | None, ...], list[float]] = {}
    valid_rows = 0
    if not result.traces or len(result.traces) != len(result.source_ports):
        raise ValueError("TCP trace flow metadata is incomplete")
    for output, source_port in zip(result.traces, result.source_ports):
        if not re.search(r"^traceroute to " + re.escape(result.resolved_ip) + r"(?:\s|\()", output):
            raise ValueError("TCP trace output has an unexpected destination or header")
        flow_hops: dict[int, str] = {}
        endpoint_response = "no-response"
        previous_ttl = 0
        flow_rows = 0
        endpoint_rtt = None
        for line in output.splitlines()[1:]:
            match = ROW.match(line)
            if not match:
                continue
            ttl, body = int(match[1]), match[2].strip()
            if not 1 <= ttl <= result.max_ttl or ttl <= previous_ttl:
                raise ValueError("TCP trace has invalid or duplicate TTLs")
            previous_ttl = ttl
            valid_rows += 1
            flow_rows += 1
            if body == "*":
                continue
            try:
                address = str(ipaddress.IPv4Address(body.split()[0]))
            except ValueError as exc:
                raise ValueError(f"Invalid TCP hop: {line}") from exc
            rtt_match = RTT.search(body)
            if not rtt_match or len(RTT.findall(body)) != 1:
                raise ValueError(f"TCP hop must contain exactly one RTT: {line}")
            rtt = float(rtt_match[1])
            if not math.isfinite(rtt):
                raise ValueError("Invalid TCP RTT")
            flow_hops[ttl] = address
            records.append({
                "probe_dst_addr": result.resolved_ip, "probe_src_port": source_port,
                "probe_dst_port": result.destination_port, "probe_protocol": 6,
                "probe_ttl": ttl, "reply_src_addr": address, "rtt": rtt * 100,
            })
            if address == result.resolved_ip:
                flags_match = FLAGS.search(body)
                flags = set(re.split(r"[\s,]+", flags_match[1])) if flags_match else set()
                if "!" in body:
                    endpoint_response = "icmp-unreachable"
                elif {"syn", "ack"} <= flags:
                    endpoint_response = "syn-ack"
                    endpoint_rtt = rtt
                elif "rst" in flags:
                    endpoint_response = "reset"
                    endpoint_rtt = rtt
                else:
                    endpoint_response = "unconfirmed"
                break  # No interfaces beyond the first target reply.
        if not flow_rows:
            raise ValueError("TCP flow returned no hop rows")
        if flow_hops:
            signature = tuple(flow_hops.get(ttl) for ttl in range(1, max(flow_hops) + 1))
            responses.setdefault(signature, set()).add(endpoint_response)
            if endpoint_rtt is not None:
                destination_rtts.setdefault(signature, []).append(endpoint_rtt)
    if not valid_rows:
        raise ValueError("TCP trace returned no hop rows")
    if not records:
        # A successful all-silent measurement is distinct from an engine failure.
        return TopologyObservation((), (), (), 0)
    observation = parse_voyage_flat(json.dumps(records), result.resolved_ip, max_ttl=result.max_ttl)
    paths = []
    for path in observation.paths:
        signature = tuple(hop.address for hop in path.hops)
        endpoint = responses[signature]
        reached = bool(endpoint & {"syn-ack", "reset"})
        paths.append(replace(
            path, complete=reached,
            destination_rtt_ms=round(median(destination_rtts[signature]), 3) if reached else None,
            endpoint_response=next(iter(endpoint)) if len(endpoint) == 1 else "mixed-responses",
        ))
    return replace(observation, paths=tuple(paths), probe_count=len(records))
