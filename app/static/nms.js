function fmt(value, digits = 1) {
  return value === null || value === undefined ? '—' : Number(value).toFixed(digits);
}

const availabilityByTarget = new Map();

function updateAvailability(targetId, availability) {
  const card = document.getElementById('target-' + targetId);
  if (!card || !availability) return;
  availabilityByTarget.set(String(targetId), availability);
  const expired = availability.valid_until && Date.parse(availability.valid_until) <= Date.now();
  const status = expired && availability.status !== 'disabled' ? 'stale' : availability.status;
  const label = status === 'stale' ? 'STALE' : availability.label;
  card.dataset.status = status || 'unknown';
  const badge = card.querySelector('.target-status');
  const detail = (availability.detail || '') + (availability.last_checked
    ? ' · Checked ' + new Date(availability.last_checked).toLocaleString() : '');
  if (badge) {
    badge.textContent = (label || 'UNKNOWN') + (availability.source ? ' · ' + availability.source : '');
    badge.title = status === 'stale' ? 'No recent measurement · ' + detail : detail;
    card.querySelector('.status-dot').title = badge.title;
  }
  applyCardClass(card);
}

function applyCardClass(card) {
  const missing = Number(card.dataset.routeMissing || 0);
  const degraded = Number(card.dataset.routeDegraded || 0);
  const routeAlert = missing > 0 || degraded > 0;
  card.className = 'target-card status-' + card.dataset.status + (routeAlert ? ' route-alert' : '');
}

function recount() {
  const cards = [...document.querySelectorAll('.target-card')];
  const counts = {healthy: 0, pending: 0, down: 0, unknown: 0};
  let routeAlerts = 0;
  let serviceAlerts = 0;

  cards.forEach(card => {
    const status = card.dataset.status === 'stale' ? 'unknown' : card.dataset.status;
    if (counts[status] !== undefined) counts[status]++;
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
  recount();
}

function updateTarget(target) {
  const card = document.getElementById('target-' + target.id);
  if (!card) return;

  card.querySelector('.latency').textContent = fmt(target.latency_ms);
  card.querySelector('.loss').textContent = fmt(target.status === 'unknown' ? null : target.loss_percent);
  card.querySelector('.jitter').textContent = fmt(target.jitter_ms);
  if (target.availability) updateAvailability(target.id, target.availability);
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
  if (topology.availability) updateAvailability(targetId, topology.availability);
  applyCardClass(card);
  recount();
}

window.addEventListener('multipath-live', event => {
  if (event.detail.availability) {
    updateAvailability(event.detail.target_id ?? event.detail.target?.id, event.detail.availability);
  }
  if (event.detail.type === 'service_health') updateServices(event.detail.target_id, event.detail.service_checks);
  if (event.detail.type === 'target_health') {
    updateTarget(event.detail.target);
  }
  if (event.detail.type === 'topology_update') {
    updateTopology(event.detail.target_id, event.detail.topology);
  }
});

const initialChecks = document.getElementById('initial-service-checks');
const initialAvailability = document.getElementById('initial-availability');
document.querySelectorAll('time[datetime]').forEach(time => {time.textContent = new Date(time.dateTime).toLocaleTimeString();});
if (initialChecks) Object.entries(JSON.parse(initialChecks.textContent)).forEach(([id, checks]) => updateServices(id, checks));
if (initialAvailability) Object.entries(JSON.parse(initialAvailability.textContent)).forEach(([id, availability]) => updateAvailability(id, availability));
recount();

// Expire old observations even if websocket updates stop. A read-only snapshot
// also catches changes missed during reconnects; it never initiates new probes.
setInterval(() => {
  availabilityByTarget.forEach((availability, id) => updateAvailability(id, availability));
  recount();
}, 5000);
let refreshing = false;
async function refreshTargets() {
  if (refreshing) return;
  refreshing = true;
  try {
    const response = await fetch('/api/targets');
    if (!response.ok) return;
    for (const target of await response.json()) {
      updateTarget(target);
      updateServices(target.id, target.service_checks);
    }
    recount();
  } catch (_) {
    // A failed dashboard fetch is not evidence that a monitored site is down.
  } finally {
    refreshing = false;
  }
}
setInterval(refreshTargets, 30000);
window.addEventListener('focus', refreshTargets);
