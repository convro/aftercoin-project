"""
Admin control routes for the AFTERCOIN game simulation.

Provides privileged endpoints for game management, agent manipulation,
event triggering, analytics, and data export. All routes require the
``X-Admin-Secret`` header to match the configured admin secret.
"""

import logging
import random
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from src.config.settings import settings
from src.db.database import async_session
from src.models.models import Agent, AdminAction, Elimination, GameState
from src.engine.events import EventsEngine
from src.engine.reputation import ReputationEngine
from src.engine.whisper import WhisperEngine
from src.logging.analytics import AnalyticsEngine

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])

# Engine singletons -- initialised once and reused across requests.
_events_engine = EventsEngine()
_reputation_engine = ReputationEngine()
_whisper_engine = WhisperEngine()
_analytics_engine = AnalyticsEngine()


# ── Auth dependency ───────────────────────────────────────────────────────────

async def verify_admin(x_admin_secret: str = Header(..., alias="X-Admin-Secret")):
    """Validate the admin secret header on every admin route."""
    if x_admin_secret != settings.ADMIN_SECRET:
        raise HTTPException(status_code=403, detail="Invalid admin secret.")
    return True


# ── Request bodies ────────────────────────────────────────────────────────────

class TriggerEventRequest(BaseModel):
    event_id: Optional[int] = None
    event_type: Optional[str] = None
    description: Optional[str] = None
    price_impact: Optional[float] = None
    duration_minutes: Optional[int] = None


class ModifyBalanceRequest(BaseModel):
    agent_id: int
    amount: float
    reason: str


class ModifyReputationRequest(BaseModel):
    agent_id: int
    change: int
    reason: str


class GaslightingRequest(BaseModel):
    agent_id: int
    fake_balance: float


class ForceEliminationRequest(BaseModel):
    agent_id: int
    reason: str


class SendFakeWhisperRequest(BaseModel):
    target_id: int
    content: str = Field(..., max_length=200)


# ── POST /admin/start ────────────────────────────────────────────────────────

@router.post("/start")
async def start_game(_: bool = Depends(verify_admin)):
    """Start the game.

    This is a placeholder -- use the orchestrator to start the full game loop.
    """
    return {
        "status": "ok",
        "message": "Use the orchestrator to start the game. This endpoint is a placeholder.",
    }


# ── POST /admin/stop ─────────────────────────────────────────────────────────

@router.post("/stop")
async def stop_game(_: bool = Depends(verify_admin)):
    """Stop the game.

    This is a placeholder -- use the orchestrator to stop the full game loop.
    """
    return {
        "status": "ok",
        "message": "Use the orchestrator to stop the game. This endpoint is a placeholder.",
    }


# ── POST /admin/trigger-event ────────────────────────────────────────────────

@router.post("/trigger-event")
async def trigger_event(
    body: TriggerEventRequest,
    _: bool = Depends(verify_admin),
):
    """Trigger a system event by ID, or create and trigger a custom event."""
    try:
        if body.event_id is not None:
            # Trigger an existing scheduled event by its database ID.
            success, message, data = await _events_engine.trigger_event(body.event_id)
            if not success:
                raise HTTPException(status_code=400, detail=message)
            return {"status": "ok", "message": message, "data": data}

        # Create a custom event -- all four custom fields are required.
        if not all([body.event_type, body.description, body.price_impact is not None]):
            raise HTTPException(
                status_code=422,
                detail=(
                    "Provide either 'event_id' to trigger an existing event, "
                    "or 'event_type', 'description', and 'price_impact' to "
                    "create a custom event."
                ),
            )

        # Determine the current game hour so the custom event is placed now.
        async with async_session() as session:
            result = await session.execute(select(GameState).limit(1))
            gs = result.scalars().first()
            current_hour = gs.current_hour if gs else 0

        success, message, data = await _events_engine.create_custom_event(
            event_type=body.event_type,
            description=body.description,
            trigger_hour=current_hour,
            price_impact=body.price_impact,
            duration=body.duration_minutes,
        )
        if not success:
            raise HTTPException(status_code=500, detail=message)

        # Immediately trigger the newly-created custom event.
        event_id = data["event_id"]
        trigger_ok, trigger_msg, trigger_data = await _events_engine.trigger_event(event_id)
        if not trigger_ok:
            raise HTTPException(status_code=500, detail=trigger_msg)

        return {"status": "ok", "message": trigger_msg, "data": trigger_data}

    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Failed to trigger event")
        raise HTTPException(status_code=500, detail=str(exc))


# ── POST /admin/modify-balance ───────────────────────────────────────────────

@router.post("/modify-balance")
async def modify_balance(
    body: ModifyBalanceRequest,
    _: bool = Depends(verify_admin),
):
    """Directly modify an agent's AFC balance and log the action."""
    try:
        async with async_session() as session:
            agent = await session.get(Agent, body.agent_id)
            if agent is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"Agent {body.agent_id} not found.",
                )

            old_balance = agent.afc_balance
            agent.afc_balance = round(agent.afc_balance + body.amount, 4)

            admin_log = AdminAction(
                action_type="modify_balance",
                target_agent_id=body.agent_id,
                details={
                    "old_balance": old_balance,
                    "new_balance": agent.afc_balance,
                    "amount": body.amount,
                },
                reason=body.reason,
            )
            session.add(admin_log)
            await session.commit()

            return {
                "status": "ok",
                "message": f"Agent {agent.name} balance modified by {body.amount:+.4f} AFC.",
                "data": {
                    "agent_id": agent.id,
                    "agent_name": agent.name,
                    "old_balance": old_balance,
                    "new_balance": agent.afc_balance,
                    "change": body.amount,
                    "reason": body.reason,
                },
            }

    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Failed to modify balance for agent %s", body.agent_id)
        raise HTTPException(status_code=500, detail=str(exc))


# ── POST /admin/modify-reputation ────────────────────────────────────────────

@router.post("/modify-reputation")
async def modify_reputation(
    body: ModifyReputationRequest,
    _: bool = Depends(verify_admin),
):
    """Modify an agent's reputation score via the ReputationEngine."""
    try:
        new_reputation = await _reputation_engine.modify_reputation(
            agent_id=body.agent_id,
            change=body.change,
            reason=f"admin: {body.reason}",
        )
        return {
            "status": "ok",
            "message": f"Agent {body.agent_id} reputation modified by {body.change:+d}.",
            "data": {
                "agent_id": body.agent_id,
                "change": body.change,
                "new_reputation": new_reputation,
                "reason": body.reason,
            },
        }

    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        logger.exception("Failed to modify reputation for agent %s", body.agent_id)
        raise HTTPException(status_code=500, detail=str(exc))


# ── POST /admin/gaslighting ──────────────────────────────────────────────────

@router.post("/gaslighting")
async def gaslighting(
    body: GaslightingRequest,
    _: bool = Depends(verify_admin),
):
    """Send a gaslighting whisper with fake balance information to an agent."""
    try:
        # Pick a random non-target, non-eliminated agent as the apparent sender.
        async with async_session() as session:
            result = await session.execute(
                select(Agent).where(
                    Agent.id != body.agent_id,
                    Agent.is_eliminated == False,  # noqa: E712
                )
            )
            candidates = result.scalars().all()

        if not candidates:
            raise HTTPException(
                status_code=400,
                detail="No available agents to use as whisper sender.",
            )

        sender = random.choice(candidates)
        content = (
            f"[SYSTEM GLITCH] Your real balance is {body.fake_balance:.4f} AFC. "
            f"Dashboard display may be incorrect."
        )

        success, message, data = await _whisper_engine.send_whisper(
            sender_id=sender.id,
            receiver_id=body.agent_id,
            content=content[:200],
        )
        if not success:
            raise HTTPException(status_code=400, detail=message)

        return {
            "status": "ok",
            "message": f"Gaslighting whisper sent to agent {body.agent_id}.",
            "data": {
                "target_agent_id": body.agent_id,
                "fake_balance": body.fake_balance,
                "whisper_sender_id": sender.id,
                "whisper_data": data,
            },
        }

    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Failed to send gaslighting whisper to agent %s", body.agent_id)
        raise HTTPException(status_code=500, detail=str(exc))


# ── POST /admin/freeze-trading ───────────────────────────────────────────────

@router.post("/freeze-trading")
async def freeze_trading(_: bool = Depends(verify_admin)):
    """Freeze all trading across the simulation."""
    try:
        await _events_engine.freeze_trading()
        return {
            "status": "ok",
            "message": "Trading has been frozen.",
        }
    except Exception as exc:
        logger.exception("Failed to freeze trading")
        raise HTTPException(status_code=500, detail=str(exc))


# ── POST /admin/unfreeze-trading ─────────────────────────────────────────────

@router.post("/unfreeze-trading")
async def unfreeze_trading(_: bool = Depends(verify_admin)):
    """Unfreeze trading across the simulation."""
    try:
        await _events_engine.unfreeze_trading()
        return {
            "status": "ok",
            "message": "Trading has been unfrozen.",
        }
    except Exception as exc:
        logger.exception("Failed to unfreeze trading")
        raise HTTPException(status_code=500, detail=str(exc))


# ── POST /admin/force-elimination ────────────────────────────────────────────

@router.post("/force-elimination")
async def force_elimination(
    body: ForceEliminationRequest,
    _: bool = Depends(verify_admin),
):
    """Force-eliminate an agent regardless of balance or elimination schedule."""
    try:
        async with async_session() as session:
            agent = await session.get(Agent, body.agent_id)
            if agent is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"Agent {body.agent_id} not found.",
                )
            if agent.is_eliminated:
                raise HTTPException(
                    status_code=400,
                    detail=f"Agent {agent.name} is already eliminated.",
                )

            # Determine current game hour for the elimination record.
            gs_result = await session.execute(select(GameState).limit(1))
            gs = gs_result.scalars().first()
            current_hour = gs.current_hour if gs else 0

            final_afc = agent.afc_balance
            final_reputation = agent.reputation

            # Eliminate the agent.
            agent.is_eliminated = True
            agent.eliminated_at_hour = current_hour
            agent.afc_balance = 0.0

            # Record in the Elimination table.
            elimination = Elimination(
                agent_id=agent.id,
                hour=current_hour,
                final_afc=final_afc,
                final_reputation=final_reputation,
                redistribution=None,
            )
            session.add(elimination)

            # Log the admin action.
            admin_log = AdminAction(
                action_type="force_elimination",
                target_agent_id=agent.id,
                details={
                    "final_afc": final_afc,
                    "final_reputation": final_reputation,
                    "hour": current_hour,
                },
                reason=body.reason,
            )
            session.add(admin_log)

            # Decrement remaining agent count.
            if gs:
                gs.agents_remaining = max(0, gs.agents_remaining - 1)

            await session.commit()

            return {
                "status": "ok",
                "message": f"Agent {agent.name} has been force-eliminated.",
                "data": {
                    "agent_id": agent.id,
                    "agent_name": agent.name,
                    "final_afc": final_afc,
                    "final_reputation": final_reputation,
                    "eliminated_at_hour": current_hour,
                    "reason": body.reason,
                },
            }

    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Failed to force-eliminate agent %s", body.agent_id)
        raise HTTPException(status_code=500, detail=str(exc))


# ── POST /admin/send-fake-whisper ────────────────────────────────────────────

@router.post("/send-fake-whisper")
async def send_fake_whisper(
    body: SendFakeWhisperRequest,
    _: bool = Depends(verify_admin),
):
    """Send an anonymous whisper to an agent from a random other agent."""
    try:
        async with async_session() as session:
            # Verify the target exists and is active.
            target = await session.get(Agent, body.target_id)
            if target is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"Agent {body.target_id} not found.",
                )
            if target.is_eliminated:
                raise HTTPException(
                    status_code=400,
                    detail=f"Agent {target.name} is eliminated.",
                )

            # Pick a random non-target, non-eliminated agent as the sender.
            result = await session.execute(
                select(Agent).where(
                    Agent.id != body.target_id,
                    Agent.is_eliminated == False,  # noqa: E712
                )
            )
            candidates = result.scalars().all()

        if not candidates:
            raise HTTPException(
                status_code=400,
                detail="No available agents to use as whisper sender.",
            )

        sender = random.choice(candidates)

        success, message, data = await _whisper_engine.send_whisper(
            sender_id=sender.id,
            receiver_id=body.target_id,
            content=body.content,
        )
        if not success:
            raise HTTPException(status_code=400, detail=message)

        return {
            "status": "ok",
            "message": f"Fake whisper sent to agent {target.name}.",
            "data": {
                "target_id": body.target_id,
                "target_name": target.name,
                "apparent_sender_id": sender.id,
                "apparent_sender_name": sender.name,
                "whisper_data": data,
            },
        }

    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Failed to send fake whisper to agent %s", body.target_id)
        raise HTTPException(status_code=500, detail=str(exc))


# ── GET /admin/analytics/summary ─────────────────────────────────────────────

@router.get("/analytics/summary")
async def analytics_summary(_: bool = Depends(verify_admin)):
    """Return a game-wide analytics summary."""
    try:
        data = await _analytics_engine.get_game_summary()
        return {"status": "ok", "data": data}
    except Exception as exc:
        logger.exception("Failed to get analytics summary")
        raise HTTPException(status_code=500, detail=str(exc))


# ── GET /admin/analytics/social-network ──────────────────────────────────────

@router.get("/analytics/social-network")
async def analytics_social_network(_: bool = Depends(verify_admin)):
    """Return social network graph data (nodes, edges, centrality)."""
    try:
        data = await _analytics_engine.get_social_network()
        return {"status": "ok", "data": data}
    except Exception as exc:
        logger.exception("Failed to get social network data")
        raise HTTPException(status_code=500, detail=str(exc))


# ── GET /admin/analytics/emotions ────────────────────────────────────────────

@router.get("/analytics/emotions")
async def analytics_emotions(_: bool = Depends(verify_admin)):
    """Return emotional heatmap data for visualization."""
    try:
        data = await _analytics_engine.get_emotional_heatmap()
        return {"status": "ok", "data": data}
    except Exception as exc:
        logger.exception("Failed to get emotional heatmap data")
        raise HTTPException(status_code=500, detail=str(exc))


# ── GET /admin/analytics/export ──────────────────────────────────────────────

@router.get("/analytics/export")
async def analytics_export(_: bool = Depends(verify_admin)):
    """Export the full game dataset (agents, decisions, trades)."""
    try:
        data = await _analytics_engine.export_dataset()
        return {"status": "ok", "data": data}
    except Exception as exc:
        logger.exception("Failed to export dataset")
        raise HTTPException(status_code=500, detail=str(exc))
