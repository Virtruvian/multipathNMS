# multipathNMS

A lightweight live route and multipath NMS built around Paris-style MDA discovery.

## Views

- **/nms** — compact operational dashboard for target health, route counts and alerts.
- **/topology** — interactive Cytoscape topology with persistent Route A/B/C identities.
- **/settings** — manage monitored targets.

Click a host name or address on **/nms** to open that host's topology directly.
NMS host rows show a status dot, compact **UP / DOWN / VERIFYING / UNKNOWN / STALE / PAUSED**
label and RTT/loss/jitter/route figures. Availability uses the configured HTTPS check
first, otherwise TCP, and explicitly labelled PING only if both service checks are
disabled. The source is shown beside the label. Enabled HTTPS without a result does
not fall back to a successful TCP check. A TCP UP result confirms a connection to
the configured port, not the website content; enable HTTPS for a verified HTTP response.
Three normal failures confirm DOWN. Unmeasured ping loss shows a dash rather than 0%.
Old results expire to STALE even if live updates stop, and a read-only target snapshot
refreshes every 30 seconds and on focus without initiating probes. Summary counts
use this availability; underlying ICMP history/status remain independent. Detailed
TCP/HTTPS badges appear in topology. The NMS summary still counts confirmed service alerts.
Topology opens in the **TCP** view with **Fit all** framing the full graph.
ICMP, comparison and Readable view remain available; live updates retain manual zoom and pan.

Live broadcasts use a snapshot of browser connections, so opening or closing a page
during an update cannot stop the monitoring loops. Unexpected ICMP/service check
errors are logged with the target ID and retried in the next regular round; other
targets continue to be checked. These internal errors do not count as network failures.
Topology discovery error events include the host name and address.

`GET /healthz` reports whether the ICMP, service and topology worker tasks are
running in its `workers` object. It returns HTTP 503 if a worker is absent or has
stopped, so a responding web server alone no longer passes the Docker health check.
This checks worker task liveness, not target reachability or measurement freshness.

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

Each TCP/HTTPS result now stores separate **DNS**, **TCP**, **TLS** and **HTTP**
phase durations and the failed phase. Later phases remain **NOT-RUN** after an
earlier failure; numeric IP targets show DNS as **SKIPPED**. The recorded DNS
addresses and actual connected IP remain separate. TLS still verifies the original
hostname even though DNS and TCP connection are measured separately. Phase durations
are per step, not cumulative. These details appear in the existing target-details
panel in topology; the compact NMS layout is unchanged.

### Automatic diagnostic evidence

ICMP suspect/down/degraded health or a failed enabled service check schedules an
independent diagnostic round. It captures saved routes and their measurement times,
the latest successful ping/service observations, three extra pings, enabled normal
TCP/HTTPS checks and one short TCP trace (one sample, 0.5-second hop waits).
It does not run extra Voyage/MDA scans. Diagnostics do not increment normal service
failure streaks or route-missing counters and never replace the current topology.

At most two rounds run concurrently, at most 16 targets wait/run, and a persistent
five-minute per-target cooldown limits repeat probing during an ongoing problem.
The round has a 30-second probe deadline; completed evidence is retained if another
probe times out. Edits, pause and deletion cancel in-flight diagnosis; previous
configuration evidence is labelled historical. A restart marks unfinished rounds
interrupted. Subsequent regular ICMP/TCP/HTTPS recoveries are saved per method with
their measurements, not treated as proof that every component has recovered.

The target-details panel shows the latest diagnosis and its reason, evidence summary
and recovery times. **Download diagnosis** saves the frozen before/during/recovery
evidence as JSON. All stored incidents can also be read through
`GET /api/targets/{id}/diagnostics`; one full incident is available through
`GET /api/targets/{id}/diagnostics/{incident_id}`. History follows target deletion.

TCP route comparison requires the same resolved IPv4 address and destination port.
An identical sample can match a saved route; a different sample can reflect multipath
or a route change. A silent hop, partial trace or missing endpoint reply does not
identify a failed router. Connected IPv6 services are labelled unsupported for the
IPv4-only diagnostic trace, rather than compared with a different IPv4 destination.

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
DIAGNOSTICS_ENABLED=true
DIAGNOSTIC_COOLDOWN_SECONDS=300
DIAGNOSTIC_TIMEOUT_SECONDS=30
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

`DIAGNOSTICS_ENABLED=false` disables automatic incident probes. The cooldown and
probe deadline are configurable through the two diagnostic settings above.
Existing history is retained by additive migrations; legacy ping samples without
an address remain stored but are not used as a baseline for the current address.

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
