'use strict';

// ── DOM refs ─────────────────────────────────────────────────────────────────
const nodeGrid        = document.getElementById('node-grid');
const servicesBody    = document.getElementById('services-body');
const detailPanel     = document.getElementById('detail-panel');
const secretsList     = document.getElementById('secrets-list');
const bucketsList     = document.getElementById('buckets-list');
const eventLog        = document.getElementById('event-log');
const connectionStatus= document.getElementById('connection-status');

// ── State ─────────────────────────────────────────────────────────────────────
const nodeCards   = {};  // node_id  -> <div> card element
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

  (snapshot.nodes    || []).forEach(renderNode);
  (snapshot.services || []).forEach(svc => { serviceData[svc.spec.name] = svc; renderService(svc); });
  (snapshot.secrets  || []).forEach(renderSecret);
  (snapshot.buckets  || []).forEach(renderBucket);
  (snapshot.events   || []).slice(-50).forEach(appendEvent);
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
      <td>:${r.host_port}</td>
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
      <thead><tr><th>ID</th><th>Node</th><th>Port</th><th>Status</th></tr></thead>
      <tbody>${replicaRows || '<tr><td colspan="4" class="muted small" style="padding:10px 14px">No replicas yet</td></tr>'}</tbody>
    </table>
  `;
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
          host_port:  payload.host_port,
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
      return `${payload.service_name} r/${payload.replica_id?.slice(0, 8)} on ${payload.node_id} :${payload.host_port}`;
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
