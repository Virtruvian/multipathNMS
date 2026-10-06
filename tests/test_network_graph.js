const test = require('node:test');
const assert = require('node:assert/strict');
const {build} = require('../app/static/network-graph.js');

function fixture() {
  const nodes = [
    {id: 'probe', ttl: 0, active: true},
    {id: 'n1', ttl: 1, address: '192.168.1.1', rtt_ms: 1, active: true},
    {id: 'n2a', ttl: 2, address: '10.0.0.1', rtt_ms: 3, active: true},
    {id: 'n2b', ttl: 2, address: '10.0.0.2', rtt_ms: 4, active: true},
    {id: 'n3', ttl: 3, address: '8.8.8.8', rtt_ms: 6, active: true}
  ];
  const edges = [['probe', 'n1'], ['n1', 'n2a'], ['n1', 'n2b'], ['n2a', 'n3'], ['n2b', 'n3']]
    .map(([source, target], id) => ({id: 'e' + id, source, target, active: true, samples: 10}));
  const routes = ['a', 'b'].map((branch, index) => ({
    id: index + 1, label: 'Route ' + (index ? 'B' : 'A'), complete: true, active: true, status: 'active',
    hops: [{ttl: 1, node_id: 'n1'}, {ttl: 2, node_id: 'n2' + branch}, {ttl: 3, node_id: 'n3'}]
  }));
  return {nodes, edges, routes};
}

test('parallel hops share a column, branch vertically and converge on the target', () => {
  const data = fixture();
  const before = JSON.stringify(data);
  const graph = build(data);
  const node = id => graph.elements.find(item => item.data.id === id);
  assert.equal(node('n2a').position.x, node('n2b').position.x);
  assert.notEqual(node('n2a').position.y, node('n2b').position.y);
  assert.ok(node('n1').position.x < node('n2a').position.x);
  assert.ok(node('n2a').position.x < node('n3').position.x);
  assert.equal(node('n3').data.role, 'destination');
  assert.match(node('n3').data.label, /RTT 6.0 ms/);
  assert.deepEqual(graph.routePaths[1].nodes, ['probe', 'n1', 'n2a', 'n3']);
  assert.equal(JSON.stringify(data), before, 'do not mutate live backend data');
});

test('disconnected replies stay in their measured TTL column instead of a root row', () => {
  const data = fixture();
  data.nodes.push({id: 'orphan', ttl: 4, address: '203.0.113.4', active: true});
  const graph = build(data);
  const orphan = graph.elements.find(item => item.data.id === 'orphan');
  const destination = graph.elements.find(item => item.data.id === 'n3');
  assert.ok(orphan.position.x > destination.position.x);
  assert.equal(graph.elements.filter(item => item.group === 'edges').length, data.edges.length);
});

test('unobserved hops have an explicit gap and never become a measured direct link', () => {
  const data = {nodes: [
    {id: 'probe', ttl: 0, active: true},
    {id: 'n1', ttl: 1, address: '192.168.1.1', active: true},
    {id: 'n4', ttl: 4, address: '8.8.8.8', active: true}
  ], edges: [], routes: [{id: 1, label: 'Route A', active: true, status: 'active', complete: true,
    hops: [{ttl: 1, node_id: 'n1'}, {ttl: 2, node_id: null}, {ttl: 3, node_id: null}, {ttl: 4, node_id: 'n4'}]}]};
  const graph = build(data);
  const segment = graph.elements.find(item => item.data.source === 'n1');
  assert.equal(segment.data.kind, 'gap');
  assert.equal(segment.data.gap_hops, 2);
  assert.match(segment.data.label, /2 unobserved hops/);
  assert.equal(segment.data.samples, null);
  assert.ok(graph.routePaths[1].edges.includes(segment.data.id));
  assert.ok(graph.elements.find(item => item.data.id === 'n4').position.x < 600,
    'do not waste columns on unobserved TTLs');
});

test('missing branch stays red while its shared links stay current', () => {
  const data = fixture();
  data.routes[1].active = false;
  data.routes[1].status = 'missing';
  data.nodes.find(node => node.id === 'n2b').active = false;
  data.edges.filter(edge => edge.source === 'n2b' || edge.target === 'n2b').forEach(edge => {edge.active = false;});
  const graph = build(data);
  assert.equal(graph.elements.find(item => item.data.id === 'e0').data.status, 'active');
  assert.equal(graph.elements.find(item => item.data.id === 'e2').data.status, 'missing');
  assert.deepEqual(graph.elements.find(item => item.data.id === 'e0').data.route_ids, [1, 2]);
});

test('empty topology renders the source without inventing paths', () => {
  const graph = build({nodes: [{id: 'probe', ttl: 0, active: true}], routes: [], edges: []});
  assert.equal(graph.elements.length, 1);
  assert.deepEqual(graph.elements[0].position, {x: 0, y: 0});
  assert.equal(graph.elements[0].data.role, 'source');
});
