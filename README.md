# multipathNMS

A lightweight live route and multipath NMS built around Paris-style MDA discovery.

## Views

- **/nms** — compact operational dashboard for target health, route counts and alerts.
- **/topology** — interactive Cytoscape topology with persistent Route A/B/C identities.
- **/settings** — manage monitored targets.

The topology view uses router symbols and horizontal hop columns: source on the left,
target on the right, with parallel paths branching and merging between them. Each
router shows its IP, TTL and measured RTT from the probe. Links show route membership;
dotted segments explicitly indicate unobserved hops, rather than a measured direct link.
Select a route to highlight it, drag to pan, and use **Fit all** or **Readable view**
to navigate large topologies. Live updates retain the viewport and route selection.

## Stack

Docker → FastAPI → Voyage / Paris MDA → WebSocket → Cytoscape.js → SQLite

## How monitoring works

Two measurement loops intentionally run at different speeds:

- **Health loop** (default every 2 seconds): target RTT, loss, jitter and healthy/degraded/suspect/down.
- **Topology loop** (default every 60 seconds): Voyage/Paris MDA discovers ECMP/multipath topology.

A route that was previously discovered but is absent from the newest MDA result is shown as **missing** and kept visible for a configurable recent-history window. Missing does not automatically prove that an intermediate router failed; it means that path is no longer being observed in the current topology.

Per route the UI keeps:

- stable Route A/B/C identity
- current destination RTT
- RTT minimum / average / maximum
- learned RTT baseline
- route-presence availability
- hop count
- number of MDA flows represented by the route
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

The container uses `NET_RAW` for ICMP probing and Voyage packet probing. Voyage is pinned to a known source commit in the Dockerfile so builds are reproducible.

## Configuration

Useful Compose environment variables:

```text
HEALTH_INTERVAL_SECONDS=2
TOPOLOGY_INTERVAL_SECONDS=60
TOPOLOGY_STALE_MINUTES=15
VOYAGE_TIMEOUT_SECONDS=90
VOYAGE_PROBING_RATE=50
VOYAGE_CONFIDENCE=99
SUSPECT_AFTER_FAILURES=3
DOWN_AFTER_FAILURES=5
DEGRADED_LOSS_PERCENT=10
DEGRADED_LATENCY_MULTIPLIER=2
```

Avoid setting topology discovery to very short intervals: MDA intentionally sends substantially more probes than a normal ping.

## Tests

The Voyage parser and SQLite topology persistence have deterministic fixture tests:

```bash
python -m unittest discover -s tests -v
```

Graph preparation tests use Node.js 22 without additional npm dependencies:

```bash
node --test tests/test_network_graph.js
```

## Security

Targets are validated as IP addresses or DNS hostnames and are passed to subprocesses as argument arrays, never through a shell. The current application is intended for trusted/internal deployment; add authentication before exposing it publicly.

See `AGENTS.md` for architecture and development rules.
