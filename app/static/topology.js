const select = document.getElementById('target-select');
const discoverButton = document.getElementById('discover-btn');
const details = document.getElementById('topology-details');
const routeList = document.getElementById('route-list');
const rawOutput = document.getElementById('voyage-output');

let currentTopology = null;
let selectedRouteId = null;

const cy = typeof cytoscape === 'function' ? cytoscape({
  container: document.getElementById('cy'),
  elements: [],
  layout: {name: 'breadthfirst', directed: true, padding: 45, spacingFactor: 1.25},
  style: [
    {selector: 'node', style: {
      'background-color': '#111f2d',
      'border-color': '#34d399',
      'border-width': 2,
      'label': 'data(label)',
      'color': '#e5e7eb',
      'font-size': 10,
      'text-valign': 'bottom',
      'text-margin-y': 8,
      'text-wrap': 'wrap',
      'text-max-width': 120,
      'width': 34,
      'height': 34
    }},
    {selector: 'node#probe', style: {
      'shape': 'diamond',
      'background-color': '#164e63',
      'border-color': '#5eead4',
      'width': 42,
      'height': 42
    }},
    {selector: 'node[active = "0"]', style: {
      'border-color': '#f87171',
      'background-color': '#3a1d24',
      'opacity': 0.72
    }},
    {selector: 'edge', style: {
      'width': 2,
      'line-color': '#3d5a70',
      'target-arrow-color': '#3d5a70',
      'target-arrow-shape': 'triangle',
      'curve-style': 'bezier'
    }},
    {selector: 'edge[active = "0"]', style: {
      'line-color': '#f87171',
      'target-arrow-color': '#f87171',
      'line-style': 'dashed',
      'opacity': 0.62
    }},
    {selector: '.selected-route', style: {
      'border-color': '#5eead4',
      'line-color': '#5eead4',
      'target-arrow-color': '#5eead4',
      'width': 4,
      'opacity': 1
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
  document.getElementById('topology-replies').textContent = String(summary.probe_replies || 0);
  document.getElementById('topology-last-scan').textContent = localTime(summary.last_scan);
}

function highlightRoute(route) {
  selectedRouteId = route.id;
  if (!cy) return;

  cy.elements().removeClass('selected-route');
  const path = route.node_path || [];
  path.forEach(nodeId => {
    const node = cy.getElementById(nodeId);
    if (node.length) node.addClass('selected-route');
  });

  for (let index = 0; index < path.length - 1; index++) {
    const source = path[index];
    const target = path[index + 1];
    cy.edges().filter(edge => (
      edge.data('source') === source && edge.data('target') === target
    )).addClass('selected-route');
  }
}

function showRouteDetails(route) {
  setDetails(route.label, [
    ['Status', String(route.status || 'unknown').toUpperCase()],
    ['Current RTT', fmt(route.destination_rtt_ms) + ' ms'],
    ['Baseline', fmt(route.baseline_rtt_ms) + ' ms'],
    ['Average RTT', fmt(route.average_rtt_ms) + ' ms'],
    ['Min / Max', fmt(route.minimum_rtt_ms) + ' / ' + fmt(route.maximum_rtt_ms) + ' ms'],
    ['Availability', fmt(route.availability_percent, 2) + '%'],
    ['Hops', String(route.hop_count || 0)],
    ['Flows', String(route.flow_count || 0)],
    ['Last seen', localTime(route.last_seen)]
  ], route.complete ? 'Destination was reached on this path.' : 'Path is incomplete; one or more downstream replies are missing.');

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
    name.textContent = route.label;

    const status = document.createElement('span');
    status.className = 'route-state';
    status.textContent = String(route.status || 'unknown').toUpperCase();
    header.append(name, status);

    const values = document.createElement('div');
    values.className = 'route-card-values';
    values.textContent = 'RTT ' + fmt(route.destination_rtt_ms)
      + ' ms · avg ' + fmt(route.average_rtt_ms)
      + ' ms · ' + (route.hop_count || 0) + ' hops';

    const meta = document.createElement('small');
    meta.textContent = 'availability ' + fmt(route.availability_percent, 2)
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
  renderSummary(data.summary || {});
  renderRoutes(data.routes || []);

  if (cy) {
    cy.elements().remove();

    const elements = [];
    (data.nodes || []).forEach(node => {
      elements.push({
        group: 'nodes',
        data: {
          id: node.id,
          label: node.label,
          active: node.active ? '1' : '0',
          ttl: node.ttl,
          address: node.address,
          rtt_ms: node.rtt_ms,
          average_rtt_ms: node.average_rtt_ms,
          samples: node.samples,
          last_seen: node.last_seen
        }
      });
    });

    (data.edges || []).forEach(edge => {
      elements.push({
        group: 'edges',
        data: {
          id: edge.id,
          source: edge.source,
          target: edge.target,
          active: edge.active ? '1' : '0',
          samples: edge.samples,
          last_seen: edge.last_seen
        }
      });
    });

    cy.add(elements);
    cy.layout({
      name: 'breadthfirst',
      directed: true,
      padding: 45,
      spacingFactor: 1.25,
      roots: '#probe'
    }).run();
  }

  setDetails(data.target.name, [
    ['Address', data.target.address],
    ['Target status', String(data.target.status || 'unknown').toUpperCase()],
    ['Target RTT', fmt(data.target.latency_ms) + ' ms'],
    ['Target loss', fmt(data.target.loss_percent) + '%'],
    ['Target jitter', fmt(data.target.jitter_ms) + ' ms'],
    ['Active routes', String((data.summary || {}).active_routes || 0)]
  ], 'Target health is sampled frequently; topology discovery runs at a slower interval.');
}

async function loadTopology(targetId) {
  if (!targetId) return;
  const response = await fetch('/api/topology/' + targetId);
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail || 'Unable to load topology');
  renderTopology(data);
}

select.addEventListener('change', async () => {
  selectedRouteId = null;
  rawOutput.textContent = '—';
  if (!select.value) return;
  try {
    await loadTopology(select.value);
  } catch (error) {
    setDetails('Topology error', [['Error', String(error)]]);
  }
});

discoverButton.addEventListener('click', async () => {
  if (!select.value) return;

  discoverButton.disabled = true;
  discoverButton.textContent = 'Discovering…';
  rawOutput.textContent = 'Running Voyage / Paris MDA…';

  try {
    const response = await fetch(
      '/api/topology/' + select.value + '/discover',
      {method: 'POST'}
    );
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || 'Discovery failed');

    rawOutput.textContent = data.raw_output || data.stderr || '(no raw output)';
    renderTopology(data);
  } catch (error) {
    rawOutput.textContent = String(error);
  } finally {
    discoverButton.disabled = false;
    discoverButton.textContent = 'Discover now';
  }
});

window.addEventListener('multipath-live', event => {
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
  }
});

if (cy) {
  cy.on('tap', 'node', event => {
    const node = event.target.data();
    setDetails(node.address || node.label, [
      ['TTL', String(node.ttl)],
      ['Current RTT', fmt(node.rtt_ms) + ' ms'],
      ['Average RTT', fmt(node.average_rtt_ms) + ' ms'],
      ['Samples', String(node.samples || 0)],
      ['State', node.active === '1' ? 'CURRENT' : 'RECENT / MISSING'],
      ['Last seen', localTime(node.last_seen)]
    ]);
  });

  cy.on('tap', 'edge', event => {
    const edge = event.target.data();
    setDetails('Link', [
      ['From', edge.source],
      ['To', edge.target],
      ['State', edge.active === '1' ? 'CURRENT' : 'RECENT / MISSING'],
      ['Samples', String(edge.samples || 0)],
      ['Last seen', localTime(edge.last_seen)]
    ]);
  });
} else {
  setDetails('Cytoscape unavailable', [
    ['Status', 'The graph library could not be loaded.']
  ], 'Check browser access to the Cytoscape CDN.');
}

if (select.options.length > 1) {
  select.selectedIndex = 1;
  select.dispatchEvent(new Event('change'));
}
