// ══════════════════════════════════════════════════════
//  PANOPTIC AI SWARM COCKPIT — Frontend Client
// ══════════════════════════════════════════════════════

const WS_BASE  = `ws://${location.host}`;
const API_BASE = `http://${location.host}`;
const CHANNELS = ['agent1', 'agent2', 'agent3', 'agent4', 'console'];

const sockets = {};
let currentModalAgent = null;

// ── WebSocket Manager ──────────────────────────────────

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
        setStatus(channel, msg.status, msg.status === 'WORKING' ? 'active' : msg.status === 'ERROR' ? 'error' : '');
      break;
    case 'loop_update':
      document.getElementById('loop-counter').textContent =
        `LOOP: ${msg.iteration}/${msg.max}`;
      break;
    case 'project':
      document.getElementById('project-name').textContent = msg.name;
      break;
    case 'console_line':
      appendConsoleLine(msg.message, msg.level || 'log');
      break;
    case 'clear':
      clearStream(channel);
      break;
    case 'ping':
      ws.send('pong');
      break;
  }
}

// ── Terminal Rendering ─────────────────────────────────

// We maintain a single "current line" span per stream for token-by-token appending.
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
  if (el) { el.innerHTML = ''; }
  delete cursors[channel];
}

function sysLog(msg, level = 'sys') {
  const el = document.getElementById('stream-console');
  if (!el) return;
  const ts = new Date().toTimeString().slice(0, 8);
  const span = document.createElement('span');
  span.className = `tok line-${level}`;
  span.textContent = `[${ts}] ${msg}`;
  el.appendChild(span);
  el.appendChild(document.createElement('br'));
  el.scrollTop = el.scrollHeight;
}

function appendConsoleLine(msg, level) {
  const el = document.getElementById('stream-console');
  if (!el) return;
  const ts = new Date().toTimeString().slice(0, 8);
  const span = document.createElement('span');
  span.className = `tok line-${level}`;
  span.textContent = `[${ts}] ${msg}`;
  el.appendChild(span);
  el.appendChild(document.createElement('br'));
  el.scrollTop = el.scrollHeight;
}

// ── Agent Status LEDs ──────────────────────────────────

function setStatus(agentId, text, ledClass) {
  const led = document.getElementById(`led-${agentId}`);
  const lbl = document.getElementById(`status-${agentId}`);
  if (!led || !lbl) return;
  led.className = 'led' + (ledClass ? ` ${ledClass}` : '');
  lbl.textContent = text;
}

// ── Iframe Console Injection ───────────────────────────

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
  function relay(lvl, args){
    const msg = Array.from(args).map(a=>{
      try{ return typeof a==='object'? JSON.stringify(a,null,2):String(a); }catch{ return String(a); }
    }).join(' ');
    window.parent.postMessage({type:'iframe-console',level:lvl,message:msg},'*');
  }
  ['log','warn','error','info','debug'].forEach(m=>{
    const orig = console[m].bind(console);
    console[m] = (...a)=>{ relay(m,a); orig(...a); };
  });
  window.addEventListener('error', e=>{
    relay('error',[e.message+' @ '+e.filename+':'+e.lineno+':'+e.colno]);
  });
  window.addEventListener('unhandledrejection', e=>{
    relay('error',['Unhandled Promise rejection: '+e.reason]);
  });
})();
    `;
    win.document.head.appendChild(script);
    sysLog('[FRAME] Console relay injected (same-origin)');
  } catch {
    sysLog('[FRAME] Cross-origin iframe — postMessage relay only', 'warn');
  }
}

window.addEventListener('message', (e) => {
  if (e.data?.type === 'iframe-console') {
    appendConsoleLine(e.data.message, e.data.level || 'log');
  }
});

// ── Command Broadcast ──────────────────────────────────

async function sendCommand(event) {
  if (event) event.preventDefault();
  const prompt  = document.getElementById('cmd-input').value.trim();
  const project = document.getElementById('project-input').value.trim() || 'New Project';
  if (!prompt) return;

  const btn = document.getElementById('send-btn');
  btn.disabled = true;
  document.getElementById('cmd-input').value = '';

  CHANNELS.filter(c => c !== 'console').forEach(clearStream);
  sysLog(`[CMD] Broadcasting: "${prompt}"`);

  try {
    const res = await fetch(`${API_BASE}/broadcast`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ prompt, project })
    });
    const data = await res.json();
    sysLog(`[CMD] ${data.status || data.error}`);
  } catch (err) {
    sysLog(`[CMD] Fetch error: ${err.message}`, 'error');
  }

  setTimeout(() => { btn.disabled = false; }, 2000);
}

// ── Agent Config Modal ─────────────────────────────────

async function openModal(agentId) {
  currentModalAgent = agentId;
  document.getElementById('modal-title').textContent = `⚙ CONFIGURE ${agentId.toUpperCase()}`;
  document.getElementById('modal-overlay').classList.add('open');

  const sel = document.getElementById('modal-model-sel');
  sel.innerHTML = '<option>Loading models…</option>';

  try {
    const res  = await fetch(`${API_BASE}/models`);
    const data = await res.json();
    const models = data.models ?? [];
    if (models.length) {
      sel.innerHTML = models.map(m => `<option value="${m}">${m}</option>`).join('');
    } else {
      sel.innerHTML = '<option value="">No models found — is Ollama running?</option>';
    }
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
  if (!model) { closeModal(); return; }

  try {
    await fetch(`${API_BASE}/agents/${currentModalAgent}/config`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ model, role })
    });
    document.getElementById(`model-label-${currentModalAgent}`).textContent = model;
    sysLog(`[CFG] ${currentModalAgent} → ${model} / ${role}`);
  } catch (err) {
    sysLog(`[CFG] Update failed: ${err.message}`, 'error');
  }
  closeModal();
}

document.getElementById('modal-overlay').addEventListener('click', function (e) {
  if (e.target === this) closeModal();
});

// ── Keyboard shortcuts ─────────────────────────────────

document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') closeModal();
});

// ── Init ───────────────────────────────────────────────

CHANNELS.forEach(ch => { sockets[ch] = connect(ch); });
sysLog('Panoptic AI Swarm Cockpit — ONLINE');
sysLog('Waiting for swarm backend on ' + location.host + '…');
