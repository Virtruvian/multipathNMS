import hashlib
import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from statistics import median


@dataclass(slots=True, frozen=True)
class HopObservation:
    ttl: int
    address: str | None
    rtt_ms: float | None
    samples: int


@dataclass(slots=True, frozen=True)
class PathObservation:
    path_hash: str
    hops: tuple[HopObservation, ...]
    flow_count: int
    complete: bool
    destination_rtt_ms: float | None


@dataclass(slots=True, frozen=True)
class NodeObservation:
    ttl: int
    address: str
    rtt_ms: float | None
    samples: int


@dataclass(slots=True, frozen=True)
class EdgeObservation:
    source_ttl: int
    source_address: str
    destination_ttl: int
    destination_address: str


@dataclass(slots=True, frozen=True)
class TopologyObservation:
    nodes: tuple[NodeObservation, ...]
    edges: tuple[EdgeObservation, ...]
    paths: tuple[PathObservation, ...]
    probe_count: int


def _extract_flat_records(stdout: str) -> list[dict]:
    decoder = json.JSONDecoder()
    for offset, character in enumerate(stdout):
        if character != "[":
            continue
        try:
            value, _ = decoder.raw_decode(stdout[offset:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    raise ValueError("Voyage flat output did not contain a JSON array")


def _rtt_ms(record: dict) -> float | None:
    raw = record.get("rtt")
    if raw is None:
        return None
    try:
        # Voyage stores caracat RTT in tenths of a millisecond in the
        # Pantrace internal object; Pantrace flat multiplies that value by 10.
        return round(float(raw) / 100.0, 3)
    except (TypeError, ValueError):
        return None


def parse_voyage_flat(stdout: str, resolved_ip: str) -> TopologyObservation:
    records = _extract_flat_records(stdout)
    if not records:
        raise ValueError("Voyage returned no replies")

    flows: dict[tuple, list[tuple[int, str, float | None]]] = defaultdict(list)
    node_rtts: dict[tuple[int, str], list[float]] = defaultdict(list)
    node_counts: Counter[tuple[int, str]] = Counter()

    for record in records:
        try:
            ttl = int(record["probe_ttl"])
            address = str(record["reply_src_addr"])
            flow_key = (
                str(record.get("probe_src_addr", "")),
                str(record.get("probe_dst_addr", resolved_ip)),
                int(record.get("probe_src_port", 0)),
                int(record.get("probe_dst_port", 0)),
                int(record.get("probe_protocol", 0)),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"Invalid Voyage flat record: {record!r}") from exc

        if not 1 <= ttl <= 255:
            continue

        rtt_ms = _rtt_ms(record)
        flows[flow_key].append((ttl, address, rtt_ms))
        node_counts[(ttl, address)] += 1
        if rtt_ms is not None:
            node_rtts[(ttl, address)].append(rtt_ms)

    if not flows:
        raise ValueError("Voyage returned no usable replies")

    nodes = tuple(
        NodeObservation(
            ttl=ttl,
            address=address,
            rtt_ms=round(median(node_rtts[(ttl, address)]), 3)
            if node_rtts[(ttl, address)]
            else None,
            samples=node_counts[(ttl, address)],
        )
        for ttl, address in sorted(node_counts)
    )

    edges: set[EdgeObservation] = set()
    grouped_paths: dict[tuple[str | None, ...], list[tuple[HopObservation, ...]]] = defaultdict(list)

    for replies in flows.values():
        replies_by_ttl: dict[int, list[tuple[str, float | None]]] = defaultdict(list)
        for ttl, address, rtt_ms in replies:
            replies_by_ttl[ttl].append((address, rtt_ms))

        selected: dict[int, HopObservation] = {}
        for ttl, values in replies_by_ttl.items():
            address_counts = Counter(address for address, _ in values)
            address = sorted(address_counts, key=lambda item: (-address_counts[item], item))[0]
            rtts = [
                rtt_ms
                for item_address, rtt_ms in values
                if item_address == address and rtt_ms is not None
            ]
            selected[ttl] = HopObservation(
                ttl=ttl,
                address=address,
                rtt_ms=round(median(rtts), 3) if rtts else None,
                samples=address_counts[address],
            )

        destination_ttls = [
            ttl for ttl, hop in selected.items() if hop.address == resolved_ip
        ]
        max_ttl = min(destination_ttls) if destination_ttls else max(selected)
        hops = tuple(
            selected.get(ttl, HopObservation(ttl=ttl, address=None, rtt_ms=None, samples=0))
            for ttl in range(1, max_ttl + 1)
        )
        signature = tuple(hop.address for hop in hops)
        grouped_paths[signature].append(hops)

        for previous, current in zip(hops, hops[1:]):
            if previous.address and current.address:
                edges.add(
                    EdgeObservation(
                        source_ttl=previous.ttl,
                        source_address=previous.address,
                        destination_ttl=current.ttl,
                        destination_address=current.address,
                    )
                )

    paths: list[PathObservation] = []
    for grouped_flows in grouped_paths.values():
        hop_rtts: dict[int, list[float]] = defaultdict(list)
        hop_samples: Counter[int] = Counter()
        for hops in grouped_flows:
            for hop in hops:
                hop_samples[hop.ttl] += hop.samples
                if hop.rtt_ms is not None:
                    hop_rtts[hop.ttl].append(hop.rtt_ms)

        template = grouped_flows[0]
        aggregate_hops = tuple(
            HopObservation(
                ttl=hop.ttl,
                address=hop.address,
                rtt_ms=round(median(hop_rtts[hop.ttl]), 3)
                if hop_rtts[hop.ttl]
                else None,
                samples=hop_samples[hop.ttl],
            )
            for hop in template
        )
        hash_input = "|".join(
            f"{hop.ttl}:{hop.address or '*'}" for hop in aggregate_hops
        )
        path_hash = hashlib.sha256(hash_input.encode()).hexdigest()[:20]
        destination_rtts = [
            hop.rtt_ms
            for hops in grouped_flows
            for hop in hops
            if hop.address == resolved_ip and hop.rtt_ms is not None
        ]
        paths.append(
            PathObservation(
                path_hash=path_hash,
                hops=aggregate_hops,
                flow_count=len(grouped_flows),
                complete=any(hop.address == resolved_ip for hop in aggregate_hops),
                destination_rtt_ms=round(median(destination_rtts), 3)
                if destination_rtts
                else None,
            )
        )

    return TopologyObservation(
        nodes=nodes,
        edges=tuple(
            sorted(
                edges,
                key=lambda edge: (
                    edge.source_ttl,
                    edge.source_address,
                    edge.destination_ttl,
                    edge.destination_address,
                ),
            )
        ),
        paths=tuple(sorted(paths, key=lambda path: path.path_hash)),
        probe_count=len(records),
    )
