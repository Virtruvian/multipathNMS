(function (root) {
  'use strict';
  const labels = {healthy: 'REACHABLE', down: 'FAILED', pending: 'VERIFYING', unknown: 'NOT MEASURED',
    disabled: 'DISABLED', stale: 'STALE', degraded: 'DEGRADED', suspect: 'SUSPECT'};
  function render(container, checks) {
    if (!container) return;
    container.replaceChildren();
    (checks || []).forEach(check => {
      const badge = document.createElement('div');
      badge.className = 'service-check service-' + (check.status || 'unknown');
      const title = document.createElement('strong');
      title.textContent = check.method.toUpperCase() + (check.port ? ':' + check.port : '') + ' · '
        + (labels[check.status] || 'UNKNOWN')
        + (check.status === 'pending' ? ' ' + check.consecutive_failures + '/' + check.confirmation_threshold : '');
      const details = document.createElement('small');
      const parts = [];
      if (check.latency_ms !== null && check.latency_ms !== undefined) parts.push(Number(check.latency_ms).toFixed(1) + ' ms');
      if (check.http_status) parts.push('HTTP ' + check.http_status);
      if (check.path) parts.push(check.path);
      if (check.resolved_ip) parts.push(check.resolved_ip);
      if (check.error) parts.push(check.error);
      if (check.failed_phase) parts.push('Failed at ' + check.failed_phase.toUpperCase());
      parts.push(check.last_checked ? 'Checked ' + new Date(check.last_checked).toLocaleString() : check.enabled === false ? 'Enable in Settings' : 'Waiting for check');
      details.textContent = parts.join(' · ');
      badge.append(title, details);
      container.appendChild(badge);
    });
  }
  root.ServiceHealth = {render};
})(typeof globalThis !== 'undefined' ? globalThis : this);
