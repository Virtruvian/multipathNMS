# multipathNMS

A lightweight live route and multipath NMS built around Paris-style MDA discovery.

## Views

- **/nms** — compact operational dashboard for target health, route counts and alerts.
- **/topology** — interactive Cytoscape topology with persistent Route A/B/C identities.
- **/settings** — manage monitored targets.

The topology view is a network path analysis: a straight primary path runs from
source to target, with alternatives branching and merging. A shared target endpoint
represents paths with different measured hop counts; their original TTLs remain in
route details. Hops show IP, TTL and RTT from the probe. ICMP links represent observed
same-flow adjacency; dashed blue TCP lines show hop order within a sampled trace.
Neither verifies physical cables or OSPF neighbours. Dotted segments
explicitly indicate unobserved or ambiguous hops. **Router symbols** switches the
compact path markers to router icons.
Select a route to highlight it, drag to pan, and use **Fit all** or **Readable view**
to navigate large topologies. Live updates retain the viewport and route selection.
The default graph shows only nodes and links belonging to reconstructed paths.
Use **Show loose replies** to inspect replies that cannot be placed on a path.
Current partial paths remain visible. **Show recent history** overlays recent missing
routes without mixing them into the default current-path view. Missing-route counts
remain visible even while the history overlay is hidden.

## Service reachability and route confirmation

The NMS and topology views keep three independent results: ICMP host health,
normal TCP connection reachability, and optional HTTPS response validation.
Service checks have their own loop (30 seconds after each round, at most eight
targets concurrently) so service timeouts never delay the fast ping loop.
A TCP trace's SYN/ACK or reset does not replace the ordinary TCP connection check.

In **Settings**, configure the TCP/HTTPS port, enable or disable the normal TCP
check, and opt into HTTPS with a relative path such as `/health`. Existing targets
get TCP checks enabled and HTTPS disabled. HTTPS uses the saved port, verifies the
certificate and hostname against the system trust store, and checks the first final
HTTP status: 200–399 is successful. Redirects are reported without being followed;
response bodies are not downloaded. This tests that endpoint, not a complete login
or application workflow. Service probes may use IPv4 or IPv6 according to host DNS;
each result exposes the connected IP separately from IPv4 topology discovery.

**Check services now** runs an immediate round from the topology view. The API is
`POST /api/targets/{id}/check-services`. The normal round has a five-second total
DNS/connection/TLS/response timeout. One or two consecutive failures are **VERIFYING**,
with the failure count and reason visible. The third emits one service-failure event;
subsequent failures do not repeat it. A successful check resets the streak and emits
one recovery event if the service had a confirmed failure. Current states and every
service sample are stored separately in SQLite. Failed HTTPS does not change TCP
or ICMP health. Edited settings immediately invalidate the affected current state;
old samples remain, and results from an older configuration revision are discarded.

A successfully completed topology scan that omits a known path removes it from the
current graph immediately but first records **pending**. The third consecutive
completed scan that omits that path for the same destination IP and protocol/port
confirms **missing** and emits one observation warning. An engine failure leaves
that streak unchanged. Reappearance resets it; a transient omission emits no
missing/recovery alarm pair. Missing still means *not observed*, not a proven outage.

DNS changes are informational: old-IP routes become **previous target IP** history
without counting as missed scans. Identical partial prefixes for different target
IPs retain separate identities. Existing route IDs and hashes stay intact; legacy
routes acquire their resolved IP from their confirmed endpoint or matching history.
Pending and previous-IP history use neutral colours. The graph never fills gaps
from previous scans. **Observed scans** is the fraction of scans in which a path
appeared, not network uptime. API timestamps explicitly carry UTC and browser
views display local times. The full historical timeline remains a future extension.

## Stack

Docker → FastAPI → Voyage / Paris MDA + TCP SYN traceroute → WebSocket → Cytoscape.js → SQLite

## How monitoring works

Two measurement loops intentionally run at different speeds:

- **Health loop** (default every 2 seconds): target RTT, loss, jitter and healthy/degraded/suspect/down.
- **Topology loop** (default 60 seconds after each round completes): ICMP Voyage/Paris MDA discovery followed by bounded TCP SYN traces to the target's saved port (default 443).

A route that was previously discovered but is absent from the newest MDA result is initially **pending**, then **missing** after three consecutive completed scans for the same resolved destination, and retained in the recent-history view for a configurable window. Missing does not automatically prove that an intermediate router failed; it means that path is no longer being observed in the current topology.

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

TCP runs three sampled traces by default, with a fixed destination port but
source ports chosen automatically by Linux, one outstanding probe and one query
per hop. On the tested deployment, fixed source ports produced only replies up
to TTL 6; the otherwise identical automatic-port trace reached the endpoint at
TTL 28. This identifies a useful compatibility setting without proving which
network device or filtering rule caused the difference.
Automatic source ports can select different ECMP flows between hops. TCP lines
therefore represent sampled hop order, not confirmed same-flow adjacency. Each
trace stays separate during parsing: one trace never supplies another's missing
hop. TCP supplements ICMP MDA and has no MDA completeness/confidence guarantee.
`TCP_FIXED_SOURCE_PORT=true` opts into the previous fixed-port probing mode for
diagnostics. The UI conservatively labels all TCP results as sampled traces,
including stored history. `flow_count` remains the existing storage/API field but
counts matching trace runs for TCP; the UI labels it **Trace samples**.
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
- protocol, destination port and TCP trace sample / ICMP MDA flow count
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
TCP_FIXED_SOURCE_PORT=false
TCP_HOP_TIMEOUT_SECONDS=1
TCP_SENDWAIT_SECONDS=0.05
SERVICE_INTERVAL_SECONDS=30
SERVICE_TIMEOUT_SECONDS=5
SERVICE_FAILURES_BEFORE_DOWN=3
ROUTE_MISSING_AFTER_SCANS=3
SUSPECT_AFTER_FAILURES=3
DOWN_AFTER_FAILURES=5
DEGRADED_LOSS_PERCENT=10
DEGRADED_LATENCY_MULTIPLIER=2
```

`TCP_ENABLED=false` skips TCP in automatic/comparison rounds; explicit manual TCP
discovery remains available. `TCP_PORT` supplies the default for new targets and
existing databases during migration. Each target then keeps its own saved port.
TCP shares `VOYAGE_MAX_TTL`. Each trace has a timeout bound of
`max_ttl × (hop_timeout + sendwait) + 5` seconds; a round includes all configured
traces plus the ICMP scan. IPv4 topology is supported in this version.

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
