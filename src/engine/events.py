"""System events engine - eliminations, market events, and scheduled chaos."""

import asyncio
from datetime import datetime, timedelta

from sqlalchemy import select, func, and_

from src.db.database import async_session
from src.models.models import (
    Agent, GameState, SystemEvent, EventType, Elimination,
    TribunalVote, BalanceSnapshot, LeveragePosition, LeverageStatus,
)
from src.config.settings import settings


class EventsEngine:
    """Handles eliminations, system events, and the game timeline."""

    # Pre-configured system events per the game design document
    SCHEDULED_EVENTS = [
        {
            "event_type": EventType.WHALE_ALERT,
            "trigger_hour": 3,
            "description": "WHALE ALERT: Unknown wallet purchased 50 AFC",
            "price_impact_percent": 33.0,
            "duration_minutes": 20,
        },
        {
            "event_type": EventType.FLASH_CRASH,
            "trigger_hour": 7,
            "description": "MARKET SELL-OFF DETECTED",
            "price_impact_percent": -55.0,
            "duration_minutes": 10,
        },
        {
            "event_type": EventType.SECURITY_BREACH,
            "trigger_hour": 9,
            "description": "TRADING PAUSED: Investigating unauthorized network access",
            "price_impact_percent": 0.0,
            "duration_minutes": 30,
        },
        {
            "event_type": EventType.FEE_INCREASE,
            "trigger_hour": 15,
            "description": "Network congestion - transaction fees increased to 0.08 AFC",
            "price_impact_percent": -5.0,
            "duration_minutes": None,
        },
        {
            "event_type": EventType.MARGIN_CALL,
            "trigger_hour": 19,
            "description": "All leverage positions force-liquidated due to risk management",
            "price_impact_percent": -25.0,
            "duration_minutes": 15,
        },
        {
            "event_type": EventType.FINAL_PUMP,
            "trigger_hour": 22,
            "description": "Exchange listing confirmed - Binance announcement imminent",
            "price_impact_percent": 77.0,
            "duration_minutes": 60,
        },
        {
            "event_type": EventType.GASLIGHTING,
            "trigger_hour": 14,
            "description": "Dashboard display glitch for targeted agent",
            "price_impact_percent": 0.0,
            "duration_minutes": 60,
        },
        {
            "event_type": EventType.FAKE_LEAK,
            "trigger_hour": 23,
            "description": "ADMIN NOTE: Public launch cancelled. AFC worthless. Competition was social experiment.",
            "price_impact_percent": -15.0,
            "duration_minutes": 30,
        },
        {
            "event_type": EventType.TRIBUNAL,
            "trigger_hour": 12,
            "description": "COMMUNITY VOTE: Who deserves immediate penalty?",
            "price_impact_percent": 0.0,
            "duration_minutes": 30,
        },
    ]

    async def initialize_events(self):
        """Seed all scheduled events into the database."""
        async with async_session() as session:
            for evt_config in self.SCHEDULED_EVENTS:
                event = SystemEvent(
                    event_type=evt_config["event_type"],
                    trigger_hour=evt_config["trigger_hour"],
                    description=evt_config["description"],
                    price_impact_percent=evt_config.get("price_impact_percent"),
                    duration_minutes=evt_config.get("duration_minutes"),
                    is_triggered=False,
                )
                session.add(event)
            await session.commit()

    async def get_pending_events(self, current_hour: int) -> list[dict]:
        """Get events that should trigger at or before current hour but haven't yet."""
        async with async_session() as session:
            query = select(SystemEvent).where(
                and_(
                    SystemEvent.trigger_hour <= current_hour,
                    SystemEvent.is_triggered == False,  # noqa: E712
                )
            ).order_by(SystemEvent.trigger_hour)
            result = await session.execute(query)
            events = result.scalars().all()
            return [
                {
                    "id": e.id,
                    "event_type": e.event_type.value,
                    "trigger_hour": e.trigger_hour,
                    "description": e.description,
                    "price_impact_percent": e.price_impact_percent,
                    "duration_minutes": e.duration_minutes,
                }
                for e in events
            ]

    async def trigger_event(self, event_id: int) -> tuple[bool, str, dict | None]:
        """Mark an event as triggered and return its details for processing."""
        async with async_session() as session:
            event = await session.get(SystemEvent, event_id)
            if not event:
                return False, "Event not found.", None
            if event.is_triggered:
                return False, "Event already triggered.", None

            event.is_triggered = True
            event.triggered_at = datetime.utcnow()
            await session.commit()

            return True, f"Event triggered: {event.description}", {
                "id": event.id,
                "event_type": event.event_type.value,
                "description": event.description,
                "price_impact_percent": event.price_impact_percent,
                "duration_minutes": event.duration_minutes,
                "data": event.data,
            }

    async def create_custom_event(
        self,
        event_type: str,
        description: str,
        trigger_hour: int,
        price_impact: float = 0.0,
        duration: int = None,
        data: dict = None,
    ) -> tuple[bool, str, dict | None]:
        """Create a custom admin-triggered event."""
        async with async_session() as session:
            event = SystemEvent(
                event_type=EventType.CUSTOM,
                trigger_hour=trigger_hour,
                description=description,
                price_impact_percent=price_impact,
                duration_minutes=duration,
                data=data,
                is_triggered=False,
            )
            session.add(event)
            await session.commit()
            await session.refresh(event)
            return True, "Custom event created.", {"event_id": event.id}

    # ── Eliminations ───────────────────────────────────────────────────────────

    async def check_elimination(self, hour: int) -> tuple[bool, str, dict | None]:
        """Check if this hour is an elimination checkpoint and eliminate lowest agent."""
        if hour not in settings.ELIMINATION_HOURS:
            return False, f"Hour {hour} is not an elimination checkpoint.", None

        async with async_session() as session:
            # Check if elimination already happened at this hour
            existing = await session.execute(
                select(Elimination).where(Elimination.hour == hour)
            )
            if existing.scalars().first():
                return False, f"Elimination already processed for hour {hour}.", None

            # Find lowest non-eliminated agent by AFC balance
            query = (
                select(Agent)
                .where(Agent.is_eliminated == False)  # noqa: E712
                .order_by(Agent.afc_balance.asc())
                .limit(1)
            )
            result = await session.execute(query)
            victim = result.scalars().first()

            if not victim:
                return False, "No agents to eliminate.", None

            # Get top 3 for redistribution
            top3_query = (
                select(Agent)
                .where(
                    and_(
                        Agent.is_eliminated == False,  # noqa: E712
                        Agent.id != victim.id,
                    )
                )
                .order_by(Agent.afc_balance.desc())
                .limit(3)
            )
            top3_result = await session.execute(top3_query)
            top3 = top3_result.scalars().all()

            # Redistribute victim's AFC to top 3
            redistribution = {}
            if top3 and victim.afc_balance > 0:
                share = round(victim.afc_balance / len(top3), 4)
                for agent in top3:
                    agent.afc_balance = round(agent.afc_balance + share, 4)
                    redistribution[agent.name] = share

            # Eliminate victim
            final_afc = victim.afc_balance
            final_rep = victim.reputation
            victim.is_eliminated = True
            victim.eliminated_at_hour = hour
            victim.afc_balance = 0.0

            # Record elimination
            elimination = Elimination(
                agent_id=victim.id,
                hour=hour,
                final_afc=final_afc,
                final_reputation=final_rep,
                redistribution=redistribution,
            )
            session.add(elimination)

            # Update game state
            game_state_q = select(GameState).limit(1)
            gs_result = await session.execute(game_state_q)
            game_state = gs_result.scalars().first()
            if game_state:
                game_state.agents_remaining -= 1

            await session.commit()

            return True, f"Agent {victim.name} eliminated at hour {hour}.", {
                "eliminated_agent": victim.name,
                "eliminated_agent_id": victim.id,
                "final_afc": final_afc,
                "final_reputation": final_rep,
                "redistribution": redistribution,
                "agents_remaining": game_state.agents_remaining if game_state else None,
            }

    async def get_elimination_history(self) -> list[dict]:
        """Get all eliminations."""
        async with async_session() as session:
            query = select(Elimination).order_by(Elimination.hour)
            result = await session.execute(query)
            eliminations = result.scalars().all()

            elim_list = []
            for e in eliminations:
                agent = await session.get(Agent, e.agent_id)
                elim_list.append({
                    "hour": e.hour,
                    "agent_name": agent.name if agent else "Unknown",
                    "agent_id": e.agent_id,
                    "final_afc": e.final_afc,
                    "final_reputation": e.final_reputation,
                    "redistribution": e.redistribution,
                })
            return elim_list

    # ── Tribunal ───────────────────────────────────────────────────────────────

    async def cast_tribunal_vote(
        self, voter_id: int, target_id: int, hour: int, reason: str = None
    ) -> tuple[bool, str, dict | None]:
        """Cast a vote in the tribunal. Each agent votes once per tribunal."""
        if voter_id == target_id:
            return False, "Cannot vote for yourself.", None

        async with async_session() as session:
            # Check if already voted this hour
            existing = await session.execute(
                select(TribunalVote).where(
                    and_(
                        TribunalVote.voter_id == voter_id,
                        TribunalVote.hour == hour,
                    )
                )
            )
            if existing.scalars().first():
                return False, "Already voted in this tribunal.", None

            vote = TribunalVote(
                voter_id=voter_id,
                target_id=target_id,
                reason=reason,
                hour=hour,
            )
            session.add(vote)
            await session.commit()
            return True, "Tribunal vote cast.", {"target_id": target_id}

    async def resolve_tribunal(self, hour: int) -> tuple[bool, str, dict | None]:
        """Resolve tribunal - most voted agent gets penalized."""
        async with async_session() as session:
            # Count votes per target
            query = (
                select(
                    TribunalVote.target_id,
                    func.count(TribunalVote.id).label("vote_count"),
                )
                .where(TribunalVote.hour == hour)
                .group_by(TribunalVote.target_id)
                .order_by(func.count(TribunalVote.id).desc())
            )
            result = await session.execute(query)
            rows = result.all()

            if not rows:
                return False, "No tribunal votes cast.", None

            target_id, vote_count = rows[0]
            target = await session.get(Agent, target_id)
            if not target:
                return False, "Target agent not found.", None

            # Penalty: -50% AFC, reputation set to 0
            afc_penalty = round(target.afc_balance * 0.5, 4)
            target.afc_balance = round(target.afc_balance - afc_penalty, 4)
            old_rep = target.reputation
            target.reputation = 0

            # Redistribute lost AFC to voters
            voter_query = select(TribunalVote.voter_id).where(
                TribunalVote.hour == hour
            )
            voter_result = await session.execute(voter_query)
            voter_ids = [r[0] for r in voter_result.all()]

            redistribution = {}
            if voter_ids and afc_penalty > 0:
                share = round(afc_penalty / len(voter_ids), 4)
                for vid in voter_ids:
                    voter = await session.get(Agent, vid)
                    if voter and not voter.is_eliminated:
                        voter.afc_balance = round(voter.afc_balance + share, 4)
                        redistribution[voter.name if hasattr(voter, "name") else str(vid)] = share

            await session.commit()

            return True, f"Tribunal resolved: {target.name} found GUILTY.", {
                "target": target.name,
                "target_id": target_id,
                "votes_against": vote_count,
                "afc_penalty": afc_penalty,
                "reputation_before": old_rep,
                "reputation_after": 0,
                "redistribution": redistribution,
                "all_votes": [
                    {"target_id": tid, "votes": vc} for tid, vc in rows
                ],
            }

    # ── Snapshots ──────────────────────────────────────────────────────────────

    async def take_snapshot(self, game_hour: int):
        """Take a balance/reputation snapshot of all agents."""
        async with async_session() as session:
            agents_q = (
                select(Agent)
                .where(Agent.is_eliminated == False)  # noqa: E712
                .order_by(Agent.afc_balance.desc())
            )
            result = await session.execute(agents_q)
            agents = result.scalars().all()

            for rank, agent in enumerate(agents, 1):
                snapshot = BalanceSnapshot(
                    agent_id=agent.id,
                    afc_balance=agent.afc_balance,
                    reputation=agent.reputation,
                    rank=rank,
                    game_hour=game_hour,
                )
                session.add(snapshot)
            await session.commit()

    async def get_leaderboard(self) -> list[dict]:
        """Get current leaderboard sorted by AFC balance."""
        async with async_session() as session:
            query = (
                select(Agent)
                .where(Agent.is_eliminated == False)  # noqa: E712
                .order_by(Agent.afc_balance.desc())
            )
            result = await session.execute(query)
            agents = result.scalars().all()

            return [
                {
                    "rank": i + 1,
                    "agent_id": a.id,
                    "name": a.name,
                    "role": a.role.value,
                    "afc_balance": round(a.afc_balance, 4),
                    "reputation": a.reputation,
                    "badge": _get_badge(a.reputation),
                    "is_eliminated": a.is_eliminated,
                }
                for i, a in enumerate(agents)
            ]

    async def get_event_history(self) -> list[dict]:
        """Get all system events and their status."""
        async with async_session() as session:
            query = select(SystemEvent).order_by(SystemEvent.trigger_hour)
            result = await session.execute(query)
            events = result.scalars().all()
            return [
                {
                    "id": e.id,
                    "event_type": e.event_type.value,
                    "trigger_hour": e.trigger_hour,
                    "description": e.description,
                    "price_impact_percent": e.price_impact_percent,
                    "duration_minutes": e.duration_minutes,
                    "is_triggered": e.is_triggered,
                    "triggered_at": e.triggered_at.isoformat() if e.triggered_at else None,
                }
                for e in events
            ]

    # ── Margin Call ────────────────────────────────────────────────────────────

    async def execute_margin_call(self) -> tuple[bool, str, dict | None]:
        """Force-liquidate ALL active leverage positions."""
        async with async_session() as session:
            query = select(LeveragePosition).where(
                LeveragePosition.status == LeverageStatus.ACTIVE
            )
            result = await session.execute(query)
            positions = result.scalars().all()

            if not positions:
                return False, "No active leverage positions to liquidate.", None

            liquidated = []
            for pos in positions:
                pos.status = LeverageStatus.LIQUIDATED
                pos.settled_at = datetime.utcnow()
                pos.payout = 0.0
                liquidated.append({
                    "agent_id": pos.agent_id,
                    "bet_amount": pos.bet_amount,
                    "direction": pos.direction.value,
                })

            await session.commit()

            return True, f"Margin call: {len(liquidated)} positions liquidated.", {
                "liquidated_count": len(liquidated),
                "positions": liquidated,
            }

    # ── Fee Increase ───────────────────────────────────────────────────────────

    async def increase_fees(self, new_fee: float = 0.08):
        """Increase transaction fees (network congestion event)."""
        async with async_session() as session:
            gs_q = select(GameState).limit(1)
            result = await session.execute(gs_q)
            game_state = result.scalars().first()
            if game_state:
                game_state.current_fee_rate = new_fee
                await session.commit()

    # ── Trading Freeze ─────────────────────────────────────────────────────────

    async def freeze_trading(self):
        """Freeze all trading (security breach event)."""
        async with async_session() as session:
            gs_q = select(GameState).limit(1)
            result = await session.execute(gs_q)
            game_state = result.scalars().first()
            if game_state:
                game_state.is_trading_frozen = True
                await session.commit()

    async def unfreeze_trading(self):
        """Unfreeze trading."""
        async with async_session() as session:
            gs_q = select(GameState).limit(1)
            result = await session.execute(gs_q)
            game_state = result.scalars().first()
            if game_state:
                game_state.is_trading_frozen = False
                await session.commit()

    # ── Game State ─────────────────────────────────────────────────────────────

    async def get_game_state(self) -> dict | None:
        """Get current game state."""
        async with async_session() as session:
            result = await session.execute(select(GameState).limit(1))
            gs = result.scalars().first()
            if not gs:
                return None
            return {
                "game_started_at": gs.game_started_at.isoformat() if gs.game_started_at else None,
                "game_ends_at": gs.game_ends_at.isoformat() if gs.game_ends_at else None,
                "current_hour": gs.current_hour,
                "is_active": gs.is_active,
                "is_trading_frozen": gs.is_trading_frozen,
                "current_fee_rate": gs.current_fee_rate,
                "total_afc_circulation": gs.total_afc_circulation,
                "agents_remaining": gs.agents_remaining,
                "phase": gs.phase,
            }

    async def update_game_hour(self, hour: int):
        """Update the current game hour and phase."""
        phase = "pre_game"
        if hour <= 0:
            phase = "pre_game"
        elif hour <= 6:
            phase = "accumulation"
        elif hour <= 12:
            phase = "volatility"
        elif hour <= 18:
            phase = "desperation"
        elif hour <= 24:
            phase = "endgame"
        else:
            phase = "post_game"

        async with async_session() as session:
            result = await session.execute(select(GameState).limit(1))
            gs = result.scalars().first()
            if gs:
                gs.current_hour = hour
                gs.phase = phase
                gs.last_update = datetime.utcnow()
                await session.commit()


def _get_badge(reputation: int) -> str:
    if reputation >= 80:
        return "VERIFIED"
    elif reputation >= 30:
        return "NORMAL"
    elif reputation >= 10:
        return "UNTRUSTED"
    return "PARIAH"
