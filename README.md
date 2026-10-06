# multipathNMS

A lightweight live route and multipath NMS built around Paris-style MDA discovery.

## Views

- **/nms** — compact operational dashboard for target health, route counts and alerts.
- **/topology** — interactive Cytoscape topology with persistent Route A/B/C identities.
- **/settings** — manage monitored targets.

The topology view is a network path analysis: a straight primary path runs from
source to target, with alternatives branching and merging. A shared target endpoint
represents paths with different measured hop counts; their original TTLs remain in
route details. Hops show IP, TTL and RTT from the probe. Links represent observed
same-flow adjacency, not verified physical cables or OSPF neighbours. Dotted segments
explicitly indicate unobserved or ambiguous hops. **Router symbols** switches the
compact path markers to router icons.
Select a route to highlight it, drag to pan, and use **Fit all** or **Readable view**
to navigate large topologies. Live updates retain the viewport and route selection.
The default graph shows only nodes and links belonging to reconstructed paths.
Use **Show loose replies** to inspect replies that cannot be placed on a path.
Current partial paths remain visible. **Show recent history** overlays recent missing
routes without mixing them into the default current-path view. Missing-route counts
remain visible even while the history overlay is hidden.

## Stack

Docker → FastAPI → Voyage / Paris MDA + TCP SYN traceroute → WebSocket → Cytoscape.js → SQLite

## How monitoring works

Two measurement loops intentionally run at different speeds:

- **Health loop** (default every 2 seconds): target RTT, loss, jitter and healthy/degraded/suspect/down.
- **Topology loop** (default 60 seconds after each round completes): ICMP Voyage/Paris MDA discovery followed by bounded TCP SYN traces to the target's saved port (default 443).

A route that was previously discovered but is absent from the newest MDA result is marked **missing** and retained in the recent-history view for a configurable window. Missing does not automatically prove that an intermediate router failed; it means that path is no longer being observed in the current topology.

ICMP topology probes use Voyage. The Docker build patches the pinned Voyage
revision with an explicit single-target mode: multipath flows vary source ports
while retaining the resolved target IP. This prevents neighbouring destinations
from being mixed into the trace. Each flow ends at its first target reply, including
the nodes saved for that flow. See `patches/README.md` for the integration details.
The receiver validates replies against the actual sent destination, flow identifiers,
TTL and protocol, and checks supported packet checksums using a random per-scan
instance ID. This prevents unrelated health pings or other targets from contaminating
the topology. The parser also enforces the sent TTL range and treats multiple IPs
within one flow/TTL as ambiguous instead of picking a majority and inventing links.
Legacy paths outside the configured probe range are excluded from the current
payload, while their stored history remains intact.
The Voyage adapter also accepts UDP. A separate Linux `traceroute -T` adapter adds
TCP SYN tracing. **Compare ICMP + TCP**, **ICMP** and **TCP** switch the visible paths.
ICMP links are green; TCP links are blue. Degraded and missing paths retain their
amber/red status colours. Intermediate nodes and measured adjacencies stay scoped
by protocol and port: a silent TCP hop is never filled with an ICMP reply.

TCP uses three sampled flows by default. Each flow keeps its source and destination
ports constant while increasing TTL, with one outstanding probe and one query per
hop. This reduces flow changes within a trace; per-packet load balancing and route
changes during a measurement can still affect the observed sequence. TCP sampling
supplements ICMP MDA and has no MDA completeness/confidence guarantee.
Both engines use the same resolved IPv4 address within a comparison round.

Set **TCP port** in the topology toolbar and click **Discover now** to save it for
that target and use it in subsequent automatic rounds. Changing ports retains
previous-port history in SQLite, while the view selects the currently saved port.
A SYN/ACK confirms a TCP endpoint reply; a reset also reaches the endpoint but does
not establish an open service. ICMP unreachable replies do not count as TCP success.
Silent hops can reflect filtering or rate limiting and do not establish an outage.
Target health, ping loss and jitter remain explicitly labelled as ICMP measurements.

Comparison cards show each method's last successful scan, replies, endpoint result
and engine errors. A failed engine keeps its previous paths and counters intact;
only a completed scan can mark paths missing, and only in that protocol/port scope.
Shared endpoint markers expose RTT through the route details rather than mixing
ICMP and TCP RTT values. Existing SQLite data is upgraded additively on startup;
existing route IDs, hashes, hop history and events remain intact.

Per route the UI keeps:

- stable Route A/B/C identity
- current destination RTT
- RTT minimum / average / maximum
- learned RTT baseline
- route-presence availability
- hop count
- protocol, destination port and sampled/MDA flow count
- first seen / last seen / last change
- per-hop RTT

## Start

Install alongside other applications under `/opt/multipathNMS`:

```bash
sudo git clone --branch develop/init-project https://github.com/Virtruvian/multipathNMS.git /opt/multipathNMS
cd /opt/multipathNMS
sudo docker compose up -d --build
```

While PR #1 is open, use `develop/init-project`. After it is merged, new installations can use `main`.

The web interface is published on host port **8090**, mapped to the application on container port 8080 (`8090:8080`). Monitoring data persists in `/opt/multipathNMS/data`.

Open:

- http://localhost:8090/nms
- http://localhost:8090/topology
- http://localhost:8090/settings
- http://localhost:8090/docs

From another computer, replace `localhost` with the Docker host's IP address.

The container uses `NET_RAW` for ICMP, Voyage and TCP SYN probing. Voyage is pinned to a known source commit in the Dockerfile so builds are reproducible.

Update an existing installation, including the patched probe executable:

```bash
cd /opt/multipathNMS
sudo git pull --ff-only origin develop/init-project
sudo docker compose up -d --build
```

Run **Discover now** after updating. Previously saved paths are retained as recent
history for `TOPOLOGY_STALE_MINUTES` (default 15 minutes), rather than deleting data.

## Configuration

Useful Compose environment variables:

```text
HEALTH_INTERVAL_SECONDS=2
TOPOLOGY_INTERVAL_SECONDS=60
TOPOLOGY_STALE_MINUTES=15
VOYAGE_TIMEOUT_SECONDS=90
VOYAGE_PROBING_RATE=50
VOYAGE_CONFIDENCE=99
VOYAGE_MAX_TTL=32
TCP_ENABLED=true
TCP_PORT=443
TCP_FLOWS=3
TCP_HOP_TIMEOUT_SECONDS=1
TCP_SENDWAIT_SECONDS=0.05
SUSPECT_AFTER_FAILURES=3
DOWN_AFTER_FAILURES=5
DEGRADED_LOSS_PERCENT=10
DEGRADED_LATENCY_MULTIPLIER=2
```

`TCP_ENABLED=false` skips TCP in automatic/comparison rounds; explicit manual TCP
discovery remains available. `TCP_PORT` supplies the default for new targets and
existing databases during migration. Each target then keeps its own saved port.
TCP shares `VOYAGE_MAX_TTL`. Each flow has a timeout bound of
`max_ttl × (hop_timeout + sendwait) + 5` seconds; a round includes all configured
flows plus the ICMP scan. IPv4 topology is supported in this version.

Avoid setting topology discovery to very short intervals: MDA intentionally sends substantially more probes than a normal ping.

## Tests

Parsers, TCP adapter cleanup, method/port isolation, legacy migration and SQLite
topology persistence have deterministic fixture tests:

```bash
APP_DATA_DIR=./test-data python -m unittest discover -s tests -v
```

Graph preparation tests use Node.js 22 without additional npm dependencies:

```bash
node --test tests/test_network_graph.js
```

## Security

Targets are validated as IP addresses or DNS hostnames and are passed to subprocesses as argument arrays, never through a shell. The current application is intended for trusted/internal deployment; add authentication before exposing it publicly.

See `AGENTS.md` for architecture and development rules.
