const select = document.getElementById('target-select');
const discoverButton = document.getElementById('discover-btn');
const details = document.getElementById('topology-details');
const routeList = document.getElementById('route-list');
const rawOutput = document.getElementById('voyage-output');
const methodSelect = document.getElementById('trace-method');
const tcpPortInput = document.getElementById('tcp-port');
const checkServicesButton = document.getElementById('check-services-btn');
let tcpPortDirty = false;

let currentTopology = null;
let selectedRouteId = null;
let currentGraph = {elements: [], routePaths: {}};
let graphTargetId = null;
let detailSelection = {kind: 'target', id: null};
let showLooseReplies = false;
let showHistory = false;
let showRouterSymbols = false;

const cy = typeof cytoscape === 'function' ? cytoscape({
  container: document.getElementById('cy'),
  elements: [],
  minZoom: 0.08,
  maxZoom: 2,
  wheelSensitivity: 0.2,
  layout: {name: 'preset'},
  style: [
    {selector: 'node', style: {
      'background-fit': 'contain',
      'background-opacity': 0,
      'border-width': 0,
      'label': 'data(label)',
      'color': '#e5e7eb',
      'font-size': 14,
      'font-family': 'ui-monospace, SFMono-Regular, Menlo, monospace',
      'text-valign': 'bottom',
      'text-margin-y': 8,
      'text-wrap': 'wrap',
      'text-max-width': 190,
      'text-background-color': '#0b1621',
      'text-background-opacity': 0.92,
      'text-background-padding': 4,
      'width': 74,
      'height': 60
    }},
    {selector: 'node[role = "source"], node[role = "destination"]', style: {
      'border-color': '#5eead4', 'border-width': 2, 'border-style': 'dashed',
      'color': '#99f6e4', 'font-weight': 'bold'
    }},
    {selector: 'node[active = "0"]', style: {
      'border-color': '#f87171',
      'border-width': 2,
      'opacity': 0.65,
      'color': '#fca5a5'
    }},
    {selector: 'edge', style: {
      'width': 2,
      'line-color': '#50c889',
      'target-arrow-color': '#50c889',
      'target-arrow-shape': 'triangle',
      'curve-style': 'unbundled-bezier',
      'control-point-distances': 'data(curve_distance)',
      'control-point-weights': 0.5,
      'label': 'data(label)',
      'font-size': 12,
      'color': '#bad5e8',
      'text-rotation': 'autorotate',
      'text-margin-y': -10,
      'text-wrap': 'wrap',
      'text-max-width': 165,
      'text-background-color': '#0b1621',
      'text-background-opacity': 0.95,
      'text-background-padding': 3
    }},
    {selector: 'edge[kind = "gap"]', style: {
      'line-style': 'dotted', 'line-color': '#94a3b8', 'target-arrow-color': '#94a3b8',
      'color': '#94a3b8'
    }},
    {selector: 'edge[protocol = "tcp"][kind != "gap"]', style: {
      'line-color': '#60a5fa', 'target-arrow-color': '#60a5fa', 'line-style': 'dashed'
    }},
    {selector: 'edge[kind = "diagnostic"]', style: {
      'line-style': 'dotted', 'line-color': '#64748b', 'target-arrow-color': '#64748b'
    }},
    {selector: 'edge[status = "degraded"]', style: {
      'line-color': '#fbbf24', 'target-arrow-color': '#fbbf24', 'color': '#fbbf24'
    }},
    {selector: 'edge[status = "missing"]', style: {
      'line-color': '#f87171',
      'target-arrow-color': '#f87171',
      'line-style': 'dashed',
      'color': '#fca5a5',
      'opacity': 0.65
    }},
    {selector: 'node.selected-route', style: {
      'underlay-color': '#5eead4', 'underlay-opacity': 0.15, 'underlay-padding': 8
    }},
    {selector: 'edge.selected-route', style: {'width': 3.5, 'opacity': 1}},
    {selector: '.route-dimmed', style: {'opacity': 0.18}},
    {selector: 'node.router-node', style: {'background-image': '/static/router.svg'}},
    {selector: 'node.path-node', style: {
      'background-image': 'none', 'background-color': '#0b1621', 'background-opacity': 1,
      'border-width': 3, 'border-color': node => node.data('active') === '0' ? '#f87171' : node.data('protocol') === 'tcp' ? '#60a5fa' : '#50c889',
      'border-style': 'solid', 'width': 30, 'height': 30, 'shape': 'ellipse',
      'text-max-width': 150, 'font-size': 13
    }},
    {selector: 'node.path-node[role = "source"], node.path-node[role = "destination"]', style: {
      'shape': 'round-rectangle', 'width': 34, 'height': 30
    }},
    {selector: 'node[status = "pending"], node[status = "different-target"]', style: {
      'border-color': '#94a3b8', 'color': '#94a3b8'
    }},
    {selector: 'edge[status = "pending"], edge[status = "different-target"]', style: {
      'line-color': '#94a3b8', 'target-arrow-color': '#94a3b8', 'line-style': 'dashed', 'color': '#94a3b8'
    }}
  ]
}) : null;

function fmt(value, digits = 1) {
  return value === null || value === undefined ? '—' : Number(value).toFixed(digits);
}

function localTime(value) {
  if (!value) return '—';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

function clearElement(element) {
  while (element.firstChild) element.removeChild(element.firstChild);
}

function setDetails(title, rows, note) {
  clearElement(details);

  const heading = document.createElement('h3');
  heading.textContent = title;
  details.appendChild(heading);

  const grid = document.createElement('div');
  grid.className = 'kv';
  rows.forEach(row => {
    const label = document.createElement('span');
    label.textContent = row[0];
    const value = document.createElement('b');
    value.textContent = row[1];
    grid.append(label, value);
  });
  details.appendChild(grid);

  if (note) {
    const paragraph = document.createElement('p');
    paragraph.className = 'detail-note';
    paragraph.textContent = note;
    details.appendChild(paragraph);
  }
}

function renderSummary(summary) {
  document.getElementById('topology-active').textContent = String(summary.active_routes || 0);
  document.getElementById('topology-degraded').textContent = String(summary.degraded_routes || 0);
  document.getElementById('topology-missing').textContent = String(summary.missing_routes || 0);
  document.getElementById('topology-pending').textContent = String(summary.pending_routes || 0);
  document.getElementById('topology-replies').textContent = String(summary.probe_replies || 0);
  document.getElementById('topology-last-scan').textContent = localTime(summary.last_scan);
}

function methodName(route) {
  return route.protocol === 'tcp' ? 'TCP:' + route.destination_port : 'ICMP';
}

function renderServiceChecks(data) {
  ServiceHealth.render(document.getElementById('service-checks'), [{method: 'icmp',
    status: data.target.enabled === false ? 'disabled' : data.target.status || 'unknown', latency_ms: data.target.latency_ms,
    last_checked: data.target.updated_at, enabled: data.target.enabled !== false}, ...(data.service_checks || [])]);
}

checkServicesButton.addEventListener('click', async () => {
  const targetId = select.value;
  if (!targetId) return;
  checkServicesButton.disabled = true;
  checkServicesButton.textContent = 'Checking…';
  try {
    const response = await fetch('/api/targets/' + targetId + '/check-services', {method: 'POST'});
    const target = await response.json();
    if (!response.ok) throw new Error(target.detail || 'Service check failed');
    if (currentTopology && select.value === targetId) {
      currentTopology.service_checks = target.service_checks;
      currentTopology.target = {...currentTopology.target, ...target};
      renderServiceChecks(currentTopology);
    }
  } catch (error) {
    if (select.value === targetId) rawOutput.textContent = String(error);
  } finally {
    checkServicesButton.disabled = false;
    checkServicesButton.textContent = 'Check services now';
  }
});

function renderMeasurements(data) {
  const panel = document.getElementById('measurement-comparison');
  clearElement(panel);
  (data.measurements || []).forEach(measurement => {
    const card = document.createElement('div');
    card.className = 'measurement-card ' + measurement.protocol;
    const title = document.createElement('strong');
    title.textContent = methodName(measurement);
    const result = document.createElement('span');
    const status = measurement.error ? 'SCAN FAILED'
      : !measurement.last_scan ? 'NOT MEASURED'
      : measurement.complete_routes ? 'TARGET REPLIED'
      : measurement.active_routes ? 'PARTIAL PATH' : 'NO PATH REPLIES';
    result.textContent = status + ' · ' + measurement.active_routes + ' current paths · '
      + measurement.probe_replies + ' replies';
    const timestamp = document.createElement('small');
    timestamp.textContent = 'Last successful scan: ' + localTime(measurement.last_scan);
    card.append(title, result, timestamp);
    if (measurement.error) {
      const error = document.createElement('small');
      error.className = 'measurement-error';
      error.textContent = 'Attempt ' + localTime(measurement.last_attempt) + ': '
        + measurement.error + '. Previously measured paths are retained.';
      card.appendChild(error);
    }
    panel.appendChild(card);
  });
}

function highlightRoute(route) {
  selectedRouteId = route.id;
  if (!cy) return;

  cy.elements().removeClass('selected-route').addClass('route-dimmed');
  const path = currentGraph.routePaths[route.id] || {nodes: [], edges: []};
  [...path.nodes, ...path.edges].forEach(id => {
    cy.getElementById(id).removeClass('route-dimmed').addClass('selected-route');
  });
}

function fitAll() {
  if (cy) cy.fit(cy.elements(), 65);
}

function readableView() {
  if (!cy || !cy.nodes().length) return;
  cy.fit(cy.elements(), 65);
  if (cy.zoom() < 0.9) {
    cy.zoom(1);
    cy.pan({x: 110, y: cy.height() / 2});
  } else if (cy.zoom() > 1.1) {
    cy.zoom(1.1);
    cy.center();
  }
}

function showTargetDetails() {
  if (!currentTopology) return;
  detailSelection = {kind: 'target', id: null};
  const data = currentTopology;
  setDetails(data.target.name, [
    ['Address', data.target.address],
    ['ICMP health', String(data.target.status || 'unknown').toUpperCase()],
    ['ICMP ping RTT', fmt(data.target.latency_ms) + ' ms'],
    ['ICMP ping loss', fmt(data.target.loss_percent) + '%'],
    ['ICMP ping jitter', fmt(data.target.jitter_ms) + ' ms'],
    ['Active routes in view', String((currentGraph.visibleRoutes || []).filter(route => route.active).length)]
  ], 'Node RTT is measured from this probe, not between routers. Dotted segments show unobserved hops.');
}

document.getElementById('graph-fit').addEventListener('click', fitAll);
document.getElementById('graph-readable').addEventListener('click', readableView);
document.getElementById('graph-loose').addEventListener('click', () => {
  showLooseReplies = !showLooseReplies;
  if (currentTopology) renderTopology(currentTopology);
  fitAll();
});
document.getElementById('graph-history').addEventListener('click', () => {
  showHistory = !showHistory;
  if (currentTopology) renderTopology(currentTopology);
  fitAll();
});
document.getElementById('graph-mode').addEventListener('click', () => {
  showRouterSymbols = !showRouterSymbols;
  if (cy) cy.nodes().toggleClass('path-node', !showRouterSymbols).toggleClass('router-node', showRouterSymbols);
  document.getElementById('graph-mode').textContent = showRouterSymbols ? 'Path symbols' : 'Router symbols';
  document.getElementById('graph-mode').setAttribute('aria-pressed', String(showRouterSymbols));
});
const graphPanel = document.querySelector('.topology-panel');
const expandButton = document.getElementById('graph-expand');
let expandedViewport = null;
function setGraphExpanded(expanded) {
  if (expanded && cy) expandedViewport = {zoom: cy.zoom(), pan: {...cy.pan()}, targetId: graphTargetId};
  graphPanel.classList.toggle('graph-expanded', expanded);
  document.body.classList.toggle('graph-expanded-active', expanded);
  expandButton.textContent = expanded ? 'Restore' : 'Expand';
  expandButton.setAttribute('aria-expanded', String(expanded));
  requestAnimationFrame(() => {
    if (!cy) return;
    cy.resize();
    if (!expanded && expandedViewport && expandedViewport.targetId === graphTargetId) {
      cy.zoom(expandedViewport.zoom);
      cy.pan(expandedViewport.pan);
    } else fitAll();
  });
}
expandButton.addEventListener('click', () => setGraphExpanded(!graphPanel.classList.contains('graph-expanded')));
document.addEventListener('keydown', event => {
  if (event.key === 'Escape' && graphPanel.classList.contains('graph-expanded')) {
    setGraphExpanded(false);
    expandButton.focus();
  }
});
document.getElementById('graph-all').addEventListener('click', () => {
  selectedRouteId = null;
  if (cy) cy.elements().removeClass('selected-route route-dimmed');
  if (currentTopology) renderRoutes(currentGraph.visibleRoutes || []);
  showTargetDetails();
});
function zoomGraph(multiplier) {
  if (cy) cy.zoom({level: cy.zoom() * multiplier, renderedPosition: {x: cy.width() / 2, y: cy.height() / 2}});
}
document.getElementById('graph-zoom-in').addEventListener('click', () => zoomGraph(1.25));
document.getElementById('graph-zoom-out').addEventListener('click', () => zoomGraph(0.8));

function showRouteDetails(route) {
  detailSelection = {kind: 'route', id: route.id};
  setDetails(route.label, [
    ['Method', methodName(route)],
    ['Endpoint reply', route.endpoint_response || (route.complete ? 'ICMP reply' : 'No target reply')],
    ['Status', String(route.status || 'unknown').toUpperCase()],
    ['Measured destination', route.resolved_ip || '—'],
    ['Absent scans', String(route.consecutive_misses || 0) + ' / ' + (route.missing_confirmation_threshold || 3)],
    [route.active ? 'Current RTT' : 'Last observed RTT', fmt(route.destination_rtt_ms) + ' ms'],
    ['Baseline', fmt(route.baseline_rtt_ms) + ' ms'],
    ['Average RTT', fmt(route.average_rtt_ms) + ' ms'],
    ['Min / Max', fmt(route.minimum_rtt_ms) + ' / ' + fmt(route.maximum_rtt_ms) + ' ms'],
    ['Observed scans', fmt(route.availability_percent, 2) + '%'],
    ['Hops', String(route.hop_count || 0)],
    [route.protocol === 'tcp' ? 'Trace samples' : 'Flows', String(route.flow_count || 0)],
    ['Last seen', localTime(route.last_seen)]
  ], !route.active
    ? route.status === 'different-target' ? 'Saved observation for a previous destination IP. It does not indicate failure of the current destination.'
      : 'This path was not observed in ' + (route.consecutive_misses || 0) + ' completed scans. RTT and endpoint replies belong to the last saved observation; absence does not establish an outage.'
    : route.complete
    ? route.endpoint_response === 'reset' ? 'A TCP reset reached the probe. The endpoint replied; this does not establish an open service.' : 'The target replied on this measured path.'
    : 'Path is incomplete. Silent or filtered hops do not establish a router outage.');

  const hopTitle = document.createElement('h4');
  hopTitle.textContent = 'Hops';
  details.appendChild(hopTitle);

  const list = document.createElement('ol');
  list.className = 'hop-list';
  (route.hops || []).forEach(hop => {
    const item = document.createElement('li');
    const address = hop.address || '*';
    item.textContent = 'TTL ' + hop.ttl + ' · ' + address + ' · ' + fmt(hop.rtt_ms) + ' ms';
    list.appendChild(item);
  });
  details.appendChild(list);
}

function renderRoutes(routes) {
  clearElement(routeList);
  if (!routes || routes.length === 0) {
    const empty = document.createElement('div');
    empty.className = 'empty';
    empty.textContent = 'No route discovery data yet.';
    routeList.appendChild(empty);
    return;
  }

  routes.forEach(route => {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'route-card route-' + route.status;
    button.dataset.routeId = String(route.id);

    const header = document.createElement('div');
    header.className = 'route-card-header';

    const name = document.createElement('strong');
    name.textContent = (route.protocol === 'tcp' ? '' : 'ICMP · ') + route.label;

    const status = document.createElement('span');
    status.className = 'route-state';
    status.textContent = route.status === 'pending' ? 'NOT OBSERVED · ' + route.consecutive_misses + '/' + route.missing_confirmation_threshold
      : route.status === 'missing' ? 'CONFIRMED ABSENT' : route.status === 'different-target' ? 'PREVIOUS TARGET IP' : String(route.status || 'unknown').toUpperCase();
    header.append(name, status);

    const values = document.createElement('div');
    values.className = 'route-card-values';
    values.textContent = (route.active ? 'RTT ' : 'Last RTT ') + fmt(route.destination_rtt_ms)
      + ' ms · avg ' + fmt(route.average_rtt_ms)
      + ' ms · ' + (route.hop_count || 0) + ' hops · ' + (route.complete ? route.active ? 'target reached' : 'target replied in saved scan' : 'partial path');

    const meta = document.createElement('small');
    meta.textContent = 'observed scans ' + fmt(route.availability_percent, 2)
      + '% · last seen ' + localTime(route.last_seen);

    button.append(header, values, meta);
    button.addEventListener('click', () => {
      document.querySelectorAll('.route-card').forEach(card => card.classList.remove('selected'));
      button.classList.add('selected');
      highlightRoute(route);
      showRouteDetails(route);
    });

    routeList.appendChild(button);
  });

  if (selectedRouteId !== null) {
    const selected = routes.find(route => route.id === selectedRouteId);
    if (selected) {
      const button = routeList.querySelector('[data-route-id="' + selected.id + '"]');
      if (button) button.classList.add('selected');
      highlightRoute(selected);
    } else {
      selectedRouteId = null;
    }
  }
}

function renderTopology(data) {
  currentTopology = data;
  if (!tcpPortDirty) tcpPortInput.value = data.target.tcp_port || 443;
  renderMeasurements(data);
  renderServiceChecks(data);
  const visibleRoutes = (data.routes || []).filter(route => methodSelect.value === 'all' || (route.protocol || 'icmp') === methodSelect.value);
  const measurements = (data.measurements || []).filter(item => methodSelect.value === 'all' || item.protocol === methodSelect.value);
  renderSummary({...data.summary,
    active_routes: visibleRoutes.filter(route => route.active).length,
    degraded_routes: visibleRoutes.filter(route => route.status === 'degraded').length,
    missing_routes: visibleRoutes.filter(route => route.status === 'missing').length,
    pending_routes: visibleRoutes.filter(route => route.status === 'pending').length,
    ...(data.measurements ? {
      probe_replies: measurements.reduce((total, item) => total + item.probe_replies, 0),
      last_scan: measurements.map(item => item.last_scan).filter(Boolean).sort().at(-1)
    } : {})
  });
  const hadHops = cy && cy.nodes().length > 1;
  currentGraph = MultipathGraph.build(data, {showLooseReplies, showHistory, protocol: methodSelect.value});
  const looseButton = document.getElementById('graph-loose');
  looseButton.textContent = showLooseReplies ? 'Hide loose replies' : 'Show loose replies';
  looseButton.setAttribute('aria-pressed', String(showLooseReplies));
  const historyButton = document.getElementById('graph-history');
  historyButton.textContent = showHistory ? 'Hide recent history' : 'Show recent history';
  historyButton.setAttribute('aria-pressed', String(showHistory));
  document.getElementById('graph-filter-info').textContent =
    (methodSelect.value === 'all' ? 'ICMP + TCP' : methodSelect.value.toUpperCase())
    + ' · ' + (showHistory ? 'Current + recent paths' : 'Latest measured paths')
    + ' · ' + (showHistory ? visibleRoutes.filter(route => !route.active).length : currentGraph.hiddenMissingCount)
    + (showHistory ? ' historical paths shown' : ' historical paths retained')
    + ' · ' + currentGraph.looseReplyCount + (showLooseReplies ? ' replies outside visible paths' : ' loose replies hidden')
    + (currentGraph.excludedRouteCount ? ' · ' + currentGraph.excludedRouteCount + ' paths outside probe range excluded' : '');

  if (cy) {
    cy.batch(() => {
      cy.elements().remove();
      cy.add(currentGraph.elements);
      cy.nodes().toggleClass('path-node', !showRouterSymbols).toggleClass('router-node', showRouterSymbols);
    });
    if (graphTargetId !== data.target.id || (!hadHops && cy.nodes().length > 1)) fitAll();
    graphTargetId = data.target.id;
  }

  renderRoutes(currentGraph.visibleRoutes || []);
  const selected = (data.routes || []).find(route => route.id === selectedRouteId);
  const detailElement = cy && cy.getElementById(detailSelection.id || '');
  if (detailSelection.kind === 'node' && detailElement && detailElement.length) showNodeDetails(detailElement.data());
  else if (detailSelection.kind === 'edge' && detailElement && detailElement.length) showEdgeDetails(detailElement.data());
  else if (selected) showRouteDetails(selected);
  else showTargetDetails();
  const empty = document.getElementById('graph-empty');
  empty.hidden = currentGraph.elements.some(item => item.group === 'nodes' && item.data.id !== 'probe');
  empty.textContent = currentGraph.hiddenMissingCount
    ? 'No current paths. Use Show recent history to inspect previous observations.'
    : 'Waiting for topology discovery. Use Discover now to find the first paths.';
}

async function loadTopology(targetId) {
  if (!targetId) return;
  const response = await fetch('/api/topology/' + targetId);
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail || 'Unable to load topology');
  if (String(targetId) === select.value) renderTopology(data);
}

select.addEventListener('change', async () => {
  selectedRouteId = null;
  detailSelection = {kind: 'target', id: null};
  rawOutput.textContent = '—';
  tcpPortDirty = false;
  if (!select.value) return;
  try {
    await loadTopology(select.value);
  } catch (error) {
    setDetails('Topology error', [['Error', String(error)]]);
  }
});

tcpPortInput.addEventListener('input', () => { tcpPortDirty = true; });
methodSelect.addEventListener('change', () => {
  selectedRouteId = null;
  detailSelection = {kind: 'target', id: null};
  tcpPortInput.disabled = discoverButton.disabled || methodSelect.value === 'icmp';
  if (currentTopology) renderTopology(currentTopology);
  fitAll();
});

discoverButton.addEventListener('click', async () => {
  if (!select.value) return;
  if (methodSelect.value !== 'icmp' && !tcpPortInput.reportValidity()) return;
  const scanTargetId = select.value;
  const scanMethod = methodSelect.value;
  const requestedPort = Number(tcpPortInput.value);

  discoverButton.disabled = true;
  tcpPortInput.disabled = true;
  discoverButton.textContent = 'Discovering…';
  rawOutput.textContent = 'Measuring ' + (methodSelect.value === 'all' ? 'ICMP + TCP' : methodSelect.value.toUpperCase()) + ' paths…';

  try {
    if (scanMethod !== 'icmp' && requestedPort !== (currentTopology?.target.tcp_port || 443)) {
      const saved = await fetch('/api/targets/' + scanTargetId, {
        method: 'PATCH', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({tcp_port: requestedPort})
      });
      const result = await saved.json();
      if (!saved.ok) throw new Error(result.detail || 'Unable to save TCP port');
    }
    tcpPortDirty = false;
    const response = await fetch(
      '/api/topology/' + scanTargetId + '/discover?protocol=' + scanMethod,
      {method: 'POST'}
    );
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || 'Discovery failed');

    if (select.value === scanTargetId) {
      rawOutput.textContent = data.raw_output || data.stderr || '(no raw output)';
      renderTopology(data);
    }
  } catch (error) {
    if (select.value === scanTargetId) rawOutput.textContent = String(error);
  } finally {
    discoverButton.disabled = false;
    tcpPortInput.disabled = methodSelect.value === 'icmp';
    discoverButton.textContent = 'Discover now';
  }
});

window.addEventListener('multipath-live', event => {
  if (event.detail.type === 'service_health' && currentTopology && String(event.detail.target_id) === select.value) {
    currentTopology.service_checks = event.detail.service_checks;
    renderServiceChecks(currentTopology);
  }
  if (
    event.detail.type === 'topology_update'
    && String(event.detail.target_id) === select.value
  ) {
    renderTopology(event.detail.topology);
  }

  if (
    event.detail.type === 'target_health'
    && currentTopology
    && String(event.detail.target.id) === select.value
  ) {
    currentTopology.target = Object.assign({}, currentTopology.target, event.detail.target);
    renderServiceChecks(currentTopology);
    if (detailSelection.kind === 'target') showTargetDetails();
  }
});

function showNodeDetails(node) {
    detailSelection = {kind: 'node', id: node.id};
    setDetails(node.address || node.label, [
      ['Methods', (node.methods || []).join(' / ') || '—'],
      ['TTL', String(node.ttl)],
      ['Observed TTLs', (node.observed_ttls || [node.ttl]).join(' / ')],
      ['Current RTT', fmt(node.rtt_ms) + ' ms'],
      ['Average RTT', fmt(node.average_rtt_ms) + ' ms'],
      ['Samples', String(node.samples || 0)],
      ['State', node.active === '1' ? 'CURRENT' : 'RECENT / MISSING'],
      ['Last seen', localTime(node.last_seen)]
    ]);
}

function showEdgeDetails(edge) {
    detailSelection = {kind: 'edge', id: edge.id};
    setDetails(edge.protocol === 'tcp' ? 'TCP hop sequence' : 'Link', [
      ['From', cy.getElementById(edge.source).data('address') || 'Local probe'],
      ['To', cy.getElementById(edge.target).data('address') || edge.target],
      ['Routes', (currentTopology.routes || []).filter(route => edge.route_ids.includes(route.id)).map(route => route.label).join(' / ') || '—'],
      ['Observation', edge.kind === 'gap' ? edge.gap_hops + ' unobserved hops' : edge.kind === 'source' ? 'Probe origin' : edge.protocol === 'tcp' ? 'Hop order within one trace' : 'Observed adjacency'],
      ['State', edge.active === '1' ? 'CURRENT' : 'RECENT / MISSING'],
      ['Samples', edge.samples === null ? '—' : String(edge.samples || 0)],
      ['Last seen', localTime(edge.last_seen)]
    ], edge.protocol === 'tcp'
      ? 'TCP segments show reply order within one trace. Source ports may vary between hops, so a single flow or direct router link is not confirmed. Gaps remain unobserved. RTT belongs to each responding node.'
      : edge.kind === 'gap' ? 'This dotted segment bridges missing replies in the same observed flow. It is not a measured direct link or proof of an outage.' : 'Per-link latency and packet loss are not measured. RTT labels belong to the responding nodes.');
}

if (cy) {
  cy.on('tap', 'node', event => showNodeDetails(event.target.data()));
  cy.on('tap', 'edge', event => showEdgeDetails(event.target.data()));
} else {
  setDetails('Cytoscape unavailable', [
    ['Status', 'The graph library could not be loaded.']
  ], 'Check browser access to the Cytoscape CDN.');
}

if (select.options.length > 1) {
  const requestedTarget = new URLSearchParams(window.location.search).get('target');
  const matchingOption = [...select.options].find(option => option.value && option.value === requestedTarget);
  select.value = matchingOption ? matchingOption.value : select.options[1].value;
  select.dispatchEvent(new Event('change'));
}
