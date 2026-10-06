(function (root) {
  'use strict';

  // Display relationships between measured flows, never physical router links.
  function build(data, {showLooseReplies = false, showHistory = true} = {}) {
    const maxTTL = Number(data.max_ttl || 32);
    const eligible = (data.routes || []).filter(route =>
      (route.hops || []).every(hop => Number(hop.ttl) <= maxTTL));
    const routes = eligible.filter(route => showHistory || route.active);
    const hiddenMissingCount = eligible.filter(route => !route.active && !showHistory).length;
    const excludedRouteCount = (data.summary || {}).excluded_routes || (data.routes || []).length - eligible.length;
    const primary = routes.slice().sort((a, b) =>
      Number(Boolean(b.active)) - Number(Boolean(a.active))
      || Number(Boolean(b.complete)) - Number(Boolean(a.complete))
      || Number(b.flow_count || 0) - Number(a.flow_count || 0)
      || (a.hops || []).length - (b.hops || []).length)[0];
    const visibleHopIds = new Set(routes.flatMap(route => (route.hops || []).map(hop => hop.node_id)));
    const rawNodes = new Map((data.nodes || []).filter(node => Number(node.ttl) <= maxTTL
      && (showHistory || node.active || node.id === 'probe' || visibleHopIds.has(node.id))).map(node => [node.id, node]));
    const lastId = route => {
      const hops = route.hops || [];
      return hops.length ? hops[hops.length - 1].node_id : null;
    };
    const destinationIds = new Set(routes.filter(route => route.complete)
      .map(lastId).filter(id => rawNodes.has(id)));
    const targetId = primary && primary.complete ? lastId(primary) : Array.from(destinationIds)[0];
    const destinationsByAddress = new Map();
    destinationIds.forEach(id => {
      const address = rawNodes.get(id).address;
      if (!destinationsByAddress.has(address)) destinationsByAddress.set(address, []);
      destinationsByAddress.get(address).push(id);
    });
    const aliases = new Map(), targetTTLs = new Map();
    destinationsByAddress.forEach(ids => {
      const canonical = ids.includes(targetId) ? targetId : ids[0];
      ids.forEach(id => aliases.set(id, canonical));
      targetTTLs.set(canonical, ids.map(id => Number(rawNodes.get(id).ttl)).sort((a, b) => a - b));
    });
    const alias = id => aliases.get(id) || id;
    const nodes = new Map();
    rawNodes.forEach(node => {
      const id = alias(node.id);
      if (nodes.has(id) && node.id !== id) return;
      nodes.set(id, {
        ...node, id, role: id === 'probe' ? 'source' : targetTTLs.has(id) ? 'destination' : 'router',
        ttl: targetTTLs.has(id) ? Math.max(...targetTTLs.get(id)) : node.ttl,
        observed_ttls: targetTTLs.has(id) ? Array.from(new Set(targetTTLs.get(id))) : [Number(node.ttl)],
        active: node.active ? '1' : '0', route_ids: []
      });
    });
    const measured = new Map();
    const key = (source, target) => JSON.stringify([source, target]);
    (data.edges || []).forEach(edge => measured.set(key(edge.source, edge.target), edge));
    const edges = new Map();
    const routePaths = {};
    const routeById = new Map(routes.map(route => [route.id, route]));

    routes.forEach(route => {
      const path = (route.hops || []).filter(hop => hop.node_id && rawNodes.has(hop.node_id));
      const originalIds = ['probe', ...path.map(hop => hop.node_id)].filter(id => rawNodes.has(id));
      const ids = originalIds.map(alias);
      const edgeIds = [];
      ids.forEach(id => {
        if (!nodes.get(id).route_ids.includes(route.id)) nodes.get(id).route_ids.push(route.id);
        if (route.active) nodes.get(id).active = '1';
      });
      for (let index = 1; index < originalIds.length; index++) {
        const oldSource = originalIds[index - 1], oldTarget = originalIds[index];
        const source = alias(oldSource), target = alias(oldTarget);
        const gap = Number(rawNodes.get(oldTarget).ttl) - Number(rawNodes.get(oldSource).ttl) - 1;
        if (source === target || gap < 0) continue;
        const observation = measured.get(key(oldSource, oldTarget));
        // Only a same-flow gap or probe origin may be drawn without an observed adjacency.
        if (!observation && gap === 0 && oldSource !== 'probe') continue;
        const kind = gap > 0 ? 'gap' : oldSource === 'probe' ? 'source' : 'observed';
        const pair = JSON.stringify([source, target, kind, gap]);
        let edge = edges.get(pair);
        if (!edge) {
          edge = {
            ...observation, id: observation ? observation.id : 'segment-' + source + '-' + target,
            source, target, kind, gap_hops: Math.max(0, gap), route_ids: [],
            active: '0', samples: kind === 'gap' ? null : observation ? observation.samples : null,
            last_seen: observation ? observation.last_seen : route.last_seen
          };
          // Aliased destinations may give a measured and a gap segment the same base ID.
          if (Array.from(edges.values()).some(item => item.id === edge.id)) edge.id += '-' + kind + '-' + gap;
          edges.set(pair, edge);
        }
        if (!edge.route_ids.includes(route.id)) edge.route_ids.push(route.id);
        if (route.active) edge.active = '1';
        edgeIds.push(edge.id);
      }
      routePaths[route.id] = {nodes: ids, edges: edgeIds};
    });

    const looseReplyCount = Array.from(nodes.values()).filter(node => node.id !== 'probe' && !node.route_ids.length).length;
    if (!showLooseReplies) {
      nodes.forEach((node, id) => { if (id !== 'probe' && !node.route_ids.length) nodes.delete(id); });
    } else {
      (data.edges || []).forEach(edge => {
        const source = alias(edge.source), target = alias(edge.target);
        if (!nodes.has(source) || !nodes.has(target) || source === target) return;
        if (Array.from(edges.values()).some(item => item.source === source && item.target === target)) return;
        edges.set('diagnostic-' + edge.id, {...edge, source, target, kind: 'diagnostic', route_ids: [],
          active: edge.active ? '1' : '0', gap_hops: 0});
      });
    }

    const columns = new Map();
    nodes.forEach(node => {
      const ttl = Number(node.ttl);
      if (!columns.has(ttl)) columns.set(ttl, []);
      columns.get(ttl).push(node);
    });
    const ttls = Array.from(columns.keys()).sort((a, b) => a - b);
    const positions = new Map();
    const primaryNodes = new Set(primary ? routePaths[primary.id].nodes : ['probe']);
    const occupied = new Map();
    const reserve = (id, lane) => {
      if (!nodes.has(id) || positions.has(id)) return;
      const column = ttls.indexOf(Number(nodes.get(id).ttl));
      if (!occupied.has(column)) occupied.set(column, new Set());
      while (occupied.get(column).has(lane)) lane += lane < 0 ? -1 : 1;
      occupied.get(column).add(lane);
      positions.set(id, {x: column * 180, y: lane * 135});
    };
    primaryNodes.forEach(id => reserve(id, 0));
    let branch = 0;
    routes.filter(route => route !== primary).forEach(route => {
      branch++;
      const lane = Math.ceil(branch / 2) * (branch % 2 ? -1 : 1);
      routePaths[route.id].nodes.forEach(id => reserve(id, lane));
    });
    nodes.forEach(node => reserve(node.id, Math.max(2, branch + 1)));

    const elements = [];
    nodes.forEach(node => {
      const rtt = node.rtt_ms === null || node.rtt_ms === undefined ? 'RTT —' : 'RTT ' + Number(node.rtt_ms).toFixed(1) + ' ms';
      node.label = node.role === 'source' ? 'Source\nLocal probe'
        : (node.role === 'destination' ? 'Target' : 'Hop ' + node.ttl) + '\n' + node.address + '\n' + rtt;
      elements.push({group: 'nodes', data: node, position: positions.get(node.id)});
    });
    edges.forEach(edge => {
      const deltaY = positions.get(edge.target).y - positions.get(edge.source).y;
      edge.curve_distance = deltaY ? Math.sign(deltaY) * 25 : 0;
      edge.label = edge.kind === 'gap' ? '* ' + edge.gap_hops + ' unobserved hop' + (edge.gap_hops === 1 ? '' : 's') : '';
      edge.status = edge.active === '0' ? 'missing'
        : edge.route_ids.length && edge.route_ids.every(id => routeById.get(id).status === 'degraded') ? 'degraded' : 'active';
      elements.push({group: 'edges', data: edge});
    });
    return {elements, routePaths, looseReplyCount, visibleRoutes: routes, primaryRouteId: primary ? primary.id : null,
      hiddenMissingCount, excludedRouteCount};
  }

  const api = {build};
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.MultipathGraph = api;
})(typeof globalThis !== 'undefined' ? globalThis : this);
