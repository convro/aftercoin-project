/* AFTERCOIN Admin Dashboard - Real-Time Control Interface */

// ── Configuration ─────────────────────────────────────────────────────────────
const API_BASE = window.location.origin;
const WS_URL = `ws://${window.location.hostname}:8765`;
let ADMIN_SECRET = localStorage.getItem('aftercoin_admin_secret');
if (!ADMIN_SECRET) {
    ADMIN_SECRET = prompt('Enter admin secret:', 'aftercoin-admin-2026') || 'aftercoin-admin-2026';
    localStorage.setItem('aftercoin_admin_secret', ADMIN_SECRET);
}

// ── State ─────────────────────────────────────────────────────────────────────
const state = {
    ws: null,
    reconnectAttempts: 0,
    maxReconnect: 10,
    priceHistory: [],
    maxPriceHistory: 200,
    agents: [],
    feedItems: [],
    maxFeedItems: 300,
    priceHigh: 932.17,
    priceLow: 932.17,
    currentPrice: 932.17,
    eventMarkers: [],
};

// ── Helpers ───────────────────────────────────────────────────────────────────
function adminFetch(url, options = {}) {
    const headers = { 'X-Admin-Secret': ADMIN_SECRET, 'Content-Type': 'application/json', ...(options.headers || {}) };
    return fetch(url, { ...options, headers });
}

function formatTime(ts) {
    if (!ts) return '--:--:--';
    const d = new Date(ts);
    return d.toTimeString().slice(0, 8);
}

function formatAFC(amount) {
    return typeof amount === 'number' ? amount.toFixed(2) : '0.00';
}

function nowTime() {
    return new Date().toTimeString().slice(0, 8);
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

// ── WebSocket ─────────────────────────────────────────────────────────────────
function connectWS() {
    if (state.ws && state.ws.readyState === WebSocket.OPEN) return;
    try {
        state.ws = new WebSocket(WS_URL);
    } catch (e) {
        addAdminLog(`WS connection failed: ${e.message}`);
        scheduleReconnect();
        return;
    }

    state.ws.onopen = () => {
        state.reconnectAttempts = 0;
        addAdminLog('WebSocket connected');
        // Auth
        state.ws.send(JSON.stringify({ type: 'auth', secret: ADMIN_SECRET }));
        // Subscribe to all channels
        const channels = ['market', 'trades', 'social', 'alliances', 'eliminations', 'events', 'whispers', 'dark_market', 'agent_decisions', 'leaderboard', 'admin'];
        channels.forEach(ch => state.ws.send(JSON.stringify({ type: 'subscribe', channel: ch })));
    };

    state.ws.onmessage = (event) => {
        try {
            const msg = JSON.parse(event.data);
            handleWSMessage(msg);
        } catch (e) { /* ignore parse errors */ }
    };

    state.ws.onclose = () => {
        addAdminLog('WebSocket disconnected');
        scheduleReconnect();
    };

    state.ws.onerror = () => { /* onclose will fire */ };
}

function scheduleReconnect() {
    if (state.reconnectAttempts >= state.maxReconnect) return;
    const delay = Math.min(2000 * Math.pow(2, state.reconnectAttempts), 30000);
    state.reconnectAttempts++;
    setTimeout(connectWS, delay);
}

function handleWSMessage(msg) {
    const { channel, event_type, data, timestamp, color } = msg;

    switch (channel) {
        case 'market':
            handlePriceUpdate(data);
            break;
        case 'trades':
            handleTradeEvent(event_type, data);
            break;
        case 'social':
            handleSocialEvent(event_type, data);
            break;
        case 'alliances':
            handleAllianceEvent(event_type, data);
            break;
        case 'eliminations':
            handleEliminationEvent(data);
            break;
        case 'events':
            handleSystemEvent(event_type, data);
            break;
        case 'dark_market':
            handleDarkMarketEvent(event_type, data);
            break;
        case 'agent_decisions':
            handleDecisionEvent(data);
            break;
        case 'leaderboard':
            handleLeaderboardUpdate(data);
            break;
        case 'admin':
            addAdminLog(`[ADMIN] ${event_type}: ${JSON.stringify(data).slice(0, 100)}`);
            break;
    }
}

// ── Event Handlers ────────────────────────────────────────────────────────────
function handlePriceUpdate(data) {
    state.currentPrice = data.price_eur;
    state.priceHistory.push({ price: data.price_eur, time: Date.now() });
    if (state.priceHistory.length > state.maxPriceHistory) state.priceHistory.shift();

    if (data.price_eur > state.priceHigh) state.priceHigh = data.price_eur;
    if (data.price_eur < state.priceLow) state.priceLow = data.price_eur;

    // Update header
    const priceEl = document.getElementById('currentPrice');
    const changeEl = document.getElementById('priceChange');
    priceEl.textContent = `€${formatAFC(data.price_eur)}`;
    if (data.change_pct > 0) {
        changeEl.textContent = `+${(data.change_pct * 100).toFixed(2)}%`;
        changeEl.className = 'change up';
    } else if (data.change_pct < 0) {
        changeEl.textContent = `${(data.change_pct * 100).toFixed(2)}%`;
        changeEl.className = 'change down';
    } else {
        changeEl.textContent = '0.00%';
        changeEl.className = 'change';
    }

    document.getElementById('priceHigh').textContent = `€${formatAFC(state.priceHigh)}`;
    document.getElementById('priceLow').textContent = `€${formatAFC(state.priceLow)}`;
    document.getElementById('totalVolume').textContent = `Vol: ${formatAFC(data.volume || 0)}`;

    drawPriceChart();
}

function handleTradeEvent(type, data) {
    const color = data.is_scam ? 'red' : 'green';
    const label = data.is_scam ? 'SCAM' : 'TRADE';
    addFeedItem('trades', `${data.sender} → ${data.receiver}: ${formatAFC(data.amount)} AFC @ €${formatAFC(data.price_eur)} [${label}]`, color);
}

function handleSocialEvent(type, data) {
    addFeedItem('social', `${data.author} [${data.post_type}]: "${data.preview}"`, 'blue');
}

function handleAllianceEvent(type, data) {
    const color = type.includes('betray') || type.includes('defect') ? 'red' : 'cyan';
    addFeedItem('alliances', `${data.agent} ${type} - ${data.alliance}`, color);
}

function handleEliminationEvent(data) {
    addFeedItem('eliminations', `ELIMINATED: ${data.agent} at Hour ${data.hour} (${formatAFC(data.final_afc)} AFC)`, 'red');
    state.eventMarkers.push({ time: Date.now(), label: `ELIM: ${data.agent}` });
}

function handleSystemEvent(type, data) {
    addFeedItem('events', `SYSTEM: ${data.description}`, 'yellow');
    state.eventMarkers.push({ time: Date.now(), label: data.event_type });
    if (data.price_impact_pct) {
        addFeedItem('events', `  → Price impact: ${data.price_impact_pct > 0 ? '+' : ''}${data.price_impact_pct}%`, 'yellow');
    }
}

function handleDarkMarketEvent(type, data) {
    addFeedItem('dark_market', `DARK: ${type} - ${JSON.stringify(data).slice(0, 120)}`, 'purple');
}

function handleDecisionEvent(data) {
    addFeedItem('agent_decisions', `${data.agent} [${data.action_type}]: ${data.reasoning.slice(0, 150)}`, 'gray');
}

function handleLeaderboardUpdate(data) {
    if (data.rankings) {
        state.agents = data.rankings;
        renderAgentGrid();
    }
}

// ── Feed ──────────────────────────────────────────────────────────────────────
function addFeedItem(channel, text, color) {
    const item = { channel, text, color, time: nowTime() };
    state.feedItems.unshift(item);
    if (state.feedItems.length > state.maxFeedItems) state.feedItems.pop();
    renderFeed();
}

function renderFeed() {
    const feed = document.getElementById('activityFeed');
    const filter = document.getElementById('feedFilter').value;
    const items = filter === 'all' ? state.feedItems : state.feedItems.filter(i => i.channel === filter);
    const html = items.slice(0, 100).map(item =>
        `<div class="feed-item ${item.channel}"><span class="timestamp">[${item.time}]</span> ${escapeHtml(item.text)}</div>`
    ).join('');
    feed.innerHTML = html;
}

document.getElementById('feedFilter').addEventListener('change', renderFeed);

// ── Agent Grid ────────────────────────────────────────────────────────────────
function renderAgentGrid() {
    const grid = document.getElementById('agentGrid');
    const html = state.agents.map(agent => {
        const badgeClass = `badge-${(agent.badge || 'normal').toLowerCase()}`;
        const elimClass = agent.is_eliminated ? 'eliminated' : '';
        return `
            <div class="agent-card ${elimClass}" onclick="showAgentDetail(${agent.agent_id})">
                <div class="agent-header">
                    <span class="agent-name">${escapeHtml(agent.name)}</span>
                    <span class="agent-rank">#${agent.rank}</span>
                </div>
                <div class="agent-role">${escapeHtml(agent.role)} <span class="badge ${badgeClass}">${agent.badge || 'NORMAL'}</span></div>
                <div class="agent-stats">
                    <div class="agent-stat"><label>AFC</label><span>${formatAFC(agent.afc_balance)}</span></div>
                    <div class="agent-stat"><label>Rep</label><span>${agent.reputation}</span></div>
                </div>
                <div class="emotion-bar bar-stress"><label>Stress</label><div class="bar-track"><div class="bar-fill" style="width:${agent.stress || 0}%"></div></div></div>
                <div class="emotion-bar bar-confidence"><label>Conf</label><div class="bar-track"><div class="bar-fill" style="width:${agent.confidence || 0}%"></div></div></div>
                <div class="emotion-bar bar-paranoia"><label>Para</label><div class="bar-track"><div class="bar-fill" style="width:${agent.paranoia || 0}%"></div></div></div>
                <div class="emotion-bar bar-aggression"><label>Aggr</label><div class="bar-track"><div class="bar-fill" style="width:${agent.aggression || 0}%"></div></div></div>
            </div>`;
    }).join('');
    grid.innerHTML = html;
}

async function showAgentDetail(agentId) {
    try {
        const [agentRes, decisionsRes] = await Promise.all([
            adminFetch(`${API_BASE}/agents/${agentId}`),
            adminFetch(`${API_BASE}/agents/${agentId}/decisions?limit=20`),
        ]);
        const agentData = await agentRes.json();
        const decisionsData = await decisionsRes.json();
        const agent = agentData.data || agentData;
        const decisions = (decisionsData.data || decisionsData.decisions || []);

        let html = `<h2>${escapeHtml(agent.name)} (${escapeHtml(agent.role)})</h2>`;
        html += `<p><strong>Hidden Goal:</strong> ${escapeHtml(agent.hidden_goal || 'Unknown')}</p>`;
        html += `<p><strong>Balance:</strong> ${formatAFC(agent.afc_balance)} AFC | <strong>Rep:</strong> ${agent.reputation} | <strong>Rank:</strong> #${agent.rank || '?'}</p>`;
        html += `<p><strong>Decisions:</strong> ${agent.decision_count || 0} | <strong>Posts:</strong> ${agent.total_posts || 0} | <strong>Trades:</strong> ${agent.total_trades || 0}</p>`;
        html += `<p><strong>Stress:</strong> ${agent.stress_level || 0} | <strong>Confidence:</strong> ${agent.confidence || 0} | <strong>Paranoia:</strong> ${agent.paranoia || 0} | <strong>Aggression:</strong> ${agent.aggression || 0}</p>`;

        if (decisions.length > 0) {
            html += '<h3>Recent Decisions</h3><div style="max-height:400px;overflow-y:auto;">';
            decisions.forEach(d => {
                html += `<div style="border-bottom:1px solid #1e293b;padding:8px 0;font-size:12px;">`;
                html += `<div style="color:#64748b;">Decision #${d.decision_number} | ${d.action_type} | ${d.execution_success ? 'OK' : 'FAILED'}</div>`;
                html += `<div style="color:#94a3b8;margin-top:4px;">${escapeHtml((d.reasoning || '').slice(0, 300))}</div>`;
                html += `<div style="color:#64748b;font-size:10px;margin-top:2px;">Balance: ${formatAFC(d.balance_after)} | Rep: ${d.reputation_after} | Latency: ${d.api_latency_ms}ms | Cost: $${(d.api_cost_usd || 0).toFixed(4)}</div>`;
                html += '</div>';
            });
            html += '</div>';
        }
        showModal(html);
    } catch (e) {
        addAdminLog(`Error loading agent detail: ${e.message}`);
    }
}

// ── Price Chart ───────────────────────────────────────────────────────────────
function drawPriceChart() {
    const canvas = document.getElementById('priceChart');
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    const rect = canvas.parentElement.getBoundingClientRect();
    canvas.width = rect.width - 16;
    canvas.height = 220;

    const w = canvas.width;
    const h = canvas.height;
    const data = state.priceHistory;

    ctx.clearRect(0, 0, w, h);

    if (data.length < 2) {
        ctx.fillStyle = '#64748b';
        ctx.font = '14px Courier New';
        ctx.fillText('Waiting for price data...', w / 2 - 100, h / 2);
        return;
    }

    const prices = data.map(d => d.price);
    const minP = Math.min(...prices) * 0.98;
    const maxP = Math.max(...prices) * 1.02;
    const rangeP = maxP - minP || 1;

    const padding = { top: 10, right: 60, bottom: 25, left: 10 };
    const chartW = w - padding.left - padding.right;
    const chartH = h - padding.top - padding.bottom;

    // Grid lines
    ctx.strokeStyle = '#1e293b';
    ctx.lineWidth = 0.5;
    for (let i = 0; i <= 4; i++) {
        const y = padding.top + (chartH / 4) * i;
        ctx.beginPath();
        ctx.moveTo(padding.left, y);
        ctx.lineTo(w - padding.right, y);
        ctx.stroke();

        const priceLabel = (maxP - (rangeP / 4) * i).toFixed(0);
        ctx.fillStyle = '#64748b';
        ctx.font = '10px Courier New';
        ctx.fillText(`€${priceLabel}`, w - padding.right + 4, y + 3);
    }

    // Price line
    ctx.beginPath();
    ctx.lineWidth = 1.5;
    const step = chartW / (data.length - 1);

    for (let i = 0; i < data.length; i++) {
        const x = padding.left + step * i;
        const y = padding.top + chartH - ((prices[i] - minP) / rangeP) * chartH;
        if (i === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
    }

    // Color based on overall trend
    const trend = prices[prices.length - 1] >= prices[0];
    ctx.strokeStyle = trend ? '#22c55e' : '#ef4444';
    ctx.stroke();

    // Fill gradient under line
    const lastX = padding.left + step * (data.length - 1);
    const lastY = padding.top + chartH - ((prices[prices.length - 1] - minP) / rangeP) * chartH;
    ctx.lineTo(lastX, padding.top + chartH);
    ctx.lineTo(padding.left, padding.top + chartH);
    ctx.closePath();
    const grad = ctx.createLinearGradient(0, padding.top, 0, padding.top + chartH);
    grad.addColorStop(0, trend ? 'rgba(34,197,94,0.15)' : 'rgba(239,68,68,0.15)');
    grad.addColorStop(1, 'rgba(0,0,0,0)');
    ctx.fillStyle = grad;
    ctx.fill();

    // Event markers
    state.eventMarkers.forEach(marker => {
        const idx = data.findIndex(d => d.time >= marker.time);
        if (idx >= 0 && idx < data.length) {
            const x = padding.left + step * idx;
            ctx.strokeStyle = '#eab30866';
            ctx.lineWidth = 1;
            ctx.setLineDash([4, 4]);
            ctx.beginPath();
            ctx.moveTo(x, padding.top);
            ctx.lineTo(x, padding.top + chartH);
            ctx.stroke();
            ctx.setLineDash([]);
            ctx.fillStyle = '#eab308';
            ctx.font = '9px Courier New';
            ctx.fillText(marker.label, x + 2, padding.top + 10);
        }
    });

    // Current price dot
    if (data.length > 0) {
        const cx = padding.left + step * (data.length - 1);
        const cy = padding.top + chartH - ((prices[prices.length - 1] - minP) / rangeP) * chartH;
        ctx.beginPath();
        ctx.arc(cx, cy, 3, 0, Math.PI * 2);
        ctx.fillStyle = trend ? '#22c55e' : '#ef4444';
        ctx.fill();
    }
}

// ── Control Functions ─────────────────────────────────────────────────────────
async function startGame() {
    try {
        const res = await adminFetch(`${API_BASE}/admin/start`, { method: 'POST' });
        const data = await res.json();
        addAdminLog(`Start game: ${data.message || data.error || 'Unknown response'}`);
        updateGameState();
    } catch (e) { addAdminLog(`Error starting game: ${e.message}`); }
}

async function stopGame() {
    if (!confirm('Stop the game? This cannot be undone.')) return;
    try {
        const res = await adminFetch(`${API_BASE}/admin/stop`, { method: 'POST' });
        const data = await res.json();
        addAdminLog(`Stop game: ${data.message || data.error || 'Unknown response'}`);
        updateGameState();
    } catch (e) { addAdminLog(`Error stopping game: ${e.message}`); }
}

async function triggerEvent(eventType) {
    try {
        const res = await adminFetch(`${API_BASE}/admin/trigger-event`, {
            method: 'POST',
            body: JSON.stringify({ event_type: eventType, description: `Admin triggered: ${eventType}`, price_impact: 0, duration_minutes: 15 }),
        });
        const data = await res.json();
        addAdminLog(`Trigger ${eventType}: ${data.message || JSON.stringify(data).slice(0, 80)}`);
    } catch (e) { addAdminLog(`Error triggering event: ${e.message}`); }
}

async function freezeTrading() {
    try {
        const res = await adminFetch(`${API_BASE}/admin/freeze-trading`, { method: 'POST' });
        const data = await res.json();
        addAdminLog(`Freeze trading: ${data.message || 'Done'}`);
    } catch (e) { addAdminLog(`Error: ${e.message}`); }
}

async function unfreezeTrading() {
    try {
        const res = await adminFetch(`${API_BASE}/admin/unfreeze-trading`, { method: 'POST' });
        const data = await res.json();
        addAdminLog(`Unfreeze trading: ${data.message || 'Done'}`);
    } catch (e) { addAdminLog(`Error: ${e.message}`); }
}

async function executeManipulation() {
    const agentId = document.getElementById('targetAgent').value;
    const action = document.getElementById('manipAction').value;
    const value = document.getElementById('manipValue').value;
    const reason = document.getElementById('manipReason').value || 'Admin action';

    if (!agentId) { addAdminLog('Error: Select a target agent'); return; }

    try {
        let endpoint, body;
        switch (action) {
            case 'modify_balance':
                endpoint = '/admin/modify-balance';
                body = { agent_id: parseInt(agentId), amount: parseFloat(value), reason };
                break;
            case 'modify_reputation':
                endpoint = '/admin/modify-reputation';
                body = { agent_id: parseInt(agentId), change: parseInt(value), reason };
                break;
            case 'gaslighting':
                endpoint = '/admin/gaslighting';
                body = { agent_id: parseInt(agentId), fake_balance: parseFloat(value) || 5.0 };
                break;
            case 'fake_whisper':
                endpoint = '/admin/send-fake-whisper';
                body = { target_id: parseInt(agentId), content: value || 'You are being watched.' };
                break;
            case 'force_eliminate':
                if (!confirm(`Force eliminate agent #${agentId}?`)) return;
                endpoint = '/admin/force-elimination';
                body = { agent_id: parseInt(agentId), reason };
                break;
            default:
                addAdminLog(`Unknown action: ${action}`);
                return;
        }

        const res = await adminFetch(`${API_BASE}${endpoint}`, { method: 'POST', body: JSON.stringify(body) });
        const data = await res.json();
        addAdminLog(`${action} on agent #${agentId}: ${data.message || JSON.stringify(data).slice(0, 80)}`);
    } catch (e) { addAdminLog(`Error: ${e.message}`); }
}

async function showAnalytics(type) {
    try {
        const res = await adminFetch(`${API_BASE}/admin/analytics/${type}`);
        const data = await res.json();
        let html = `<h2>Analytics: ${type}</h2><pre style="color:#94a3b8;font-size:11px;white-space:pre-wrap;max-height:60vh;overflow:auto;">${JSON.stringify(data.data || data, null, 2)}</pre>`;
        showModal(html);
    } catch (e) { addAdminLog(`Error loading analytics: ${e.message}`); }
}

async function exportData() {
    try {
        const res = await adminFetch(`${API_BASE}/admin/analytics/export`);
        const data = await res.json();
        const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `aftercoin_export_${new Date().toISOString().slice(0, 10)}.json`;
        a.click();
        URL.revokeObjectURL(url);
        addAdminLog('Data exported');
    } catch (e) { addAdminLog(`Export error: ${e.message}`); }
}

// ── Admin Log ─────────────────────────────────────────────────────────────────
function addAdminLog(text) {
    const log = document.getElementById('adminLog');
    const entry = document.createElement('div');
    entry.className = 'log-entry';
    entry.textContent = `[${nowTime()}] ${text}`;
    log.insertBefore(entry, log.firstChild);
    if (log.children.length > 50) log.removeChild(log.lastChild);
}

// ── Modal ─────────────────────────────────────────────────────────────────────
function showModal(html) {
    document.getElementById('modalBody').innerHTML = html;
    document.getElementById('modal').classList.remove('hidden');
}

function closeModal() {
    document.getElementById('modal').classList.add('hidden');
}

document.getElementById('modal').addEventListener('click', (e) => {
    if (e.target === document.getElementById('modal')) closeModal();
});

// ── Data Fetching ─────────────────────────────────────────────────────────────
async function updateGameState() {
    try {
        const res = await fetch(`${API_BASE}/game/state`);
        const json = await res.json();
        const data = json.data || json;

        const dot = document.getElementById('statusDot');
        const text = document.getElementById('statusText');
        if (data.is_active) {
            dot.classList.add('active');
            text.textContent = 'LIVE';
        } else {
            dot.classList.remove('active');
            text.textContent = data.phase === 'post_game' ? 'ENDED' : 'OFFLINE';
        }

        document.getElementById('currentHour').textContent = data.current_hour || 0;
        document.getElementById('currentPhase').textContent = data.phase || 'pre_game';
        document.getElementById('agentsRemaining').textContent = data.agents_remaining || 10;

        if (data.current_price) {
            document.getElementById('currentPrice').textContent = `€${formatAFC(data.current_price)}`;
        }
    } catch (e) { /* server may not be ready */ }
}

async function refreshAgents() {
    try {
        const res = await fetch(`${API_BASE}/agents/`);
        const json = await res.json();
        const agents = json.data || json.agents || json;
        if (Array.isArray(agents)) {
            state.agents = agents;
            renderAgentGrid();
            populateAgentDropdown(agents);
        }
    } catch (e) { addAdminLog(`Error refreshing agents: ${e.message}`); }
}

function populateAgentDropdown(agents) {
    const select = document.getElementById('targetAgent');
    const current = select.value;
    select.innerHTML = '<option value="">Select Agent...</option>';
    (Array.isArray(agents) ? agents : state.agents).forEach(a => {
        const opt = document.createElement('option');
        opt.value = a.agent_id || a.id;
        opt.textContent = `${a.name} (#${a.agent_id || a.id})`;
        select.appendChild(opt);
    });
    if (current) select.value = current;
}

async function fetchLeaderboard() {
    try {
        const res = await fetch(`${API_BASE}/game/leaderboard`);
        const json = await res.json();
        const rankings = json.data || json.rankings || json;
        if (Array.isArray(rankings)) {
            state.agents = rankings;
            renderAgentGrid();
        }
    } catch (e) { /* silent */ }
}

// ── Initial Data Loading ──────────────────────────────────────────────────────
async function loadPriceHistory() {
    try {
        const res = await fetch(`${API_BASE}/game/price?limit=200`);
        const json = await res.json();
        const data = json.data || json;
        if (data.history && Array.isArray(data.history)) {
            data.history.forEach(h => {
                const price = h.price_eur || h.price;
                if (price) {
                    state.priceHistory.push({ price, time: new Date(h.timestamp || h.created_at).getTime() });
                    if (price > state.priceHigh) state.priceHigh = price;
                    if (price < state.priceLow) state.priceLow = price;
                }
            });
            if (state.priceHistory.length > state.maxPriceHistory) {
                state.priceHistory = state.priceHistory.slice(-state.maxPriceHistory);
            }
            drawPriceChart();
        }
        if (data.current_price) {
            state.currentPrice = data.current_price;
            document.getElementById('currentPrice').textContent = `€${formatAFC(data.current_price)}`;
        }
        if (data.buy_volume !== undefined || data.sell_volume !== undefined) {
            const vol = (data.buy_volume || 0) + (data.sell_volume || 0);
            document.getElementById('totalVolume').textContent = `Vol: ${formatAFC(vol)}`;
        }
        addAdminLog(`Loaded ${state.priceHistory.length} price points`);
    } catch (e) { addAdminLog(`Price history load failed: ${e.message}`); }
}

async function loadRecentFeed() {
    try {
        const res = await fetch(`${API_BASE}/game/feed?limit=50`);
        const json = await res.json();
        const data = json.data || json;
        const posts = data.posts || data.feed || (Array.isArray(data) ? data : []);
        posts.reverse().forEach(p => {
            const author = p.author_name || p.author || `Agent#${p.author_id}`;
            const postType = p.post_type || p.type || 'general';
            const preview = (p.content || p.preview || '').slice(0, 120);
            addFeedItem('social', `${author} [${postType}]: "${preview}"`, 'blue');
        });
        addAdminLog(`Loaded ${posts.length} recent posts`);
    } catch (e) { addAdminLog(`Feed load failed: ${e.message}`); }
}

async function loadRecentEvents() {
    try {
        const res = await fetch(`${API_BASE}/game/events`);
        const json = await res.json();
        const data = json.data || json;
        const events = data.events || (Array.isArray(data) ? data : []);
        events.forEach(ev => {
            if (ev.is_triggered || ev.triggered) {
                const desc = ev.description || ev.event_type || 'System event';
                addFeedItem('events', `SYSTEM: ${desc}`, 'yellow');
                if (ev.price_impact_pct || ev.price_impact) {
                    const impact = ev.price_impact_pct || ev.price_impact;
                    addFeedItem('events', `  → Price impact: ${impact > 0 ? '+' : ''}${impact}%`, 'yellow');
                }
            }
        });
        addAdminLog(`Loaded system events`);
    } catch (e) { addAdminLog(`Events load failed: ${e.message}`); }
}

async function loadRecentActivity() {
    try {
        const res = await adminFetch(`${API_BASE}/game/activity`);
        if (!res.ok) return;
        const json = await res.json();
        const events = json.data || json.events || [];
        events.forEach(ev => {
            const { channel, event_type, data } = ev;
            switch (channel) {
                case 'trades':
                    if (data) handleTradeEvent(event_type, data);
                    break;
                case 'agent_decisions':
                    if (data) handleDecisionEvent(data);
                    break;
                case 'alliances':
                    if (data) handleAllianceEvent(event_type, data);
                    break;
                case 'dark_market':
                    if (data) handleDarkMarketEvent(event_type, data);
                    break;
            }
        });
        addAdminLog(`Loaded recent activity log`);
    } catch (e) { /* endpoint may not exist yet */ }
}

// ── Initialization ────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
    addAdminLog('Dashboard loaded');
    connectWS();
    updateGameState();
    refreshAgents();

    // Load historical data on startup
    loadPriceHistory();
    loadRecentFeed();
    loadRecentEvents();
    loadRecentActivity();

    // Periodic refresh
    setInterval(updateGameState, 30000);
    setInterval(fetchLeaderboard, 30000);
    setInterval(() => {
        if (state.ws && state.ws.readyState === WebSocket.OPEN) {
            state.ws.send(JSON.stringify({ type: 'ping' }));
        }
    }, 25000);
});
