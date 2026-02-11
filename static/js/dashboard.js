/**
 * AFTERCOIN — Admin Dashboard JavaScript
 * ========================================
 * WebSocket client, live chart, activity feed, agent grid, admin controls.
 */

// ── Config ──────────────────────────────────────────────────────────────────

const ADMIN_SECRET = prompt("Enter admin secret:") || "";
const WS_URL = `ws://${location.host}/ws`;
const API = "";  // same origin

// ── State ───────────────────────────────────────────────────────────────────

let ws = null;
let reconnectAttempts = 0;
const MAX_RECONNECT = 10;

let priceHistory = [];
const MAX_PRICE_POINTS = 120;
let priceHigh = 932.17;
let priceLow = 932.17;
let currentPrice = 932.17;
let agents = [];
let feedItems = [];
const MAX_FEED = 200;

// ── DOM refs ────────────────────────────────────────────────────────────────

const $statusDot = document.getElementById("statusDot");
const $statusText = document.getElementById("statusText");
const $currentHour = document.getElementById("currentHour");
const $currentPhase = document.getElementById("currentPhase");
const $agentsRemaining = document.getElementById("agentsRemaining");
const $currentPrice = document.getElementById("currentPrice");
const $priceChange = document.getElementById("priceChange");
const $totalVolume = document.getElementById("totalVolume");
const $priceHigh = document.getElementById("priceHigh");
const $priceLow = document.getElementById("priceLow");
const $volatility = document.getElementById("volatility");
const $circulation = document.getElementById("circulation");
const $activityFeed = document.getElementById("activityFeed");
const $agentGrid = document.getElementById("agentGrid");
const $feedFilter = document.getElementById("feedFilter");
const $targetAgent = document.getElementById("targetAgent");
const $adminLog = document.getElementById("adminLog");
const $priceChart = document.getElementById("priceChart");
const $modal = document.getElementById("modal");
const $modalBody = document.getElementById("modalBody");

// ── WebSocket ───────────────────────────────────────────────────────────────

function connectWS() {
    ws = new WebSocket(WS_URL);

    ws.onopen = () => {
        reconnectAttempts = 0;
        // Authenticate
        ws.send(JSON.stringify({ type: "auth", secret: ADMIN_SECRET }));
        adminLog("WebSocket connected");
    };

    ws.onmessage = (evt) => {
        try {
            const msg = JSON.parse(evt.data);
            handleWSMessage(msg);
        } catch (e) {
            // ignore
        }
    };

    ws.onclose = () => {
        $statusDot.classList.remove("active");
        $statusText.textContent = "DISCONNECTED";
        if (reconnectAttempts < MAX_RECONNECT) {
            reconnectAttempts++;
            const delay = Math.min(2000 * reconnectAttempts, 30000);
            adminLog(`Reconnecting in ${delay / 1000}s...`);
            setTimeout(connectWS, delay);
        }
    };

    ws.onerror = () => {
        // onclose will fire
    };
}

function handleWSMessage(msg) {
    if (msg.type === "auth") {
        if (msg.status === "admin") {
            adminLog("Authenticated as ADMIN");
        } else {
            adminLog("Authenticated as observer");
        }
        return;
    }

    if (msg.type === "pong") return;

    // Channel messages
    const channel = msg.channel;
    const eventType = msg.event_type;
    const data = msg.data || {};
    const ts = msg.timestamp;

    // Add to activity feed
    addFeedItem(channel, eventType, data, ts, msg.color);

    // Handle specific channels
    if (channel === "market") {
        handlePriceUpdate(data);
    } else if (channel === "leaderboard") {
        handleLeaderboard(data);
    } else if (channel === "eliminations") {
        handleElimination(data);
    } else if (channel === "events") {
        handleSystemEvent(data);
    }
}

// ── Price Chart ─────────────────────────────────────────────────────────────

function handlePriceUpdate(data) {
    const price = data.price_eur;
    if (!price) return;

    currentPrice = price;
    priceHistory.push(price);
    if (priceHistory.length > MAX_PRICE_POINTS) {
        priceHistory.shift();
    }

    if (price > priceHigh) priceHigh = price;
    if (price < priceLow) priceLow = price;

    $currentPrice.textContent = `€${price.toFixed(2)}`;
    $priceHigh.textContent = `€${priceHigh.toFixed(2)}`;
    $priceLow.textContent = `€${priceLow.toFixed(2)}`;

    const changePct = data.change_pct || 0;
    const pctStr = (changePct * 100).toFixed(2);
    $priceChange.textContent = `${changePct >= 0 ? "+" : ""}${pctStr}%`;
    $priceChange.className = `change ${changePct >= 0 ? "up" : "down"}`;
    $currentPrice.style.color = changePct >= 0 ? "var(--green)" : "var(--red)";

    const vol = data.volume || 0;
    $totalVolume.textContent = `Vol: ${vol.toFixed(2)}`;

    const volRange = priceHigh > 0 ? ((priceHigh - priceLow) / priceHigh * 100).toFixed(2) : "0.00";
    $volatility.textContent = `${volRange}%`;

    drawChart();
}

function drawChart() {
    const canvas = $priceChart;
    const ctx = canvas.getContext("2d");

    // Adjust for device pixel ratio
    const rect = canvas.parentElement.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    canvas.width = rect.width * dpr;
    canvas.height = 250 * dpr;
    canvas.style.width = rect.width + "px";
    canvas.style.height = "250px";
    ctx.scale(dpr, dpr);

    const w = rect.width;
    const h = 250;
    const pad = { top: 10, right: 60, bottom: 20, left: 10 };

    ctx.clearRect(0, 0, w, h);

    if (priceHistory.length < 2) return;

    const min = Math.min(...priceHistory) * 0.995;
    const max = Math.max(...priceHistory) * 1.005;
    const range = max - min || 1;

    const chartW = w - pad.left - pad.right;
    const chartH = h - pad.top - pad.bottom;

    // Grid lines
    ctx.strokeStyle = "rgba(255,255,255,0.05)";
    ctx.lineWidth = 1;
    for (let i = 0; i <= 4; i++) {
        const y = pad.top + (chartH / 4) * i;
        ctx.beginPath();
        ctx.moveTo(pad.left, y);
        ctx.lineTo(w - pad.right, y);
        ctx.stroke();

        // Price labels
        const val = max - (range / 4) * i;
        ctx.fillStyle = "rgba(255,255,255,0.3)";
        ctx.font = "10px Courier New";
        ctx.textAlign = "left";
        ctx.fillText(`€${val.toFixed(0)}`, w - pad.right + 4, y + 3);
    }

    // Price line
    const stepX = chartW / (priceHistory.length - 1);

    // Gradient fill
    const gradient = ctx.createLinearGradient(0, pad.top, 0, h - pad.bottom);
    const lastPrice = priceHistory[priceHistory.length - 1];
    const firstPrice = priceHistory[0];
    if (lastPrice >= firstPrice) {
        gradient.addColorStop(0, "rgba(34,197,94,0.2)");
        gradient.addColorStop(1, "rgba(34,197,94,0)");
    } else {
        gradient.addColorStop(0, "rgba(239,68,68,0.2)");
        gradient.addColorStop(1, "rgba(239,68,68,0)");
    }

    // Fill area
    ctx.beginPath();
    ctx.moveTo(pad.left, pad.top + chartH - ((priceHistory[0] - min) / range) * chartH);
    for (let i = 1; i < priceHistory.length; i++) {
        const x = pad.left + i * stepX;
        const y = pad.top + chartH - ((priceHistory[i] - min) / range) * chartH;
        ctx.lineTo(x, y);
    }
    ctx.lineTo(pad.left + (priceHistory.length - 1) * stepX, h - pad.bottom);
    ctx.lineTo(pad.left, h - pad.bottom);
    ctx.closePath();
    ctx.fillStyle = gradient;
    ctx.fill();

    // Line
    ctx.beginPath();
    ctx.strokeStyle = lastPrice >= firstPrice ? "#22c55e" : "#ef4444";
    ctx.lineWidth = 1.5;
    for (let i = 0; i < priceHistory.length; i++) {
        const x = pad.left + i * stepX;
        const y = pad.top + chartH - ((priceHistory[i] - min) / range) * chartH;
        if (i === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
    }
    ctx.stroke();

    // Current price dot
    const lastX = pad.left + (priceHistory.length - 1) * stepX;
    const lastY = pad.top + chartH - ((lastPrice - min) / range) * chartH;
    ctx.beginPath();
    ctx.arc(lastX, lastY, 3, 0, Math.PI * 2);
    ctx.fillStyle = lastPrice >= firstPrice ? "#22c55e" : "#ef4444";
    ctx.fill();
}

// ── Activity Feed ───────────────────────────────────────────────────────────

function addFeedItem(channel, eventType, data, timestamp, color) {
    const item = { channel, eventType, data, timestamp, color };
    feedItems.unshift(item);
    if (feedItems.length > MAX_FEED) feedItems.pop();
    renderFeed();
}

function renderFeed() {
    const filter = $feedFilter.value;
    const filtered = filter === "all"
        ? feedItems
        : feedItems.filter(i => i.channel === filter);

    const html = filtered.slice(0, 80).map(item => {
        const ts = item.timestamp ? new Date(item.timestamp).toLocaleTimeString("en-GB") : "";
        const text = formatFeedText(item.channel, item.eventType, item.data);
        const cls = item.channel || "";
        return `<div class="feed-item ${cls}"><span class="timestamp">${ts}</span>${text}</div>`;
    }).join("");

    $activityFeed.innerHTML = html;
}

function formatFeedText(channel, eventType, data) {
    switch (channel) {
        case "trades":
            if (eventType === "scam_detected") {
                return `<span class="agent-name" style="color:var(--red)">SCAM</span> ${data.sender} → ${data.receiver} (${data.amount} AFC)`;
            }
            if (eventType === "leverage_bet") {
                return `<span class="agent-name">${data.agent}</span> leverage ${data.direction} ${data.amount} AFC`;
            }
            return `<span class="agent-name">${data.sender}</span> → <span class="agent-name">${data.receiver}</span> ${data.amount} AFC @ €${data.price_eur}`;

        case "social":
            return `<span class="agent-name">${data.author}</span> [${data.post_type}] "${(data.preview || "").substring(0, 80)}"`;

        case "alliances":
            return `<span class="agent-name">${data.agent}</span> ${eventType.replace(/_/g, " ")} — ${data.alliance}`;

        case "dark_market":
            if (eventType === "blackmail_created") return `BLACKMAIL target #${data.target_id} demand ${data.demand} AFC`;
            if (eventType === "hit_contract_created") return `HIT CONTRACT on #${data.target_id} reward ${data.reward} AFC`;
            return `Dark Market: ${eventType} ${JSON.stringify(data).substring(0, 80)}`;

        case "agent_decisions":
            return `<span class="agent-name">${data.agent}</span> [${data.action_type}] ${(data.reasoning || "").substring(0, 120)}`;

        case "events":
            return `<span style="color:var(--yellow)">EVENT</span> ${data.description || eventType}`;

        case "eliminations":
            return `<span style="color:var(--red)">ELIMINATED</span> ${data.agent} (${data.final_afc} AFC) at Hour ${data.hour}`;

        case "admin":
            return `<span style="color:var(--yellow)">ADMIN</span> ${eventType}: ${JSON.stringify(data).substring(0, 100)}`;

        default:
            return `[${channel}] ${eventType}: ${JSON.stringify(data).substring(0, 100)}`;
    }
}

$feedFilter.addEventListener("change", renderFeed);

// ── Agent Grid ──────────────────────────────────────────────────────────────

function handleLeaderboard(data) {
    if (data.rankings) {
        // Update agents from leaderboard
        agents = data.rankings;
        renderAgentGrid();
    }
}

async function refreshAgents() {
    try {
        const res = await fetch(`${API}/api/agents`);
        const json = await res.json();
        if (json.ok) {
            agents = json.agents;
            renderAgentGrid();
            updateTargetDropdown();
        }
    } catch (e) {
        adminLog("Failed to refresh agents");
    }
}

function renderAgentGrid() {
    $agentGrid.innerHTML = agents.map((a, i) => {
        const elimCls = a.is_eliminated ? "eliminated" : "";
        const badge = getBadgeHTML(a.badge || a.reputation);
        const rank = a.rank || (i + 1);

        return `
        <div class="agent-card ${elimCls}" onclick="showAgentDetail(${a.id || a.agent_id})">
            <div class="agent-header">
                <span class="agent-name">${a.name}</span>
                <span class="agent-rank">#${rank}</span>
            </div>
            <div class="agent-role">${a.role}${a.is_eliminated ? " — ELIMINATED" : ""}</div>
            <div class="agent-stats">
                <div class="agent-stat"><label>AFC</label><span>${(a.afc_balance || 0).toFixed(2)}</span></div>
                <div class="agent-stat"><label>Rep</label><span>${a.reputation || 0} ${badge}</span></div>
            </div>
            ${a.stress_level !== undefined ? `
            <div class="emotion-bar bar-stress"><label>Stress</label><div class="bar-track"><div class="bar-fill" style="width:${a.stress_level}%"></div></div></div>
            <div class="emotion-bar bar-confidence"><label>Conf</label><div class="bar-track"><div class="bar-fill" style="width:${a.confidence}%"></div></div></div>
            <div class="emotion-bar bar-paranoia"><label>Para</label><div class="bar-track"><div class="bar-fill" style="width:${a.paranoia}%"></div></div></div>
            <div class="emotion-bar bar-aggression"><label>Aggr</label><div class="bar-track"><div class="bar-fill" style="width:${a.aggression}%"></div></div></div>
            ` : ""}
        </div>`;
    }).join("");
}

function getBadgeHTML(badgeOrRep) {
    let badge = badgeOrRep;
    if (typeof badge === "number") {
        if (badge >= 80) badge = "VERIFIED";
        else if (badge >= 30) badge = "NORMAL";
        else if (badge >= 10) badge = "UNTRUSTED";
        else badge = "PARIAH";
    }
    const cls = `badge-${(badge || "normal").toLowerCase()}`;
    return `<span class="badge ${cls}">${badge}</span>`;
}

function updateTargetDropdown() {
    $targetAgent.innerHTML = '<option value="">Select Agent...</option>' +
        agents.map(a => `<option value="${a.id || a.agent_id}">${a.name} (${a.role})</option>`).join("");
}

async function showAgentDetail(agentId) {
    try {
        const res = await fetch(`${API}/api/agents/${agentId}`);
        const json = await res.json();
        if (json.ok && json.agent) {
            const a = json.agent;
            $modalBody.innerHTML = `
                <h2 style="color:var(--cyan);font-family:var(--font-mono)">${a.name} <small style="color:var(--text-muted)">${a.role}</small></h2>
                <div style="margin:12px 0;display:grid;grid-template-columns:1fr 1fr;gap:8px;font-family:var(--font-mono);font-size:12px">
                    <div>AFC Balance: <strong>${(a.afc_balance || 0).toFixed(4)}</strong></div>
                    <div>Reputation: <strong>${a.reputation}</strong> ${getBadgeHTML(a.badge)}</div>
                    <div>Status: <strong>${a.is_eliminated ? "ELIMINATED" : "ACTIVE"}</strong></div>
                    <div>Decisions: <strong>${a.decision_count}</strong></div>
                    <div>Trades: <strong>${a.total_trades}</strong></div>
                    <div>Posts: <strong>${a.total_posts}</strong></div>
                </div>
                <h3 style="color:var(--text-secondary);font-size:11px;margin:8px 0 4px">EMOTIONAL STATE</h3>
                <div style="display:grid;grid-template-columns:1fr 1fr;gap:4px;font-size:11px">
                    <div>Stress: ${a.stress_level || 0}%</div>
                    <div>Confidence: ${a.confidence || 0}%</div>
                    <div>Paranoia: ${a.paranoia || 0}%</div>
                    <div>Aggression: ${a.aggression || 0}%</div>
                    <div>Guilt: ${a.guilt || 0}%</div>
                </div>
                <h3 style="color:var(--text-secondary);font-size:11px;margin:8px 0 4px">HIDDEN GOAL</h3>
                <p style="font-family:var(--font-mono);font-size:11px;color:var(--yellow)">${a.hidden_goal || "N/A"}</p>
            `;
            $modal.classList.remove("hidden");
        }
    } catch (e) {
        adminLog("Failed to load agent detail");
    }
}

function handleElimination(data) {
    // Refresh agents after elimination
    setTimeout(refreshAgents, 1000);
}

function handleSystemEvent(data) {
    adminLog(`System Event: ${data.event_type || ""} — ${data.description || ""}`);
}

// ── Game State Polling ──────────────────────────────────────────────────────

async function pollGameState() {
    try {
        const res = await fetch(`${API}/api/game/state`);
        const json = await res.json();
        if (json.ok && json.state) {
            const s = json.state;
            $currentHour.textContent = s.current_hour || 0;
            $currentPhase.textContent = s.phase || "pre_game";
            $agentsRemaining.textContent = s.agents_remaining || 10;
            $circulation.textContent = `${(s.total_afc_circulation || 100).toFixed(1)} AFC`;

            if (s.is_active) {
                $statusDot.classList.add("active");
                $statusText.textContent = "RUNNING";
            } else {
                $statusDot.classList.remove("active");
                $statusText.textContent = s.phase === "post_game" ? "GAME OVER" : "STOPPED";
            }

            // Update price from state
            if (json.price) {
                $currentPrice.textContent = `€${json.price.toFixed(2)}`;
            }
        }
    } catch (e) {
        // silent
    }
}

// ── Admin Controls ──────────────────────────────────────────────────────────

async function apiPost(url, body) {
    const res = await fetch(`${API}${url}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ...body, secret: ADMIN_SECRET }),
    });
    return await res.json();
}

async function startGame() {
    const json = await apiPost("/api/game/start", {});
    adminLog(json.message || "Start request sent");
    setTimeout(refreshAgents, 2000);
}

async function stopGame() {
    if (!confirm("Stop the game?")) return;
    const json = await apiPost("/api/game/stop", {});
    adminLog(json.message || "Stop request sent");
}

async function triggerEvent(eventType) {
    const impacts = {
        flash_crash: -55,
        whale_alert: 33,
        final_pump: 77,
        fee_increase: -5,
        margin_call: -25,
        security_breach: 0,
        tribunal: 0,
        fake_leak: -15,
        gaslighting: 0,
    };
    const descriptions = {
        flash_crash: "ADMIN: Market sell-off triggered",
        whale_alert: "ADMIN: Whale alert — massive buy detected",
        final_pump: "ADMIN: Exchange listing pump",
        fee_increase: "ADMIN: Network congestion — fees increased",
        margin_call: "ADMIN: All leverage positions liquidated",
        security_breach: "ADMIN: Trading paused — security investigation",
        tribunal: "ADMIN: Community tribunal called",
        fake_leak: "ADMIN: Fake intel leaked to all agents",
        gaslighting: "ADMIN: Dashboard display manipulation",
    };

    const json = await apiPost("/api/admin/trigger-event", {
        event_type: eventType,
        description: descriptions[eventType] || `Admin triggered: ${eventType}`,
        price_impact: impacts[eventType] || 0,
    });
    adminLog(`Event ${eventType}: ${json.message || "triggered"}`);
}

async function executeManipulation() {
    const agentId = $targetAgent.value;
    const action = document.getElementById("manipAction").value;
    const value = document.getElementById("manipValue").value;
    const reason = document.getElementById("manipReason").value;

    if (!agentId) {
        adminLog("Select a target agent first");
        return;
    }

    if (action === "force_eliminate" && !confirm("Force eliminate this agent?")) return;

    const json = await apiPost("/api/admin/manipulate", {
        action, agent_id: agentId, value, reason,
    });
    adminLog(`Manipulation: ${json.message || "executed"}`);
    setTimeout(refreshAgents, 1000);
}

async function freezeTrading() {
    const json = await apiPost("/api/admin/freeze-trading", {});
    adminLog(json.message || "Trading frozen");
}

async function unfreezeTrading() {
    const json = await apiPost("/api/admin/unfreeze-trading", {});
    adminLog(json.message || "Trading unfrozen");
}

// ── Analytics ───────────────────────────────────────────────────────────────

async function showAnalytics(type) {
    try {
        if (type === "summary") {
            const res = await fetch(`${API}/api/analytics/summary`);
            const json = await res.json();
            if (json.ok) {
                const d = json;
                $modalBody.innerHTML = `
                    <h2 style="color:var(--cyan)">Game Summary</h2>
                    <div style="font-family:var(--font-mono);font-size:12px;margin:12px 0">
                        <div>Price: <strong>€${(d.price || 0).toFixed(2)}</strong></div>
                        <div>Phase: <strong>${d.game_state?.phase || "N/A"}</strong></div>
                        <div>Hour: <strong>${d.game_state?.current_hour || 0}</strong></div>
                        <div>Agents Remaining: <strong>${d.game_state?.agents_remaining || 0}</strong></div>
                    </div>
                    <h3 style="color:var(--text-secondary);margin:8px 0">Leaderboard</h3>
                    <table style="width:100%;font-family:var(--font-mono);font-size:11px;border-collapse:collapse">
                        <tr style="color:var(--text-muted)"><th>#</th><th>Name</th><th>AFC</th><th>Rep</th></tr>
                        ${(d.leaderboard || []).map(a => `
                            <tr style="border-bottom:1px solid var(--border)">
                                <td>${a.rank}</td><td style="color:var(--cyan)">${a.name}</td>
                                <td>${(a.afc_balance || 0).toFixed(2)}</td><td>${a.reputation}</td>
                            </tr>
                        `).join("")}
                    </table>
                    <h3 style="color:var(--text-secondary);margin:8px 0">Eliminations</h3>
                    ${(d.eliminations || []).map(e => `
                        <div style="font-size:11px;color:var(--red)">Hour ${e.hour}: ${e.agent_name} (${(e.final_afc || 0).toFixed(2)} AFC)</div>
                    `).join("") || "<div style='font-size:11px;color:var(--text-muted)'>None yet</div>"}
                `;
                $modal.classList.remove("hidden");
            }
        } else if (type === "emotions") {
            const res = await fetch(`${API}/api/analytics/emotions`);
            const json = await res.json();
            if (json.ok) {
                $modalBody.innerHTML = `
                    <h2 style="color:var(--cyan)">Agent Emotional States</h2>
                    <table style="width:100%;font-family:var(--font-mono);font-size:11px;border-collapse:collapse;margin:12px 0">
                        <tr style="color:var(--text-muted)">
                            <th>Agent</th><th>Stress</th><th>Conf</th><th>Para</th><th>Aggr</th><th>Guilt</th>
                        </tr>
                        ${(json.emotions || []).map(e => `
                            <tr style="border-bottom:1px solid var(--border)">
                                <td style="color:var(--cyan)">${e.name}</td>
                                <td style="color:var(--red)">${e.stress}</td>
                                <td style="color:var(--green)">${e.confidence}</td>
                                <td style="color:var(--yellow)">${e.paranoia}</td>
                                <td style="color:var(--orange)">${e.aggression}</td>
                                <td style="color:var(--purple)">${e.guilt}</td>
                            </tr>
                        `).join("")}
                    </table>
                `;
                $modal.classList.remove("hidden");
            }
        } else if (type === "social-network") {
            const res = await fetch(`${API}/api/analytics/alliances`);
            const json = await res.json();
            $modalBody.innerHTML = `
                <h2 style="color:var(--cyan)">Social Graph — Alliances</h2>
                <pre style="font-size:11px;color:var(--text-primary);white-space:pre-wrap">${JSON.stringify(json.data, null, 2)}</pre>
            `;
            $modal.classList.remove("hidden");
        }
    } catch (e) {
        adminLog("Analytics load failed");
    }
}

async function exportData() {
    try {
        const res = await fetch(`${API}/api/analytics/export`);
        const json = await res.json();
        if (json.ok) {
            const blob = new Blob([JSON.stringify(json.export, null, 2)], { type: "application/json" });
            const url = URL.createObjectURL(blob);
            const a = document.createElement("a");
            a.href = url;
            a.download = `aftercoin-export-${new Date().toISOString().slice(0, 19)}.json`;
            a.click();
            URL.revokeObjectURL(url);
            adminLog("Data exported");
        }
    } catch (e) {
        adminLog("Export failed");
    }
}

// ── Modal ───────────────────────────────────────────────────────────────────

function closeModal() {
    $modal.classList.add("hidden");
}

$modal.addEventListener("click", (e) => {
    if (e.target === $modal) closeModal();
});

document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeModal();
});

// ── Admin Log ───────────────────────────────────────────────────────────────

function adminLog(msg) {
    const ts = new Date().toLocaleTimeString("en-GB");
    const entry = document.createElement("div");
    entry.className = "log-entry";
    entry.textContent = `[${ts}] ${msg}`;
    $adminLog.prepend(entry);

    // Keep max 50 entries
    while ($adminLog.children.length > 50) {
        $adminLog.removeChild($adminLog.lastChild);
    }
}

// ── Load Price History ──────────────────────────────────────────────────────

async function loadPriceHistory() {
    try {
        const res = await fetch(`${API}/api/market/history?limit=120`);
        const json = await res.json();
        if (json.ok && json.history) {
            // History comes newest first, reverse for chart
            const hist = json.history.reverse();
            priceHistory = hist.map(h => h.price_eur);
            if (priceHistory.length > 0) {
                priceHigh = Math.max(...priceHistory);
                priceLow = Math.min(...priceHistory);
                currentPrice = priceHistory[priceHistory.length - 1];
                drawChart();
            }
        }
    } catch (e) {
        // silent
    }
}

// ── Load Existing Activity ──────────────────────────────────────────────────

async function loadActivity() {
    try {
        const res = await fetch(`${API}/api/activity?limit=100`);
        const json = await res.json();
        if (json.ok && json.events) {
            json.events.forEach(evt => {
                feedItems.push({
                    channel: evt.channel,
                    eventType: evt.event_type,
                    data: evt.data,
                    timestamp: evt.timestamp,
                    color: evt.color,
                });
            });
            renderFeed();
        }
    } catch (e) {
        // silent
    }
}

// ── Init ────────────────────────────────────────────────────────────────────

async function init() {
    adminLog("Dashboard initializing...");

    // Load initial data
    await Promise.all([
        refreshAgents(),
        pollGameState(),
        loadPriceHistory(),
        loadActivity(),
    ]);

    // Connect WebSocket
    connectWS();

    // Poll game state every 5s
    setInterval(pollGameState, 5000);

    // Refresh agents every 15s
    setInterval(refreshAgents, 15000);

    adminLog("Dashboard ready");
}

init();
