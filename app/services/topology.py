import asyncio
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
)
from ..websocket import manager
from .topology_parser import TopologyObservation, parse_voyage_flat
from .voyage import run_voyage


def route_label(index: int) -> str:
    value = max(1, index)
    letters = ""
    while value:
        value, remainder = divmod(value - 1, 26)
        letters = chr(65 + remainder) + letters
    return f"Route {letters}"


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
                except Exception as exc:
                    self._record_failure(target_id, exc)

            await asyncio.sleep(settings.topology_interval_seconds)

    async def discover(self, target_id: int, *, include_raw: bool = False) -> dict:
        async with self._locks[target_id]:
            with SessionLocal() as db:
                target = db.get(Target, target_id)
                if not target:
                    raise ValueError("Target not found")
                if not target.enabled:
                    raise ValueError("Target is disabled")
                address = target.address

            result = await run_voyage(address)
            if result.return_code != 0:
                detail = result.stderr.strip() or result.stdout.strip() or "Unknown Voyage error"
                raise RuntimeError(
                    f"Voyage exited with code {result.return_code}: {detail[:500]}"
                )

            try:
                observation = parse_voyage_flat(result.stdout, result.resolved_ip)
            except ValueError as exc:
                raise RuntimeError(f"Unable to parse Voyage output: {exc}") from exc

            self._persist(target_id, result.resolved_ip, observation)

            with SessionLocal() as db:
                payload = topology_payload(db, target_id)

            await manager.broadcast(
                {
                    "type": "topology_update",
                    "target_id": target_id,
                    "topology": payload,
                }
            )

            if include_raw:
                payload = dict(payload)
                payload["raw_output"] = result.stdout
                payload["stderr"] = result.stderr
            return payload

    @staticmethod
    def _record_failure(target_id: int, exc: Exception) -> None:
        message = f"Topology discovery failed: {str(exc)[:500]}"
        with SessionLocal() as db:
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
                    probe_count=observation.probe_count,
                    node_count=len(observation.nodes),
                    edge_count=len(observation.edges),
                    route_count=len(observation.paths),
                )
            )

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
                    select(RoutePath).where(RoutePath.target_id == target_id)
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
                seen_hashes.add(item.path_hash)
                route = existing_paths.get(item.path_hash)
                is_new = route is None
                previous_status = route.status if route else None

                if route is None:
                    route = RoutePath(
                        target_id=target_id,
                        path_hash=item.path_hash,
                        route_index=next_index,
                        first_seen=now,
                        last_seen=now,
                        last_change=now,
                    )
                    next_index += 1
                    db.add(route)
                    db.flush()
                    existing_paths[item.path_hash] = route

                route.active = True
                route.complete = item.complete
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

                label = route_label(route.route_index)
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
                                f"{route_label(route.route_index)} disappeared "
                                "from the current topology"
                            ),
                        )
                    )

            db.commit()


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
                (RoutePath.active.is_(True))
                | (RoutePath.last_seen >= cutoff),
            )
            .order_by(RoutePath.route_index)
        )
    )
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
        .where(TopologySnapshot.target_id == target_id)
        .order_by(desc(TopologySnapshot.created_at))
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
            node_id = (
                node_lookup.get((hop.ttl, hop.address))
                if hop.address is not None
                else None
            )
            if node_id is not None:
                node_path.append(f"node-{node_id}")
            hops.append(
                {
                    "ttl": hop.ttl,
                    "address": hop.address,
                    "rtt_ms": hop.rtt_ms,
                    "samples": hop.samples,
                    "node_id": (
                        f"node-{node_id}"
                        if node_id is not None
                        else None
                    ),
                }
            )

        route_payload.append(
            {
                "id": route.id,
                "label": route_label(route.route_index),
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

    return {
        "target": {
            "id": target.id,
            "name": target.name,
            "address": target.address,
            "status": target.status,
            "latency_ms": target.latency_ms,
            "loss_percent": target.loss_percent,
            "jitter_ms": target.jitter_ms,
        },
        "summary": {
            "active_routes": sum(route.active for route in routes),
            "degraded_routes": sum(
                route.status == "degraded" for route in routes
            ),
            "missing_routes": sum(
                route.status == "missing" for route in routes
            ),
            "nodes": len(nodes),
            "edges": len(edges),
            "probe_replies": (
                latest_snapshot.probe_count
                if latest_snapshot
                else 0
            ),
            "last_scan": (
                latest_snapshot.created_at.isoformat()
                if latest_snapshot
                else None
            ),
        },
        "nodes": graph_nodes,
        "edges": graph_edges,
        "routes": route_payload,
    }


topology = TopologyService()
