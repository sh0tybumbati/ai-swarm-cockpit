// ══════════════════════════════════════════════════════
//  PANOPTIC AI SWARM COCKPIT — Frontend Client v1.2
// ══════════════════════════════════════════════════════

const WS_BASE  = `ws://${location.host}`;
const API_BASE = `http://${location.host}`;
const CHANNELS = ['agent1', 'agent2', 'agent3', 'agent4', 'console'];

const sockets = {};
let currentModalAgent = null;
let currentProject    = 'ENMERKAR';
let savedFilesCount   = 0;
let loopRunning       = false;
let autoRefresh       = false;
let agent4Backend     = 'gpu';   // 'gpu' | 'npu'
let modalBackend      = 'gpu';   // tracks selection inside modal

// ── Command history ────────────────────────────────────
let cmdHistory  = JSON.parse(localStorage.getItem('swarm_cmd_history') || '[]');
let historyIdx  = -1;

// ── WebSocket manager ──────────────────────────────────

function connect(channel) {
  const ws = new WebSocket(`${WS_BASE}/ws/${channel}`);

  ws.onopen = () => {
    sysLog(`[WS] /${channel} connected`);
    if (channel !== 'console') setStatus(channel, 'IDLE', '');
  };

  ws.onmessage = ({ data }) => {
    let msg;
    try { msg = JSON.parse(data); } catch { msg = { type: 'token', content: data }; }
    handleMsg(channel, msg);
  };

  ws.onclose = () => {
    sysLog(`[WS] /${channel} disconnected — retrying in 3s`, 'warn');
    if (channel !== 'console') setStatus(channel, 'OFFLINE', 'error');
    setTimeout(() => { sockets[channel] = connect(channel); }, 3000);
  };

  ws.onerror = () => ws.close();
  return ws;
}

function handleMsg(channel, msg) {
  switch (msg.type) {
    case 'token':
      appendTokens(channel, msg.content ?? '');
      break;
    case 'status':
      if (channel !== 'console')
        setStatus(channel, msg.status,
          msg.status === 'WORKING' ? 'active' : msg.status === 'ERROR' ? 'error' : '');
      break;
    case 'loop_update':
      document.getElementById('loop-counter').textContent =
        `LOOP: ${msg.iteration}/${msg.max}`;
      break;
    case 'loop_state':
      setLoopRunning(msg.running);
      break;
    case 'project':
      document.getElementById('project-name').textContent = msg.name;
      currentProject = msg.name;
      break;
    case 'console_line':
      appendConsoleLine(msg.message, msg.level || 'log');
      break;
    case 'backend':
      if (channel === 'agent4') updateBackendBadge(msg.backend);
      break;
    case 'file_saved':
      onFileSaved(msg);
      break;
    case 'clear':
      clearStream(channel);
      break;
    case 'ping':
      sockets[channel]?.send('pong');
      break;
  }
}

// ── Terminal rendering ─────────────────────────────────

const cursors = {};

function appendTokens(channel, text) {
  if (!text) return;
  const el = document.getElementById(`stream-${channel}`);
  if (!el) return;
  const parts = text.split('\n');
  parts.forEach((chunk, i) => {
    if (!cursors[channel] || i > 0) {
      cursors[channel] = document.createElement('span');
      cursors[channel].className = 'tok';
      el.appendChild(cursors[channel]);
    }
    cursors[channel].textContent += chunk;
    if (i < parts.length - 1) {
      el.appendChild(document.createElement('br'));
      cursors[channel] = null;
    }
  });
  el.scrollTop = el.scrollHeight;
}

function clearStream(channel) {
  const el = document.getElementById(`stream-${channel}`);
  if (el) el.innerHTML = '';
  delete cursors[channel];
}

function sysLog(msg, level = 'sys') {
  appendConsoleLine(msg, level);
}

function appendConsoleLine(msg, level) {
  const el = document.getElementById('stream-console');
  if (!el) return;
  const ts   = new Date().toTimeString().slice(0, 8);
  const span = document.createElement('span');
  span.className = `tok line-${level}`;
  span.textContent = `[${ts}] ${msg}`;
  el.appendChild(span);
  el.appendChild(document.createElement('br'));
  el.scrollTop = el.scrollHeight;
}

// ── Agent status LEDs ──────────────────────────────────

function setStatus(agentId, text, ledClass) {
  const led = document.getElementById(`led-${agentId}`);
  const lbl = document.getElementById(`status-${agentId}`);
  if (!led || !lbl) return;
  led.className = 'led' + (ledClass ? ` ${ledClass}` : '');
  lbl.textContent = text;
}

// ── Loop running state ─────────────────────────────────

function setLoopRunning(running) {
  loopRunning = running;
  const sendBtn = document.getElementById('send-btn');
  const stopBtn = document.getElementById('stop-btn');
  sendBtn.disabled    = running;
  stopBtn.style.display = running ? 'inline-block' : 'none';
}

async function stopLoop() {
  try {
    await fetch(`${API_BASE}/stop`, { method: 'POST' });
    sysLog('[CMD] Stop signal sent', 'warn');
  } catch (err) {
    sysLog(`[CMD] Stop failed: ${err.message}`, 'error');
  }
}

// ── File saved notifications ───────────────────────────

function onFileSaved(msg) {
  savedFilesCount++;
  const badge = document.getElementById('files-badge');
  badge.textContent = savedFilesCount;
  badge.style.display = 'inline';
  const kb = msg.size ? ` (${(msg.size / 1024).toFixed(1)} KB)` : '';
  sysLog(`[FILE] Saved: ${msg.file}${kb}`, 'info');

  // Auto-refresh iframe for web assets
  const webExts = ['.html', '.js', '.ts', '.css'];
  if (autoRefresh && webExts.some(ext => msg.file.endsWith(ext))) {
    setTimeout(() => {
      const frame = document.getElementById('preview-frame');
      if (frame.src && frame.src !== 'about:blank') {
        frame.src = frame.src;
        sysLog(`[AUTO] Preview refreshed (${msg.file})`, 'info');
      }
    }, 600);
  }
}

// ── Auto-refresh toggle ────────────────────────────────

function toggleAutoRefresh() {
  autoRefresh = !autoRefresh;
  const btn = document.getElementById('auto-refresh-btn');
  btn.className = autoRefresh ? 'ar-on' : 'ar-off';
  btn.title = autoRefresh ? 'Auto-refresh ON — click to disable' : 'Auto-refresh OFF — click to enable';
  sysLog(`[AUTO] Preview auto-refresh: ${autoRefresh ? 'ON' : 'OFF'}`, 'sys');
}

// ── Iframe console injection ───────────────────────────

function loadPreview() {
  const url = document.getElementById('preview-url-input').value.trim();
  if (!url) return;
  document.getElementById('preview-frame').src = url;
  try { document.getElementById('host-url').textContent = new URL(url).host; } catch {}
}

document.getElementById('preview-frame').addEventListener('load', function () {
  injectConsoleRelay(this);
});

function injectConsoleRelay(iframe) {
  try {
    const win = iframe.contentWindow;
    if (!win?.document?.head) return;
    const script = win.document.createElement('script');
    script.textContent = `
(function(){
  if(window.__cockpitInjected) return;
  window.__cockpitInjected = true;
  function relay(lvl,args){
    const msg=Array.from(args).map(a=>{
      try{return typeof a==='object'?JSON.stringify(a,null,2):String(a);}catch{return String(a);}
    }).join(' ');
    window.parent.postMessage({type:'iframe-console',level:lvl,message:msg},'*');
  }
  ['log','warn','error','info','debug'].forEach(m=>{
    const orig=console[m].bind(console);
    console[m]=(...a)=>{relay(m,a);orig(...a);};
  });
  window.addEventListener('error',e=>{
    relay('error',[e.message+' @ '+e.filename+':'+e.lineno+':'+e.colno]);
  });
  window.addEventListener('unhandledrejection',e=>{
    relay('error',['Unhandled Promise: '+e.reason]);
  });
})();`;
    win.document.head.appendChild(script);
    sysLog('[FRAME] Console relay injected');
  } catch {
    sysLog('[FRAME] Cross-origin iframe — postMessage only', 'warn');
  }
}

window.addEventListener('message', (e) => {
  if (e.data?.type === 'iframe-console')
    appendConsoleLine(e.data.message, e.data.level || 'log');
});

// ── Command broadcast ──────────────────────────────────

async function sendCommand(event) {
  if (event) event.preventDefault();
  const prompt  = document.getElementById('cmd-input').value.trim();
  const project = document.getElementById('project-input').value.trim() || currentProject;
  if (!prompt || loopRunning) return;

  // Save to history
  if (prompt !== cmdHistory[cmdHistory.length - 1]) {
    cmdHistory.push(prompt);
    if (cmdHistory.length > 50) cmdHistory.shift();
    localStorage.setItem('swarm_cmd_history', JSON.stringify(cmdHistory));
  }
  historyIdx = -1;

  document.getElementById('cmd-input').value = '';
  CHANNELS.filter(c => c !== 'console').forEach(clearStream);
  savedFilesCount = 0;
  document.getElementById('files-badge').style.display = 'none';
  currentProject = project;
  sysLog(`[CMD] Broadcasting: "${prompt}"`);

  try {
    const res  = await fetch(`${API_BASE}/broadcast`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ prompt, project })
    });
    const data = await res.json();
    sysLog(`[CMD] ${data.status || data.error}`);
  } catch (err) {
    sysLog(`[CMD] Fetch error: ${err.message}`, 'error');
    setLoopRunning(false);
  }
}

// ── Per-agent fire ─────────────────────────────────────

async function fireAgent(agentId) {
  const prompt  = document.getElementById('cmd-input').value.trim();
  const project = document.getElementById('project-input').value.trim() || currentProject;
  if (!prompt) {
    sysLog(`[FIRE] Type a prompt in the command bar first`, 'warn');
    document.getElementById('cmd-input').focus();
    return;
  }
  if (loopRunning) {
    sysLog('[FIRE] Stop the current loop first', 'warn');
    return;
  }

  clearStream(agentId);
  sysLog(`[FIRE] ${agentId} solo: "${prompt.slice(0, 50)}"`);

  try {
    const res  = await fetch(`${API_BASE}/agents/${agentId}/run`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ prompt, project })
    });
    const data = await res.json();
    sysLog(`[FIRE] ${data.status || data.error}`);
  } catch (err) {
    sysLog(`[FIRE] Error: ${err.message}`, 'error');
  }
}

// ── Command history navigation ─────────────────────────

document.getElementById('cmd-input').addEventListener('keydown', (e) => {
  if (e.key === 'ArrowUp') {
    e.preventDefault();
    historyIdx = Math.min(historyIdx + 1, cmdHistory.length - 1);
    e.target.value = cmdHistory[cmdHistory.length - 1 - historyIdx] ?? '';
    // Move cursor to end
    setTimeout(() => e.target.setSelectionRange(9999, 9999), 0);
  } else if (e.key === 'ArrowDown') {
    e.preventDefault();
    historyIdx = Math.max(historyIdx - 1, -1);
    e.target.value = historyIdx === -1 ? '' : (cmdHistory[cmdHistory.length - 1 - historyIdx] ?? '');
  } else {
    historyIdx = -1;
  }
});

// ── Model status indicators ────────────────────────────

async function checkModelStatus() {
  const agents   = ['agent1', 'agent2', 'agent3', 'agent4'];
  const dotEls   = agents.map(id => document.getElementById(`mdot-${id}`));

  try {
    const res    = await fetch(`${API_BASE}/models`);
    const data   = await res.json();
    const models = new Set(data.models ?? []);

    // Also get configured models
    const agentRes  = await fetch(`${API_BASE}/agents`);
    const agentData = await agentRes.json();

    agents.forEach((id, i) => {
      const dot   = dotEls[i];
      if (!dot) return;
      const model = agentData[id]?.model ?? '';
      // Ollama model names can omit the :latest tag
      const found = models.has(model) ||
                    models.has(model + ':latest') ||
                    [...models].some(m => m.startsWith(model.split(':')[0]));

      if (data.error) {
        dot.className = 'model-dot offline';
        dot.title     = 'Ollama offline';
      } else if (found) {
        dot.className = 'model-dot ok';
        dot.title     = `${model} — available`;
      } else {
        dot.className = 'model-dot missing';
        dot.title     = `${model} — not found in Ollama (run: ollama pull ${model})`;
      }
    });
  } catch {
    dotEls.forEach(d => { if (d) { d.className = 'model-dot offline'; d.title = 'Backend offline'; } });
  }
}

// ── NPU backend controls ───────────────────────────────

function updateBackendBadge(backend) {
  agent4Backend = backend;
  const badge = document.getElementById('backend-badge-agent4');
  const prof  = document.getElementById('profile-agent4');
  const cli   = document.querySelector('.cli-block.a4-cli');
  if (!badge) return;
  badge.textContent = backend.toUpperCase();
  badge.className   = `backend-badge${backend === 'npu' ? ' npu' : ''}`;
  prof?.classList.toggle('npu-active', backend === 'npu');
  cli?.classList.toggle('npu-active',  backend === 'npu');
}

function setBackend(backend) {
  modalBackend = backend;
  document.getElementById('btn-gpu').classList.toggle('active', backend === 'gpu');
  document.getElementById('btn-npu').classList.toggle('active', backend === 'npu');
  const npuFields = document.getElementById('npu-config-fields');
  npuFields.style.display = backend === 'npu' ? 'flex' : 'none';
  if (backend === 'npu') checkNpuHealth();
}

async function checkNpuHealth() {
  const statusEl = document.getElementById('npu-status-line');
  statusEl.textContent = 'Checking NPU server…';
  statusEl.className   = 'npu-status';
  try {
    const res  = await fetch(`${API_BASE}/npu/health`);
    const data = await res.json();
    if (data.online) {
      statusEl.textContent = `● ONLINE — ${data.models?.join(', ') || 'models loaded'}`;
      statusEl.className   = 'npu-status online';
    } else {
      statusEl.textContent = `○ OFFLINE — ${data.error || 'server not reachable'}`;
      statusEl.className   = 'npu-status offline';
    }
  } catch {
    statusEl.textContent = '○ Backend unreachable';
    statusEl.className   = 'npu-status offline';
  }
}

// ── Agent config modal ─────────────────────────────────

async function openModal(agentId) {
  currentModalAgent = agentId;
  const names = { agent1: 'AN', agent2: 'ENLIL', agent3: 'ENKI', agent4: 'ENZU' };
  document.getElementById('modal-title').textContent = `⚙ CONFIGURE ${names[agentId] || agentId}`;
  document.getElementById('modal-overlay').classList.add('open');

  // Show NPU section only for ENZU
  const npuSection = document.getElementById('npu-section');
  npuSection.style.display = agentId === 'agent4' ? 'flex' : 'none';

  if (agentId === 'agent4') {
    // Load current NPU config
    try {
      const res  = await fetch(`${API_BASE}/npu/config`);
      const data = await res.json();
      document.getElementById('npu-host-input').value  = data.host  || 'http://localhost:8080';
      document.getElementById('npu-model-input').value = data.model || 'fastflow-lm';
      modalBackend = data.backend || 'gpu';
      setBackend(modalBackend);
    } catch { /* use defaults */ }
  }

  const sel = document.getElementById('modal-model-sel');
  sel.innerHTML = '<option>Loading…</option>';
  try {
    const res    = await fetch(`${API_BASE}/models`);
    const data   = await res.json();
    const models = data.models ?? [];
    sel.innerHTML = models.length
      ? models.map(m => `<option value="${m}">${m}</option>`).join('')
      : '<option value="">No models found — is Ollama running?</option>';
  } catch {
    sel.innerHTML = '<option value="">Backend unreachable</option>';
  }
}

function closeModal() {
  document.getElementById('modal-overlay').classList.remove('open');
  currentModalAgent = null;
}

async function applyAgentConfig() {
  if (!currentModalAgent) return;
  const model = document.getElementById('modal-model-sel').value;
  const role  = document.getElementById('modal-role-sel').value;

  try {
    // Save GPU model + role
    if (model) {
      await fetch(`${API_BASE}/agents/${currentModalAgent}/config`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ model, role })
      });
      document.getElementById(`model-label-${currentModalAgent}`)
              .childNodes[0].textContent = model + ' ';
    }

    // Save NPU settings if this is ENZU
    if (currentModalAgent === 'agent4') {
      const npuHost  = document.getElementById('npu-host-input').value.trim();
      const npuModel = document.getElementById('npu-model-input').value.trim();

      if (npuHost || npuModel) {
        await fetch(`${API_BASE}/npu/config`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ host: npuHost, model: npuModel })
        });
      }

      await fetch(`${API_BASE}/npu/backend`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ backend: modalBackend })
      });

      updateBackendBadge(modalBackend);
      sysLog(`[CFG] ENZU backend → ${modalBackend.toUpperCase()}${modalBackend === 'npu' ? ` (${npuModel || 'fastflow-lm'})` : ` (${model})`}`);
    } else {
      sysLog(`[CFG] ${currentModalAgent} → ${model}`);
    }

    await checkModelStatus();
  } catch (err) {
    sysLog(`[CFG] Update failed: ${err.message}`, 'error');
  }
  closeModal();
}

document.getElementById('modal-overlay').addEventListener('click', function (e) {
  if (e.target === this) closeModal();
});

// ── Files modal ────────────────────────────────────────

async function openFilesModal() {
  const project = document.getElementById('project-input').value.trim() || currentProject;
  document.getElementById('files-modal-title').textContent = `𒁹 OUTPUT — ${project}`;
  document.getElementById('files-modal-overlay').classList.add('open');
  await refreshFilesList(project);
}

function closeFilesModal() {
  document.getElementById('files-modal-overlay').classList.remove('open');
}

async function refreshFilesList(project) {
  const empty = document.getElementById('files-empty');
  const table = document.getElementById('files-table');
  const tbody = document.getElementById('files-tbody');
  empty.textContent = 'Loading…';
  empty.style.display = 'block';
  table.style.display = 'none';
  tbody.innerHTML = '';

  try {
    const res   = await fetch(`${API_BASE}/files/${encodeURIComponent(project)}`);
    const data  = await res.json();
    const files = data.files ?? [];
    if (!files.length) { empty.textContent = 'No files generated yet.'; return; }
    empty.style.display = 'none';
    table.style.display = 'table';
    files.forEach(f => {
      const tr = document.createElement('tr');
      const kb = (f.size / 1024).toFixed(1);
      tr.innerHTML = `
        <td>${f.name}</td>
        <td>${kb} KB</td>
        <td><a href="${API_BASE}/files/${encodeURIComponent(project)}/${encodeURIComponent(f.name)}"
           target="_blank" download="${f.name}">↓ DOWNLOAD</a></td>`;
      tbody.appendChild(tr);
    });
  } catch (err) {
    empty.textContent = `Error: ${err.message}`;
  }
}

document.getElementById('files-modal-overlay').addEventListener('click', function (e) {
  if (e.target === this) closeFilesModal();
});

// ── Keyboard shortcuts ─────────────────────────────────

document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') { closeModal(); closeFilesModal(); }
});

// ── Init ───────────────────────────────────────────────

CHANNELS.forEach(ch => { sockets[ch] = connect(ch); });
sysLog('Panoptic AI Swarm Cockpit v1.2 — ONLINE');
sysLog('𒀭 AN  𒂗𒍪 ENLIL  𒂗𒆳 ENKI  𒂗𒍪 ENZU — standing by');

// Check model availability on load, then every 60s
checkModelStatus();
setInterval(checkModelStatus, 60_000);

// Load ENZU backend state
fetch(`${API_BASE}/npu/config`)
  .then(r => r.json())
  .then(d => updateBackendBadge(d.backend || 'gpu'))
  .catch(() => {});
