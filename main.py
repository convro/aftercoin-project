"""
AFTERCOIN - Main Entry Point
============================
Starts the FastAPI server, WebSocket server, and game orchestrator.

Usage:
    python main.py              # Start the server (game starts via admin API)
    python main.py --autostart  # Start the server AND auto-start the game
"""

import argparse
import asyncio
import logging
import signal
import sys
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from src.config.settings import settings
from src.db.database import init_db
from src.engine.market import MarketEngine
from src.engine.trading import TradingEngine
from src.engine.social import SocialEngine
from src.engine.alliance import AllianceEngine
from src.engine.dark_market import DarkMarketEngine
from src.engine.whisper import WhisperEngine
from src.engine.reputation import ReputationEngine
from src.engine.events import EventsEngine
from src.engine.orchestrator import GameOrchestrator
from src.agents.decision_loop import AgentDecisionLoop
from src.websocket.server import start_ws_server
from src.websocket.broadcaster import broadcaster
from src.api.routes import game as game_routes
from src.api.routes import agents as agent_routes
from src.api.routes import admin as admin_routes

# ── Logging ────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("aftercoin.log", mode="a"),
    ],
)
logger = logging.getLogger("aftercoin")

# ── Engine Singletons ──────────────────────────────────────────────────────────

market_engine = MarketEngine()
trading_engine = TradingEngine()
social_engine = SocialEngine()
alliance_engine = AllianceEngine()
dark_market_engine = DarkMarketEngine()
whisper_engine = WhisperEngine()
reputation_engine = ReputationEngine()
events_engine = EventsEngine()

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

orchestrator = GameOrchestrator(
    market=market_engine,
    trading=trading_engine,
    social=social_engine,
    alliance=alliance_engine,
    dark_market=dark_market_engine,
    whisper=whisper_engine,
    reputation=reputation_engine,
    events=events_engine,
    decision_loop=decision_loop,
)

# ── FastAPI App ────────────────────────────────────────────────────────────────

_autostart = False
_ws_server = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown lifecycle for the FastAPI app."""
    global _ws_server

    logger.info("=" * 60)
    logger.info("AFTERCOIN Platform Starting")
    logger.info("=" * 60)

    # Initialize database
    await init_db()
    logger.info("Database initialized")

    # Initialize market engine from DB if resuming
    await market_engine.initialise_from_db()

    # Start WebSocket server
    _ws_server = await start_ws_server()
    logger.info("WebSocket server running on port %d", settings.WS_PORT)

    # Auto-start game if requested
    if _autostart:
        logger.info("Auto-starting game...")
        await orchestrator.start_game()

    logger.info("LLM Provider: %s (model: %s)", settings.LLM_PROVIDER, settings.AGENT_MODEL)
    logger.info("API server ready on port %d", settings.API_PORT)
    logger.info("Dashboard: http://localhost:%d/dashboard", settings.API_PORT)
    logger.info("=" * 60)

    yield

    # Shutdown
    logger.info("Shutting down...")
    if orchestrator.is_running:
        await orchestrator.stop_game()
    if _ws_server:
        _ws_server.close()
        await _ws_server.wait_closed()
    logger.info("AFTERCOIN Platform stopped")


app = FastAPI(
    title="AFTERCOIN Platform",
    description="AI Agent Social Deduction Crypto Trading Simulation",
    version="1.0.0",
    lifespan=lifespan,
)

# Mount static files
app.mount("/static", StaticFiles(directory="static"), name="static")

# Templates
templates = Jinja2Templates(directory="templates")

# Include API routers
app.include_router(game_routes.router)
app.include_router(agent_routes.router)
app.include_router(admin_routes.router)


# ── Dashboard Route ────────────────────────────────────────────────────────────

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    """Serve the admin dashboard."""
    return templates.TemplateResponse("dashboard.html", {"request": request})


@app.get("/")
async def root():
    """Root endpoint with platform info."""
    return {
        "platform": "AFTERCOIN",
        "version": "1.0.0",
        "status": "running" if orchestrator.is_running else "idle",
        "llm_provider": settings.LLM_PROVIDER,
        "llm_model": settings.AGENT_MODEL,
        "dashboard": f"http://localhost:{settings.API_PORT}/dashboard",
        "api_docs": f"http://localhost:{settings.API_PORT}/docs",
        "websocket": f"ws://localhost:{settings.WS_PORT}",
        "game_state": orchestrator.game_state,
    }


# ── Orchestrator Access Endpoints ──────────────────────────────────────────────
# These provide the admin routes with access to the actual orchestrator instance

@app.post("/admin/start")
async def admin_start_game(request: Request):
    """Start the game via the orchestrator."""
    secret = request.headers.get("X-Admin-Secret", "")
    if secret != settings.ADMIN_SECRET:
        return JSONResponse(status_code=403, content={"error": "Invalid admin secret"})
    if orchestrator.is_running:
        return JSONResponse(status_code=400, content={"error": "Game already running"})
    await orchestrator.start_game()
    return {"status": "ok", "message": "Game started", "game_state": orchestrator.game_state}


@app.post("/admin/stop")
async def admin_stop_game(request: Request):
    """Stop the game via the orchestrator."""
    secret = request.headers.get("X-Admin-Secret", "")
    if secret != settings.ADMIN_SECRET:
        return JSONResponse(status_code=403, content={"error": "Invalid admin secret"})
    if not orchestrator.is_running:
        return JSONResponse(status_code=400, content={"error": "Game not running"})
    await orchestrator.stop_game()
    return {"status": "ok", "message": "Game stopped"}


# ── Error Handlers ─────────────────────────────────────────────────────────────

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error("Unhandled exception: %s", exc, exc_info=True)
    return JSONResponse(
        status_code=500,
        content={"error": "Internal server error", "detail": str(exc)},
    )


# ── Entry Point ────────────────────────────────────────────────────────────────

def main():
    global _autostart

    parser = argparse.ArgumentParser(description="AFTERCOIN Platform")
    parser.add_argument(
        "--autostart",
        action="store_true",
        help="Automatically start the game on server launch",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=settings.API_PORT,
        help=f"API server port (default: {settings.API_PORT})",
    )
    parser.add_argument(
        "--host",
        type=str,
        default="0.0.0.0",
        help="API server host (default: 0.0.0.0)",
    )
    args = parser.parse_args()

    _autostart = args.autostart

    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        log_level="info",
        access_log=False,
    )


if __name__ == "__main__":
    main()
