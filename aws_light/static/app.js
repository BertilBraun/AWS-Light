'use strict';

// ── DOM refs ─────────────────────────────────────────────────────────────────
const nodeGrid        = document.getElementById('node-grid');
const platformBody    = document.getElementById('platform-body');
const platformDetailPanel = document.getElementById('platform-detail-panel');
const servicesBody    = document.getElementById('services-body');
const detailPanel     = document.getElementById('detail-panel');
const secretsList     = document.getElementById('secrets-list');
const bucketsList     = document.getElementById('buckets-list');
const eventLog        = document.getElementById('event-log');
const connectionStatus= document.getElementById('connection-status');

// ── State ─────────────────────────────────────────────────────────────────────
const nodeCards   = {};  // node_id  -> <div> card element
const platformRows = {}; // platform service -> <tr> element
const platformData = {}; // platform service -> service object
const serviceRows = {};  // svc name -> <tr> element
const serviceData = {};  // svc name -> full service object (from snapshot/events)
let selectedService = null;

// ── WebSocket ─────────────────────────────────────────────────────────────────
function connect() {
  const ws = new WebSocket(`ws://${location.host}/ws`);

  ws.onopen = () => {
    connectionStatus.textContent = 'connected';
    connectionStatus.className = 'connected';
  };

  ws.onclose = () => {
    connectionStatus.textContent = 'reconnecting…';
    connectionStatus.className = 'disconnected';
    setTimeout(connect, 2000);
  };

  ws.onmessage = (msg) => {
    const data = JSON.parse(msg.data);
    if (data.kind === 'snapshot') applySnapshot(data);
    else if (data.kind !== 'ping') handleEvent(data);
  };
}

// ── Snapshot ──────────────────────────────────────────────────────────────────
function applySnapshot(snapshot) {
  nodeGrid.innerHTML     = '';
  servicesBody.innerHTML = '';
  secretsList.innerHTML  = '';
  bucketsList.innerHTML  = '';
  eventLog.innerHTML     = '';
  detailPanel.hidden     = true;
  selectedService        = null;

  Object.keys(nodeCards).forEach(k   => delete nodeCards[k]);
  Object.keys(serviceRows).forEach(k => delete serviceRows[k]);
  Object.keys(serviceData).forEach(k => delete serviceData[k]);

  (snapshot.nodes    || [])
    .slice()
    .sort((a, b) => a.spec.node_id.localeCompare(b.spec.node_id))
    .forEach(renderNode);
  (snapshot.services || []).forEach(svc => { serviceData[svc.spec.name] = svc; renderService(svc); });
  (snapshot.secrets  || []).forEach(renderSecret);
  (snapshot.buckets  || []).forEach(renderBucket);
  (snapshot.events   || []).slice(-50).forEach(appendEvent);
}

// Platform services -----------------------------------------------------------
async function loadPlatformServices() {
  try {
    const response = await fetch('/api/v1/platform/services');
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    renderPlatformServices(await response.json());
  } catch (error) {
    platformBody.innerHTML = `
      <tr><td colspan="5" class="muted small">Could not load platform services: ${escapeHtml(error.message)}</td></tr>
    `;
  }
}

function renderPlatformServices(services) {
  platformBody.innerHTML = '';
  Object.keys(platformRows).forEach(k => delete platformRows[k]);
  Object.keys(platformData).forEach(k => delete platformData[k]);

  services.forEach(service => {
    platformData[service.service] = service;
    const row = document.createElement('tr');
    row.className = 'platform-row';
    row.innerHTML = `
      <td class="mono">${escapeHtml(service.service)}</td>
      <td><span class="badge ${statusClass(service.status)}">${escapeHtml(service.status || 'unknown')}</span></td>
      <td><span class="badge ${healthClass(service.health)}">${escapeHtml(service.health || '-')}</span></td>
      <td class="muted small">${escapeHtml(service.role || '')}</td>
      <td class="muted small">${escapeHtml((service.ports || []).join(', ') || '-')}</td>
    `;
    row.addEventListener('click', () => showPlatformDetail(service.service));
    platformBody.appendChild(row);
    platformRows[service.service] = row;
  });
}

function showPlatformDetail(serviceName) {
  const service = platformData[serviceName];
  if (!service) return;
  Object.values(platformRows).forEach(r => r.classList.remove('selected'));
  platformRows[serviceName]?.classList.add('selected');
  platformDetailPanel.hidden = false;
  platformDetailPanel.innerHTML = `
    <div class="detail-header">
      <strong>${escapeHtml(service.service)}</strong>
      <span class="muted small">${escapeHtml(service.container_name || '')}</span>
      <button class="logs-btn" onclick="loadPlatformActivity('${service.service}')">Activity</button>
      <button class="logs-btn" onclick="loadPlatformLogs('${service.service}')">Logs</button>
      <button class="close-btn" onclick="closePlatformDetail()">&#x2715;</button>
    </div>
    <div class="detail-meta">
      <span>Status: ${escapeHtml(service.status || 'unknown')}</span>
      <span>Health: ${escapeHtml(service.health || '-')}</span>
      <span>Image: ${escapeHtml(service.image || '-')}</span>
    </div>
    <div id="platform-logs-panel" class="logs-panel" hidden></div>
  `;
}

function closePlatformDetail() {
  platformDetailPanel.hidden = true;
  Object.values(platformRows).forEach(r => r.classList.remove('selected'));
}

window.closePlatformDetail = closePlatformDetail;

async function loadPlatformActivity(serviceName) {
  const panel = document.getElementById('platform-logs-panel');
  if (!panel) return;
  panel.hidden = false;
  panel.innerHTML = '<div class="logs-loading">Loading activity...</div>';

  try {
    const response = await fetch(`/api/v1/platform/services/${encodeURIComponent(serviceName)}/activity`);
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    const activities = data.activities || [];
    const rows = activities.map(activity => `
      <li>
        <span class="event-time">${new Date(activity.timestamp).toLocaleTimeString()}</span>
        <span class="event-kind">${escapeHtml(activity.kind)}</span>
        <span class="event-detail">${escapeHtml(activity.summary)}</span>
      </li>
    `).join('');
    panel.innerHTML = `
      <div class="log-block">
        <div class="log-title">${escapeHtml(serviceName)} activity</div>
        <ul class="activity-list">${rows || '<li class="logs-loading">No recent activity.</li>'}</ul>
      </div>
    `;
  } catch (error) {
    panel.innerHTML = `<div class="logs-error">Could not load activity: ${escapeHtml(error.message)}</div>`;
  }
}

window.loadPlatformActivity = loadPlatformActivity;

async function loadPlatformLogs(serviceName) {
  const panel = document.getElementById('platform-logs-panel');
  if (!panel) return;
  panel.hidden = false;
  panel.innerHTML = '<div class="logs-loading">Loading logs...</div>';

  try {
    const response = await fetch(`/api/v1/platform/services/${encodeURIComponent(serviceName)}/logs?tail=160`);
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    panel.innerHTML = `
      <div class="log-block">
        <div class="log-title">${escapeHtml(data.container_name || serviceName)}</div>
        <pre>${escapeHtml(reverseLogLines(data.logs) || 'No logs yet.')}</pre>
      </div>
    `;
  } catch (error) {
    panel.innerHTML = `<div class="logs-error">Could not load logs: ${escapeHtml(error.message)}</div>`;
  }
}

window.loadPlatformLogs = loadPlatformLogs;

function statusClass(status) {
  return status === 'running' ? 'running' : 'stopped';
}

function healthClass(health) {
  if (health === 'healthy') return 'running';
  if (health === 'unhealthy') return 'failed';
  if (health === 'starting') return 'pending';
  return 'stopped';
}

// ── Nodes ─────────────────────────────────────────────────────────────────────
function renderNode(node) {
  const card = document.createElement('div');
  card.className = 'node-card';
  card.dataset.cpuCapacity = node.spec.cpu_capacity;
  card.dataset.memCapacity = node.spec.memory_capacity_mb;
  card.innerHTML = `
    <div class="node-id">${node.spec.node_id}</div>
    <div class="bar-label">CPU</div>
    <div class="bar-track"><div class="bar-fill cpu-bar" style="width:0%"></div></div>
    <div class="bar-label">MEM</div>
    <div class="bar-track"><div class="bar-fill mem-bar" style="width:0%"></div></div>
    <div class="replica-count">0 replicas</div>
  `;
  nodeGrid.appendChild(card);
  nodeCards[node.spec.node_id] = card;
  applyNodeUsage(card, node.usage.cpu_used, node.usage.memory_used_mb, node.replica_ids.length);
}

function applyNodeUsage(card, cpuUsed, memUsed, replicaCount) {
  const cpuCapacity = parseFloat(card.dataset.cpuCapacity) || 1;
  const memCapacity = parseFloat(card.dataset.memCapacity) || 1;
  const cpuPct = Math.min(100, (cpuUsed / cpuCapacity) * 100);
  const memPct = Math.min(100, (memUsed  / memCapacity) * 100);

  const cpuBar = card.querySelector('.cpu-bar');
  cpuBar.style.width = `${cpuPct.toFixed(1)}%`;
  cpuBar.className   = `bar-fill cpu-bar${cpuPct > 80 ? ' danger' : cpuPct > 60 ? ' warn' : ''}`;

  const memBar = card.querySelector('.mem-bar');
  memBar.style.width = `${memPct.toFixed(1)}%`;
  memBar.className   = `bar-fill mem-bar${memPct > 80 ? ' danger' : memPct > 60 ? ' warn' : ''}`;

  card.querySelector('.replica-count').textContent =
    `${replicaCount} replica${replicaCount !== 1 ? 's' : ''}`;
}

// ── Services ──────────────────────────────────────────────────────────────────
function renderService(svc) {
  const name = svc.spec.name;
  const row  = document.createElement('tr');
  row.className = 'service-row';
  row.innerHTML = buildServiceRowHtml(svc);
  row.addEventListener('click', () => toggleDetail(name));
  servicesBody.appendChild(row);
  serviceRows[name] = row;
}

function buildServiceRowHtml(svc) {
  const running = svc.replicas.filter(r => r.status === 'running').length;
  return `
    <td>${svc.spec.name}</td>
    <td class="muted small">${svc.spec.image}</td>
    <td><span class="badge ${svc.status}">${svc.status}</span></td>
    <td>${running} / ${svc.spec.replicas}</td>
  `;
}

function refreshServiceRow(name) {
  const svc = serviceData[name];
  const row = serviceRows[name];
  if (!svc || !row) return;
  row.innerHTML = buildServiceRowHtml(svc);
  row.addEventListener('click', () => toggleDetail(name));
  if (selectedService === name) {
    row.classList.add('selected');
    renderDetailPanel(svc);
  }
}

// ── Service detail panel ──────────────────────────────────────────────────────
function toggleDetail(name) {
  if (selectedService === name) {
    closeDetail();
    return;
  }
  selectedService = name;
  Object.values(serviceRows).forEach(r => r.classList.remove('selected'));
  serviceRows[name]?.classList.add('selected');
  renderDetailPanel(serviceData[name]);
  detailPanel.hidden = false;
}

function closeDetail() {
  selectedService = null;
  detailPanel.hidden = true;
  Object.values(serviceRows).forEach(r => r.classList.remove('selected'));
}

window.closeDetail = closeDetail;

function renderDetailPanel(svc) {
  if (!svc) return;
  const replicaRows = svc.replicas.map(r => `
    <tr>
      <td class="mono">${r.replica_id.slice(0, 8)}</td>
      <td>${r.node_id}</td>
      <td class="mono">${r.container_ip || '-'}:${svc.spec.port}</td>
      <td><span class="badge ${r.status}">${r.status}</span></td>
    </tr>
  `).join('');

  const secretPill = svc.spec.secret_refs?.length
    ? `<span>Secrets: ${svc.spec.secret_refs.join(', ')}</span>`
    : '';

  detailPanel.innerHTML = `
    <div class="detail-header">
      <strong>${svc.spec.name}</strong>
      <span class="muted small">${svc.spec.image}</span>
      <button class="logs-btn" onclick="loadServiceLogs('${svc.spec.name}')">Logs</button>
      <button class="close-btn" onclick="closeDetail()">&#x2715;</button>
    </div>
    <div class="detail-meta">
      <span>CPU req: ${svc.spec.cpu_request}</span>
      <span>Mem req: ${svc.spec.memory_request_mb} MB</span>
      <span>Port: ${svc.spec.port}</span>
      <span>Health: ${svc.spec.health_check_path}</span>
      ${secretPill}
    </div>
    <table class="replica-table">
      <thead><tr><th>ID</th><th>Node</th><th>Endpoint</th><th>Status</th></tr></thead>
      <tbody>${replicaRows || '<tr><td colspan="4" class="muted small" style="padding:10px 14px">No replicas yet</td></tr>'}</tbody>
    </table>
    <div id="logs-panel" class="logs-panel" hidden></div>
  `;
}

async function loadServiceLogs(serviceName) {
  const panel = document.getElementById('logs-panel');
  if (!panel) return;

  panel.hidden = false;
  panel.innerHTML = '<div class="logs-loading">Loading logs...</div>';

  try {
    const response = await fetch(`/api/v1/services/${encodeURIComponent(serviceName)}/logs?tail=120`);
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    const blocks = (data.replicas || []).map(replica => `
      <div class="log-block">
        <div class="log-title">replica ${escapeHtml(replica.replica_id.slice(0, 8))} · ${escapeHtml(replica.node_id)}</div>
        <pre>${escapeHtml(reverseLogLines(replica.logs) || 'No logs yet.')}</pre>
      </div>
    `).join('');
    panel.innerHTML = blocks || '<div class="logs-loading">No replicas.</div>';
  } catch (error) {
    panel.innerHTML = `<div class="logs-error">Could not load logs: ${escapeHtml(error.message)}</div>`;
  }
}

window.loadServiceLogs = loadServiceLogs;

function reverseLogLines(logs) {
  return String(logs || '')
    .split('\n')
    .filter(line => line.length > 0)
    .reverse()
    .join('\n');
}

function escapeHtml(value) {
  return String(value)
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#039;');
}

// ── Secrets ───────────────────────────────────────────────────────────────────
function renderSecret(name) {
  const item = document.createElement('li');
  item.className = 'resource-item';
  item.dataset.secretName = name;
  item.innerHTML = `<span class="mono">${name}</span><span class="lock-icon">&#x1F512;</span>`;
  secretsList.appendChild(item);
}

// ── Buckets ───────────────────────────────────────────────────────────────────
function renderBucket(bucket) {
  const item = document.createElement('li');
  item.className = 'resource-item';
  item.dataset.bucketName = bucket.name;
  item.innerHTML = `<span class="mono">${bucket.name}</span>`;
  bucketsList.appendChild(item);
}

// ── Event handling ────────────────────────────────────────────────────────────
function handleEvent(event) {
  appendEvent(event);
  const payload = event.payload || {};

  switch (event.kind) {
    case 'service.updated': {
      const svc = payload.service;
      if (!svc) break;
      const name = svc.spec.name;
      serviceData[name] = svc;
      if (serviceRows[name]) {
        refreshServiceRow(name);
      } else {
        renderService(svc);
      }
      break;
    }

    case 'replica.started': {
      const svc = serviceData[payload.service_name];
      if (!svc) break;
      const alreadyPresent = svc.replicas.some(r => r.replica_id === payload.replica_id);
      if (!alreadyPresent) {
        svc.replicas.push({
          replica_id: payload.replica_id,
          node_id:    payload.node_id,
          container_ip: payload.container_ip,
          status:     'running',
          container_id: '',
          cpu_percent:  0,
          memory_mb:    0,
          started_at:   new Date().toISOString(),
        });
      }
      refreshServiceRow(payload.service_name);
      break;
    }

    case 'replica.stopped': {
      const svc = serviceData[payload.service_name];
      if (!svc) break;
      svc.replicas = svc.replicas.filter(r => r.replica_id !== payload.replica_id);
      refreshServiceRow(payload.service_name);
      break;
    }

    case 'node.updated': {
      const card = nodeCards[payload.node_id];
      if (card) {
        applyNodeUsage(card, payload.cpu_used ?? 0, payload.memory_used_mb ?? 0, payload.replica_count ?? 0);
      }
      break;
    }

    case 'secret.created': {
      const name = payload.secret_name;
      if (name && !document.querySelector(`[data-secret-name="${name}"]`)) {
        renderSecret(name);
      }
      break;
    }

    case 'bucket.created': {
      const name = payload.bucket_name;
      if (name && !document.querySelector(`[data-bucket-name="${name}"]`)) {
        renderBucket({ name });
      }
      break;
    }
  }
}

// ── Event log ─────────────────────────────────────────────────────────────────
function appendEvent(event) {
  const time   = new Date(event.timestamp).toLocaleTimeString();
  const payload= event.payload || {};

  const item = document.createElement('li');
  item.innerHTML = `
    <span class="event-time">${time}</span>
    <span class="event-kind">${event.kind}</span>
    <span class="event-detail">${summarisePayload(event.kind, payload)}</span>
  `;
  eventLog.prepend(item);
  while (eventLog.children.length > 100) eventLog.removeChild(eventLog.lastChild);
}

function summarisePayload(kind, payload) {
  switch (kind) {
    case 'service.updated':
      return `${payload.service_name} → ${payload.status} (${payload.replica_count} replicas)`;
    case 'replica.started':
      return `${payload.service_name} r/${payload.replica_id?.slice(0, 8)} on ${payload.node_id} ${payload.container_ip || ''}`;
    case 'replica.stopped':
      return `${payload.service_name} r/${payload.replica_id?.slice(0, 8)}`;
    case 'replica.failed':
      return `${payload.service_name} r/${payload.replica_id?.slice(0, 8)} — ${payload.error}`;
    case 'node.updated':
      return `${payload.node_id} cpu=${payload.cpu_used?.toFixed(2)} mem=${payload.memory_used_mb?.toFixed(0)}MB`;
    case 'autoscale.triggered':
      return `${payload.service_name} ${payload.from_replicas}→${payload.to_replicas} (${payload.reason})`;
    case 'rollout.progress':
      return `${payload.service_name} step ${payload.step}/${payload.total_steps}`;
    case 'health_check.failed':
      return `${payload.service_name} r/${payload.replica_id?.slice(0, 8)} failures=${payload.consecutive_failures}`;
    case 'secret.created':
      return payload.secret_name;
    case 'bucket.created':
      return payload.bucket_name;
    case 'object.uploaded':
      return `${payload.bucket_name}/${payload.object_key} (${payload.size_bytes}b)`;
    default:
      return JSON.stringify(payload).slice(0, 80);
  }
}

connect();
loadPlatformServices();
setInterval(loadPlatformServices, 5000);
