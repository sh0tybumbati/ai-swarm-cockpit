// ══════════════════════════════════════════════════════
//  PANOPTIC AI SWARM COCKPIT — Frontend Client v1.2
// ══════════════════════════════════════════════════════

const WS_BASE  = `ws://${location.host}`;
const API_BASE = `http://${location.host}`;
const CHANNELS = ['agent1', 'agent2', 'agent3', 'agent4', 'agent5', 'console'];

const sockets = {};
let currentModalAgent = null;
let currentProject    = 'ENMERKAR';
let savedFilesCount   = 0;
let loopRunning       = false;
let autoRefresh       = false;
const agentBackends   = {};      // per-agent backend state
let modalBackend      = 'gpu';   // tracks selection inside modal
const tpsData         = {};      // per-agent: {samples, peak}
let awaitingReply     = false;   // true when an agent has paused and needs user input

const agentDeity = { agent1: 'AN', agent2: 'ENLIL', agent3: 'ENKI', agent4: 'ENZU', agent5: 'NISABA' };

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
      if (channel !== 'console') {
        const cls = msg.status === 'WORKING' ? 'active'
                  : msg.status === 'ERROR'   ? 'error'
                  : msg.status === 'WAITING' ? 'waiting'
                  : '';
        setStatus(channel, msg.status, cls);
        if (msg.status === 'WORKING') resetTpsStats(channel);
      }
      break;
    case 'waiting_for_input':
      if (channel === 'console') {
        awaitingReply = true;
        const deity = agentDeity[msg.agent] || msg.agent || 'AGENT';
        enterReplyMode(deity, msg.question || 'Your input is needed');
      }
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
      updateBackendBadge(channel, msg.backend);
      break;
    case 'file_saved':
      onFileSaved(msg);
      break;
    case 'clear':
      clearStream(channel);
      break;
    case 'tps':
      updateTps(channel, msg.value ?? 0);
      break;
    case 'mode':
      if (channel === 'agent5') updateNisabaMode(msg.mode);
      break;
    case 'ping':
      sockets[channel]?.send('pong');
      break;
  }
}

function updateNisabaMode(mode) {
  const badge = document.getElementById('nisaba-mode');
  if (!badge) return;
  if (mode === 'scribe') {
    badge.textContent = '✍️  SCRIBE';
    badge.className   = 'nisaba-mode-badge scribe';
  } else {
    badge.textContent = '📖 LIBRARIAN';
    badge.className   = 'nisaba-mode-badge';
  }
}

function resetTpsStats(agentId) {
  tpsData[agentId] = { samples: [], peak: 0 };
  const topEl = document.getElementById(`tps-top-${agentId}`);
  const avgEl = document.getElementById(`tps-avg-${agentId}`);
  if (topEl) topEl.textContent = '—';
  if (avgEl) avgEl.textContent = '—';
}

function updateTps(agentId, value) {
  const valEl  = document.getElementById(`tps-val-${agentId}`);
  const fillEl = document.getElementById(`tps-fill-${agentId}`);
  if (!valEl || !fillEl) return;
  if (value <= 0) {
    valEl.textContent = '—';
    fillEl.style.width = '0%';
    return;
  }
  valEl.textContent = value.toFixed(1);
  fillEl.style.width = Math.min(100, (value / 80) * 100) + '%';

  if (!tpsData[agentId]) tpsData[agentId] = { samples: [], peak: 0 };
  const d = tpsData[agentId];
  d.samples.push(value);
  if (value > d.peak) d.peak = value;

  const topEl = document.getElementById(`tps-top-${agentId}`);
  const avgEl = document.getElementById(`tps-avg-${agentId}`);
  if (topEl) topEl.textContent = d.peak.toFixed(1);
  if (avgEl) {
    const avg = d.samples.reduce((a, b) => a + b, 0) / d.samples.length;
    avgEl.textContent = avg.toFixed(1);
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
  sendBtn.disabled = false;   // always enabled — inject while running, submit when idle
  if (!awaitingReply) {
    sendBtn.textContent = running ? '⚡ INJECT' : '▶ SUBMIT';
  }
  stopBtn.style.display = running ? 'inline-block' : 'none';
}

async function stopLoop() {
  try {
    await fetch(`${API_BASE}/stop`, { method: 'POST' });
    exitReplyMode();
    sysLog('[CMD] Stop signal sent', 'warn');
  } catch (err) {
    sysLog(`[CMD] Stop failed: ${err.message}`, 'error');
  }
}

// ── Reply mode — triggered when an agent pauses with CLARIFY ───

function enterReplyMode(deity, question) {
  const input = document.getElementById('cmd-input');
  const btn   = document.getElementById('send-btn');
  input.placeholder = `[YOU → ${deity}] : ${question}`;
  input.classList.add('reply-active');
  btn.textContent = '↩ REPLY';
  btn.disabled = false;
  sysLog(`[⏸ ${deity}] ${question}`, 'warn');
  input.focus();
}

function exitReplyMode() {
  if (!awaitingReply) return;
  awaitingReply = false;
  const input = document.getElementById('cmd-input');
  const btn   = document.getElementById('send-btn');
  input.placeholder = '[YOU] : Broadcast to swarm... (↑↓ history)';
  input.classList.remove('reply-active');
  btn.textContent = loopRunning ? '⚡ INJECT' : '▶ SUBMIT';
}

async function sendReply() {
  const answer = document.getElementById('cmd-input').value.trim();
  document.getElementById('cmd-input').value = '';
  exitReplyMode();
  sysLog(`[YOU] ${answer || '(no answer — continuing)'}`, 'sys');
  try {
    await fetch(`${API_BASE}/reply`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ answer }),
    });
  } catch (err) {
    sysLog(`[REPLY] Error: ${err.message}`, 'error');
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

  // Auto-load index.html (or first .html) into preview when saved
  if (msg.file.match(/index\.html?$/i) || msg.file.match(/\.html?$/i)) {
    const project = currentProject || document.getElementById('project-input').value.trim();
    const url = `${API_BASE}/files/${encodeURIComponent(project)}/${encodeURIComponent(msg.file)}`;
    const input = document.getElementById('preview-url-input');
    const frame = document.getElementById('preview-frame');
    input.value = url;
    frame.src = url;
    sysLog(`[PREVIEW] Auto-loaded: ${msg.file}`, 'info');
    return;
  }

  // Auto-refresh iframe for other web assets if already loaded
  const webExts = ['.js', '.ts', '.css'];
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

  // If an agent is waiting for clarification, this is a reply, not a new broadcast
  if (awaitingReply) {
    await sendReply();
    return;
  }

  const prompt       = document.getElementById('cmd-input').value.trim();
  const project      = document.getElementById('project-input').value.trim() || currentProject;
  const maxLoops     = parseInt(document.getElementById('max-loops-input').value) || 6;
  const projectRoot  = document.getElementById('project-root-input').value.trim() || null;
  if (!prompt) return;

  // If a loop is running, inject the message rather than starting a new run
  if (loopRunning) {
    document.getElementById('cmd-input').value = '';
    sysLog(`[⚡ INJECT] "${prompt}"`, 'warn');
    try {
      await fetch(`${API_BASE}/inject`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: prompt }),
      });
    } catch (err) {
      sysLog(`[INJECT] Error: ${err.message}`, 'error');
    }
    return;
  }

  // Save to history
  if (prompt !== cmdHistory[cmdHistory.length - 1]) {
    cmdHistory.push(prompt);
    if (cmdHistory.length > 50) cmdHistory.shift();
    localStorage.setItem('swarm_cmd_history', JSON.stringify(cmdHistory));
  }
  historyIdx = -1;

  document.getElementById('cmd-input').value = '';
  CHANNELS.filter(c => c !== 'console' && c !== 'agent5').forEach(clearStream);
  savedFilesCount = 0;
  document.getElementById('files-badge').style.display = 'none';
  currentProject = project;
  sysLog(`[CMD] Broadcasting: "${prompt}"`);

  try {
    const res  = await fetch(`${API_BASE}/broadcast`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ prompt, project, max_iterations: maxLoops, project_root: projectRoot })
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
  const agents   = ['agent1', 'agent2', 'agent3', 'agent4', 'agent5'];
  const dotEls   = agents.map(id => document.getElementById(`mdot-${id}`));

  try {
    const res    = await fetch(`${API_BASE}/models`);
    const data   = await res.json();
    const models = new Set(data.models ?? []);

    // Also get configured models
    const agentRes  = await fetch(`${API_BASE}/agents`);
    const agentData = await agentRes.json();

    agents.forEach((id, i) => {
      const dot     = dotEls[i];
      if (!dot) return;
      const model   = agentData[id]?.model ?? '';
      const backend = agentData[id]?.backend || 'gpu';

      if (backend === 'claude') {
        dot.className = 'model-dot ok';
        dot.title     = `Claude API — ${agentData[id]?.model || 'claude-opus-4-7'}`;
        return;
      }
      if (backend === 'cli') {
        dot.className = 'model-dot ok';
        dot.title     = 'Claude Code CLI (subscription)';
        return;
      }
      if (backend === 'npu') {
        dot.className = 'model-dot ok';
        dot.title     = 'NPU — FastFlowLM';
        return;
      }

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

// ── Backend badge controls (any agent) ────────────────

const CLI_SUFFIX = { agent1: 'a1-cli', agent2: 'a2-cli', agent3: 'a3-cli', agent4: 'a4-cli', agent5: 'a5-cli' };

const BACKEND_LABELS = { gpu: 'GPU', cpu: 'CPU', npu: 'NPU', claude: 'API', cli: 'CLI' };

function updateBackendBadge(agentId, backend) {
  agentBackends[agentId] = backend;
  const badge   = document.getElementById(`backend-badge-${agentId}`);
  const prof    = document.getElementById(`profile-${agentId}`);
  const cliEl   = document.querySelector(`.cli-block.${CLI_SUFFIX[agentId]}`);
  if (!badge) return;
  badge.textContent = BACKEND_LABELS[backend] || backend.toUpperCase();
  const isGpu    = backend === 'gpu';
  const isCpu    = backend === 'cpu';
  const isNpu    = backend === 'npu';
  const isClaude = backend === 'claude';
  const isCli    = backend === 'cli';
  badge.className   = `backend-badge${isCpu ? ' cpu' : isNpu ? ' npu' : isClaude ? ' claude' : isCli ? ' cli' : ''}`;
  prof?.classList.toggle('gpu-active',    isGpu);
  prof?.classList.toggle('cpu-active',    isCpu);
  prof?.classList.toggle('npu-active',    isNpu);
  prof?.classList.toggle('claude-active', isClaude);
  prof?.classList.toggle('cli-active',    isCli);
  cliEl?.classList.toggle('gpu-active',   isGpu);
  cliEl?.classList.toggle('cpu-active',   isCpu);
  cliEl?.classList.toggle('npu-active',   isNpu);
  cliEl?.classList.toggle('claude-active',isClaude);
  cliEl?.classList.toggle('cli-active',   isCli);
}

function setBackend(backend) {
  modalBackend = backend;
  ['gpu', 'cpu', 'npu', 'claude', 'cli'].forEach(b => {
    document.getElementById(`btn-${b}`)?.classList.toggle('active', backend === b);
  });
  document.getElementById('gpu-config-fields').style.display    = backend === 'gpu'    ? 'flex' : 'none';
  document.getElementById('cpu-config-fields').style.display    = backend === 'cpu'    ? 'flex' : 'none';
  document.getElementById('npu-config-fields').style.display    = backend === 'npu'    ? 'flex' : 'none';
  document.getElementById('claude-config-fields').style.display = backend === 'claude' ? 'flex' : 'none';
  document.getElementById('cli-config-fields').style.display    = backend === 'cli'    ? 'flex' : 'none';
  if (backend === 'cpu')    checkCpuHealth();
  if (backend === 'npu')    checkNpuHealth();
  if (backend === 'claude') checkClaudeHealth();
  if (backend === 'cli')    checkCliHealth();
}

async function checkCpuHealth() {
  const statusEl = document.getElementById('cpu-status-line');
  statusEl.textContent = 'Checking CPU Ollama…';
  statusEl.className   = 'npu-status';
  try {
    const res  = await fetch(`${API_BASE}/cpu/health`);
    const data = await res.json();
    if (data.online) {
      statusEl.textContent = `● ONLINE — ${data.models?.slice(0,3).join(', ') || 'models loaded'}`;
      statusEl.className   = 'npu-status online';
    } else {
      statusEl.textContent = `○ OFFLINE — ${data.error || 'start: OLLAMA_HOST=127.0.0.1:11435 OLLAMA_NUM_GPU=0 ollama serve'}`;
      statusEl.className   = 'npu-status offline';
    }
  } catch {
    statusEl.textContent = '○ CPU Ollama unreachable';
    statusEl.className   = 'npu-status offline';
  }
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

async function checkClaudeHealth() {
  const statusEl = document.getElementById('claude-status-line');
  statusEl.textContent = 'Checking Claude API…';
  statusEl.className   = 'npu-status';
  try {
    const res  = await fetch(`${API_BASE}/claude/health`);
    const data = await res.json();
    if (data.online) {
      statusEl.textContent = `● ONLINE — ${data.model}`;
      statusEl.className   = 'npu-status online';
    } else {
      statusEl.textContent = `○ ${data.reason || data.error || 'API key not configured'}`;
      statusEl.className   = 'npu-status offline';
    }
  } catch {
    statusEl.textContent = '○ Backend unreachable';
    statusEl.className   = 'npu-status offline';
  }
}

async function checkCliHealth() {
  const statusEl = document.getElementById('cli-status-line');
  statusEl.textContent = 'Detecting claude CLI…';
  statusEl.className   = 'npu-status';
  try {
    const res  = await fetch(`${API_BASE}/claude-cli/health`);
    const data = await res.json();
    if (data.online) {
      statusEl.textContent = `● FOUND — ${data.version || data.bin} (${data.model})`;
      statusEl.className   = 'npu-status online';
    } else {
      statusEl.textContent = `○ ${data.reason || 'CLI not found'}`;
      statusEl.className   = 'npu-status offline';
    }
  } catch {
    statusEl.textContent = '○ Health check failed';
    statusEl.className   = 'npu-status offline';
  }
}

// ── Agent config modal ─────────────────────────────────

async function openModal(agentId) {
  currentModalAgent = agentId;
  const names = { agent1: 'AN', agent2: 'ENLIL', agent3: 'ENKI', agent4: 'ENZU', agent5: 'NISABA' };
  document.getElementById('modal-title').textContent = `⚙ CONFIGURE ${names[agentId] || agentId}`;
  document.getElementById('modal-overlay').classList.add('open');

  // Load current backend for this agent
  try {
    const agentRes  = await fetch(`${API_BASE}/agents`);
    const agentData = await agentRes.json();
    modalBackend = agentData[agentId]?.backend || 'gpu';
  } catch { modalBackend = 'gpu'; }

  // Load CPU Ollama config
  try {
    const res  = await fetch(`${API_BASE}/cpu/config`);
    const data = await res.json();
    document.getElementById('cpu-host-input').value  = data.host  || 'http://localhost:11435';
    document.getElementById('cpu-model-input').value = data.model || 'llama3.1:8b';
  } catch { /* use defaults */ }

  // Load NPU config (for NPU fields)
  try {
    const res  = await fetch(`${API_BASE}/npu/config`);
    const data = await res.json();
    document.getElementById('npu-host-input').value  = data.host  || 'http://localhost:8080';
    document.getElementById('npu-model-input').value = data.model || 'fastflow-lm';
  } catch { /* use defaults */ }

  // Load Claude API config
  try {
    const res  = await fetch(`${API_BASE}/claude/config`);
    const data = await res.json();
    document.getElementById('claude-model-input').value = data.model || 'claude-opus-4-7';
    document.getElementById('claude-key-input').value   = '';
    document.getElementById('claude-key-input').placeholder =
      data.has_key ? '(key saved — enter new key to change)' : 'sk-ant-...';
  } catch { /* use defaults */ }

  // Load Claude CLI config
  try {
    const res  = await fetch(`${API_BASE}/claude-cli/config`);
    const data = await res.json();
    document.getElementById('cli-model-input').value = data.model || 'claude-opus-4-7';
    document.getElementById('cli-bin-input').value   = data.bin === 'claude' ? '' : (data.bin || '');
  } catch { /* use defaults */ }

  setBackend(modalBackend);

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
  const agentId = currentModalAgent;
  const model   = document.getElementById('modal-model-sel').value;

  try {
    if (model) {
      await fetch(`${API_BASE}/agents/${agentId}/config`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ model })
      });
      document.getElementById(`model-label-${agentId}`)
              .childNodes[0].textContent = model + ' ';
    }

    // Save CPU Ollama config if CPU is selected
    if (modalBackend === 'cpu') {
      const cpuHost  = document.getElementById('cpu-host-input').value.trim();
      const cpuModel = document.getElementById('cpu-model-input').value.trim();
      if (cpuHost || cpuModel) {
        await fetch(`${API_BASE}/cpu/config`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ host: cpuHost, model: cpuModel })
        });
      }
    }

    // Save NPU config if NPU is selected
    if (modalBackend === 'npu') {
      const npuHost  = document.getElementById('npu-host-input').value.trim();
      const npuModel = document.getElementById('npu-model-input').value.trim();
      if (npuHost || npuModel) {
        await fetch(`${API_BASE}/npu/config`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ host: npuHost, model: npuModel })
        });
      }
    }

    // Save Claude API config if selected
    if (modalBackend === 'claude') {
      const claudeKey   = document.getElementById('claude-key-input').value.trim();
      const claudeModel = document.getElementById('claude-model-input').value.trim();
      const claudeBody  = {};
      if (claudeKey)   claudeBody.api_key = claudeKey;
      if (claudeModel) claudeBody.model   = claudeModel;
      if (Object.keys(claudeBody).length) {
        await fetch(`${API_BASE}/claude/config`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(claudeBody)
        });
      }
    }

    // Save Claude CLI config if selected
    if (modalBackend === 'cli') {
      const cliModel = document.getElementById('cli-model-input').value.trim();
      const cliBin   = document.getElementById('cli-bin-input').value.trim();
      const cliBody  = {};
      if (cliModel) cliBody.model = cliModel;
      if (cliBin)   cliBody.bin   = cliBin;
      if (Object.keys(cliBody).length) {
        await fetch(`${API_BASE}/claude-cli/config`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(cliBody)
        });
      }
    }

    // Set backend for this agent (works for all agents, all backends)
    await fetch(`${API_BASE}/claude/backend`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ agent_id: agentId, backend: modalBackend })
    });

    updateBackendBadge(agentId, modalBackend);

    // Update model label to reflect active backend
    const labelEl = document.getElementById(`model-label-${agentId}`);
    if (labelEl) {
      let labelText = '';
      if (modalBackend === 'gpu') {
        labelText = model || labelEl.childNodes[0]?.textContent?.trim() || '';
      } else if (modalBackend === 'cpu') {
        labelText = document.getElementById('cpu-model-input').value.trim() || 'llama3.1:8b';
      } else if (modalBackend === 'npu') {
        labelText = document.getElementById('npu-model-input').value.trim() || 'llama3.2';
      } else if (modalBackend === 'claude') {
        labelText = document.getElementById('claude-model-input').value.trim() || 'claude-opus-4-7';
      } else if (modalBackend === 'cli') {
        labelText = document.getElementById('cli-model-input').value.trim() || 'claude-sonnet-4-6';
        labelText = '⬡ ' + labelText;
      }
      if (labelText) labelEl.childNodes[0].textContent = labelText + ' ';
    }

    const names = { agent1: 'AN', agent2: 'ENLIL', agent3: 'ENKI', agent4: 'ENZU', agent5: 'NISABA' };
    sysLog(`[CFG] ${names[agentId] || agentId} backend → ${modalBackend.toUpperCase()}`);

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

function previewFile(url) {
  document.getElementById('preview-url-input').value = url;
  document.getElementById('preview-frame').src = url;
  closeFilesModal();
  sysLog(`[PREVIEW] Loaded: ${url.split('/').pop()}`, 'info');
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
    const previewExts = new Set(['.html', '.htm', '.css', '.js', '.ts', '.json', '.txt', '.md']);
    files.forEach(f => {
      const tr  = document.createElement('tr');
      const kb  = (f.size / 1024).toFixed(1);
      const url = `${API_BASE}/files/${encodeURIComponent(project)}/${encodeURIComponent(f.name)}`;
      const ext = f.name.slice(f.name.lastIndexOf('.')).toLowerCase();
      const previewBtn = previewExts.has(ext)
        ? `<button onclick="previewFile('${url}')" style="margin-right:6px">◈ PREVIEW</button>`
        : '';
      tr.innerHTML = `
        <td>${f.name}</td>
        <td>${kb} KB</td>
        <td>${previewBtn}<a href="${url}" target="_blank" download="${f.name}">↓ DL</a></td>`;
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
sysLog('Panoptic AI Swarm Cockpit v1.3 — ONLINE');
sysLog('𒀭 AN  𒂗𒍪 ENLIL  𒂗𒆳 ENKI  𒂗𒍪 ENZU  𒀭𒇻 NISABA — standing by');

// Check model availability on load, then every 60s
checkModelStatus();
setInterval(checkModelStatus, 60_000);

// ── Daemon status badges ────────────────────────────────

function _setDaemonDot(id, state) {
  const el = document.getElementById(id);
  if (!el) return;
  el.className = 'daemon-dot ' + state;
}

async function pollDaemons() {
  try {
    const data = await fetch(`${API_BASE}/services`).then(r => r.json());
    _setDaemonDot('daemon-gpu', data.ollama?.online     ? 'online' : 'offline');
    _setDaemonDot('daemon-cpu', data.cpu_ollama?.online ? 'online' : 'offline');
  } catch {
    _setDaemonDot('daemon-gpu', 'offline');
    _setDaemonDot('daemon-cpu', 'offline');
  }
}

async function startCpuOllama() {
  const el = document.getElementById('daemon-cpu');
  if (el?.classList.contains('starting')) return;  // already in progress
  _setDaemonDot('daemon-cpu', 'starting');
  try {
    await fetch(`${API_BASE}/services/start-cpu`, { method: 'POST' });
  } catch {}
  // Poll a few times to pick up the new state
  for (let i = 0; i < 5; i++) {
    await new Promise(r => setTimeout(r, 2000));
    await pollDaemons();
    if (document.getElementById('daemon-cpu')?.classList.contains('online')) break;
  }
}

pollDaemons();
setInterval(pollDaemons, 15_000);

// Load all agent backend states and model labels on startup
fetch(`${API_BASE}/agents`)
  .then(r => r.json())
  .then(data => {
    Object.keys(data).forEach(agentId => {
      const agent = data[agentId];
      updateBackendBadge(agentId, agent.backend || 'gpu');
      const labelEl = document.getElementById(`model-label-${agentId}`);
      if (labelEl && agent.model) {
        const dot = labelEl.querySelector('.model-dot');
        labelEl.childNodes[0].textContent = agent.model + ' ';
        if (dot) labelEl.appendChild(dot);
      }
    });
  })
  .catch(() => {});
