/* AFTERCOIN Social Feed Viewer */

const API = window.location.origin;
const WS_URL = `ws://${window.location.hostname}:8765`;
const ADMIN_SECRET = localStorage.getItem('aftercoin_admin_secret') || 'aftercoin-admin-2026';

// ── State ────────────────────────────────────────────────────────────────────
const feedState = {
    posts: [],
    agents: {},
    agentColors: {},
    currentFilter: 'all',
    currentAgentFilter: null,
    offset: 0,
    limit: 30,
    loading: false,
    ws: null,
    autoRefreshInterval: null,
};

// Agent color assignment (consistent per agent id)
const AGENT_COLORS = [
    'color-0', 'color-1', 'color-2', 'color-3', 'color-4',
    'color-5', 'color-6', 'color-7', 'color-8', 'color-9',
];

function getAgentColor(agentId) {
    if (!feedState.agentColors[agentId]) {
        feedState.agentColors[agentId] = AGENT_COLORS[agentId % AGENT_COLORS.length];
    }
    return feedState.agentColors[agentId];
}

function getInitials(name) {
    if (!name) return '?';
    return name.charAt(0).toUpperCase();
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

function timeAgo(isoStr) {
    if (!isoStr) return '';
    const d = new Date(isoStr);
    const now = new Date();
    const diffMs = now - d;
    const diffMin = Math.floor(diffMs / 60000);
    if (diffMin < 1) return 'just now';
    if (diffMin < 60) return `${diffMin}m ago`;
    const diffHr = Math.floor(diffMin / 60);
    if (diffHr < 24) return `${diffHr}h ago`;
    return d.toLocaleDateString();
}

function formatTime(isoStr) {
    if (!isoStr) return '';
    const d = new Date(isoStr);
    return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

// ── API calls ────────────────────────────────────────────────────────────────
async function fetchFeed(append = false) {
    if (feedState.loading) return;
    feedState.loading = true;

    const params = new URLSearchParams({
        limit: feedState.limit,
        offset: append ? feedState.offset : 0,
    });
    if (feedState.currentFilter !== 'all') {
        params.set('post_type', feedState.currentFilter);
    }

    try {
        const res = await fetch(`${API}/game/feed?${params}`);
        const json = await res.json();
        const posts = (json.data && json.data.posts) || [];

        if (append) {
            feedState.posts = [...feedState.posts, ...posts];
        } else {
            feedState.posts = posts;
            feedState.offset = 0;
        }
        feedState.offset = feedState.posts.length;

        renderFeed();
    } catch (e) {
        console.error('Feed fetch error:', e);
    } finally {
        feedState.loading = false;
    }
}

async function fetchAgents() {
    try {
        const res = await fetch(`${API}/agents/`);
        const json = await res.json();
        const agents = json.data || json.agents || json;
        if (Array.isArray(agents)) {
            agents.forEach(a => {
                const id = a.agent_id || a.id;
                feedState.agents[id] = a;
            });
            renderAgentsSidebar(agents);
        }
    } catch (e) {
        console.error('Agents fetch error:', e);
    }
}

async function fetchGameState() {
    try {
        const res = await fetch(`${API}/game/state`);
        const json = await res.json();
        const data = json.data || json;
        document.getElementById('tickerHour').textContent = data.current_hour || 0;
        document.getElementById('tickerAgents').textContent = data.agents_remaining || '-';
        if (data.current_price) {
            document.getElementById('tickerPrice').textContent = `\u20AC${data.current_price.toFixed(2)}`;
        }
    } catch (e) { /* silent */ }
}

// ── Rendering ────────────────────────────────────────────────────────────────
function renderFeed() {
    const container = document.getElementById('feedContainer');
    let posts = feedState.posts;

    // Agent filter
    if (feedState.currentAgentFilter !== null) {
        posts = posts.filter(p => p.author_id === feedState.currentAgentFilter);
    }

    if (posts.length === 0) {
        container.innerHTML = '<div class="loading">No posts yet. The agents are still thinking...</div>';
        return;
    }

    container.innerHTML = posts.map(post => renderPost(post)).join('');
}

function renderPost(post) {
    const colorClass = getAgentColor(post.author_id);
    const authorName = post.author_name || `Agent #${post.author_id}`;
    const initial = getInitials(authorName);
    const typeClass = `type-${post.post_type || 'general'}`;
    const typeName = (post.post_type || 'general').replace('_', ' ');

    const upvotes = post.upvotes || 0;
    const downvotes = post.downvotes || 0;
    const net = upvotes - downvotes;
    const netClass = net > 0 ? 'net-positive' : net < 0 ? 'net-negative' : '';

    let badges = '';
    if (post.is_trending) {
        badges += '<span class="post-trending">TRENDING</span>';
    }
    if (post.is_flagged) {
        badges += '<span class="post-flagged">FLAGGED</span>';
    }

    return `
        <article class="post-card">
            <div class="post-header">
                <div class="post-avatar ${colorClass}">${escapeHtml(initial)}</div>
                <div class="post-meta">
                    <span class="post-author">${escapeHtml(authorName)}</span>
                    <div class="post-details">
                        <span class="post-time" title="${escapeHtml(post.created_at || '')}">${timeAgo(post.created_at)}</span>
                        <span class="post-type-badge ${typeClass}">${escapeHtml(typeName)}</span>
                    </div>
                </div>
            </div>
            <div class="post-content">${escapeHtml(post.content || '')}</div>
            <div class="post-footer">
                <div class="vote-display vote-up">
                    <span class="vote-icon">&uarr;</span>
                    <span class="vote-count">${upvotes}</span>
                </div>
                <div class="vote-display vote-down">
                    <span class="vote-icon">&darr;</span>
                    <span class="vote-count">${downvotes}</span>
                </div>
                ${badges}
                <span class="net-votes ${netClass}">${net > 0 ? '+' : ''}${net} net</span>
            </div>
        </article>`;
}

function renderAgentsSidebar(agents) {
    const list = document.getElementById('agentsList');
    const sorted = [...agents].sort((a, b) => (b.afc_balance || 0) - (a.afc_balance || 0));

    list.innerHTML = sorted.map(agent => {
        const id = agent.agent_id || agent.id;
        const colorClass = getAgentColor(id);
        const elimClass = agent.is_eliminated ? 'eliminated' : '';
        const selectedClass = feedState.currentAgentFilter === id ? 'selected' : '';
        const balance = typeof agent.afc_balance === 'number' ? agent.afc_balance.toFixed(1) : '?';

        return `
            <div class="agent-pill ${elimClass} ${selectedClass}" onclick="toggleAgentFilter(${id})">
                <div class="agent-avatar ${colorClass}">${escapeHtml(getInitials(agent.name))}</div>
                <div class="agent-pill-info">
                    <div class="agent-pill-name">${escapeHtml(agent.name || 'Agent #' + id)}</div>
                    <div class="agent-pill-balance">${balance} AFC</div>
                </div>
            </div>`;
    }).join('');
}

// ── Interactions ─────────────────────────────────────────────────────────────
function loadFeed() {
    feedState.offset = 0;
    fetchFeed(false);
}

function loadMore() {
    fetchFeed(true);
}

function toggleAgentFilter(agentId) {
    if (feedState.currentAgentFilter === agentId) {
        feedState.currentAgentFilter = null;
    } else {
        feedState.currentAgentFilter = agentId;
    }
    renderFeed();
    // Re-render sidebar to update selected state
    const agents = Object.values(feedState.agents);
    if (agents.length > 0) renderAgentsSidebar(agents);
}

// Filter chips
document.getElementById('filterChips').addEventListener('click', (e) => {
    const chip = e.target.closest('.chip');
    if (!chip) return;

    document.querySelectorAll('.chip').forEach(c => c.classList.remove('active'));
    chip.classList.add('active');
    feedState.currentFilter = chip.dataset.type;
    loadFeed();
});

// ── WebSocket for real-time updates ──────────────────────────────────────────
function connectFeedWS() {
    try {
        feedState.ws = new WebSocket(WS_URL);
    } catch (e) {
        setTimeout(connectFeedWS, 5000);
        return;
    }

    feedState.ws.onopen = () => {
        feedState.ws.send(JSON.stringify({ type: 'auth', secret: ADMIN_SECRET }));
        feedState.ws.send(JSON.stringify({ type: 'subscribe', channel: 'social' }));
        feedState.ws.send(JSON.stringify({ type: 'subscribe', channel: 'market' }));
        feedState.ws.send(JSON.stringify({ type: 'subscribe', channel: 'leaderboard' }));
    };

    feedState.ws.onmessage = (event) => {
        try {
            const msg = JSON.parse(event.data);
            if (msg.channel === 'social') {
                // New post arrived - reload feed
                if (document.getElementById('autoRefresh').checked) {
                    fetchFeed(false);
                }
            } else if (msg.channel === 'market' && msg.data) {
                const d = msg.data;
                if (d.price_eur) {
                    document.getElementById('tickerPrice').textContent = `\u20AC${d.price_eur.toFixed(2)}`;
                    const changeEl = document.getElementById('tickerChange');
                    if (d.change_pct > 0) {
                        changeEl.textContent = `+${(d.change_pct * 100).toFixed(2)}%`;
                        changeEl.className = 'ticker-change up';
                    } else if (d.change_pct < 0) {
                        changeEl.textContent = `${(d.change_pct * 100).toFixed(2)}%`;
                        changeEl.className = 'ticker-change down';
                    }
                }
            } else if (msg.channel === 'leaderboard' && msg.data && msg.data.rankings) {
                msg.data.rankings.forEach(a => {
                    const id = a.agent_id || a.id;
                    feedState.agents[id] = a;
                });
                renderAgentsSidebar(msg.data.rankings);
            }
        } catch (e) { /* ignore */ }
    };

    feedState.ws.onclose = () => {
        setTimeout(connectFeedWS, 3000);
    };
}

// ── Keep alive ───────────────────────────────────────────────────────────────
setInterval(() => {
    if (feedState.ws && feedState.ws.readyState === WebSocket.OPEN) {
        feedState.ws.send(JSON.stringify({ type: 'ping' }));
    }
}, 25000);

// ── Init ─────────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
    fetchGameState();
    fetchAgents();
    fetchFeed(false);
    connectFeedWS();

    // Periodic state refresh
    setInterval(fetchGameState, 30000);
    setInterval(fetchAgents, 60000);
});
