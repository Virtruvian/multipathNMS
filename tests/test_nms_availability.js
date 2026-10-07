const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');

function dashboard() {
  let now = Date.now();
  const elements = new Map();
  const callbacks = new Map();
  const timers = new Map();
  const parts = new Map(['.target-status', '.status-dot', '.latency', '.loss', '.jitter',
    '.routes-active', '.routes-degraded', '.routes-missing', '.routes-pending'].map(key => [key, {textContent: '', title: ''}]));
  const card = {dataset: {status: 'unknown'}, className: '', querySelector: key => parts.get(key)};
  elements.set('target-1', card);
  for (const key of ['total', 'healthy', 'pending', 'down', 'unknown', 'route-alerts', 'service-alerts']) {
    elements.set('count-' + key, {textContent: ''});
  }
  const availability = (status, source = 'TCP:443') => ({status, source,
    label: {healthy: 'UP', down: 'DOWN', pending: 'VERIFYING', unknown: 'UNKNOWN', disabled: 'PAUSED'}[status],
    detail: 'Measurement details', last_checked: new Date(now).toISOString(),
    valid_until: status === 'disabled' || status === 'unknown' ? null : new Date(now + 95000).toISOString()});
  elements.set('initial-service-checks', {textContent: '{}'});
  elements.set('initial-availability', {textContent: JSON.stringify({1: availability('healthy')})});
  class Clock extends Date {static now() {return now;}}
  let snapshot = [];
  let failFetch = false;
  const context = vm.createContext({Date: Clock, Map, Number, String, JSON,
    document: {getElementById: key => elements.get(key), querySelectorAll: selector => selector === '.target-card' ? [card] : []},
    window: {addEventListener: (name, fn) => callbacks.set(name, fn)},
    setInterval: (fn, delay) => timers.set(delay, fn),
    fetch: async () => ({ok: !failFetch, json: async () => snapshot}),
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../app/static/nms.js'), 'utf8'), context);
  return {card, parts, elements, availability,
    send: detail => callbacks.get('multipath-live')({detail}),
    expire: () => {now += 100000; timers.get(5000)();},
    poll: async (targets, failed = false) => {snapshot = targets; failFetch = failed; await timers.get(30000)();},
  };
}

test('site availability stays UP when a separate ping update reports DOWN', () => {
  const app = dashboard();
  app.send({type: 'target_health', target: {id: 1, status: 'down', latency_ms: null, loss_percent: 100, jitter_ms: null},
    availability: app.availability('healthy')});
  assert.equal(app.card.dataset.status, 'healthy');
  assert.equal(app.parts.get('.target-status').textContent, 'UP · TCP:443');
  assert.equal(app.parts.get('.loss').textContent, '100.0');
  assert.equal(app.elements.get('count-healthy').textContent, 1);
  app.send({type: 'target_health', target: {id: 1, status: 'unknown', loss_percent: 0}});
  assert.equal(app.parts.get('.loss').textContent, '—');
  assert.equal(app.card.dataset.status, 'healthy');
});

test('HTTPS failure changes the host status and counters despite healthy TCP', () => {
  const app = dashboard();
  app.send({type: 'service_health', target_id: 1, availability: app.availability('down', 'HTTPS:443'),
    service_checks: [{method: 'tcp', status: 'healthy'}, {method: 'https', status: 'down'}]});
  assert.equal(app.card.dataset.status, 'down');
  assert.equal(app.parts.get('.target-status').textContent, 'DOWN · HTTPS:443');
  assert.equal(app.elements.get('count-down').textContent, 1);
  assert.equal(app.elements.get('count-healthy').textContent, 0);
  assert.equal(app.elements.get('count-service-alerts').textContent, 1);
  app.send({type: 'service_health', target_id: 1, availability: app.availability('pending'), service_checks: []});
  assert.equal(app.parts.get('.target-status').textContent, 'VERIFYING · TCP:443');
  assert.equal(app.elements.get('count-down').textContent, 0);
  assert.equal(app.elements.get('count-pending').textContent, 1);
});

test('a stopped update stream expires UP to STALE and fresh evidence restores UP', () => {
  const app = dashboard();
  app.expire();
  assert.equal(app.card.dataset.status, 'stale');
  assert.equal(app.parts.get('.target-status').textContent, 'STALE · TCP:443');
  assert.equal(app.elements.get('count-healthy').textContent, 0);
  assert.equal(app.elements.get('count-unknown').textContent, 1);
  app.send({type: 'target_health', target: {id: 1, status: 'unknown',
    availability: app.availability('healthy')}, availability: app.availability('healthy')});
  assert.equal(app.card.dataset.status, 'healthy');
  assert.equal(app.elements.get('count-healthy').textContent, 1);
});

test('read-only snapshots catch missed status changes; failed fetches never declare site failure', async () => {
  const app = dashboard();
  await app.poll([], true);
  assert.equal(app.card.dataset.status, 'healthy');
  await app.poll([{id: 1, status: 'healthy', availability: app.availability('down'), service_checks: [{status: 'down'}]}]);
  assert.equal(app.card.dataset.status, 'down');
  await app.poll([{id: 1, enabled: false, status: 'healthy', availability: app.availability('disabled', ''), service_checks: []}]);
  assert.equal(app.parts.get('.target-status').textContent, 'PAUSED');
  assert.equal(app.elements.get('count-down').textContent, 0);
  assert.equal(app.elements.get('count-service-alerts').textContent, 0);
});
