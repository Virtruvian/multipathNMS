function fmt(value, digits = 1) {
  return value === null || value === undefined ? '—' : Number(value).toFixed(digits);
}

function applyCardClass(card) {
  const missing = Number(card.dataset.routeMissing || 0);
  const degraded = Number(card.dataset.routeDegraded || 0);
  const routeAlert = missing > 0 || degraded > 0;
  card.className = 'target-card status-' + card.dataset.status + (routeAlert ? ' route-alert' : '');
}

function recount() {
  const cards = [...document.querySelectorAll('.target-card')];
  const counts = {healthy: 0, degraded: 0, suspect: 0, down: 0};
  let routeAlerts = 0;

  cards.forEach(card => {
    if (counts[card.dataset.status] !== undefined) counts[card.dataset.status]++;
    if (
      Number(card.dataset.routeMissing || 0) > 0
      || Number(card.dataset.routeDegraded || 0) > 0
    ) routeAlerts++;
  });

  document.getElementById('count-total').textContent = cards.length;
  Object.entries(counts).forEach(([key, value]) => {
    const node = document.getElementById('count-' + key);
    if (node) node.textContent = value;
  });
  document.getElementById('count-route-alerts').textContent = routeAlerts;
}

function updateTarget(target) {
  const card = document.getElementById('target-' + target.id);
  if (!card) return;

  card.dataset.status = target.status;
  card.querySelector('.status-text').textContent = target.status.toUpperCase();
  card.querySelector('.latency').textContent = fmt(target.latency_ms);
  card.querySelector('.loss').textContent = fmt(target.loss_percent);
  card.querySelector('.jitter').textContent = fmt(target.jitter_ms);
  applyCardClass(card);
  recount();
}

function updateTopology(targetId, topology) {
  const card = document.getElementById('target-' + targetId);
  if (!card || !topology || !topology.summary) return;

  const summary = topology.summary;
  card.dataset.routeMissing = String(summary.missing_routes || 0);
  card.dataset.routeDegraded = String(summary.degraded_routes || 0);
  card.querySelector('.routes-active').textContent = String(summary.active_routes || 0);
  card.querySelector('.routes-degraded').textContent = String(summary.degraded_routes || 0);
  card.querySelector('.routes-missing').textContent = String(summary.missing_routes || 0);
  applyCardClass(card);
  recount();
}

window.addEventListener('multipath-live', event => {
  if (event.detail.type === 'target_health') {
    updateTarget(event.detail.target);
  }
  if (event.detail.type === 'topology_update') {
    updateTopology(event.detail.target_id, event.detail.topology);
  }
});

recount();
