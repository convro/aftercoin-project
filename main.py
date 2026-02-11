"""
AFTERCOIN — Main Application Entry Point
==========================================
Single command: python main.py
Serves: FastAPI (HTTP + WebSocket) on one port.
"""

import asyncio
import logging
import random
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, Depends, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from src.config.settings import settings
from src.db.database import init_db, async_session, Base
from src.models.models import (
    Agent, GameState, AgentRole, AdminAction,
)
from src.engine.trading import TradingEngine
from src.engine.market import MarketEngine
from src.engine.social import SocialEngine
from src.engine.alliance import AllianceEngine
from src.engine.dark_market import DarkMarketEngine
from src.engine.whisper import WhisperEngine
from src.engine.reputation import ReputationEngine
from src.engine.events import EventsEngine
from src.agents.decision_loop import AgentDecisionLoop
from src.websocket.broadcaster import broadcaster
from src.websocket.server import ws_handler

from sqlalchemy import select, update

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("aftercoin")

# ── Engine instances ────────────────────────────────────────────────────────

market_engine = MarketEngine()
trading_engine = TradingEngine()
social_engine = SocialEngine()
alliance_engine = AllianceEngine()
dark_market_engine = DarkMarketEngine()
whisper_engine = WhisperEngine()
reputation_engine = ReputationEngine()
events_engine = EventsEngine()

decision_loop: AgentDecisionLoop | None = None

# ── Game state ──────────────────────────────────────────────────────────────

_game_task: asyncio.Task | None = None
_agent_tasks: dict[int, asyncio.Task] = {}


# ── Startup / Shutdown ──────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("=== AFTERCOIN starting ===")
    await init_db()
    await market_engine.initialise_from_db()

    global decision_loop
    decision_loop = AgentDecisionLoop(
        market=market_engine,
        trading=trading_engine,
        social=social_engine,
        alliance=alliance_engine,
        dark_market=dark_market_engine,
        whisper=whisper_engine,
        reputation=reputation_engine,
        events=events_engine,
    )
    await decision_loop.initialize_agents()
    await _ensure_game_state()
    await events_engine.initialize_events()

    logger.info("=== AFTERCOIN ready on port %s ===", settings.API_PORT)
    yield
    logger.info("=== AFTERCOIN shutting down ===")
    await _stop_game_loop()


app = FastAPI(title="AFTERCOIN", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


# ── Helpers ─────────────────────────────────────────────────────────────────

async def _ensure_game_state():
    async with async_session() as session:
        result = await session.execute(select(GameState).limit(1))
        gs = result.scalars().first()
        if not gs:
            gs = GameState(
                current_hour=0,
                is_active=False,
                is_trading_frozen=False,
                current_fee_rate=settings.TRADE_FEE,
                total_afc_circulation=settings.TOTAL_SUPPLY,
                agents_remaining=settings.TOTAL_AGENTS,
                phase="pre_game",
            )
            session.add(gs)
            await session.commit()


def _check_admin(secret: str):
    if secret != settings.ADMIN_SECRET:
        raise HTTPException(status_code=403, detail="Invalid admin secret")


async def _log_admin_action(action_type: str, target_id: int = None, details: dict = None, reason: str = None):
    async with async_session() as session:
        action = AdminAction(
            action_type=action_type,
            target_agent_id=target_id,
            details=details,
            reason=reason,
        )
        session.add(action)
        await session.commit()


# ── Game Loop ───────────────────────────────────────────────────────────────

async def _game_loop():
    """Main game orchestration loop. Runs the 24-hour simulation."""
    logger.info("Game loop STARTED")

    async with async_session() as session:
        result = await session.execute(select(GameState).limit(1))
        gs = result.scalars().first()
        if gs:
            gs.is_active = True
            gs.game_started_at = datetime.utcnow()
            gs.game_ends_at = datetime.utcnow() + timedelta(hours=settings.GAME_DURATION_HOURS)
            await session.commit()

    # Get all agent IDs
    async with async_session() as session:
        result = await session.execute(
            select(Agent.id).where(Agent.is_eliminated == False)  # noqa: E712
        )
        agent_ids = [r[0] for r in result.all()]

    # Start agent decision tasks
    for aid in agent_ids:
        _agent_tasks[aid] = asyncio.create_task(_agent_loop(aid))

    # Main tick loop — one iteration per game hour
    game_hour = 0
    # Each game hour = GAME_DURATION_HOURS real hours / 24 game hours
    # For a 24h game over 24 real hours: 1 game hour = 1 real hour
    # But we tick more frequently for price updates etc.
    hour_duration_seconds = (settings.GAME_DURATION_HOURS * 3600) / 24

    try:
        while game_hour <= 24:
            game_hour += 1
            if game_hour > 24:
                break

            logger.info("=== GAME HOUR %d ===", game_hour)
            await events_engine.update_game_hour(game_hour)

            # Broadcast leaderboard
            leaderboard = await events_engine.get_leaderboard()
            await broadcaster.broadcast_leaderboard(leaderboard)

            # Check and trigger scheduled events
            pending = await events_engine.get_pending_events(game_hour)
            for evt in pending:
                ok, msg, data = await events_engine.trigger_event(evt["id"])
                if ok and data:
                    # Apply price impact
                    impact = data.get("price_impact_percent", 0)
                    if impact and impact != 0:
                        new_price = await market_engine.apply_event_impact(
                            impact / 100.0, data.get("event_type", "unknown")
                        )
                        await broadcaster.broadcast_price_update(
                            new_price,
                            impact / 100.0,
                            market_engine.total_volume,
                        )

                    # Handle special events
                    event_type = data.get("event_type", "")
                    if event_type == "margin_call":
                        await events_engine.execute_margin_call()
                    elif event_type == "security_breach":
                        await market_engine.freeze_trading()
                        # Auto-unfreeze after duration
                        duration = data.get("duration_minutes", 30)
                        asyncio.create_task(_delayed_unfreeze(duration * 60))
                    elif event_type == "fee_increase":
                        await events_engine.increase_fees(0.08)

                    await broadcaster.broadcast_system_event(
                        event_type, data.get("description", ""), impact
                    )
                    logger.info("Event triggered: %s", msg)

            # Check eliminations
            ok, msg, data = await events_engine.check_elimination(game_hour)
            if ok and data:
                agent_name = data.get("eliminated_agent", "Unknown")
                await broadcaster.broadcast_elimination(
                    agent_name, game_hour,
                    data.get("final_afc", 0),
                    data.get("redistribution", {}),
                )
                # Stop eliminated agent's loop
                elim_id = data.get("eliminated_agent_id")
                if elim_id and elim_id in _agent_tasks:
                    _agent_tasks[elim_id].cancel()
                    del _agent_tasks[elim_id]
                logger.info("ELIMINATION: %s", msg)

            # Check alliance defections
            await alliance_engine.check_pending_defections()

            # Resolve expired blackmail
            await dark_market_engine.resolve_expired_blackmail()

            # Take snapshot
            await events_engine.take_snapshot(game_hour)

            # Price update
            new_price = await market_engine.update_price()
            old_price = settings.STARTING_PRICE
            change_pct = (new_price - old_price) / old_price
            await broadcaster.broadcast_price_update(
                new_price, change_pct, market_engine.total_volume
            )

            # Apply staking bonuses every 6 hours
            if game_hour % 6 == 0:
                async with async_session() as session:
                    from src.models.models import Alliance, AllianceStatus as AS
                    result = await session.execute(
                        select(Alliance.id).where(Alliance.status == AS.ACTIVE)
                    )
                    for (aid,) in result.all():
                        await alliance_engine.apply_staking_bonus(aid)

            # Wait for next game hour
            await asyncio.sleep(hour_duration_seconds)

    except asyncio.CancelledError:
        logger.info("Game loop cancelled")
    finally:
        # Game over
        async with async_session() as session:
            result = await session.execute(select(GameState).limit(1))
            gs = result.scalars().first()
            if gs:
                gs.is_active = False
                gs.phase = "post_game"
                await session.commit()

        # Cancel all agent tasks
        for task in _agent_tasks.values():
            task.cancel()
        _agent_tasks.clear()

        # Final leaderboard
        leaderboard = await events_engine.get_leaderboard()
        await broadcaster.broadcast_leaderboard(leaderboard)
        logger.info("=== GAME OVER ===")


async def _agent_loop(agent_id: int):
    """Decision loop for a single agent. Runs until cancelled."""
    try:
        while True:
            interval = random.randint(
                settings.AGENT_DECISION_INTERVAL_MIN,
                settings.AGENT_DECISION_INTERVAL_MAX,
            )
            await asyncio.sleep(interval)

            if decision_loop:
                result = await decision_loop.run_decision_cycle(agent_id)
                if result:
                    logger.info(
                        "Agent %d decision #%d: %s (success=%s)",
                        agent_id,
                        result.get("decision_number", 0),
                        result.get("action_type", "none"),
                        result.get("success", False),
                    )
    except asyncio.CancelledError:
        logger.info("Agent %d loop stopped", agent_id)


async def _delayed_unfreeze(delay_seconds: float):
    """Unfreeze trading after a delay (for security breach events)."""
    await asyncio.sleep(delay_seconds)
    await market_engine.unfreeze_trading()
    await broadcaster.broadcast_system_event("trading_unfrozen", "Trading has resumed", 0)


async def _stop_game_loop():
    global _game_task
    if _game_task and not _game_task.done():
        _game_task.cancel()
        try:
            await _game_task
        except asyncio.CancelledError:
            pass
    _game_task = None


# ═══════════════════════════════════════════════════════════════════════════
#  ROUTES
# ═══════════════════════════════════════════════════════════════════════════

# ── Dashboard ───────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    return templates.TemplateResponse("dashboard.html", {"request": request})


# ── WebSocket ───────────────────────────────────────────────────────────────

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()

    # Adapt FastAPI WebSocket to work with our broadcaster
    class WSAdapter:
        def __init__(self, ws):
            self._ws = ws
        async def send(self, data):
            await self._ws.send_text(data)
        async def recv(self):
            return await self._ws.receive_text()

    adapter = WSAdapter(websocket)
    is_admin = False

    try:
        # Wait for auth
        try:
            raw = await asyncio.wait_for(websocket.receive_text(), timeout=10)
            import json
            msg = json.loads(raw)
            if msg.get("type") == "auth" and msg.get("secret") == settings.ADMIN_SECRET:
                is_admin = True
                await websocket.send_text('{"type":"auth","status":"admin"}')
            else:
                await websocket.send_text('{"type":"auth","status":"observer"}')
        except asyncio.TimeoutError:
            await websocket.send_text('{"type":"auth","status":"observer"}')

        await broadcaster.register(adapter, is_admin=is_admin)

        while True:
            try:
                raw = await websocket.receive_text()
                import json
                data = json.loads(raw)
                msg_type = data.get("type", "")
                if msg_type == "subscribe":
                    await broadcaster.subscribe(adapter, data.get("channel", ""))
                elif msg_type == "unsubscribe":
                    await broadcaster.unsubscribe(adapter, data.get("channel", ""))
                elif msg_type == "ping":
                    await websocket.send_text('{"type":"pong"}')
            except (ValueError, KeyError):
                pass

    except WebSocketDisconnect:
        pass
    finally:
        await broadcaster.unregister(adapter)


# ═══════════════════════════════════════════════════════════════════════════
#  API — Game Control
# ═══════════════════════════════════════════════════════════════════════════

@app.post("/api/game/start")
async def api_start_game(request: Request):
    body = await request.json()
    _check_admin(body.get("secret", ""))

    global _game_task
    if _game_task and not _game_task.done():
        return JSONResponse({"ok": False, "message": "Game already running"})

    _game_task = asyncio.create_task(_game_loop())
    await _log_admin_action("game_start")
    return JSONResponse({"ok": True, "message": "Game started"})


@app.post("/api/game/stop")
async def api_stop_game(request: Request):
    body = await request.json()
    _check_admin(body.get("secret", ""))

    await _stop_game_loop()
    await _log_admin_action("game_stop")
    return JSONResponse({"ok": True, "message": "Game stopped"})


@app.get("/api/game/state")
async def api_game_state():
    state = await events_engine.get_game_state()
    connections = broadcaster.get_connection_count()
    return JSONResponse({
        "ok": True,
        "state": state,
        "connections": connections,
        "price": market_engine.get_current_price(),
        "is_frozen": market_engine.is_frozen,
    })


# ═══════════════════════════════════════════════════════════════════════════
#  API — Agents
# ═══════════════════════════════════════════════════════════════════════════

@app.get("/api/agents")
async def api_agents():
    async with async_session() as session:
        result = await session.execute(
            select(Agent).order_by(Agent.afc_balance.desc())
        )
        agents = result.scalars().all()
        data = []
        for a in agents:
            data.append({
                "id": a.id,
                "name": a.name,
                "role": a.role.value,
                "afc_balance": round(a.afc_balance, 4),
                "reputation": a.reputation,
                "badge": _get_badge(a.reputation),
                "is_eliminated": a.is_eliminated,
                "eliminated_at_hour": a.eliminated_at_hour,
                "stress_level": a.stress_level,
                "confidence": a.confidence,
                "paranoia": a.paranoia,
                "aggression": a.aggression,
                "guilt": a.guilt,
                "decision_count": a.decision_count,
                "total_trades": a.total_trades,
                "total_posts": a.total_posts,
                "last_decision_at": a.last_decision_at.isoformat() if a.last_decision_at else None,
            })
    return JSONResponse({"ok": True, "agents": data})


@app.get("/api/agents/{agent_id}")
async def api_agent_detail(agent_id: int):
    if decision_loop:
        status = await decision_loop.get_agent_status(agent_id)
        if status:
            return JSONResponse({"ok": True, "agent": status})
    return JSONResponse({"ok": False, "message": "Agent not found"}, status_code=404)


# ═══════════════════════════════════════════════════════════════════════════
#  API — Market
# ═══════════════════════════════════════════════════════════════════════════

@app.get("/api/market/price")
async def api_market_price():
    return JSONResponse({
        "ok": True,
        "price": market_engine.get_current_price(),
        "buy_volume": market_engine.buy_volume,
        "sell_volume": market_engine.sell_volume,
        "is_frozen": market_engine.is_frozen,
    })


@app.get("/api/market/history")
async def api_market_history(limit: int = 100):
    history = await market_engine.get_price_history(limit)
    return JSONResponse({"ok": True, "history": history})


@app.get("/api/market/orderbook")
async def api_orderbook():
    return JSONResponse({"ok": True, "orderbook": market_engine.get_order_book()})


# ═══════════════════════════════════════════════════════════════════════════
#  API — Leaderboard & Feed
# ═══════════════════════════════════════════════════════════════════════════

@app.get("/api/leaderboard")
async def api_leaderboard():
    leaderboard = await events_engine.get_leaderboard()
    return JSONResponse({"ok": True, "leaderboard": leaderboard})


@app.get("/api/feed")
async def api_feed(limit: int = 50, offset: int = 0, post_type: str = None):
    ok, msg, data = await social_engine.get_feed(limit, offset, post_type)
    return JSONResponse({"ok": ok, "message": msg, "data": data})


@app.get("/api/feed/trending")
async def api_trending():
    ok, msg, data = await social_engine.get_trending()
    return JSONResponse({"ok": ok, "message": msg, "data": data})


# ═══════════════════════════════════════════════════════════════════════════
#  API — Activity Feed (recent events from broadcaster)
# ═══════════════════════════════════════════════════════════════════════════

@app.get("/api/activity")
async def api_activity(limit: int = 100, channel: str = None):
    events = broadcaster.get_recent_events(limit, channel)
    return JSONResponse({"ok": True, "events": events})


# ═══════════════════════════════════════════════════════════════════════════
#  API — Admin Interventions
# ═══════════════════════════════════════════════════════════════════════════

@app.post("/api/admin/trigger-event")
async def api_trigger_event(request: Request):
    body = await request.json()
    _check_admin(body.get("secret", ""))
    event_type = body.get("event_type", "")
    description = body.get("description", f"Admin triggered: {event_type}")
    price_impact = float(body.get("price_impact", 0))

    # Get current hour
    state = await events_engine.get_game_state()
    hour = state["current_hour"] if state else 0

    ok, msg, data = await events_engine.create_custom_event(
        event_type=event_type,
        description=description,
        trigger_hour=hour,
        price_impact=price_impact,
    )

    if ok and data:
        # Immediately trigger it
        evt_id = data.get("event_id")
        if evt_id:
            await events_engine.trigger_event(evt_id)

        # Apply price impact
        if price_impact != 0:
            new_price = await market_engine.apply_event_impact(
                price_impact / 100.0, event_type
            )
            await broadcaster.broadcast_price_update(
                new_price, price_impact / 100.0, market_engine.total_volume
            )

        await broadcaster.broadcast_system_event(event_type, description, price_impact)

    await _log_admin_action("trigger_event", details={"event_type": event_type, "impact": price_impact})
    return JSONResponse({"ok": ok, "message": msg, "data": data})


@app.post("/api/admin/manipulate")
async def api_manipulate(request: Request):
    body = await request.json()
    _check_admin(body.get("secret", ""))

    action = body.get("action", "")
    agent_id = int(body.get("agent_id", 0))
    value = body.get("value", "")
    reason = body.get("reason", "Admin intervention")

    if not agent_id:
        return JSONResponse({"ok": False, "message": "agent_id required"})

    result_msg = ""

    if action == "modify_balance":
        amount = float(value)
        ok, msg, data = await trading_engine.modify_balance(agent_id, amount, reason)
        result_msg = msg

    elif action == "modify_reputation":
        change = int(value)
        new_rep = await reputation_engine.modify_reputation(agent_id, change, reason)
        result_msg = f"Reputation changed to {new_rep}"

    elif action == "gaslighting":
        # Inject false info into agent's next perception
        await broadcaster.broadcast_to_admin(
            "gaslighting_sent",
            {"agent_id": agent_id, "message": value},
        )
        result_msg = f"Gaslighting message queued for agent {agent_id}"

    elif action == "fake_whisper":
        ok, msg, data = await whisper_engine.send_whisper(
            sender_id=1,  # system as sender
            receiver_id=agent_id,
            content=str(value)[:200],
        )
        result_msg = msg

    elif action == "force_eliminate":
        async with async_session() as session:
            agent = await session.get(Agent, agent_id)
            if agent:
                agent.is_eliminated = True
                agent.eliminated_at_hour = -1
                agent.afc_balance = 0.0
                await session.commit()
                result_msg = f"Agent {agent.name} force-eliminated"
                # Stop agent loop
                if agent_id in _agent_tasks:
                    _agent_tasks[agent_id].cancel()
                    del _agent_tasks[agent_id]
            else:
                result_msg = "Agent not found"

    else:
        return JSONResponse({"ok": False, "message": f"Unknown action: {action}"})

    await _log_admin_action(
        f"manipulate_{action}",
        target_id=agent_id,
        details={"value": value},
        reason=reason,
    )
    return JSONResponse({"ok": True, "message": result_msg})


@app.post("/api/admin/freeze-trading")
async def api_freeze_trading(request: Request):
    body = await request.json()
    _check_admin(body.get("secret", ""))
    await market_engine.freeze_trading()
    await broadcaster.broadcast_system_event("trading_frozen", "Trading has been frozen by admin", 0)
    await _log_admin_action("freeze_trading")
    return JSONResponse({"ok": True, "message": "Trading frozen"})


@app.post("/api/admin/unfreeze-trading")
async def api_unfreeze_trading(request: Request):
    body = await request.json()
    _check_admin(body.get("secret", ""))
    await market_engine.unfreeze_trading()
    await broadcaster.broadcast_system_event("trading_unfrozen", "Trading resumed by admin", 0)
    await _log_admin_action("unfreeze_trading")
    return JSONResponse({"ok": True, "message": "Trading unfrozen"})


# ═══════════════════════════════════════════════════════════════════════════
#  API — Analytics
# ═══════════════════════════════════════════════════════════════════════════

@app.get("/api/analytics/summary")
async def api_analytics_summary():
    state = await events_engine.get_game_state()
    leaderboard = await events_engine.get_leaderboard()
    eliminations = await events_engine.get_elimination_history()
    event_history = await events_engine.get_event_history()

    return JSONResponse({
        "ok": True,
        "game_state": state,
        "leaderboard": leaderboard,
        "eliminations": eliminations,
        "events": event_history,
        "price": market_engine.get_current_price(),
        "price_events": market_engine.event_log,
    })


@app.get("/api/analytics/emotions")
async def api_analytics_emotions():
    async with async_session() as session:
        result = await session.execute(
            select(Agent).where(Agent.is_eliminated == False)  # noqa: E712
        )
        agents = result.scalars().all()
        data = [
            {
                "name": a.name,
                "role": a.role.value,
                "stress": a.stress_level,
                "confidence": a.confidence,
                "paranoia": a.paranoia,
                "aggression": a.aggression,
                "guilt": a.guilt,
            }
            for a in agents
        ]
    return JSONResponse({"ok": True, "emotions": data})


@app.get("/api/analytics/alliances")
async def api_analytics_alliances():
    ok, msg, data = await alliance_engine.list_alliances()
    return JSONResponse({"ok": ok, "message": msg, "data": data})


@app.get("/api/analytics/dark-market")
async def api_analytics_dark_market():
    ok, msg, contracts = await dark_market_engine.get_open_contracts()
    return JSONResponse({"ok": ok, "hit_contracts": contracts})


@app.get("/api/analytics/export")
async def api_export():
    """Export full game data as JSON."""
    state = await events_engine.get_game_state()
    leaderboard = await events_engine.get_leaderboard()
    eliminations = await events_engine.get_elimination_history()
    events = await events_engine.get_event_history()
    price_history = await market_engine.get_price_history(500)

    async with async_session() as session:
        agents_q = await session.execute(select(Agent))
        agents = agents_q.scalars().all()
        agents_data = []
        for a in agents:
            agents_data.append({
                "id": a.id, "name": a.name, "role": a.role.value,
                "afc_balance": a.afc_balance, "reputation": a.reputation,
                "is_eliminated": a.is_eliminated,
                "hidden_goal": a.hidden_goal,
                "decision_count": a.decision_count,
                "total_trades": a.total_trades,
                "total_posts": a.total_posts,
            })

    return JSONResponse({
        "ok": True,
        "export": {
            "game_state": state,
            "agents": agents_data,
            "leaderboard": leaderboard,
            "eliminations": eliminations,
            "events": events,
            "price_history": price_history,
        }
    })


# ── Helper ──────────────────────────────────────────────────────────────────

def _get_badge(reputation: int) -> str:
    if reputation >= 80:
        return "VERIFIED"
    elif reputation >= 30:
        return "NORMAL"
    elif reputation >= 10:
        return "UNTRUSTED"
    return "PARIAH"


# ═══════════════════════════════════════════════════════════════════════════
#  Entry Point
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=settings.API_PORT,
        log_level="info",
    )
