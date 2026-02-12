"""
Agent status routes for the AFTERCOIN API.

Provides endpoints for querying agent information, decision history,
trade history, posts, and whispers (admin-only).
"""

import logging

from fastapi import APIRouter, Header, HTTPException, Query
from sqlalchemy import select, desc

from src.config.settings import settings
from src.db.database import async_session
from src.models.models import (
    Agent,
    AgentDecision,
    Trade,
    Post,
    Whisper,
    TradeStatus,
)
from src.engine.social import SocialEngine
from src.engine.whisper import WhisperEngine
from src.engine.trading import TradingEngine

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agents", tags=["agents"])

# Engine singletons
_social_engine = SocialEngine()
_whisper_engine = WhisperEngine()
_trading_engine = TradingEngine()


# ── Helpers ──────────────────────────────────────────────────────────────────

def _get_badge(reputation: int) -> str:
    """Map a numeric reputation to its badge string."""
    if reputation >= 80:
        return "VERIFIED"
    elif reputation >= 30:
        return "NORMAL"
    elif reputation >= 10:
        return "UNTRUSTED"
    return "PARIAH"


# ── GET /agents/ ─────────────────────────────────────────────────────────────

@router.get("/")
async def list_agents():
    """Return all agents with basic public information."""
    try:
        async with async_session() as session:
            result = await session.execute(
                select(Agent).order_by(Agent.afc_balance.desc())
            )
            agents = result.scalars().all()

            agent_list = [
                {
                    "id": a.id,
                    "name": a.name,
                    "role": a.role.value,
                    "afc_balance": round(a.afc_balance, 4),
                    "reputation": a.reputation,
                    "badge": _get_badge(a.reputation),
                    "is_eliminated": a.is_eliminated,
                    "eliminated_at_hour": a.eliminated_at_hour,
                    "decision_count": a.decision_count,
                    "total_trades": a.total_trades,
                    "total_posts": a.total_posts,
                }
                for a in agents
            ]

        return {"status": "ok", "data": {"agents": agent_list}}

    except Exception as exc:
        logger.exception("Failed to list agents")
        raise HTTPException(status_code=500, detail=str(exc))


# ── GET /agents/{agent_id} ───────────────────────────────────────────────────

@router.get("/{agent_id}")
async def get_agent(agent_id: int):
    """Return detailed status for a single agent.

    Includes balance, reputation, emotional state, and activity counters.
    """
    try:
        async with async_session() as session:
            agent = await session.get(Agent, agent_id)
            if agent is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"Agent {agent_id} not found.",
                )

            data = {
                "id": agent.id,
                "name": agent.name,
                "role": agent.role.value,
                "afc_balance": round(agent.afc_balance, 4),
                "reputation": agent.reputation,
                "badge": _get_badge(agent.reputation),
                "is_eliminated": agent.is_eliminated,
                "eliminated_at_hour": agent.eliminated_at_hour,
                "stress_level": agent.stress_level,
                "confidence": agent.confidence,
                "paranoia": agent.paranoia,
                "aggression": agent.aggression,
                "guilt": agent.guilt,
                "decision_count": agent.decision_count,
                "total_trades": agent.total_trades,
                "total_posts": agent.total_posts,
                "last_decision_at": (
                    agent.last_decision_at.isoformat()
                    if agent.last_decision_at
                    else None
                ),
                "created_at": (
                    agent.created_at.isoformat()
                    if agent.created_at
                    else None
                ),
            }

        return {"status": "ok", "data": data}

    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Failed to get agent %s", agent_id)
        raise HTTPException(status_code=500, detail=str(exc))


# ── GET /agents/{agent_id}/decisions ─────────────────────────────────────────

@router.get("/{agent_id}/decisions")
async def get_agent_decisions(
    agent_id: int,
    limit: int = Query(default=20, ge=1, le=100),
):
    """Return the decision history for an agent.

    Query Parameters
    ----------------
    limit : int
        Maximum number of decisions to return (default 20, max 100).
    """
    try:
        async with async_session() as session:
            # Verify agent exists
            agent = await session.get(Agent, agent_id)
            if agent is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"Agent {agent_id} not found.",
                )

            result = await session.execute(
                select(AgentDecision)
                .where(AgentDecision.agent_id == agent_id)
                .order_by(desc(AgentDecision.timestamp))
                .limit(limit)
            )
            decisions = result.scalars().all()

            decision_list = [
                {
                    "id": d.id,
                    "decision_number": d.decision_number,
                    "timestamp": (
                        d.timestamp.isoformat() if d.timestamp else None
                    ),
                    "action_type": (
                        d.action_type.value
                        if d.action_type
                        else None
                    ),
                    "action_details": d.action_details,
                    "reasoning": d.reasoning,
                    "emotional_markers": d.emotional_markers,
                    "execution_success": d.execution_success,
                    "execution_notes": d.execution_notes,
                    "balance_after": d.balance_after,
                    "reputation_after": d.reputation_after,
                    "api_model": d.api_model,
                    "api_tokens_input": d.api_tokens_input,
                    "api_tokens_output": d.api_tokens_output,
                    "api_cost_usd": d.api_cost_usd,
                    "api_latency_ms": d.api_latency_ms,
                }
                for d in decisions
            ]

        return {
            "status": "ok",
            "data": {
                "agent_id": agent_id,
                "agent_name": agent.name,
                "decisions": decision_list,
            },
        }

    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Failed to get decisions for agent %s", agent_id)
        raise HTTPException(status_code=500, detail=str(exc))


# ── GET /agents/{agent_id}/trades ────────────────────────────────────────────

@router.get("/{agent_id}/trades")
async def get_agent_trades(agent_id: int):
    """Return the trade history for an agent (sent and received)."""
    try:
        async with async_session() as session:
            agent = await session.get(Agent, agent_id)
            if agent is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"Agent {agent_id} not found.",
                )

        success, message, data = await _trading_engine.get_trade_history(
            agent_id=agent_id,
            limit=50,
        )
        if not success:
            raise HTTPException(status_code=500, detail=message)

        return {
            "status": "ok",
            "message": message,
            "data": {
                "agent_id": agent_id,
                **data,
            },
        }

    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Failed to get trades for agent %s", agent_id)
        raise HTTPException(status_code=500, detail=str(exc))


# ── GET /agents/{agent_id}/posts ─────────────────────────────────────────────

@router.get("/{agent_id}/posts")
async def get_agent_posts(agent_id: int):
    """Return all posts authored by an agent."""
    try:
        success, message, data = await _social_engine.get_agent_posts(
            agent_id=agent_id,
            limit=50,
        )
        if not success:
            raise HTTPException(status_code=404, detail=message)

        return {
            "status": "ok",
            "message": message,
            "data": {
                "agent_id": agent_id,
                **data,
            },
        }

    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Failed to get posts for agent %s", agent_id)
        raise HTTPException(status_code=500, detail=str(exc))


# ── GET /agents/{agent_id}/whispers ──────────────────────────────────────────

@router.get("/{agent_id}/whispers")
async def get_agent_whispers(
    agent_id: int,
    x_admin_secret: str = Header(..., alias="X-Admin-Secret"),
):
    """Return all whispers involving an agent (sent and received).

    **Admin only** -- requires the ``X-Admin-Secret`` header.
    Whispers are private by design; this endpoint exposes them for
    administrative oversight.
    """
    if x_admin_secret != settings.ADMIN_SECRET:
        raise HTTPException(status_code=403, detail="Invalid admin secret.")

    try:
        async with async_session() as session:
            agent = await session.get(Agent, agent_id)
            if agent is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"Agent {agent_id} not found.",
                )

        whispers = await _whisper_engine.get_all_whispers_for_agent(agent_id)

        return {
            "status": "ok",
            "data": {
                "agent_id": agent_id,
                "whispers": whispers,
            },
        }

    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Failed to get whispers for agent %s", agent_id)
        raise HTTPException(status_code=500, detail=str(exc))
