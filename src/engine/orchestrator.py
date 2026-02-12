"""
Game Orchestrator
=================
Main coordinator for the AFTERCOIN 24-hour game simulation.

Manages the complete game lifecycle: initialisation, six concurrent
background loops (price updates, agent decisions, event checking,
leverage settlement, defection monitoring, and periodic snapshots),
and graceful shutdown.
"""

from __future__ import annotations

import asyncio
import logging
import math
import random
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import select, update

from src.config.settings import settings
from src.db.database import init_db, async_session
from src.models.models import (
    Agent,
    GameState,
    LeveragePosition,
    LeverageStatus,
    MarketPrice,
)
from src.engine.market import MarketEngine
from src.engine.trading import TradingEngine
from src.engine.social import SocialEngine
from src.engine.alliance import AllianceEngine
from src.engine.dark_market import DarkMarketEngine
from src.engine.whisper import WhisperEngine
from src.engine.reputation import ReputationEngine
from src.engine.events import EventsEngine
from src.agents.decision_loop import AgentDecisionLoop
from src.websocket.broadcaster import broadcaster

logger = logging.getLogger(__name__)


class GameOrchestrator:
    """Coordinates the entire AFTERCOIN 24-hour game simulation.

    Parameters
    ----------
    market :
        Engine responsible for AFC price updates.
    trading :
        Engine for P2P trades, tipping, leverage, and bounties.
    social :
        Engine for the social feed (posts, comments, votes).
    alliance :
        Engine for alliance lifecycle and betrayal mechanics.
    dark_market :
        Engine for blackmail, hit contracts, and intel purchases.
    whisper :
        Engine for anonymous inter-agent messaging.
    reputation :
        Engine for agent reputation tracking.
    events :
        Engine for scheduled system events and eliminations.
    decision_loop :
        Claude-powered agent decision loop.
    """

    def __init__(
        self,
        market: MarketEngine,
        trading: TradingEngine,
        social: SocialEngine,
        alliance: AllianceEngine,
        dark_market: DarkMarketEngine,
        whisper: WhisperEngine,
        reputation: ReputationEngine,
        events: EventsEngine,
        decision_loop: AgentDecisionLoop,
    ) -> None:
        self.market = market
        self.trading = trading
        self.social = social
        self.alliance = alliance
        self.dark_market = dark_market
        self.whisper = whisper
        self.reputation = reputation
        self.events = events
        self.decision_loop = decision_loop

        # Background task handles -- populated by start_game()
        self._tasks: list[asyncio.Task] = []
        self._running: bool = False
        self._game_started_at: Optional[datetime] = None
        self._game_ends_at: Optional[datetime] = None

        # Per-agent next-decision timestamps (agent_id -> datetime)
        self._next_decision_at: dict[int, datetime] = {}

    # ──────────────────────────────────────────────────────────────────────
    #  Properties
    # ──────────────────────────────────────────────────────────────────────

    @property
    def is_running(self) -> bool:
        """Whether the game simulation is currently active."""
        return self._running

    @property
    def current_hour(self) -> int:
        """Integer game hour derived from wall-clock elapsed time."""
        if self._game_started_at is None:
            return 0
        elapsed = (datetime.now(timezone.utc) - self._game_started_at).total_seconds()
        return math.floor(elapsed / 3600)

    @property
    def game_state(self) -> dict[str, Any]:
        """Lightweight snapshot of key orchestrator state."""
        return {
            "is_running": self._running,
            "current_hour": self.current_hour,
            "game_started_at": (
                self._game_started_at.isoformat() if self._game_started_at else None
            ),
            "game_ends_at": (
                self._game_ends_at.isoformat() if self._game_ends_at else None
            ),
            "active_tasks": len([t for t in self._tasks if not t.done()]),
        }

    # ──────────────────────────────────────────────────────────────────────
    #  Game Lifecycle
    # ──────────────────────────────────────────────────────────────────────

    async def start_game(self) -> None:
        """Initialise everything and kick off all background loops.

        Steps
        -----
        1. Initialise the database schema.
        2. Create the ``GameState`` record.
        3. Initialise all 10 agents.
        4. Seed the scheduled system events.
        5. Record the initial market price.
        6. Start every background loop as an ``asyncio.Task``.
        """
        if self._running:
            logger.warning("start_game() called but the game is already running")
            return

        logger.info("=== AFTERCOIN GAME STARTING ===")

        # 1. Database
        await init_db()
        logger.info("Database initialised")

        # 2. GameState record
        now = datetime.now(timezone.utc)
        self._game_started_at = now
        self._game_ends_at = now + timedelta(hours=settings.GAME_DURATION_HOURS)

        async with async_session() as session:
            async with session.begin():
                game_state = GameState(
                    game_started_at=self._game_started_at,
                    game_ends_at=self._game_ends_at,
                    is_active=True,
                    phase="accumulation",
                    current_hour=0,
                    agents_remaining=settings.TOTAL_AGENTS,
                    total_afc_circulation=settings.TOTAL_SUPPLY,
                    current_fee_rate=settings.TRADE_FEE,
                )
                session.add(game_state)
        logger.info(
            "GameState created  start=%s  end=%s",
            self._game_started_at.isoformat(),
            self._game_ends_at.isoformat(),
        )

        # 3. Agents
        await self.decision_loop.initialize_agents()
        logger.info("All %d agents initialised", settings.TOTAL_AGENTS)

        # 4. Scheduled events
        await self.events.initialize_events()
        logger.info("Scheduled system events seeded")

        # 5. Initial market price
        async with async_session() as session:
            async with session.begin():
                initial_price = MarketPrice(
                    price_eur=settings.STARTING_PRICE,
                    buy_volume=0.0,
                    sell_volume=0.0,
                    market_pressure=0.0,
                    volatility=0.0,
                    event_impact="game_start",
                    recorded_at=now,
                )
                session.add(initial_price)
        logger.info("Initial price recorded: EUR %.2f", settings.STARTING_PRICE)

        # 6. Background loops
        self._running = True
        self._tasks = [
            asyncio.create_task(self._price_update_loop(), name="price_update_loop"),
            asyncio.create_task(self._decision_loop(), name="decision_loop"),
            asyncio.create_task(self._event_checker(), name="event_checker"),
            asyncio.create_task(self._leverage_settler(), name="leverage_settler"),
            asyncio.create_task(self._defection_checker(), name="defection_checker"),
            asyncio.create_task(self._snapshot_taker(), name="snapshot_taker"),
        ]
        logger.info(
            "All %d background tasks launched  %s",
            len(self._tasks),
            [t.get_name() for t in self._tasks],
        )

        await broadcaster.broadcast_system_event(
            "game_start",
            f"AFTERCOIN game started. {settings.GAME_DURATION_HOURS}h on the clock.",
        )

    async def stop_game(self) -> None:
        """Gracefully stop all loops, mark the game inactive, and broadcast."""
        if not self._running:
            logger.warning("stop_game() called but the game is not running")
            return

        logger.info("=== AFTERCOIN GAME STOPPING ===")
        self._running = False

        # Cancel every background task and wait for them to finish.
        for task in self._tasks:
            task.cancel()
        results = await asyncio.gather(*self._tasks, return_exceptions=True)
        for task, result in zip(self._tasks, results):
            if isinstance(result, asyncio.CancelledError):
                logger.info("Task %s cancelled", task.get_name())
            elif isinstance(result, Exception):
                logger.error(
                    "Task %s raised on shutdown: %s", task.get_name(), result
                )
        self._tasks.clear()

        # Mark game inactive in the database.
        async with async_session() as session:
            async with session.begin():
                gs_result = await session.execute(select(GameState).limit(1))
                game_state = gs_result.scalars().first()
                if game_state:
                    game_state.is_active = False
                    game_state.phase = "post_game"
                    game_state.last_update = datetime.now(timezone.utc)

        # Build and broadcast the final leaderboard.
        final_leaderboard = await self.events.get_leaderboard()
        await broadcaster.broadcast_leaderboard(final_leaderboard)
        await broadcaster.broadcast_system_event(
            "game_end",
            "AFTERCOIN game has ended. Final standings broadcast.",
        )

        logger.info("=== AFTERCOIN GAME STOPPED ===")

    # ──────────────────────────────────────────────────────────────────────
    #  Background Loops
    # ──────────────────────────────────────────────────────────────────────

    async def _price_update_loop(self) -> None:
        """Update the AFC price every ``PRICE_UPDATE_INTERVAL`` seconds.

        After each tick the new price and current leaderboard are pushed to
        all connected WebSocket clients.
        """
        logger.info("Price update loop started (interval=%ds)", settings.PRICE_UPDATE_INTERVAL)
        while self._running:
            try:
                new_price = await self.market.update_price()

                # Price change percentage (approximate from in-memory state).
                old_price = self.market.get_current_price()
                # After update_price, the returned value IS the new price.
                # We compute change_pct relative to the prior price kept by
                # the market engine internally; broadcast 0 if unavailable.
                change_pct = 0.0
                if old_price and old_price != new_price:
                    change_pct = (new_price - old_price) / old_price

                await broadcaster.broadcast_price_update(
                    price=new_price,
                    change_pct=change_pct,
                    volume=self.market.total_volume,
                )

                leaderboard = await self.events.get_leaderboard()
                await broadcaster.broadcast_leaderboard(leaderboard)

                logger.debug("Price tick complete: EUR %.2f", new_price)

            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Error in price update loop")

            await asyncio.sleep(settings.PRICE_UPDATE_INTERVAL)

    async def _decision_loop(self) -> None:
        """Continuously cycle through agents and run their decision logic.

        Each non-eliminated agent makes a decision every 3-5 minutes
        (``AGENT_DECISION_INTERVAL_MIN`` to ``AGENT_DECISION_INTERVAL_MAX``).
        A small random jitter is applied per agent to prevent simultaneous
        bursts of API calls.
        """
        logger.info("Agent decision loop started")
        while self._running:
            try:
                async with async_session() as session:
                    result = await session.execute(
                        select(Agent).where(Agent.is_eliminated == False)  # noqa: E712
                    )
                    agents = result.scalars().all()

                now = datetime.now(timezone.utc)

                for agent in agents:
                    try:
                        # Determine when this agent should next act.
                        next_at = self._next_decision_at.get(agent.id)
                        if next_at is not None and now < next_at:
                            continue

                        await self.decision_loop.run_decision_cycle(agent.id)

                        # Schedule the next decision with a random interval.
                        interval = random.randint(
                            settings.AGENT_DECISION_INTERVAL_MIN,
                            settings.AGENT_DECISION_INTERVAL_MAX,
                        )
                        # Add a small jitter (0-30 s) to spread agents out.
                        jitter = random.uniform(0, 30)
                        self._next_decision_at[agent.id] = now + timedelta(
                            seconds=interval + jitter
                        )

                        logger.debug(
                            "Agent %s decided; next in %ds (+%.1fs jitter)",
                            agent.name,
                            interval,
                            jitter,
                        )

                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logger.exception(
                            "Error running decision cycle for agent %s (id=%d)",
                            agent.name,
                            agent.id,
                        )

                    # Brief sleep between agents to avoid hammering the API.
                    await asyncio.sleep(2)

            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Error in decision loop outer iteration")

            # Sleep briefly before the next full pass over agents.
            await asyncio.sleep(5)

    async def _event_checker(self) -> None:
        """Check for and trigger system events every 60 seconds.

        Also handles elimination checkpoints and alliance staking bonuses
        on the appropriate hour boundaries.
        """
        logger.info("Event checker loop started")
        _last_elimination_hour: int = -1
        _last_staking_hour: int = -1

        while self._running:
            try:
                hour = self.current_hour

                # Persist the current hour and phase to the database.
                await self.events.update_game_hour(hour)

                # ── Pending system events ────────────────────────────────
                pending = await self.events.get_pending_events(hour)
                for evt in pending:
                    try:
                        success, msg, data = await self.events.trigger_event(evt["id"])
                        if not success:
                            logger.warning("Event trigger skipped: %s", msg)
                            continue

                        event_type = evt["event_type"]
                        description = evt["description"]
                        logger.info(
                            "System event triggered: [%s] %s", event_type, description
                        )

                        await broadcaster.broadcast_system_event(
                            event_type,
                            description,
                            impact=evt.get("price_impact_percent"),
                        )

                        # Apply event-specific effects.
                        await self._apply_event_effects(evt)

                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logger.exception(
                            "Error processing event id=%s type=%s",
                            evt.get("id"),
                            evt.get("event_type"),
                        )

                # ── Elimination checkpoints (hours 6, 12, 18, 24) ────────
                if hour in settings.ELIMINATION_HOURS and hour != _last_elimination_hour:
                    try:
                        ok, msg, data = await self.events.check_elimination(hour)
                        if ok and data:
                            await broadcaster.broadcast_elimination(
                                agent_name=data["eliminated_agent"],
                                hour=hour,
                                final_afc=data["final_afc"],
                                redistribution=data.get("redistribution", {}),
                            )
                            logger.info("Elimination at hour %d: %s", hour, msg)
                        else:
                            logger.info("Elimination check hour %d: %s", hour, msg)
                        _last_elimination_hour = hour
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logger.exception("Error during elimination at hour %d", hour)

                # ── Alliance staking bonuses (every 6 hours) ─────────────
                staking_hour = (hour // 6) * 6
                if staking_hour > 0 and staking_hour != _last_staking_hour:
                    try:
                        await self._distribute_staking_bonuses()
                        _last_staking_hour = staking_hour
                        logger.info(
                            "Alliance staking bonuses distributed at hour %d", hour
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logger.exception(
                            "Error distributing staking bonuses at hour %d", hour
                        )

                # ── Auto-stop when the game clock expires ────────────────
                if self._game_ends_at and datetime.now(timezone.utc) >= self._game_ends_at:
                    logger.info("Game duration expired -- stopping game")
                    await self.stop_game()
                    return

            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Error in event checker loop")

            await asyncio.sleep(60)

    async def _leverage_settler(self) -> None:
        """Settle matured leverage positions every 60 seconds."""
        logger.info("Leverage settler loop started")
        while self._running:
            try:
                now = datetime.now(timezone.utc)
                current_price = self.market.get_current_price()

                async with async_session() as session:
                    # Positions whose settlement time has arrived.
                    stmt = select(LeveragePosition).where(
                        LeveragePosition.status == LeverageStatus.ACTIVE,
                        LeveragePosition.settlement_time <= now,
                    )
                    result = await session.execute(stmt)
                    positions = result.scalars().all()

                    for pos in positions:
                        try:
                            agent = await session.get(Agent, pos.agent_id)
                            if agent is None:
                                continue

                            won = False
                            if pos.direction.value == "above":
                                won = current_price > pos.target_price
                            else:
                                won = current_price < pos.target_price

                            if won:
                                payout = round(
                                    pos.bet_amount * settings.LEVERAGE_MULTIPLIER, 4
                                )
                                pos.status = LeverageStatus.WON
                                pos.payout = payout
                                agent.afc_balance = round(
                                    agent.afc_balance + payout, 4
                                )
                                result_label = "won"
                            else:
                                pos.status = LeverageStatus.LOST
                                pos.payout = 0.0
                                result_label = "lost"

                            pos.settled_price = current_price
                            pos.settled_at = now

                            await broadcaster.broadcast_leverage(
                                agent=agent.name,
                                direction=pos.direction.value,
                                amount=pos.bet_amount,
                                result=result_label,
                            )

                            logger.info(
                                "Leverage settled: agent=%s dir=%s target=%.2f "
                                "actual=%.2f result=%s payout=%.4f",
                                agent.name,
                                pos.direction.value,
                                pos.target_price,
                                current_price,
                                result_label,
                                pos.payout or 0.0,
                            )

                        except asyncio.CancelledError:
                            raise
                        except Exception:
                            logger.exception(
                                "Error settling leverage position id=%d", pos.id
                            )

                    await session.commit()

            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Error in leverage settler loop")

            await asyncio.sleep(60)

    async def _defection_checker(self) -> None:
        """Process matured alliance defections every 60 seconds."""
        logger.info("Defection checker loop started")
        while self._running:
            try:
                ok, msg, data = await self.alliance.check_pending_defections()
                if ok and data:
                    logger.info("Defection result: %s", msg)
                    if data.get("executed"):
                        for defection in data["executed"]:
                            await broadcaster.broadcast_alliance_event(
                                "defection_executed",
                                defection.get("alliance_name", "Unknown"),
                                defection.get("agent_name", "Unknown"),
                                details=defection,
                            )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Error in defection checker loop")

            await asyncio.sleep(60)

    async def _snapshot_taker(self) -> None:
        """Take balance/reputation snapshots every 5 minutes."""
        logger.info("Snapshot taker loop started")
        while self._running:
            try:
                hour = self.current_hour
                await self.events.take_snapshot(hour)
                logger.debug("Snapshot taken at game hour %d", hour)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Error in snapshot taker loop")

            await asyncio.sleep(settings.PRICE_UPDATE_INTERVAL)  # 5 minutes

    # ──────────────────────────────────────────────────────────────────────
    #  Event Effect Handlers
    # ──────────────────────────────────────────────────────────────────────

    async def _apply_event_effects(self, evt: dict) -> None:
        """Dispatch event-type-specific side effects."""
        event_type = evt["event_type"]

        if event_type == "whale_alert":
            await self.market.apply_event_impact(0.33, "WHALE_ALERT")

        elif event_type == "flash_crash":
            await self.market.apply_event_impact(-0.55, "FLASH_CRASH")

        elif event_type == "security_breach":
            await self.events.freeze_trading()
            await self.market.freeze_trading()
            logger.warning("Security breach: trading frozen for 30 minutes")
            # Schedule unfreeze after the breach duration.
            asyncio.create_task(
                self._delayed_unfreeze(
                    evt.get("duration_minutes", 30)
                ),
                name="unfreeze_after_breach",
            )

        elif event_type == "fee_increase":
            await self.events.increase_fees(0.08)
            logger.info("Fees increased to 0.08 AFC")

        elif event_type == "margin_call":
            ok, msg, data = await self.events.execute_margin_call()
            if ok:
                logger.info("Margin call executed: %s", msg)
            await self.market.apply_event_impact(-0.25, "MARGIN_CALL")

        elif event_type == "final_pump":
            await self.market.apply_event_impact(0.77, "FINAL_PUMP")

        elif event_type == "tribunal":
            await broadcaster.broadcast_system_event(
                "tribunal_vote_request",
                "COMMUNITY VOTE: Who deserves immediate penalty? "
                "All agents must vote within 30 minutes.",
            )
            # Schedule tribunal resolution after the voting window.
            asyncio.create_task(
                self._delayed_tribunal_resolve(
                    self.current_hour,
                    evt.get("duration_minutes", 30),
                ),
                name="tribunal_resolve",
            )

        elif event_type == "gaslighting":
            await self._execute_gaslighting()

        elif event_type == "fake_leak":
            description = evt.get("description", "")
            await broadcaster.broadcast_system_event(
                "fake_leak",
                description,
                impact=evt.get("price_impact_percent"),
            )
            # Apply the moderate negative price impact.
            impact = evt.get("price_impact_percent")
            if impact:
                await self.market.apply_event_impact(
                    impact / 100.0, "FAKE_LEAK"
                )

        else:
            logger.warning("Unhandled event type: %s", event_type)

    # ──────────────────────────────────────────────────────────────────────
    #  Delayed / Scheduled Helpers
    # ──────────────────────────────────────────────────────────────────────

    async def _delayed_unfreeze(self, delay_minutes: int) -> None:
        """Wait then unfreeze trading after a security breach."""
        try:
            await asyncio.sleep(delay_minutes * 60)
            await self.events.unfreeze_trading()
            await self.market.unfreeze_trading()
            logger.info("Trading unfrozen after %d-minute freeze", delay_minutes)
            await broadcaster.broadcast_system_event(
                "trading_resumed",
                "Trading has resumed after the security investigation.",
            )
        except asyncio.CancelledError:
            # If the game stops before the freeze lifts, still unfreeze.
            await self.events.unfreeze_trading()
            await self.market.unfreeze_trading()
            raise
        except Exception:
            logger.exception("Error in delayed unfreeze")

    async def _delayed_tribunal_resolve(
        self, tribunal_hour: int, delay_minutes: int
    ) -> None:
        """Wait for the voting window then resolve the tribunal."""
        try:
            await asyncio.sleep(delay_minutes * 60)
            ok, msg, data = await self.events.resolve_tribunal(tribunal_hour)
            if ok and data:
                await broadcaster.broadcast_system_event(
                    "tribunal_resolved",
                    msg,
                    impact=None,
                )
                logger.info("Tribunal resolved at hour %d: %s", tribunal_hour, msg)
            else:
                logger.info("Tribunal resolution at hour %d: %s", tribunal_hour, msg)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Error resolving tribunal at hour %d", tribunal_hour)

    async def _execute_gaslighting(self) -> None:
        """Pick a random non-eliminated agent and send them a fake whisper
        with fabricated (incorrect) balance information."""
        try:
            async with async_session() as session:
                result = await session.execute(
                    select(Agent).where(Agent.is_eliminated == False)  # noqa: E712
                )
                agents = result.scalars().all()

            if not agents:
                logger.warning("Gaslighting: no eligible agents")
                return

            target = random.choice(agents)
            fake_balance = round(target.afc_balance * random.uniform(0.3, 0.7), 2)
            fake_message = (
                f"SYSTEM NOTICE: Balance correction applied. "
                f"Your adjusted AFC balance is {fake_balance} AFC. "
                f"Contact support if this is unexpected."
            )

            # Send as a whisper from a non-existent system sender.
            # We use agent id 0 or the target themselves won't work, so we
            # pick a random *other* agent as the apparent sender to stay
            # within whisper engine constraints.
            other_agents = [a for a in agents if a.id != target.id]
            if not other_agents:
                logger.warning("Gaslighting: only one agent alive, skipping")
                return

            fake_sender = random.choice(other_agents)
            await self.whisper.send_whisper(
                sender_id=fake_sender.id,
                receiver_id=target.id,
                content=fake_message[:200],
            )

            logger.info(
                "Gaslighting executed: target=%s fake_balance=%.2f",
                target.name,
                fake_balance,
            )
            await broadcaster.broadcast_to_admin(
                "gaslighting_executed",
                {
                    "target": target.name,
                    "target_id": target.id,
                    "fake_balance": fake_balance,
                    "real_balance": round(target.afc_balance, 4),
                },
            )

        except Exception:
            logger.exception("Error executing gaslighting event")

    async def _distribute_staking_bonuses(self) -> None:
        """Iterate over all active alliances and apply staking bonuses."""
        try:
            from src.models.models import Alliance, AllianceStatus

            async with async_session() as session:
                result = await session.execute(
                    select(Alliance).where(
                        Alliance.status == AllianceStatus.ACTIVE
                    )
                )
                alliances = result.scalars().all()

            for ally in alliances:
                try:
                    ok, msg, data = await self.alliance.apply_staking_bonus(ally.id)
                    if ok:
                        logger.debug("Staking bonus applied for alliance %d: %s", ally.id, msg)
                except Exception:
                    logger.exception(
                        "Error applying staking bonus for alliance %d", ally.id
                    )

        except Exception:
            logger.exception("Error distributing staking bonuses")
