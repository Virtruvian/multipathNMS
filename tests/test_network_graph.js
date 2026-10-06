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

test('loose replies and links are hidden by default and available for diagnosis', () => {
  const data = fixture();
  data.nodes.push({id: 'orphan', ttl: 4, address: '203.0.113.4', active: true});
  data.edges.push({id: 'loose-edge', source: 'n3', target: 'orphan', active: true});
  const graph = build(data);
  assert.equal(graph.looseReplyCount, 1);
  assert.equal(graph.elements.some(item => item.data.id === 'orphan'), false);
  assert.equal(graph.elements.some(item => item.data.id === 'loose-edge'), false);
  const diagnostic = build(data, {showLooseReplies: true});
  const orphan = diagnostic.elements.find(item => item.data.id === 'orphan');
  const destination = diagnostic.elements.find(item => item.data.id === 'n3');
  assert.ok(orphan.position.x > destination.position.x);
  assert.equal(diagnostic.elements.filter(item => item.group === 'edges').length, data.edges.length);
});

test('an edge between relevant nodes still needs membership in a reconstructed path', () => {
  const data = fixture();
  data.edges.push({id: 'unrelated', source: 'n2a', target: 'n2b', active: true});
  assert.equal(build(data).elements.some(item => item.data.id === 'unrelated'), false);
});

test('partial paths remain visible when the target does not respond', () => {
  const data = fixture();
  data.routes = [{id: 1, label: 'Route A', complete: false, active: true, status: 'active',
    hops: [{ttl: 1, node_id: 'n1'}, {ttl: 2, node_id: 'n2a'}]}];
  const graph = build(data);
  assert.deepEqual(graph.routePaths[1].nodes, ['probe', 'n1', 'n2a']);
  assert.equal(graph.elements.some(item => item.data.id === 'n2a'), true);
  assert.equal(graph.elements.some(item => item.data.id === 'n3'), false);
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

test('primary path stays on one line, alternatives branch and merge', () => {
  const data = fixture();
  const graph = build(data);
  assert.equal(graph.primaryRouteId, 1);
  const y = id => graph.elements.find(item => item.data.id === id).position.y;
  ['probe', 'n1', 'n2a', 'n3'].forEach(id => assert.equal(y(id), 0));
  assert.notEqual(y('n2b'), 0);
  assert.equal(graph.elements.find(item => item.data.id === 'e3').data.label, '');
});

test('different hop counts terminate at one visual target without inventing a link', () => {
  const data = fixture();
  data.nodes.push({id:'n4',ttl:4,address:'8.8.8.8',active:true});
  data.routes[1].hops = [{ttl:1,node_id:'n1'},{ttl:2,node_id:'n2b'},
    {ttl:3,node_id:null},{ttl:4,node_id:'n4'}];
  data.edges = data.edges.filter(edge => edge.source !== 'n2b');
  const graph = build(data);
  const targets = graph.elements.filter(item => item.group === 'nodes' && item.data.role === 'destination');
  assert.equal(targets.length, 1);
  assert.deepEqual(targets[0].data.observed_ttls, [3, 4]);
  assert.equal(graph.routePaths[1].nodes.at(-1), graph.routePaths[2].nodes.at(-1));
  const branchEdge = graph.elements.find(item => item.data.source === 'n2b');
  assert.equal(branchEdge.data.kind, 'gap');
  assert.equal(branchEdge.data.gap_hops, 1);
  assert.equal(branchEdge.data.samples, null);
});

test('recent history is retained but can be separated from current paths', () => {
  const data = fixture();
  data.routes[1].active = false;
  data.routes[1].status = 'missing';
  const current = build(data, {showHistory:false});
  assert.equal(current.hiddenMissingCount, 1);
  assert.deepEqual(current.visibleRoutes.map(route => route.id), [1]);
  assert.equal(current.elements.some(item => item.data.id === 'n2b'), false);
  assert.equal(build(data, {showHistory:true}).elements.some(item => item.data.id === 'n2b'), true);
});

test('a previous DNS destination keeps its own endpoint rather than merging with another IP', () => {
  const data = fixture();
  data.nodes.push({id:'old-target',ttl:3,address:'1.1.1.1',active:false});
  data.routes[1].active = false;
  data.routes[1].hops.at(-1).node_id = 'old-target';
  const graph = build(data, {showHistory:true});
  const targets = graph.elements.filter(item => item.group === 'nodes' && item.data.role === 'destination');
  assert.equal(targets.length, 2);
  assert.notEqual(graph.routePaths[1].nodes.at(-1), graph.routePaths[2].nodes.at(-1));
});

test('unprobed TTLs never appear even in the diagnostic view', () => {
  const data = fixture();
  data.max_ttl = 32;
  data.nodes.push({id:'bogus',ttl:54,address:'52.174.3.88',active:true});
  data.routes.push({id:3,active:true,complete:false,hops:[{ttl:54,node_id:'bogus'}]});
  const graph = build(data, {showLooseReplies:true,showHistory:true});
  assert.equal(graph.excludedRouteCount, 1);
  assert.equal(graph.elements.some(item => item.data.id === 'bogus'), false);
});

test('comparison retains separate ICMP and TCP adjacencies and method RTTs', () => {
  const data = {
    target: {id: 1, address: '8.8.8.8'},
    nodes: [
      {id: 'probe', ttl: 0, active: true},
      {id: 'i1', ttl: 1, address: '10.0.0.1', rtt_ms: 1, active: true, protocol: 'icmp'},
      {id: 'it', ttl: 2, address: '8.8.8.8', rtt_ms: 2, active: true, protocol: 'icmp'},
      {id: 't1', ttl: 1, address: '10.0.0.1', rtt_ms: 5, active: true, protocol: 'tcp'},
      {id: 'tt', ttl: 2, address: '8.8.8.8', rtt_ms: 8, active: true, protocol: 'tcp'}
    ],
    edges: [
      {id: 'icmp-link', source: 'i1', target: 'it', active: true},
      {id: 'tcp-link', source: 't1', target: 'tt', active: true}
    ],
    routes: [
      {id: 1, protocol: 'icmp', active: true, complete: true, hops: [{ttl: 1, node_id: 'i1'}, {ttl: 2, node_id: 'it'}]},
      {id: 2, protocol: 'tcp', destination_port: 443, active: true, complete: true, hops: [{ttl: 1, node_id: 't1'}, {ttl: 2, node_id: 'tt'}]}
    ]
  };
  const combined = build(data, {showHistory: false});
  assert.equal(combined.visibleRoutes.length, 2);
  const routerIds = combined.elements.filter(item => item.group === 'nodes' && item.data.role === 'router').map(item => item.data.id);
  assert.deepEqual(new Set(routerIds), new Set(['i1', 't1']));
  const endpoint = combined.elements.find(item => item.group === 'nodes' && item.data.role === 'destination');
  assert.deepEqual(new Set(endpoint.data.methods), new Set(['ICMP', 'TCP:443']));
  assert.equal(endpoint.data.rtt_ms, null);
  assert.match(endpoint.data.label, /RTT per route/);
  assert.equal(combined.elements.filter(item => item.group === 'edges' && item.data.protocol === 'tcp').length, 2);
  for (const protocol of ['icmp', 'tcp']) {
    const filtered = build(data, {protocol, showLooseReplies: true});
    assert.equal(filtered.visibleRoutes.length, 1);
    assert.ok(filtered.elements.filter(item => item.group === 'nodes' && item.data.id !== 'probe')
      .every(item => item.data.protocol === protocol));
    assert.ok(filtered.elements.filter(item => item.group === 'edges').every(item => item.data.protocol === protocol));
  }
});

test('a missing TCP hop is never filled by an ICMP hop at the same TTL', () => {
  const data = {
    nodes: [
      {id: 'probe', ttl: 0, active: true},
      {id: 'icmp2', ttl: 2, address: '10.0.0.2', protocol: 'icmp', active: true},
      {id: 'tcp1', ttl: 1, address: '10.0.0.1', protocol: 'tcp', active: true},
      {id: 'tcp3', ttl: 3, address: '8.8.8.8', protocol: 'tcp', active: true}
    ], edges: [],
    routes: [{id: 3, protocol: 'tcp', destination_port: 443, active: true, complete: true,
      hops: [{ttl: 1, node_id: 'tcp1'}, {ttl: 2, node_id: null}, {ttl: 3, node_id: 'tcp3'}]}]
  };
  const graph = build(data);
  assert.equal(graph.elements.some(item => item.data.id === 'icmp2'), false);
  const gaps = graph.elements.filter(item => item.group === 'edges' && item.data.kind === 'gap');
  assert.equal(gaps.length, 1);
  assert.equal(gaps[0].data.gap_hops, 1);
  assert.equal(gaps[0].data.protocol, 'tcp');
});
