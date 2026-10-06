import asyncio
import hashlib
import ipaddress
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from sqlalchemy import desc, func, select

from ..config import settings
from ..database import SessionLocal
from ..models import (
    Event,
    RouteHop,
    RoutePath,
    Target,
    TopologyEdge,
    TopologyNode,
    TopologySnapshot,
    TopologyProbeState,
)
from ..websocket import manager
from .topology_parser import TopologyObservation, parse_voyage_flat
from .voyage import run_voyage, resolve_target
from .tcp import run_tcp_trace, parse_tcp_traces


def route_label(index: int) -> str:
    value = max(1, index)
    letters = ""
    while value:
        value, remainder = divmod(value - 1, 26)
        letters = chr(65 + remainder) + letters
    return f"Route {letters}"


class DiscoveryFailure(RuntimeError):
    """All selected engines failed; their scoped error states are already saved."""


class TopologyService:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

    def start(self) -> None:
        if not self._task or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self._run(), name="topology-monitor")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run(self) -> None:
        await asyncio.sleep(3)
        while not self._stop.is_set():
            with SessionLocal() as db:
                target_ids = list(
                    db.scalars(
                        select(Target.id)
                        .where(Target.enabled.is_(True))
                        .order_by(Target.id)
                    )
                )

            for target_id in target_ids:
                if self._stop.is_set():
                    break
                try:
                    await self.discover(target_id)
                except DiscoveryFailure:
                    pass
                except Exception as exc:
                    self._record_failure(target_id, exc)

            await asyncio.sleep(settings.topology_interval_seconds)

    async def discover(
        self, target_id: int, *, include_raw: bool = False, protocol: str = "all",
    ) -> dict:
        if protocol not in {"all", "icmp", "tcp"}:
            raise ValueError("Unsupported topology protocol")
        async with self._locks[target_id]:
            with SessionLocal() as db:
                target = db.get(Target, target_id)
                if not target:
                    raise ValueError("Target not found")
                if not target.enabled:
                    raise ValueError("Target is disabled")
                address, tcp_port = target.address, target.tcp_port

            methods = ["icmp", "tcp"] if protocol == "all" else [protocol]
            if protocol == "all" and not settings.tcp_enabled:
                methods = ["icmp"]
            # Compare the same resolved destination, including hosts with multiple A records.
            try:
                resolved_ip = await resolve_target(address)
                if ipaddress.ip_address(resolved_ip).version != 4:
                    raise ValueError("Topology tracing currently supports IPv4 only")
            except (OSError, ValueError) as exc:
                for method in methods:
                    self._record_failure(target_id, exc, protocol=method, destination_port=tcp_port if method == "tcp" else 0)
                with SessionLocal() as db:
                    payload = topology_payload(db, target_id)
                await manager.broadcast({"type": "topology_update", "target_id": target_id, "topology": payload})
                raise DiscoveryFailure(str(exc)) from exc
            raw, errors, successes = [], {}, 0
            for method in methods:
                port = tcp_port if method == "tcp" else 0
                try:
                    if method == "icmp":
                        result = await run_voyage(resolved_ip)
                        if result.return_code != 0:
                            detail = result.stderr.strip() or result.stdout.strip() or "Unknown Voyage error"
                            raise RuntimeError(f"Voyage exited with code {result.return_code}: {detail[:500]}")
                        observation = parse_voyage_flat(result.stdout, resolved_ip, max_ttl=result.max_ttl)
                        output = result.stdout
                    else:
                        result = await run_tcp_trace(resolved_ip, tcp_port)
                        observation = parse_tcp_traces(result)
                        output = "\n\n".join(
                            f"Flow {index + 1} · TCP {source_port} → {tcp_port}\n{trace}"
                            for index, (source_port, trace) in enumerate(zip(result.source_ports, result.traces))
                        )
                    self._persist(target_id, resolved_ip, observation, protocol=method, destination_port=port)
                    successes += 1
                    if include_raw:
                        raw.append(f"=== {method.upper()}{(':' + str(port)) if port else ''} ===\n{output}\n{result.stderr}")
                except (OSError, RuntimeError, ValueError) as exc:
                    errors[method] = str(exc)[:500]
                    self._record_failure(target_id, exc, protocol=method, destination_port=port)
                    if include_raw:
                        raw.append(f"=== {method.upper()} failed ===\n{errors[method]}")

            with SessionLocal() as db:
                payload = topology_payload(db, target_id)
            await manager.broadcast({"type": "topology_update", "target_id": target_id, "topology": payload})
            if not successes:
                raise DiscoveryFailure("; ".join(f"{method.upper()}: {error}" for method, error in errors.items()))
            if include_raw:
                payload = dict(payload, raw_output="\n\n".join(raw), scan_errors=errors)
            return payload

    @staticmethod
    def _record_failure(
        target_id: int, exc: Exception, *, protocol: str = "icmp", destination_port: int = 0,
    ) -> None:
        scope = f"{protocol.upper()}{(':' + str(destination_port)) if destination_port else ''}"
        message = f"{scope} topology discovery failed: {str(exc)[:500]}"
        with SessionLocal() as db:
            if not db.get(Target, target_id):
                return
            state = probe_state(db, target_id, protocol, destination_port)
            state.last_attempt = datetime.now(timezone.utc)
            state.error = str(exc)[:500]
            latest = db.scalar(
                select(Event)
                .where(
                    Event.target_id == target_id,
                    Event.event_type == "topology_discovery_failed",
                )
                .order_by(desc(Event.created_at))
                .limit(1)
            )
            if not latest or latest.message != message:
                db.add(
                    Event(
                        target_id=target_id,
                        severity="warning",
                        event_type="topology_discovery_failed",
                        message=message,
                    )
                )
            db.commit()

    @staticmethod
    def _persist(
        target_id: int,
        resolved_ip: str,
        observation: TopologyObservation,
        *, protocol: str = "icmp", destination_port: int = 0,
    ) -> None:
        now = datetime.now(timezone.utc)

        with SessionLocal() as db:
            target = db.get(Target, target_id)
            if not target:
                return

            db.add(
                TopologySnapshot(
                    target_id=target_id,
                    created_at=now,
                    resolved_ip=resolved_ip,
                    protocol=protocol,
                    destination_port=destination_port,
                    probe_count=observation.probe_count,
                    node_count=len(observation.nodes),
                    edge_count=len(observation.edges),
                    route_count=len(observation.paths),
                )
            )

            if protocol == "icmp":
                existing_nodes = {
                    (node.ttl, node.address): node
                    for node in db.scalars(
                        select(TopologyNode).where(TopologyNode.target_id == target_id)
                    )
                }
                for node in existing_nodes.values():
                    node.active = False

                for item in observation.nodes:
                    key = (item.ttl, item.address)
                    node = existing_nodes.get(key)
                    if node is None:
                        node = TopologyNode(
                            target_id=target_id,
                            ttl=item.ttl,
                            address=item.address,
                            first_seen=now,
                            last_seen=now,
                        )
                        db.add(node)
                        db.flush()
                        existing_nodes[key] = node

                    old_count = node.sample_count
                    node.active = True
                    node.last_seen = now
                    node.last_rtt_ms = item.rtt_ms
                    node.sample_count += item.samples
                    if item.rtt_ms is not None:
                        if node.average_rtt_ms is None or old_count == 0:
                            node.average_rtt_ms = item.rtt_ms
                        else:
                            node.average_rtt_ms = (
                                (node.average_rtt_ms * old_count)
                                + (item.rtt_ms * item.samples)
                            ) / max(1, old_count + item.samples)

                existing_edges = {
                    (edge.source_node_id, edge.destination_node_id): edge
                    for edge in db.scalars(
                        select(TopologyEdge).where(TopologyEdge.target_id == target_id)
                    )
                }
                for edge in existing_edges.values():
                    edge.active = False

                for item in observation.edges:
                    source = existing_nodes.get((item.source_ttl, item.source_address))
                    destination = existing_nodes.get(
                        (item.destination_ttl, item.destination_address)
                    )
                    if source is None or destination is None:
                        continue

                    key = (source.id, destination.id)
                    edge = existing_edges.get(key)
                    if edge is None:
                        edge = TopologyEdge(
                            target_id=target_id,
                            source_node_id=source.id,
                            destination_node_id=destination.id,
                            sample_count=0,
                            first_seen=now,
                            last_seen=now,
                        )
                        db.add(edge)
                        existing_edges[key] = edge

                    edge.active = True
                    edge.last_seen = now
                    edge.sample_count += 1

            existing_paths = {
                route.path_hash: route
                for route in db.scalars(
                    select(RoutePath).where(
                        RoutePath.target_id == target_id, RoutePath.protocol == protocol,
                        RoutePath.destination_port == destination_port,
                    )
                )
            }
            next_index = (
                db.scalar(
                    select(func.max(RoutePath.route_index)).where(
                        RoutePath.target_id == target_id
                    )
                )
                or 0
            ) + 1
            seen_hashes: set[str] = set()

            for item in observation.paths:
                # Preserve old ICMP hashes; include TCP protocol/port in the stable path identity.
                path_hash = item.path_hash if protocol == "icmp" else hashlib.sha256(
                    f"{protocol}:{destination_port}:{item.path_hash}".encode()
                ).hexdigest()[:20]
                seen_hashes.add(path_hash)
                route = existing_paths.get(path_hash)
                is_new = route is None
                previous_status = route.status if route else None

                if route is None:
                    route = RoutePath(
                        target_id=target_id,
                        path_hash=path_hash,
                        protocol=protocol,
                        destination_port=destination_port,
                        route_index=next_index,
                        first_seen=now,
                        last_seen=now,
                        last_change=now,
                    )
                    next_index += 1
                    db.add(route)
                    db.flush()
                    existing_paths[path_hash] = route

                route.active = True
                route.complete = item.complete
                route.endpoint_response = item.endpoint_response
                route.hop_count = len(item.hops)
                route.flow_count = item.flow_count
                route.destination_rtt_ms = item.destination_rtt_ms
                route.last_seen = now
                route.seen_count += 1

                if item.destination_rtt_ms is not None:
                    route.minimum_rtt_ms = (
                        item.destination_rtt_ms
                        if route.minimum_rtt_ms is None
                        else min(route.minimum_rtt_ms, item.destination_rtt_ms)
                    )
                    route.maximum_rtt_ms = (
                        item.destination_rtt_ms
                        if route.maximum_rtt_ms is None
                        else max(route.maximum_rtt_ms, item.destination_rtt_ms)
                    )
                    if route.average_rtt_ms is None or route.rtt_sample_count == 0:
                        route.average_rtt_ms = item.destination_rtt_ms
                    else:
                        route.average_rtt_ms = (
                            route.average_rtt_ms * route.rtt_sample_count
                            + item.destination_rtt_ms
                        ) / (route.rtt_sample_count + 1)
                    route.rtt_sample_count += 1

                if route.baseline_rtt_ms is None and item.destination_rtt_ms is not None:
                    route.baseline_rtt_ms = item.destination_rtt_ms

                new_status = "active"
                if (
                    route.baseline_rtt_ms
                    and item.destination_rtt_ms
                    and item.destination_rtt_ms
                    >= route.baseline_rtt_ms * settings.degraded_latency_multiplier
                ):
                    new_status = "degraded"

                if new_status == "active" and item.destination_rtt_ms is not None:
                    if route.baseline_rtt_ms is None:
                        route.baseline_rtt_ms = item.destination_rtt_ms
                    else:
                        route.baseline_rtt_ms = (
                            route.baseline_rtt_ms * 0.95
                            + item.destination_rtt_ms * 0.05
                        )

                route.status = new_status
                if previous_status != new_status:
                    route.last_change = now

                existing_hops = {hop.ttl: hop for hop in route.hops}
                for hop_item in item.hops:
                    hop = existing_hops.get(hop_item.ttl)
                    if hop is None:
                        hop = RouteHop(route_path_id=route.id, ttl=hop_item.ttl)
                        db.add(hop)
                    hop.address = hop_item.address
                    hop.rtt_ms = hop_item.rtt_ms
                    hop.samples = hop_item.samples

                label = scoped_route_label(route)
                if is_new:
                    db.add(
                        Event(
                            target_id=target_id,
                            severity="info",
                            event_type="route_discovered",
                            message=f"{label} discovered ({route.hop_count} hops)",
                        )
                    )
                elif previous_status == "missing":
                    db.add(
                        Event(
                            target_id=target_id,
                            severity="info",
                            event_type="route_recovered",
                            message=f"{label} reappeared in the topology",
                        )
                    )
                elif previous_status != "degraded" and new_status == "degraded":
                    db.add(
                        Event(
                            target_id=target_id,
                            severity="warning",
                            event_type="route_degraded",
                            message=(
                                f"{label} latency degraded to "
                                f"{item.destination_rtt_ms:.1f} ms"
                            ),
                        )
                    )
                elif previous_status == "degraded" and new_status == "active":
                    db.add(
                        Event(
                            target_id=target_id,
                            severity="info",
                            event_type="route_recovered",
                            message=f"{label} latency returned to baseline",
                        )
                    )

            for path_hash, route in existing_paths.items():
                if route.hop_count > settings.voyage_max_ttl:
                    # Retain out-of-range legacy data without treating it as
                    # a newly disappeared measured path or emitting an alarm.
                    route.active = False
                    route.status = "missing"
                    continue
                if path_hash in seen_hashes:
                    continue
                was_active = route.active
                route.active = False
                route.miss_count += 1
                route.status = "missing"
                if was_active:
                    route.last_change = now
                    db.add(
                        Event(
                            target_id=target_id,
                            severity="warning",
                            event_type="route_missing",
                            message=(
                                f"{scoped_route_label(route)} disappeared "
                                "from the current topology"
                            ),
                        )
                    )

            state = probe_state(db, target_id, protocol, destination_port)
            state.last_attempt = state.last_success = now
            state.error = None
            db.commit()


def scoped_route_label(route: RoutePath) -> str:
    label = route_label(route.route_index)
    return label if route.protocol == "icmp" else f"TCP:{route.destination_port} {label}"


def probe_state(db, target_id: int, protocol: str, destination_port: int) -> TopologyProbeState:
    state = db.scalar(select(TopologyProbeState).where(
        TopologyProbeState.target_id == target_id, TopologyProbeState.protocol == protocol,
        TopologyProbeState.destination_port == destination_port,
    ))
    if not state:
        state = TopologyProbeState(target_id=target_id, protocol=protocol, destination_port=destination_port)
        db.add(state)
    return state


def topology_payload(db, target_id: int) -> dict:
    target = db.get(Target, target_id)
    if not target:
        raise ValueError("Target not found")

    cutoff = datetime.now(timezone.utc) - timedelta(
        minutes=settings.topology_stale_minutes
    )

    nodes = list(
        db.scalars(
            select(TopologyNode)
            .where(
                TopologyNode.target_id == target_id,
                TopologyNode.ttl <= settings.voyage_max_ttl,
                (TopologyNode.active.is_(True))
                | (TopologyNode.last_seen >= cutoff),
            )
            .order_by(TopologyNode.ttl, TopologyNode.address)
        )
    )
    node_ids = {node.id for node in nodes}

    edges = [
        edge
        for edge in db.scalars(
            select(TopologyEdge)
            .where(
                TopologyEdge.target_id == target_id,
                (TopologyEdge.active.is_(True))
                | (TopologyEdge.last_seen >= cutoff),
            )
            .order_by(TopologyEdge.id)
        )
        if edge.source_node_id in node_ids
        and edge.destination_node_id in node_ids
    ]

    routes = list(
        db.scalars(
            select(RoutePath)
            .where(
                RoutePath.target_id == target_id,
                (RoutePath.protocol == "icmp") | ((RoutePath.protocol == "tcp") & (RoutePath.destination_port == target.tcp_port)),
                (RoutePath.active.is_(True))
                | (RoutePath.last_seen >= cutoff),
            )
            .order_by(RoutePath.route_index)
        )
    )
    excluded_routes = sum(route.hop_count > settings.voyage_max_ttl for route in routes)
    routes = [route for route in routes if route.hop_count <= settings.voyage_max_ttl]
    route_ids = [route.id for route in routes]
    hops_by_route: dict[int, list[RouteHop]] = defaultdict(list)
    if route_ids:
        for hop in db.scalars(
            select(RouteHop)
            .where(RouteHop.route_path_id.in_(route_ids))
            .order_by(RouteHop.route_path_id, RouteHop.ttl)
        ):
            hops_by_route[hop.route_path_id].append(hop)

    node_lookup = {
        (node.ttl, node.address): node.id
        for node in nodes
    }
    latest_snapshot = db.scalar(
        select(TopologySnapshot)
        .where(
            TopologySnapshot.target_id == target_id,
            (TopologySnapshot.protocol == "icmp") | (
                (TopologySnapshot.protocol == "tcp") & (TopologySnapshot.destination_port == target.tcp_port)
            ),
        )
        .order_by(desc(TopologySnapshot.created_at), desc(TopologySnapshot.id))
        .limit(1)
    )

    graph_nodes = [
        {
            "id": "probe",
            "label": "Probe",
            "ttl": 0,
            "address": None,
            "rtt_ms": 0.0,
            "active": True,
        }
    ]
    graph_nodes.extend(
        {
            "id": f"node-{node.id}",
            "label": (
                f"TTL {node.ttl}\n{node.address}\n{node.last_rtt_ms:.1f} ms"
                if node.last_rtt_ms is not None
                else f"TTL {node.ttl}\n{node.address}"
            ),
            "ttl": node.ttl,
            "protocol": "icmp",
            "destination_port": 0,
            "address": node.address,
            "rtt_ms": node.last_rtt_ms,
            "average_rtt_ms": node.average_rtt_ms,
            "samples": node.sample_count,
            "active": node.active,
            "last_seen": node.last_seen.isoformat(),
        }
        for node in nodes
    )

    graph_edges = [
        {
            "id": f"edge-{edge.id}",
            "protocol": "icmp",
            "source": f"node-{edge.source_node_id}",
            "target": f"node-{edge.destination_node_id}",
            "active": edge.active,
            "samples": edge.sample_count,
            "last_seen": edge.last_seen.isoformat(),
        }
        for edge in edges
    ]

    active_nodes = [node for node in nodes if node.active]
    if active_nodes:
        first_ttl = min(node.ttl for node in active_nodes)
        for node in active_nodes:
            if node.ttl == first_ttl:
                graph_edges.append(
                    {
                        "id": f"probe-{node.id}",
                        "source": "probe",
                        "target": f"node-{node.id}",
                        "active": True,
                        "samples": 0,
                        "last_seen": node.last_seen.isoformat(),
                    }
                )

    route_payload = []
    for route in routes:
        total = route.seen_count + route.miss_count
        availability = (
            round((route.seen_count / total) * 100, 3)
            if total
            else None
        )
        hops = []
        node_path = []
        for hop in hops_by_route[route.id]:
            node_id = None
            if hop.address is not None:
                if route.protocol == "icmp":
                    cached_id = node_lookup.get((hop.ttl, hop.address))
                    node_id = f"node-{cached_id}" if cached_id is not None else None
                else:
                    node_id = f"tcp-{route.destination_port}-{hop.ttl}-{hop.address}"
            if node_id is not None:
                node_path.append(node_id)
            hops.append(
                {
                    "ttl": hop.ttl,
                    "address": hop.address,
                    "rtt_ms": hop.rtt_ms,
                    "samples": hop.samples,
                    "node_id": node_id,
                }
            )

        route_payload.append(
            {
                "id": route.id,
                "label": scoped_route_label(route),
                "protocol": route.protocol,
                "destination_port": route.destination_port,
                "endpoint_response": route.endpoint_response,
                "path_hash": route.path_hash,
                "active": route.active,
                "status": route.status,
                "complete": route.complete,
                "hop_count": route.hop_count,
                "flow_count": route.flow_count,
                "destination_rtt_ms": route.destination_rtt_ms,
                "baseline_rtt_ms": route.baseline_rtt_ms,
                "minimum_rtt_ms": route.minimum_rtt_ms,
                "average_rtt_ms": route.average_rtt_ms,
                "maximum_rtt_ms": route.maximum_rtt_ms,
                "availability_percent": availability,
                "first_seen": route.first_seen.isoformat(),
                "last_seen": route.last_seen.isoformat(),
                "last_change": route.last_change.isoformat(),
                "node_path": node_path,
                "hops": hops,
            }
        )

    # Each TCP adjacency comes exclusively from consecutive hops of a single stored TCP path.
    # Never fill a missing TCP hop with an ICMP reply, even at the same TTL.
    tcp_nodes, tcp_edges = {}, {}
    for route in route_payload:
        if route["protocol"] != "tcp":
            continue
        for hop in route["hops"]:
            if not hop["node_id"]:
                continue
            node = dict(hop, id=hop["node_id"], protocol="tcp", destination_port=route["destination_port"],
                        active=route["active"], last_seen=route["last_seen"], average_rtt_ms=None)
            previous = tcp_nodes.get(node["id"])
            if previous is None or (node["active"], node["last_seen"]) > (previous["active"], previous["last_seen"]):
                tcp_nodes[node["id"]] = node
        for first, second in zip(route["hops"], route["hops"][1:]):
            if not first["node_id"] or not second["node_id"] or second["ttl"] != first["ttl"] + 1:
                continue
            key = (first["node_id"], second["node_id"])
            edge = dict(id="tcp-edge-" + "-".join(key), source=key[0], target=key[1], protocol="tcp",
                        active=route["active"], samples=min(first["samples"], second["samples"]), last_seen=route["last_seen"])
            if key not in tcp_edges or route["active"]:
                tcp_edges[key] = edge
    graph_nodes.extend(tcp_nodes.values())
    graph_edges.extend(tcp_edges.values())

    measurements = []
    for protocol, port in (("icmp", 0), ("tcp", target.tcp_port)):
        snapshot = db.scalar(select(TopologySnapshot).where(
            TopologySnapshot.target_id == target_id, TopologySnapshot.protocol == protocol,
            TopologySnapshot.destination_port == port,
        ).order_by(desc(TopologySnapshot.created_at), desc(TopologySnapshot.id)).limit(1))
        state = db.scalar(select(TopologyProbeState).where(
            TopologyProbeState.target_id == target_id, TopologyProbeState.protocol == protocol,
            TopologyProbeState.destination_port == port,
        ))
        scoped_routes = [route for route in route_payload if route["protocol"] == protocol]
        measurements.append({
            "protocol": protocol, "destination_port": port,
            "engine": "Voyage / Paris MDA" if protocol == "icmp" else "TCP SYN / sampled flows",
            "last_scan": snapshot.created_at.isoformat() if snapshot else None,
            "last_attempt": state.last_attempt.isoformat() if state else None,
            "error": state.error if state else None,
            "probe_replies": snapshot.probe_count if snapshot else 0,
            "active_routes": sum(route["active"] for route in scoped_routes),
            "complete_routes": sum(route["active"] and route["complete"] for route in scoped_routes),
            "missing_routes": sum(not route["active"] for route in scoped_routes),
        })

    return {
        "target": {
            "id": target.id,
            "name": target.name,
            "address": target.address,
            "tcp_port": target.tcp_port,
            "status": target.status,
            "latency_ms": target.latency_ms,
            "loss_percent": target.loss_percent,
            "jitter_ms": target.jitter_ms,
        },
        "resolved_ip": latest_snapshot.resolved_ip if latest_snapshot else None,
        "max_ttl": settings.voyage_max_ttl,
        "summary": {
            "active_routes": sum(route.active for route in routes),
            "degraded_routes": sum(
                route.status == "degraded" for route in routes
            ),
            "missing_routes": sum(
                route.status == "missing" for route in routes
            ),
            "excluded_routes": excluded_routes,
            "nodes": len(graph_nodes) - 1,
            "edges": len(graph_edges),
            "probe_replies": sum(measurement["probe_replies"] for measurement in measurements),
            "last_scan": (
                latest_snapshot.created_at.isoformat()
                if latest_snapshot
                else None
            ),
        },
        "nodes": graph_nodes,
        "edges": graph_edges,
        "routes": route_payload,
        "measurements": measurements,
    }


topology = TopologyService()
