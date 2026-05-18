'use strict';

const state = {
  token: localStorage.getItem('aws-light-token') || '',
  selectedService: '',
  selectedPlatform: '',
  serviceLogsOpen: false,
  platformLogsOpen: false,
  currentView: 'overview',
  data: {
    overview: null,
    services: [],
    nodes: [],
    platform: [],
    metrics: null,
    timeseries: { bucket_seconds: 10, buckets: [] },
    events: [],
    topology: { nodes: [], edges: [] },
    routing: { services: [] },
  },
};

const els = {
  loginView: document.getElementById('login-view'),
  loginForm: document.getElementById('login-form'),
  loginError: document.getElementById('login-error'),
  overviewView: document.getElementById('overview-view'),
  servicesView: document.getElementById('services-view'),
  trafficView: document.getElementById('traffic-view'),
  topologyView: document.getElementById('topology-view'),
  platformView: document.getElementById('platform-view'),
  connectionStatus: document.getElementById('connection-status'),
  refreshButton: document.getElementById('refresh-button'),
  summaryServices: document.getElementById('summary-services'),
  summaryReplicas: document.getElementById('summary-replicas'),
  summaryCpu: document.getElementById('summary-cpu'),
  summaryMemory: document.getElementById('summary-memory'),
  summaryScheduler: document.getElementById('summary-scheduler'),
  warningsPanel: document.getElementById('warnings-panel'),
  overviewServices: document.getElementById('overview-services'),
  overviewServiceDetail: document.getElementById('overview-service-detail'),
  overviewServicesCount: document.getElementById('overview-services-count'),
  servicesBody: document.getElementById('services-body'),
  servicesCount: document.getElementById('services-count'),
  serviceDetail: document.getElementById('service-detail'),
  nodeGrid: document.getElementById('node-grid'),
  trafficWindow: document.getElementById('traffic-window'),
  trafficSummary: document.getElementById('traffic-summary'),
  trafficChart: document.getElementById('traffic-chart'),
  trafficServices: document.getElementById('traffic-services'),
  nodesCount: document.getElementById('nodes-count'),
  platformList: document.getElementById('platform-list'),
  platformCount: document.getElementById('platform-count'),
  platformDetail: document.getElementById('platform-detail'),
  eventLog: document.getElementById('event-log'),
  eventCount: document.getElementById('event-count'),
  topologyCanvas: document.getElementById('topology-canvas'),
  routingList: document.getElementById('routing-list'),
  routingCount: document.getElementById('routing-count'),
};

function authHeaders() {
  return state.token ? { Authorization: `Bearer ${state.token}` } : {};
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: {
      'Content-Type': 'application/json',
      ...authHeaders(),
      ...(options.headers || {}),
    },
  });
  if (response.status === 401 || response.status === 403) {
    localStorage.removeItem('aws-light-token');
    state.token = '';
    showLogin();
    throw new Error(`HTTP ${response.status}`);
  }
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return response.json();
}

function showLogin() {
  els.loginView.hidden = false;
  els.overviewView.hidden = true;
  els.servicesView.hidden = true;
  els.trafficView.hidden = true;
  els.topologyView.hidden = true;
  els.platformView.hidden = true;
  els.connectionStatus.textContent = 'signed out';
  els.connectionStatus.className = 'status-pill disconnected';
}

function showApp() {
  els.loginView.hidden = true;
  els.overviewView.hidden = state.currentView !== 'overview';
  els.servicesView.hidden = state.currentView !== 'services';
  els.trafficView.hidden = state.currentView !== 'traffic';
  els.topologyView.hidden = state.currentView !== 'topology';
  els.platformView.hidden = state.currentView !== 'platform';
}

els.loginForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  els.loginError.hidden = true;
  const username = document.getElementById('username').value;
  const password = document.getElementById('password').value;
  try {
    const data = await api('/api/v1/auth/login', {
      method: 'POST',
      headers: {},
      body: JSON.stringify({ username, password }),
    });
    state.token = data.access_token;
    localStorage.setItem('aws-light-token', state.token);
    showApp();
    await refreshAll();
  } catch (error) {
    els.loginError.textContent = `Login failed: ${error.message}`;
    els.loginError.hidden = false;
  }
});

document.querySelectorAll('.tab').forEach((button) => {
  button.addEventListener('click', () => {
    state.currentView = button.dataset.view;
    document.querySelectorAll('.tab').forEach((item) => item.classList.remove('active'));
    button.classList.add('active');
    showApp();
    render();
  });
});

els.refreshButton.addEventListener('click', () => refreshAll());

async function refreshAll() {
  if (!state.token) {
    showLogin();
    return;
  }
  try {
    const [
      overview,
      services,
      nodes,
      platform,
      metrics,
      timeseries,
      events,
      topology,
      routing,
    ] = await Promise.all([
      api('/api/v1/overview'),
      api('/api/v1/services'),
      api('/api/v1/nodes'),
      api('/api/v1/platform/services'),
      api('/api/v1/platform/metrics'),
      api('/api/v1/platform/timeseries?buckets=36'),
      api('/api/v1/platform/events?limit=80'),
      api('/api/v1/platform/topology'),
      api('/api/v1/platform/routing'),
    ]);
    state.data = {
      overview,
      services,
      nodes,
      platform,
      metrics,
      timeseries,
      events: events.events || [],
      topology,
      routing,
    };
    els.connectionStatus.textContent = 'live';
    els.connectionStatus.className = 'status-pill connected';
    showApp();
    render();
  } catch (error) {
    if (state.token) {
      els.connectionStatus.textContent = 'stale';
      els.connectionStatus.className = 'status-pill disconnected';
    }
  }
}

function render() {
  renderSummary();
  renderWarnings();
  renderOverviewServices();
  renderServices();
  renderTraffic();
  refreshSelectedServiceTraffic();
  renderNodes();
  renderPlatform();
  renderEvents();
  renderRouting();
  if (state.currentView === 'topology') renderTopology();
}

function renderOverviewServices() {
  const diagnosticsByService = routingStatsByService();
  const services = state.data.services
    .slice()
    .sort((a, b) => {
      const statusRank = statusPriority(a.status) - statusPriority(b.status);
      return statusRank || a.spec.name.localeCompare(b.spec.name);
    });
  els.overviewServicesCount.textContent = `${services.length}`;
  els.overviewServices.innerHTML = services.map((svc) => {
    const running = svc.replicas.filter((replica) => replica.status === 'running').length;
    const routing = diagnosticsByService.get(svc.spec.name) || { routeable: 0, total: 0 };
    const selected = state.selectedService === svc.spec.name ? ' selected' : '';
    return `
      <button class="overview-service-row${selected}" data-overview-service="${escapeAttr(svc.spec.name)}">
        <span class="mono strong">${escapeHtml(svc.spec.name)}</span>
        <span class="badge ${badgeClass(svc.status)}">${escapeHtml(svc.status)}</span>
        <span>${running}/${svc.spec.replicas}/${svc.spec.max_replicas} replicas</span>
        <span>${routing.routeable}/${routing.total} routeable</span>
      </button>
    `;
  }).join('') || '<div class="empty">No services</div>';
  els.overviewServices.querySelectorAll('[data-overview-service]').forEach((row) => {
    row.addEventListener('click', () => {
      selectOverviewService(row.dataset.overviewService);
    });
  });
  if (
    state.selectedService &&
    state.currentView === 'overview' &&
    !state.data.services.some((svc) => svc.spec.name === state.selectedService)
  ) {
    state.selectedService = '';
    els.overviewServiceDetail.hidden = true;
  }
}

function renderSummary() {
  const overview = state.data.overview;
  if (!overview) return;
  const services = overview.services || {};
  const nodes = overview.nodes || {};
  els.summaryServices.textContent = `${services.running || 0}/${services.total || 0}`;
  els.summaryReplicas.textContent = `${services.actual_replicas || 0}/${services.desired_replicas || 0}`;
  els.summaryCpu.textContent =
    `${formatNumber(nodes.cpu_actual)}/${formatNumber(nodes.cpu_reserved)} reserved / ${formatNumber(nodes.cpu_capacity)}`;
  els.summaryMemory.textContent =
    `${formatNumber(nodes.memory_actual_mb)}/${formatNumber(nodes.memory_reserved_mb)} reserved / ${formatNumber(nodes.memory_capacity_mb)} MB`;
  els.summaryScheduler.textContent = overview.scheduler_policy || '-';
}

function renderWarnings() {
  const warnings = state.data.overview?.warnings || [];
  els.warningsPanel.hidden = warnings.length === 0;
  els.warningsPanel.innerHTML = warnings.map((warning) => `<div>${escapeHtml(warning)}</div>`).join('');
}

function renderServices() {
  const diagnosticsByService = routingStatsByService();
  const rows = state.data.services
    .slice()
    .sort((a, b) => a.spec.name.localeCompare(b.spec.name))
    .map((svc) => {
      const running = svc.replicas.filter((replica) => replica.status === 'running').length;
      const routing = diagnosticsByService.get(svc.spec.name) || { routeable: 0, total: 0 };
      return `
        <tr class="${state.selectedService === svc.spec.name ? 'selected' : ''}" data-service="${escapeAttr(svc.spec.name)}">
          <td class="mono strong">${escapeHtml(svc.spec.name)}</td>
          <td><span class="badge ${badgeClass(svc.status)}">${escapeHtml(svc.status)}</span></td>
          <td>${running}/${svc.spec.replicas}</td>
          <td>${routing.routeable}/${routing.total}</td>
          <td>${svc.spec.max_replicas}</td>
          <td class="muted truncate">${escapeHtml(svc.spec.image)}</td>
        </tr>
      `;
    })
    .join('');
  els.servicesBody.innerHTML = rows || '<tr><td colspan="6" class="empty">No services</td></tr>';
  els.servicesCount.textContent = `${state.data.services.length}`;
  els.servicesBody.querySelectorAll('tr[data-service]').forEach((row) => {
    row.addEventListener('click', () => selectService(row.dataset.service));
  });
  if (state.selectedService && !state.data.services.some((svc) => svc.spec.name === state.selectedService)) {
    state.selectedService = '';
    state.serviceLogsOpen = false;
    els.serviceDetail.hidden = true;
  }
}

async function selectService(name) {
  state.selectedService = state.selectedService === name ? '' : name;
  state.serviceLogsOpen = false;
  renderServices();
  if (state.selectedService) {
    els.serviceDetail.hidden = false;
    await renderServiceDetail(name, els.serviceDetail);
  } else {
    els.serviceDetail.hidden = true;
  }
}

async function selectOverviewService(name) {
  state.selectedService = state.selectedService === name ? '' : name;
  renderServices();
  if (state.selectedService) {
    els.overviewServiceDetail.hidden = false;
    await renderServiceDetail(name, els.overviewServiceDetail, { compact: true, logs: false });
  } else {
    els.overviewServiceDetail.hidden = true;
  }
}

async function renderServiceDetail(name, target = els.serviceDetail, options = {}) {
  const svc = state.data.services.find((item) => item.spec.name === name);
  if (!svc) return;
  let diagnostics = null;
  try {
    diagnostics = await api(`/api/v1/services/${encodeURIComponent(name)}/diagnostics`);
  } catch (error) {
    diagnostics = null;
  }
  const placement = diagnostics?.node_placement || svc.replicas.map((replica) => ({
    replica_id: replica.replica_id,
    node_id: replica.node_id,
    status: replica.status,
    container_ip: replica.container_ip,
    routed: false,
    route_healthy: false,
  }));
  const showLogs = options.logs !== false;
  target.hidden = false;
  target.innerHTML = `
    <div class="detail-header">
      <div>
        <strong>${escapeHtml(name)}</strong>
        <span>${escapeHtml(svc.spec.image)}</span>
      </div>
      ${showLogs ? `<button class="secondary-button" data-log-service="${escapeAttr(name)}">Logs</button>` : ''}
    </div>
    <div class="metric-row">
      <span>Desired ${svc.spec.replicas}</span>
      <span>Max ${svc.spec.max_replicas}</span>
      <span>Actual ${diagnostics?.actual_replicas ?? '-'}</span>
      <span>Routeable ${diagnostics?.routeable_replicas ?? '-'}</span>
      <span>CPU ${svc.spec.cpu_request}</span>
      <span>Memory ${svc.spec.memory_request_mb} MB</span>
    </div>
    ${renderServiceMetrics(name)}
    ${renderWarningList(diagnostics?.warnings || [])}
    <table class="inner-table">
      <thead><tr><th>Replica</th><th>Node</th><th>Status</th><th>Route</th><th>Endpoint</th></tr></thead>
      <tbody>
        ${placement.map((replica) => `
          <tr>
            <td class="mono">${escapeHtml(shortId(replica.replica_id))}</td>
            <td>${escapeHtml(replica.node_id || '-')}</td>
            <td><span class="badge ${badgeClass(replica.status)}">${escapeHtml(replica.status || '-')}</span></td>
            <td>${replica.routed ? (replica.route_healthy ? 'healthy' : 'unhealthy') : 'missing'}</td>
            <td class="mono">${escapeHtml(replica.container_ip || '-')}</td>
          </tr>
        `).join('') || '<tr><td colspan="5" class="empty">No replicas</td></tr>'}
      </tbody>
    </table>
    <div id="service-logs" class="logs-panel" hidden></div>
  `;
  target.querySelector('[data-log-service]')?.addEventListener('click', () => loadServiceLogs(name));
}

async function loadServiceLogs(name) {
  const panel = document.getElementById('service-logs');
  if (!panel) return;
  state.serviceLogsOpen = true;
  panel.hidden = false;
  panel.textContent = 'Loading...';
  try {
    const data = await api(`/api/v1/services/${encodeURIComponent(name)}/logs?tail=160`);
    panel.innerHTML = (data.replicas || []).map((replica) => `
      <div class="log-block">
        <div class="log-title">${escapeHtml(shortId(replica.replica_id))} on ${escapeHtml(replica.node_id)}</div>
        <pre>${escapeHtml(reverseLines(replica.logs) || 'No logs')}</pre>
      </div>
    `).join('') || '<div class="empty">No replicas</div>';
  } catch (error) {
    panel.innerHTML = `<div class="error-text">Could not load logs: ${escapeHtml(error.message)}</div>`;
  }
}

function renderTraffic() {
  const bucketSeconds = state.data.timeseries?.bucket_seconds || 10;
  const buckets = continuousBuckets(state.data.timeseries?.buckets || [], bucketSeconds, 36);
  const totals = aggregateTraffic(buckets);
  els.trafficWindow.textContent = `${Math.round((buckets.length * bucketSeconds) / 60)} min`;
  els.trafficSummary.innerHTML = `
    <div><span>Requests</span><strong>${totals.requests}</strong></div>
    <div><span>Errors</span><strong>${totals.errors}</strong></div>
    <div><span>Error Rate</span><strong>${formatPercent(totals.errorRate)}</strong></div>
  `;
  els.trafficChart.innerHTML = renderTrafficTimeline(buckets);

  const perService = aggregateTrafficByService(buckets);
  els.trafficServices.innerHTML = Array.from(perService.entries())
    .sort((a, b) => b[1].requests - a[1].requests || a[0].localeCompare(b[0]))
    .map(([service, metrics]) => `
      <div class="traffic-service-row">
        <span class="mono">${escapeHtml(service)}</span>
        <span>${metrics.requests} req</span>
        <span>${metrics.errors} err</span>
        <span>${formatNumber(metrics.latencyMs)} ms</span>
      </div>
    `).join('') || '<div class="empty">No services with traffic</div>';
}

function renderServiceMetrics(serviceName) {
  const bucketSeconds = state.data.timeseries?.bucket_seconds || 10;
  const buckets = continuousBuckets(state.data.timeseries?.buckets || [], bucketSeconds, 36);
  const metrics = aggregateTrafficByService(buckets).get(serviceName) || {
    requests: 0,
    errors: 0,
    errorRate: 0,
    latencyMs: 0,
  };
  return `
    <div class="metric-row service-metrics">
      <span>Requests ${metrics.requests}</span>
      <span>Errors ${metrics.errors}</span>
      <span>Error rate ${formatPercent(metrics.errorRate)}</span>
      <span>Avg latency ${formatNumber(metrics.latencyMs)} ms</span>
    </div>
    <div class="service-traffic-panel" data-service-traffic-panel="${escapeAttr(serviceName)}">
      <div class="subheader">Service Traffic</div>
      ${renderTrafficTimeline(buckets, serviceName)}
    </div>
  `;
}

function refreshSelectedServiceTraffic() {
  const bucketSeconds = state.data.timeseries?.bucket_seconds || 10;
  const buckets = continuousBuckets(state.data.timeseries?.buckets || [], bucketSeconds, 36);
  document.querySelectorAll('[data-service-traffic-panel]').forEach((panel) => {
    const serviceName = panel.dataset.serviceTrafficPanel;
    panel.innerHTML = `
      <div class="subheader">Service Traffic</div>
      ${renderTrafficTimeline(buckets, serviceName)}
    `;
  });
}

function renderNodes() {
  els.nodesCount.textContent = `${state.data.nodes.length}`;
  els.nodeGrid.innerHTML = state.data.nodes
    .slice()
    .sort((a, b) => a.spec.node_id.localeCompare(b.spec.node_id))
    .map((node) => {
      const cpuPct = percent(node.usage.cpu_used, node.spec.cpu_capacity);
      const memPct = percent(node.usage.memory_used_mb, node.spec.memory_capacity_mb);
      const actualCpu = node.actual_usage || { cpu_used: 0, memory_used_mb: 0 };
      const actualCpuPct = percent(actualCpu.cpu_used, node.spec.cpu_capacity);
      const actualMemPct = percent(actualCpu.memory_used_mb, node.spec.memory_capacity_mb);
      return `
        <article class="node-card">
          <div class="node-card-head">
            <strong>${escapeHtml(node.spec.node_id)}</strong>
            <span>${node.replica_ids.length}</span>
          </div>
          ${bar('CPU', node.usage.cpu_used, actualCpu.cpu_used, node.spec.cpu_capacity, cpuPct, actualCpuPct)}
          ${bar('MEM', node.usage.memory_used_mb, actualCpu.memory_used_mb, node.spec.memory_capacity_mb, memPct, actualMemPct)}
          <div class="replica-list">${node.replica_ids.map((id) => `<span>${escapeHtml(shortId(id))}</span>`).join('')}</div>
        </article>
      `;
    })
    .join('');
}

function renderPlatform() {
  els.platformCount.textContent = `${state.data.platform.length}`;
  els.platformList.innerHTML = state.data.platform.map((service) => `
    <button class="platform-row ${state.selectedPlatform === service.service ? 'selected' : ''}" data-platform="${escapeAttr(service.service)}">
      <span class="mono">${escapeHtml(service.service)}</span>
      <span class="badge ${healthBadgeClass(service.health || service.status)}">${escapeHtml(service.health || service.status || '-')}</span>
    </button>
  `).join('');
  els.platformList.querySelectorAll('[data-platform]').forEach((button) => {
    button.addEventListener('click', () => selectPlatform(button.dataset.platform));
  });
  if (
    state.selectedPlatform &&
    !state.data.platform.some((service) => service.service === state.selectedPlatform)
  ) {
    state.selectedPlatform = '';
    state.platformLogsOpen = false;
    els.platformDetail.hidden = true;
  }
}

async function selectPlatform(name) {
  state.selectedPlatform = state.selectedPlatform === name ? '' : name;
  state.platformLogsOpen = false;
  renderPlatform();
  if (state.selectedPlatform) {
    els.platformDetail.hidden = false;
    await renderPlatformDetail(name);
  } else {
    els.platformDetail.hidden = true;
  }
}

async function renderPlatformDetail(name) {
  const service = state.data.platform.find((item) => item.service === name);
  if (!service) return;
  let activity = [];
  try {
    const data = await api(`/api/v1/platform/services/${encodeURIComponent(name)}/activity`);
    activity = data.activities || [];
  } catch (error) {
    activity = [];
  }
  els.platformDetail.hidden = false;
  els.platformDetail.innerHTML = `
    <div class="detail-header">
      <div>
        <strong>${escapeHtml(name)}</strong>
        <span>${escapeHtml(service.role || '')}</span>
      </div>
      <button class="secondary-button" data-platform-logs="${escapeAttr(name)}">Logs</button>
    </div>
    <div class="metric-row">
      <span>${escapeHtml(service.status || '-')}</span>
      <span>${escapeHtml(service.health || '-')}</span>
    </div>
    <ol class="event-log mini">
      ${activity.map((item) => `
        <li><span>${formatTime(item.timestamp)}</span><strong>${escapeHtml(item.kind)}</strong><em>${escapeHtml(item.summary)}</em></li>
      `).join('') || '<li class="empty">No recent activity</li>'}
    </ol>
    <div id="platform-logs" class="logs-panel" hidden></div>
  `;
  els.platformDetail.querySelector('[data-platform-logs]')?.addEventListener('click', () => loadPlatformLogs(name));
}

async function loadPlatformLogs(name) {
  const panel = document.getElementById('platform-logs');
  if (!panel) return;
  state.platformLogsOpen = true;
  panel.hidden = false;
  panel.textContent = 'Loading...';
  try {
    const data = await api(`/api/v1/platform/services/${encodeURIComponent(name)}/logs?tail=180`);
    panel.innerHTML = `
      <div class="log-block">
        <div class="log-title">${escapeHtml(data.container_name || name)}</div>
        <pre>${escapeHtml(reverseLines(data.logs) || 'No logs')}</pre>
      </div>
    `;
  } catch (error) {
    panel.innerHTML = `<div class="error-text">Could not load logs: ${escapeHtml(error.message)}</div>`;
  }
}

function renderEvents() {
  els.eventCount.textContent = `${state.data.events.length}`;
  els.eventLog.innerHTML = state.data.events.map((event) => `
    <li>
      <span>${formatTime(event.timestamp)}</span>
      <strong>${escapeHtml(event.kind)}</strong>
      <em>${escapeHtml(eventSummary(event))}</em>
    </li>
  `).join('');
}

function renderRouting() {
  const services = state.data.routing.services || [];
  const total = services.reduce((sum, service) => sum + service.endpoints.length, 0);
  els.routingCount.textContent = `${total}`;
  els.routingList.innerHTML = services.map((service) => `
    <section class="routing-service">
      <h3>${escapeHtml(service.service)}</h3>
      ${service.endpoints.map((endpoint) => `
        <div class="routing-endpoint">
          <span class="mono">${escapeHtml(shortId(endpoint.replica_id))}</span>
          <span>${escapeHtml(endpoint.host)}:${endpoint.port}</span>
          <span class="badge ${endpoint.healthy ? 'running' : 'failed'}">${endpoint.healthy ? 'healthy' : 'unhealthy'}</span>
        </div>
      `).join('') || '<div class="empty">No endpoints</div>'}
    </section>
  `).join('') || '<div class="empty">No routes</div>';
}

function renderTopology() {
  const svg = els.topologyCanvas;
  const graph = state.data.topology || { nodes: [], edges: [] };
  const width = svg.clientWidth || 900;
  const height = svg.clientHeight || 620;
  svg.setAttribute('viewBox', `0 0 ${width} ${height}`);
  const positions = layoutTopology(graph.nodes, width, height);
  const edgeMarkup = graph.edges.map((edge) => {
    const source = positions.get(edge.source);
    const target = positions.get(edge.target);
    if (!source || !target) return '';
    return `
      <g class="topology-edge-group" tabindex="0">
        <line class="topology-edge" data-source="${escapeAttr(edge.source)}" data-target="${escapeAttr(edge.target)}" x1="${source.x}" y1="${source.y}" x2="${target.x}" y2="${target.y}"></line>
        <line class="topology-edge-hitbox" x1="${source.x}" y1="${source.y}" x2="${target.x}" y2="${target.y}"></line>
        <text class="topology-edge-label" x="${(source.x + target.x) / 2}" y="${(source.y + target.y) / 2 - 8}">${escapeHtml(edge.label)}</text>
      </g>
    `;
  }).join('');
  const nodeMarkup = graph.nodes.map((node) => {
    const position = positions.get(node.id);
    if (!position) return '';
    return `
      <g class="topology-node ${escapeAttr(node.kind)}" data-node-id="${escapeAttr(node.id)}" tabindex="0" transform="translate(${position.x},${position.y})">
        <circle r="36"></circle>
        <text y="-4">${escapeHtml(shortLabel(node.label, 14))}</text>
        <text class="topology-node-kind" y="13">${escapeHtml(node.kind)}</text>
        <title>${escapeHtml(node.label)} (${escapeHtml(node.kind)})</title>
      </g>
    `;
  }).join('');
  svg.innerHTML = `${edgeMarkup}${nodeMarkup}`;
  bindTopologyHover(svg);
}

function bindTopologyHover(svg) {
  svg.querySelectorAll('.topology-node').forEach((node) => {
    node.addEventListener('mouseenter', () => setTopologyHighlight(svg, node.dataset.nodeId));
    node.addEventListener('mouseleave', () => setTopologyHighlight(svg, null));
    node.addEventListener('focus', () => setTopologyHighlight(svg, node.dataset.nodeId));
    node.addEventListener('blur', () => setTopologyHighlight(svg, null));
  });
}

function setTopologyHighlight(svg, nodeId) {
  svg.querySelectorAll('.topology-edge').forEach((edge) => {
    const connected = nodeId && (edge.dataset.source === nodeId || edge.dataset.target === nodeId);
    edge.classList.toggle('connected', Boolean(connected));
  });
  svg.querySelectorAll('.topology-node').forEach((node) => {
    node.classList.toggle('connected', Boolean(nodeId && node.dataset.nodeId === nodeId));
  });
}

function layoutTopology(nodes, width, height) {
  const groups = {
    external: sortedTopologyNodes(nodes, 'external'),
    platform: sortedTopologyNodes(nodes, 'platform'),
    datastore: sortedTopologyNodes(nodes, 'datastore'),
    service: sortedTopologyNodes(nodes, 'service'),
    replica: sortedTopologyNodes(nodes, 'replica'),
    node: sortedTopologyNodes(nodes, 'node'),
  };
  const columns = [
    ['external', 0.08],
    ['platform', 0.25],
    ['datastore', 0.42],
    ['service', 0.58],
    ['replica', 0.75],
    ['node', 0.92],
  ];
  const positions = new Map();
  columns.forEach(([kind, xFactor]) => {
    const items = groups[kind] || [];
    const gap = height / (items.length + 1 || 1);
    items.forEach((node, index) => {
      positions.set(node.id, { x: width * xFactor, y: gap * (index + 1) });
    });
  });
  return positions;
}

function sortedTopologyNodes(nodes, kind) {
  const order = topologyOrder(kind);
  return nodes
    .filter((node) => node.kind === kind)
    .slice()
    .sort((a, b) => {
      const aRank = order.get(a.id) ?? Number.MAX_SAFE_INTEGER;
      const bRank = order.get(b.id) ?? Number.MAX_SAFE_INTEGER;
      if (aRank !== bRank) return aRank - bRank;
      return a.id.localeCompare(b.id);
    });
}

function topologyOrder(kind) {
  if (kind === 'platform') {
    return new Map([
      ['control-plane', 0],
      ['autoscaler', 1],
      ['orchestrator', 2],
      ['proxy', 3],
      ['health-checker', 4],
    ]);
  }
  if (kind === 'datastore') {
    return new Map([
      ['storage', 0],
      ['postgres', 1],
      ['redis-routing', 2],
      ['redis-metrics', 3],
    ]);
  }
  return new Map();
}

function routingStatsByService() {
  const stats = new Map();
  (state.data.routing.services || []).forEach((service) => {
    stats.set(service.service, {
      total: service.endpoints.length,
      routeable: service.endpoints.filter((endpoint) => endpoint.healthy).length,
    });
  });
  return stats;
}

function aggregateTraffic(buckets) {
  const requests = buckets.reduce((sum, bucket) => sum + (bucket.requests_total || 0), 0);
  const errors = buckets.reduce((sum, bucket) => sum + (bucket.errors_total || 0), 0);
  return {
    requests,
    errors,
    errorRate: requests ? errors / requests : 0,
  };
}

function continuousBuckets(rawBuckets, bucketSeconds, count) {
  const safeBucketSeconds = Number(bucketSeconds || 10);
  const currentBucket = Math.floor(Date.now() / 1000 / safeBucketSeconds) * safeBucketSeconds;
  const byBucket = new Map((rawBuckets || []).map((bucket) => [Number(bucket.bucket), bucket]));
  return Array.from({ length: count }, (_, index) => {
    const bucket = currentBucket - (count - index - 1) * safeBucketSeconds;
    return normalizeTrafficBucket(bucket, byBucket.get(bucket));
  });
}

function normalizeTrafficBucket(bucket, value) {
  return {
    bucket,
    requests_total: value?.requests_total || 0,
    errors_total: value?.errors_total || 0,
    requests_by_service: value?.requests_by_service || {},
    errors_by_service: value?.errors_by_service || {},
    responses_by_status: value?.responses_by_status || {},
    avg_latency_ms_by_service: value?.avg_latency_ms_by_service || {},
  };
}

function renderTrafficTimeline(buckets, serviceName = null) {
  const width = 720;
  const height = 180;
  const padding = { top: 16, right: 16, bottom: 28, left: 38 };
  const values = buckets.map((bucket) => trafficBucketValues(bucket, serviceName));
  const maxValue = Math.max(1, ...values.map((item) => Math.max(item.requests, item.errors)));
  const plotWidth = width - padding.left - padding.right;
  const plotHeight = height - padding.top - padding.bottom;
  const xFor = (index) => padding.left + (values.length <= 1 ? 0 : (index / (values.length - 1)) * plotWidth);
  const yFor = (value) => padding.top + plotHeight - (value / maxValue) * plotHeight;
  const requestPoints = values.map((item, index) => `${xFor(index)},${yFor(item.requests)}`).join(' ');
  const errorPoints = values.map((item, index) => `${xFor(index)},${yFor(item.errors)}`).join(' ');
  const slots = values.map((item, index) => {
    const x = xFor(index);
    return `<line class="timeline-slot" x1="${x}" y1="${padding.top}" x2="${x}" y2="${padding.top + plotHeight}"></line>`;
  }).join('');
  const points = values.map((item, index) => {
    const x = xFor(index);
    return `
      <circle class="timeline-point requests" cx="${x}" cy="${yFor(item.requests)}" r="3">
        <title>${formatTime(item.bucket * 1000)} requests=${item.requests} errors=${item.errors}</title>
      </circle>
      <circle class="timeline-point errors" cx="${x}" cy="${yFor(item.errors)}" r="3">
        <title>${formatTime(item.bucket * 1000)} requests=${item.requests} errors=${item.errors}</title>
      </circle>
    `;
  }).join('');
  return `
    <div class="traffic-timeline">
      <svg viewBox="0 0 ${width} ${height}" role="img" aria-label="Traffic timeline">
        <line class="timeline-axis" x1="${padding.left}" y1="${padding.top + plotHeight}" x2="${width - padding.right}" y2="${padding.top + plotHeight}"></line>
        <line class="timeline-axis" x1="${padding.left}" y1="${padding.top}" x2="${padding.left}" y2="${padding.top + plotHeight}"></line>
        ${slots}
        <polyline class="timeline-line requests" points="${requestPoints}"></polyline>
        <polyline class="timeline-line errors" points="${errorPoints}"></polyline>
        ${points}
        <text class="timeline-label" x="${padding.left}" y="12">${maxValue}/bucket</text>
        <text class="timeline-label" x="${padding.left}" y="${height - 7}">${formatTime(values[0]?.bucket * 1000)}</text>
        <text class="timeline-label end" x="${width - padding.right}" y="${height - 7}">${formatTime(values.at(-1)?.bucket * 1000)}</text>
      </svg>
      <div class="timeline-legend">
        <span><i class="legend-requests"></i>Requests</span>
        <span><i class="legend-errors"></i>Errors</span>
      </div>
    </div>
  `;
}

function trafficBucketValues(bucket, serviceName) {
  if (!serviceName) {
    return {
      bucket: bucket.bucket,
      requests: bucket.requests_total || 0,
      errors: bucket.errors_total || 0,
    };
  }
  return {
    bucket: bucket.bucket,
    requests: (bucket.requests_by_service || {})[serviceName] || 0,
    errors: (bucket.errors_by_service || {})[serviceName] || 0,
  };
}

function aggregateTrafficByService(buckets) {
  const perService = new Map();
  buckets.forEach((bucket) => {
    const requests = bucket.requests_by_service || {};
    const errors = bucket.errors_by_service || {};
    const latency = bucket.avg_latency_ms_by_service || {};
    const services = new Set([...Object.keys(requests), ...Object.keys(errors), ...Object.keys(latency)]);
    services.forEach((service) => {
      const current = perService.get(service) || {
        requests: 0,
        errors: 0,
        latencyTotal: 0,
        latencySamples: 0,
        latencyMs: 0,
        errorRate: 0,
      };
      const requestCount = requests[service] || 0;
      current.requests += requestCount;
      current.errors += errors[service] || 0;
      if (latency[service]) {
        current.latencyTotal += latency[service] * Math.max(1, requestCount);
        current.latencySamples += Math.max(1, requestCount);
      }
      current.errorRate = current.requests ? current.errors / current.requests : 0;
      current.latencyMs = current.latencySamples ? current.latencyTotal / current.latencySamples : 0;
      perService.set(service, current);
    });
  });
  return perService;
}

function bar(label, reserved, actual, capacity, reservedPct, actualPct) {
  return `
    <div class="bar-row">
      <span>${label}</span>
      <span>actual ${formatNumber(actual)} / reserved ${formatNumber(reserved)} / ${formatNumber(capacity)}</span>
    </div>
    <div class="bar-track layered">
      <div class="bar-fill reserved ${reservedPct > 80 ? 'danger' : reservedPct > 60 ? 'warn' : ''}" style="width:${reservedPct}%"></div>
      <div class="bar-fill actual ${actualPct > 80 ? 'danger' : actualPct > 60 ? 'warn' : ''}" style="width:${actualPct}%"></div>
    </div>
  `;
}

function renderWarningList(warnings) {
  if (!warnings.length) return '';
  return `<div class="warning-list">${warnings.map((warning) => `<div>${escapeHtml(warning)}</div>`).join('')}</div>`;
}

function eventSummary(event) {
  const payload = event.payload || {};
  if (payload.service_name) return `${payload.service_name} ${payload.node_id || payload.reason || ''}`.trim();
  if (payload.node_id) return payload.node_id;
  if (payload.bucket_name) return payload.object_key ? `${payload.bucket_name}/${payload.object_key}` : payload.bucket_name;
  return JSON.stringify(payload).slice(0, 90);
}

function percent(used, capacity) {
  if (!capacity) return 0;
  return Math.max(0, Math.min(100, (Number(used || 0) / Number(capacity)) * 100));
}

function formatNumber(value) {
  const number = Number(value || 0);
  return Number.isInteger(number) ? String(number) : number.toFixed(1);
}

function formatTime(value) {
  if (!value) return '-';
  return new Date(value).toLocaleTimeString();
}

function formatPercent(value) {
  return `${(Number(value || 0) * 100).toFixed(1)}%`;
}

function badgeClass(status) {
  if (status === 'running') return 'running';
  if (status === 'degraded') return 'degraded';
  if (status === 'pending') return 'pending';
  if (status === 'updating') return 'updating';
  if (status === 'failed') return 'failed';
  return 'stopped';
}

function healthBadgeClass(value) {
  if (value === 'healthy' || value === 'running') return 'running';
  if (value === 'starting') return 'pending';
  if (value === 'unhealthy' || value === 'failed') return 'failed';
  return 'stopped';
}

function statusPriority(status) {
  if (status === 'failed') return 0;
  if (status === 'degraded') return 1;
  if (status === 'pending' || status === 'updating') return 2;
  if (status === 'running') return 3;
  return 4;
}

function shortId(value) {
  return String(value || '').slice(0, 8);
}

function shortLabel(value, maxLength = 12) {
  const text = String(value || '');
  return text.length > maxLength ? text.slice(0, maxLength - 1) : text;
}

function reverseLines(value) {
  return String(value || '').split('\n').filter(Boolean).reverse().join('\n');
}

function escapeHtml(value) {
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#039;');
}

function escapeAttr(value) {
  return escapeHtml(value).replaceAll('`', '&#096;');
}

if (state.token) {
  showApp();
  refreshAll();
} else {
  showLogin();
}

setInterval(refreshAll, 4000);
window.addEventListener('resize', () => {
  if (state.currentView === 'topology') renderTopology();
});
