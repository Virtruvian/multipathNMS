# AGENTS.md — multipathNMS

This file is the source of truth for development in this repository.

## Product goal

multipathNMS is a lightweight Docker-based route monitoring application with two views over the same data:

1. `/nms` — compact NMS dashboard for operational monitoring, alarms and route health.
2. `/topology` — visual live topology view for route/multipath analysis.

The primary purpose is to detect route changes, degradation and outages with useful measurements such as latency, packet loss, jitter, last seen and availability.

## Architecture

- Docker / Docker Compose
- Python 3.12
- FastAPI backend
- Voyage / Paris MDA topology discovery behind an adapter
- WebSocket live updates
- Cytoscape.js topology rendering
- SQLite history for v1

Keep measurement engines behind interfaces so Voyage can later be supplemented or replaced by Scamper, MTR or another probe engine without redesigning the UI or database.

## Monitoring principles

- Fast health checks and slower topology discovery are separate jobs.
- A non-responsive intermediate hop does not automatically mean a route is down.
- A previously discovered path that is absent from the newest MDA scan is `missing`, not automatically proven failed.
- Route status and target reachability are separate concepts.
- Avoid excessive probing. Defaults should be conservative and configurable.
- Preserve historical events when routes appear, disappear, degrade or recover.
- Keep stable route identities based on their ordered TTL/IP signature.

Target health status model:

- `healthy`
- `degraded`
- `suspect`
- `down`

Topology route status model:

- `active`
- `degraded`
- `missing`
- `pending` (not observed, awaiting confirmation)
- `different-target` (history for a previous resolved DNS address)

Service checks have independent `unknown`, `healthy`, `pending`, `down`, `disabled`
and display-only `stale` states. Never replace ICMP target health with service status.

## Security

- Never execute user input through a shell.
- Use argument arrays with `asyncio.create_subprocess_exec`.
- Validate targets before probing.
- The application is intended for trusted/internal deployment unless authentication and access controls are explicitly added.
- Docker capabilities should be limited to what the probe engine needs.
- Pin external measurement-engine source revisions used in Docker builds.

## Code conventions

- Keep API, monitoring logic, persistence and frontend concerns separated.
- Type annotate Python code.
- Configuration belongs in environment variables/settings.
- Keep v1 dependencies small.
- Do not silently swallow probe errors.
- Do not call a missing intermediate hop a route outage without downstream evidence.
- Treat this AGENTS.md as authoritative for future changes.

## Branching

- `main` is stable.
- Development happens in feature branches.
- Changes go through pull requests.

## Implemented v1 foundation

- FastAPI application in Docker
- `/nms`, `/topology`, `/settings`
- target CRUD
- fast ICMP target-health monitoring
- SQLite samples/events/history
- WebSocket live updates
- Voyage flat-output parser
- stable multipath route IDs
- persistent nodes, links, routes and hops
- route discovery/recovery/missing events
- route RTT baseline and min/avg/max values
- Cytoscape live graph
- recent missing paths remain available through the history overlay with visible missing counts
- parser unit tests
- SQLite topology persistence regression tests
- horizontal TTL-based graph with router symbols and route highlighting
- graph gaps explicitly distinguish unobserved segments from measured links
- graph viewport and selected route survive live updates
- dependency-free Node.js graph tests
- default graph filters replies/links without reconstructed path membership
- diagnostic toggle retains access to loose replies; partial/missing paths stay visible
- patched pinned Voyage single-target mode varies source ports instead of destination IPs
- parser clips each flow and its nodes at the first destination reply
- receiver correlates replies with sent probes and validates supported checksums
- random nonzero Voyage instance ID per scan and configured TTL range enforcement
- multiple interfaces in one flow/TTL remain ambiguous; never majority-derived links
- straight primary path, branch lanes, shared target marker and optional router icons
- default current-path view with explicit recent-history overlay; no stored history is deleted
- separate TCP SYN adapter with bounded sampled traces and per-target destination port
- automatic TCP source ports by default; fixed source ports remain an explicit diagnostic option
- TCP lines show sampled hop order, not confirmed same-flow adjacency; separate trace runs never fill each other's gaps
- ICMP/TCP comparison with isolated method/port route IDs, RTTs, missing counts and engine errors
- TCP graph nodes/adjacencies derive only from that scope's persisted RouteHop records
- additive SQLite scope migration preserving all existing ICMP IDs and history
- TCP SYN/ACK vs reset vs ICMP error distinction; target health remains ICMP-labelled
- Docker CI executes the installed TCP engine against an open/closed loopback port
- independent normal TCP-connect and opt-in verified HTTPS checks, with per-target settings
- bounded service probes and three-failure confirmation with durable samples and state-change events
- per-target revision guards discard service results raced with edits, pause/resume or deletion
- three completed same-destination scans confirm absence; engine errors and DNS rotations do not count as disappeared routes
- additive schema upgrades retain samples, route IDs, hashes and historical events
- API timestamps explicitly carry UTC because SQLite omits timezone metadata
- service checks and route confirmation are exposed in both NMS and topology views
- separate DNS/TCP/TLS/HTTP timings and failure phases, preserving TLS hostname verification
- bounded automatic incident evidence on ICMP/service problems; extra probes never increment normal counters
- frozen saved routes and successful observations, independent ping/service/TCP samples and per-method recovery evidence
- persistent per-target cooldown, global concurrency/queue bounds and cancellation on edit/pause/delete
- diagnostic summaries and JSON downloads in existing topology details; compact NMS unchanged
- browser connection changes during broadcasts cannot interrupt monitor tasks
- unexpected ICMP/service round errors are logged and retried without counting as network failures
- health endpoint checks background task liveness; topology failure events identify the host
- compact NMS availability uses HTTPS, then TCP, then explicit PING fallback; stored ICMP status remains independent
- UP/DOWN confirmation uses normal checks; unmeasured/stale/paused states remain explicit, with read-only snapshot refresh

## Next implementation step

Add reference-target correlation, multiple measurement locations and time-series charts for RTT/loss and route-change history. Keep probes conservative and do not infer failure solely from an ICMP-silent intermediate hop.
