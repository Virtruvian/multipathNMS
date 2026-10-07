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
  let serviceAlerts = 0;

  cards.forEach(card => {
    if (counts[card.dataset.status] !== undefined) counts[card.dataset.status]++;
    if (
      Number(card.dataset.routeMissing || 0) > 0
      || Number(card.dataset.routeDegraded || 0) > 0
    ) routeAlerts++;
    if (Number(card.dataset.serviceDown || 0) > 0) serviceAlerts++;
  });

  document.getElementById('count-total').textContent = cards.length;
  Object.entries(counts).forEach(([key, value]) => {
    const node = document.getElementById('count-' + key);
    if (node) node.textContent = value;
  });
  document.getElementById('count-route-alerts').textContent = routeAlerts;
  document.getElementById('count-service-alerts').textContent = serviceAlerts;
}

function updateServices(targetId, checks) {
  const card = document.getElementById('target-' + targetId);
  if (!card) return;
  card.dataset.serviceDown = String((checks || []).filter(check => check.status === 'down').length);
  ServiceHealth.render(card.querySelector('.service-checks'), checks);
  recount();
}

function updateTarget(target) {
  const card = document.getElementById('target-' + target.id);
  if (!card) return;

  card.dataset.status = target.enabled === false ? 'disabled' : target.status;
  card.querySelector('.status-text').textContent = target.enabled === false ? 'PAUSED' : target.status.toUpperCase();
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
  card.querySelector('.routes-pending').textContent = String(summary.pending_routes || 0);
  if (topology.service_checks) updateServices(targetId, topology.service_checks);
  applyCardClass(card);
  recount();
}

window.addEventListener('multipath-live', event => {
  if (event.detail.type === 'service_health') updateServices(event.detail.target_id, event.detail.service_checks);
  if (event.detail.type === 'target_health') {
    updateTarget(event.detail.target);
  }
  if (event.detail.type === 'topology_update') {
    updateTopology(event.detail.target_id, event.detail.topology);
  }
});

const initialChecks = document.getElementById('initial-service-checks');
document.querySelectorAll('time[datetime]').forEach(time => {time.textContent = new Date(time.dateTime).toLocaleTimeString();});
if (initialChecks) Object.entries(JSON.parse(initialChecks.textContent)).forEach(([id, checks]) => updateServices(id, checks));
recount();
