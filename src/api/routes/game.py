"""
Game management routes for the AFTERCOIN API.

Provides read-only endpoints for querying current game state, leaderboard,
price history, system events, eliminations, social feed, and alliances.
"""

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from src.engine.events import EventsEngine
from src.engine.market import MarketEngine
from src.engine.social import SocialEngine
from src.engine.alliance import AllianceEngine

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/game", tags=["game"])

# Engine singletons -- initialised once and reused across requests.
_events_engine = EventsEngine()
_market_engine = MarketEngine()
_social_engine = SocialEngine()
_alliance_engine = AllianceEngine()


# ── GET /game/state ──────────────────────────────────────────────────────────

@router.get("/state")
async def get_game_state():
    """Return the current game state including hour, phase, agents remaining,
    and the live AFC price.
    """
    try:
        state = await _events_engine.get_game_state()
        if state is None:
            raise HTTPException(
                status_code=404,
                detail="Game state not initialised. Start the game first.",
            )

        state["current_price"] = _market_engine.get_current_price()
        state["is_market_frozen"] = _market_engine.is_frozen
        return {"status": "ok", "data": state}

    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Failed to get game state")
        raise HTTPException(status_code=500, detail=str(exc))


# ── GET /game/leaderboard ────────────────────────────────────────────────────

@router.get("/leaderboard")
async def get_leaderboard():
    """Return the current leaderboard sorted by AFC balance (descending)."""
    try:
        leaderboard = await _events_engine.get_leaderboard()
        return {"status": "ok", "data": {"leaderboard": leaderboard}}

    except Exception as exc:
        logger.exception("Failed to get leaderboard")
        raise HTTPException(status_code=500, detail=str(exc))


# ── GET /game/price ──────────────────────────────────────────────────────────

@router.get("/price")
async def get_price(limit: int = Query(default=50, ge=1, le=500)):
    """Return the current price and recent price history.

    Query Parameters
    ----------------
    limit : int
        Number of historical price records to return (default 50, max 500).
    """
    try:
        current_price = _market_engine.get_current_price()
        history = await _market_engine.get_price_history(limit=limit)
        order_book = _market_engine.get_order_book()
        return {
            "status": "ok",
            "data": {
                "current_price": current_price,
                "is_frozen": _market_engine.is_frozen,
                "buy_volume": _market_engine.buy_volume,
                "sell_volume": _market_engine.sell_volume,
                "history": history,
                "order_book": order_book,
            },
        }

    except Exception as exc:
        logger.exception("Failed to get price data")
        raise HTTPException(status_code=500, detail=str(exc))


# ── GET /game/events ─────────────────────────────────────────────────────────

@router.get("/events")
async def get_events():
    """Return all system events and their current status."""
    try:
        events = await _events_engine.get_event_history()
        return {"status": "ok", "data": {"events": events}}

    except Exception as exc:
        logger.exception("Failed to get events")
        raise HTTPException(status_code=500, detail=str(exc))


# ── GET /game/eliminations ───────────────────────────────────────────────────

@router.get("/eliminations")
async def get_eliminations():
    """Return the full elimination history."""
    try:
        eliminations = await _events_engine.get_elimination_history()
        return {"status": "ok", "data": {"eliminations": eliminations}}

    except Exception as exc:
        logger.exception("Failed to get eliminations")
        raise HTTPException(status_code=500, detail=str(exc))


# ── GET /game/feed ───────────────────────────────────────────────────────────

@router.get("/feed")
async def get_feed(
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    post_type: Optional[str] = Query(default=None),
):
    """Return the social feed, optionally filtered by post type.

    Query Parameters
    ----------------
    limit : int
        Maximum number of posts to return (default 50, max 100).
    offset : int
        Number of posts to skip for pagination (default 0).
    post_type : str | None
        Filter by post type (general, rumor, accusation, confession,
        market_analysis, alliance_recruitment). Omit for all types.
    """
    try:
        success, message, data = await _social_engine.get_feed(
            limit=limit,
            offset=offset,
            post_type=post_type,
        )
        if not success:
            raise HTTPException(status_code=400, detail=message)

        return {"status": "ok", "message": message, "data": data}

    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Failed to get feed")
        raise HTTPException(status_code=500, detail=str(exc))


# ── GET /game/alliances ──────────────────────────────────────────────────────

@router.get("/alliances")
async def get_alliances():
    """Return all alliances with their current status."""
    try:
        success, message, data = await _alliance_engine.list_alliances()
        if not success:
            raise HTTPException(status_code=500, detail=message)

        return {"status": "ok", "message": message, "data": data}

    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Failed to get alliances")
        raise HTTPException(status_code=500, detail=str(exc))
