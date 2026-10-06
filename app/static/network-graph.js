(function (root) {
  'use strict';

  // Pure graph preparation: keep measured TTLs and links, and mark gaps explicitly.
  function build(data) {
    const routes = data.routes || [];
    const destinationIds = new Set(routes.filter(route => route.complete).map(route => {
      const hops = route.hops || [];
      return hops.length ? hops[hops.length - 1].node_id : null;
    }).filter(Boolean));
    const nodes = new Map((data.nodes || []).map(node => [node.id, {
      ...node,
      role: node.id === 'probe' ? 'source' : destinationIds.has(node.id) ? 'destination' : 'router',
      active: node.active ? '1' : '0',
      route_ids: []
    }]));
    const edges = new Map();
    const pairs = new Map();
    const routePaths = {};
    const key = (source, target) => JSON.stringify([source, target]);

    (data.edges || []).forEach(edge => {
      if (!nodes.has(edge.source) || !nodes.has(edge.target)) return;
      const sourceTTL = Number(nodes.get(edge.source).ttl);
      const targetTTL = Number(nodes.get(edge.target).ttl);
      const gap = targetTTL - sourceTTL - 1;
      const kind = edge.source === 'probe' ? (gap > 0 ? 'gap' : 'source') : 'observed';
      const item = {...edge, kind, gap_hops: Math.max(0, gap), active: edge.active ? '1' : '0', route_ids: []};
      edges.set(edge.id, item);
      pairs.set(key(edge.source, edge.target), item);
    });

    routes.forEach(route => {
      const path = (route.hops || []).filter(hop => hop.node_id && nodes.has(hop.node_id));
      const ids = ['probe', ...path.map(hop => hop.node_id)].filter(id => nodes.has(id));
      const edgeIds = [];
      ids.forEach(id => nodes.get(id).route_ids.push(route.id));
      for (let index = 1; index < ids.length; index++) {
        const source = ids[index - 1];
        const target = ids[index];
        const gap = Number(nodes.get(target).ttl) - Number(nodes.get(source).ttl) - 1;
        let edge = pairs.get(key(source, target));
        // A missing TTL is a gap in the observation, never a measured direct link.
        if (!edge && (gap > 0 || source === 'probe')) {
          const id = 'segment-' + source + '-' + target;
          edge = {id, source, target, kind: gap > 0 ? 'gap' : 'source', gap_hops: Math.max(0, gap),
            active: route.active ? '1' : '0', route_ids: [], samples: null, last_seen: route.last_seen};
          edges.set(id, edge);
          pairs.set(key(source, target), edge);
        }
        if (edge) {
          edge.route_ids.push(route.id);
          if (route.active) edge.active = '1';
          edgeIds.push(edge.id);
        }
      }
      routePaths[route.id] = {nodes: ids, edges: edgeIds};
    });

    const routeOrder = new Map(routes.map((route, index) => [route.id, index]));
    const routeById = new Map(routes.map(route => [route.id, route]));
    const rank = node => node.route_ids.length
      ? node.route_ids.reduce((total, id) => total + routeOrder.get(id), 0) / node.route_ids.length
      : routes.length;
    const columns = new Map();
    nodes.forEach(node => {
      const ttl = Number(node.ttl);
      if (!columns.has(ttl)) columns.set(ttl, []);
      columns.get(ttl).push(node);
    });
    // Compress unobserved columns; the original TTL remains in every label.
    const ttls = Array.from(columns.keys()).sort((a, b) => a - b);
    const positions = new Map();
    ttls.forEach((ttl, column) => {
      const group = columns.get(ttl).sort((a, b) => rank(a) - rank(b)
        || String(a.address || a.id).localeCompare(String(b.address || b.id)));
      group.forEach((node, row) => positions.set(node.id, {
        x: column * 230,
        y: (row - (group.length - 1) / 2) * 190
      }));
    });

    const elements = [];
    nodes.forEach(node => {
      const rtt = node.rtt_ms === null || node.rtt_ms === undefined ? 'RTT —' : 'RTT ' + Number(node.rtt_ms).toFixed(1) + ' ms';
      node.label = node.role === 'source' ? 'Source\nLocal probe'
        : (node.role === 'destination' ? 'Target · TTL ' : 'Hop ') + node.ttl + '\n' + node.address + '\n' + rtt;
      elements.push({group: 'nodes', data: node, position: positions.get(node.id)});
    });
    edges.forEach(edge => {
      const labels = Array.from(new Set(edge.route_ids.map(id => routeById.get(id).label)));
      const names = labels.length > 3 ? labels.slice(0, 2).join(' / ') + ' / +' + (labels.length - 2)
        : labels.join(' / ');
      edge.label = edge.kind === 'gap' ? '* ' + edge.gap_hops + ' unobserved hop' + (edge.gap_hops === 1 ? '' : 's')
        : names;
      edge.status = edge.active === '0' ? 'missing'
        : edge.route_ids.length && edge.route_ids.every(id => routeById.get(id).status === 'degraded') ? 'degraded' : 'active';
      elements.push({group: 'edges', data: edge});
    });
    return {elements, routePaths};
  }

  const api = {build};
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.MultipathGraph = api;
})(typeof globalThis !== 'undefined' ? globalThis : this);
