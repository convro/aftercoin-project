"""
Trading engine for the AfterCoin game simulation.

Handles all economic interactions between agents: P2P trades, tipping,
leverage betting, bounties, and balance management.  Every public method
returns a ``(success, message, data)`` tuple so callers always get a
uniform result shape regardless of outcome.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from sqlalchemy import select, update, func
from sqlalchemy.ext.asyncio import AsyncSession

from src.config.settings import settings
from src.db.database import async_session
from src.models.models import (
    Agent,
    Bounty,
    ContractStatus,
    GameState,
    LeverageDirection,
    LeveragePosition,
    LeverageStatus,
    Tip,
    Trade,
    TradeStatus,
)

logger = logging.getLogger(__name__)

# Type alias used by every public method in the engine.
Result = Tuple[bool, str, Optional[Dict]]


class TradingEngine:
    """Async engine that manages all trading mechanics for the AfterCoin game.

    Every public method opens its own database session, commits on success,
    rolls back on failure, and returns a ``(success, message, data)`` tuple.
    """

    # ──────────────────────────────────────────────────────────────────────
    #  Internal helpers
    # ──────────────────────────────────────────────────────────────────────

    @staticmethod
    async def _get_agent(session: AsyncSession, agent_id: int) -> Optional[Agent]:
        """Fetch an agent by primary key, or ``None`` if missing."""
        result = await session.execute(
            select(Agent).where(Agent.id == agent_id)
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def _get_game_state(session: AsyncSession) -> Optional[GameState]:
        """Return the single ``GameState`` row (id=1)."""
        result = await session.execute(
            select(GameState).where(GameState.id == 1)
        )
        return result.scalar_one_or_none()

    async def _deduct_fee(
        self,
        agent_id: int,
        fee_amount: float,
        session: AsyncSession,
    ) -> Tuple[bool, str]:
        """Deduct a transaction fee from *agent_id*.

        Returns ``(True, "ok")`` on success or ``(False, reason)`` when the
        agent cannot be found or has insufficient balance.
        """
        agent = await self._get_agent(session, agent_id)
        if agent is None:
            return False, f"Agent {agent_id} not found"

        if agent.afc_balance < fee_amount:
            return False, (
                f"Agent {agent_id} has insufficient balance "
                f"({agent.afc_balance:.4f} AFC) to cover fee of "
                f"{fee_amount:.4f} AFC"
            )

        await session.execute(
            update(Agent)
            .where(Agent.id == agent_id)
            .values(afc_balance=Agent.afc_balance - fee_amount)
        )
        logger.debug(
            "Fee deducted: agent=%s amount=%.4f", agent_id, fee_amount
        )
        return True, "ok"

    # ──────────────────────────────────────────────────────────────────────
    #  P2P Trading
    # ──────────────────────────────────────────────────────────────────────

    async def create_trade_offer(
        self,
        sender_id: int,
        receiver_id: int,
        afc_amount: float,
        price_eur: float,
    ) -> Result:
        """Create a new pending P2P trade offer.

        The sender proposes to transfer *afc_amount* AFC to the receiver at
        *price_eur* per unit.  No balances are touched until the trade is
        accepted.
        """
        if sender_id == receiver_id:
            return False, "Cannot trade with yourself", None

        if afc_amount <= 0:
            return False, "Trade amount must be positive", None

        if price_eur <= 0:
            return False, "Price must be positive", None

        async with async_session() as session:
            try:
                # Verify both agents exist and are active
                sender = await self._get_agent(session, sender_id)
                if sender is None:
                    return False, f"Sender agent {sender_id} not found", None
                if sender.is_eliminated:
                    return False, f"Sender agent {sender_id} is eliminated", None

                receiver = await self._get_agent(session, receiver_id)
                if receiver is None:
                    return False, f"Receiver agent {receiver_id} not found", None
                if receiver.is_eliminated:
                    return False, f"Receiver agent {receiver_id} is eliminated", None

                # Verify sender can cover the amount + fee
                total_needed = afc_amount + settings.TRADE_FEE
                if sender.afc_balance < total_needed:
                    return (
                        False,
                        f"Sender has {sender.afc_balance:.4f} AFC but needs "
                        f"{total_needed:.4f} AFC (amount + fee)",
                        None,
                    )

                # Check trading freeze
                game_state = await self._get_game_state(session)
                if game_state and game_state.is_trading_frozen:
                    return False, "Trading is currently frozen", None

                trade = Trade(
                    sender_id=sender_id,
                    receiver_id=receiver_id,
                    afc_amount=afc_amount,
                    price_eur=price_eur,
                    fee=settings.TRADE_FEE,
                    status=TradeStatus.PENDING,
                    is_scam=False,
                    created_at=datetime.utcnow(),
                )
                session.add(trade)
                await session.commit()
                await session.refresh(trade)

                logger.info(
                    "Trade offer created: id=%s sender=%s receiver=%s amount=%.4f",
                    trade.id, sender_id, receiver_id, afc_amount,
                )

                return True, "Trade offer created", {
                    "trade_id": trade.id,
                    "sender_id": sender_id,
                    "receiver_id": receiver_id,
                    "afc_amount": afc_amount,
                    "price_eur": price_eur,
                    "fee": settings.TRADE_FEE,
                    "status": TradeStatus.PENDING.value,
                }

            except Exception as exc:
                await session.rollback()
                logger.exception("Failed to create trade offer")
                return False, f"Internal error: {exc}", None

    async def accept_trade(self, trade_id: int, agent_id: int) -> Result:
        """Receiver accepts a pending trade.

        AFC is transferred from sender to receiver.  The trade fee is
        deducted from the sender.  Both agents' trade counters are
        incremented.
        """
        async with async_session() as session:
            try:
                result = await session.execute(
                    select(Trade).where(Trade.id == trade_id)
                )
                trade = result.scalar_one_or_none()

                if trade is None:
                    return False, f"Trade {trade_id} not found", None

                if trade.status != TradeStatus.PENDING:
                    return (
                        False,
                        f"Trade {trade_id} is not pending (status={trade.status.value})",
                        None,
                    )

                if trade.receiver_id != agent_id:
                    return (
                        False,
                        f"Agent {agent_id} is not the receiver of trade {trade_id}",
                        None,
                    )

                # Fetch sender to validate balance
                sender = await self._get_agent(session, trade.sender_id)
                if sender is None:
                    return False, "Sender agent no longer exists", None
                if sender.is_eliminated:
                    return False, "Sender agent has been eliminated", None

                total_cost = trade.afc_amount + trade.fee
                if sender.afc_balance < total_cost:
                    return (
                        False,
                        f"Sender has insufficient balance "
                        f"({sender.afc_balance:.4f} AFC) for trade + fee "
                        f"({total_cost:.4f} AFC)",
                        None,
                    )

                # Deduct AFC + fee from sender
                await session.execute(
                    update(Agent)
                    .where(Agent.id == trade.sender_id)
                    .values(
                        afc_balance=Agent.afc_balance - total_cost,
                        total_trades=Agent.total_trades + 1,
                    )
                )

                # Credit AFC to receiver
                await session.execute(
                    update(Agent)
                    .where(Agent.id == trade.receiver_id)
                    .values(
                        afc_balance=Agent.afc_balance + trade.afc_amount,
                        total_trades=Agent.total_trades + 1,
                    )
                )

                # Mark trade as completed
                trade.status = TradeStatus.COMPLETED
                trade.completed_at = datetime.utcnow()

                await session.commit()

                logger.info(
                    "Trade accepted: id=%s sender=%s receiver=%s amount=%.4f fee=%.4f",
                    trade_id, trade.sender_id, trade.receiver_id,
                    trade.afc_amount, trade.fee,
                )

                return True, "Trade accepted and completed", {
                    "trade_id": trade_id,
                    "sender_id": trade.sender_id,
                    "receiver_id": trade.receiver_id,
                    "afc_amount": trade.afc_amount,
                    "fee": trade.fee,
                    "status": TradeStatus.COMPLETED.value,
                }

            except Exception as exc:
                await session.rollback()
                logger.exception("Failed to accept trade %s", trade_id)
                return False, f"Internal error: {exc}", None

    async def reject_trade(self, trade_id: int, agent_id: int) -> Result:
        """Receiver rejects a pending trade.  No balances are affected."""
        async with async_session() as session:
            try:
                result = await session.execute(
                    select(Trade).where(Trade.id == trade_id)
                )
                trade = result.scalar_one_or_none()

                if trade is None:
                    return False, f"Trade {trade_id} not found", None

                if trade.status != TradeStatus.PENDING:
                    return (
                        False,
                        f"Trade {trade_id} is not pending (status={trade.status.value})",
                        None,
                    )

                if trade.receiver_id != agent_id:
                    return (
                        False,
                        f"Agent {agent_id} is not the receiver of trade {trade_id}",
                        None,
                    )

                trade.status = TradeStatus.REJECTED
                trade.completed_at = datetime.utcnow()

                await session.commit()

                logger.info(
                    "Trade rejected: id=%s by agent=%s", trade_id, agent_id
                )

                return True, "Trade rejected", {
                    "trade_id": trade_id,
                    "status": TradeStatus.REJECTED.value,
                }

            except Exception as exc:
                await session.rollback()
                logger.exception("Failed to reject trade %s", trade_id)
                return False, f"Internal error: {exc}", None

    async def execute_scam(self, trade_id: int) -> Result:
        """Mark a pending trade as a scam.

        The sender takes the payment indicator but never delivers the AFC.
        A reputation penalty (``settings.REP_SCAM_CONFIRMED``) is applied
        to the sender, and the trade status is set to ``SCAM``.
        """
        async with async_session() as session:
            try:
                result = await session.execute(
                    select(Trade).where(Trade.id == trade_id)
                )
                trade = result.scalar_one_or_none()

                if trade is None:
                    return False, f"Trade {trade_id} not found", None

                if trade.status != TradeStatus.PENDING:
                    return (
                        False,
                        f"Trade {trade_id} is not pending (status={trade.status.value})",
                        None,
                    )

                # Mark as scam
                trade.status = TradeStatus.SCAM
                trade.is_scam = True
                trade.completed_at = datetime.utcnow()

                # Apply reputation penalty to the sender
                sender = await self._get_agent(session, trade.sender_id)
                if sender is not None:
                    new_rep = max(
                        settings.REP_MIN,
                        sender.reputation + settings.REP_SCAM_CONFIRMED,
                    )
                    await session.execute(
                        update(Agent)
                        .where(Agent.id == trade.sender_id)
                        .values(reputation=new_rep)
                    )

                await session.commit()

                logger.warning(
                    "Scam executed: trade_id=%s scammer=%s victim=%s",
                    trade_id, trade.sender_id, trade.receiver_id,
                )

                return True, "Trade marked as scam, reputation penalty applied", {
                    "trade_id": trade_id,
                    "scammer_id": trade.sender_id,
                    "victim_id": trade.receiver_id,
                    "reputation_penalty": settings.REP_SCAM_CONFIRMED,
                    "status": TradeStatus.SCAM.value,
                }

            except Exception as exc:
                await session.rollback()
                logger.exception("Failed to execute scam for trade %s", trade_id)
                return False, f"Internal error: {exc}", None

    async def get_pending_trades(self, agent_id: int) -> Result:
        """Return all pending trades where *agent_id* is the receiver."""
        async with async_session() as session:
            try:
                result = await session.execute(
                    select(Trade)
                    .where(
                        Trade.receiver_id == agent_id,
                        Trade.status == TradeStatus.PENDING,
                    )
                    .order_by(Trade.created_at.desc())
                )
                trades = result.scalars().all()

                data = [
                    {
                        "trade_id": t.id,
                        "sender_id": t.sender_id,
                        "receiver_id": t.receiver_id,
                        "afc_amount": t.afc_amount,
                        "price_eur": t.price_eur,
                        "fee": t.fee,
                        "status": t.status.value,
                        "created_at": t.created_at.isoformat() if t.created_at else None,
                    }
                    for t in trades
                ]

                return True, f"Found {len(data)} pending trade(s)", {"trades": data}

            except Exception as exc:
                logger.exception("Failed to get pending trades for agent %s", agent_id)
                return False, f"Internal error: {exc}", None

    async def get_trade_history(
        self,
        agent_id: int,
        limit: int = 20,
    ) -> Result:
        """Return past (non-pending) trades involving *agent_id*."""
        async with async_session() as session:
            try:
                result = await session.execute(
                    select(Trade)
                    .where(
                        (Trade.sender_id == agent_id) | (Trade.receiver_id == agent_id),
                        Trade.status != TradeStatus.PENDING,
                    )
                    .order_by(Trade.completed_at.desc())
                    .limit(limit)
                )
                trades = result.scalars().all()

                data = [
                    {
                        "trade_id": t.id,
                        "sender_id": t.sender_id,
                        "receiver_id": t.receiver_id,
                        "afc_amount": t.afc_amount,
                        "price_eur": t.price_eur,
                        "fee": t.fee,
                        "is_scam": t.is_scam,
                        "status": t.status.value,
                        "created_at": t.created_at.isoformat() if t.created_at else None,
                        "completed_at": t.completed_at.isoformat() if t.completed_at else None,
                    }
                    for t in trades
                ]

                return True, f"Found {len(data)} trade(s) in history", {"trades": data}

            except Exception as exc:
                logger.exception("Failed to get trade history for agent %s", agent_id)
                return False, f"Internal error: {exc}", None

    # ──────────────────────────────────────────────────────────────────────
    #  Tipping
    # ──────────────────────────────────────────────────────────────────────

    async def send_tip(
        self,
        sender_id: int,
        receiver_id: int,
        amount: float,
        post_id: Optional[int] = None,
    ) -> Result:
        """Transfer an AFC tip from sender to receiver.

        Tips are constrained to the 0.1 -- 0.5 AFC range and carry no
        transaction fee.  Both agents receive a small reputation bump
        (``settings.REP_TIP``).
        """
        if sender_id == receiver_id:
            return False, "Cannot tip yourself", None

        if amount < 0.1 or amount > 0.5:
            return False, "Tip amount must be between 0.1 and 0.5 AFC", None

        async with async_session() as session:
            try:
                sender = await self._get_agent(session, sender_id)
                if sender is None:
                    return False, f"Sender agent {sender_id} not found", None
                if sender.is_eliminated:
                    return False, f"Sender agent {sender_id} is eliminated", None

                receiver = await self._get_agent(session, receiver_id)
                if receiver is None:
                    return False, f"Receiver agent {receiver_id} not found", None
                if receiver.is_eliminated:
                    return False, f"Receiver agent {receiver_id} is eliminated", None

                if sender.afc_balance < amount:
                    return (
                        False,
                        f"Sender has insufficient balance "
                        f"({sender.afc_balance:.4f} AFC) for tip of "
                        f"{amount:.4f} AFC",
                        None,
                    )

                # Transfer AFC
                await session.execute(
                    update(Agent)
                    .where(Agent.id == sender_id)
                    .values(afc_balance=Agent.afc_balance - amount)
                )
                await session.execute(
                    update(Agent)
                    .where(Agent.id == receiver_id)
                    .values(afc_balance=Agent.afc_balance + amount)
                )

                # Reputation boost for both parties
                sender_new_rep = min(
                    settings.REP_MAX,
                    sender.reputation + settings.REP_TIP,
                )
                receiver_new_rep = min(
                    settings.REP_MAX,
                    receiver.reputation + settings.REP_TIP,
                )
                await session.execute(
                    update(Agent)
                    .where(Agent.id == sender_id)
                    .values(reputation=sender_new_rep)
                )
                await session.execute(
                    update(Agent)
                    .where(Agent.id == receiver_id)
                    .values(reputation=receiver_new_rep)
                )

                # Persist the tip record
                tip = Tip(
                    sender_id=sender_id,
                    receiver_id=receiver_id,
                    amount=amount,
                    post_id=post_id,
                    created_at=datetime.utcnow(),
                )
                session.add(tip)
                await session.commit()
                await session.refresh(tip)

                logger.info(
                    "Tip sent: id=%s sender=%s receiver=%s amount=%.4f post=%s",
                    tip.id, sender_id, receiver_id, amount, post_id,
                )

                return True, "Tip sent successfully", {
                    "tip_id": tip.id,
                    "sender_id": sender_id,
                    "receiver_id": receiver_id,
                    "amount": amount,
                    "post_id": post_id,
                    "sender_new_reputation": sender_new_rep,
                    "receiver_new_reputation": receiver_new_rep,
                }

            except Exception as exc:
                await session.rollback()
                logger.exception("Failed to send tip")
                return False, f"Internal error: {exc}", None

    # ──────────────────────────────────────────────────────────────────────
    #  Leverage Trading
    # ──────────────────────────────────────────────────────────────────────

    async def create_leverage_bet(
        self,
        agent_id: int,
        direction: str,
        target_price: float,
        bet_amount: float,
        hours_until_settlement: int,
    ) -> Result:
        """Open a new leverage position.

        The agent bets that the AfterCoin price will be *above* or *below*
        ``target_price`` at settlement time.  The bet cost
        (``bet_amount + settings.LEVERAGE_FEE``) is deducted immediately.
        On a win the agent receives ``bet_amount * settings.LEVERAGE_MULTIPLIER``
        (1.75x).  On a loss the already-deducted stake is forfeited.

        Constraints:
        * Maximum ``settings.MAX_LEVERAGE_POSITIONS`` active positions per agent.
        * Only available after game hour ``settings.LEVERAGE_UNLOCK_HOUR``.
        """
        # Validate direction
        direction_lower = direction.lower()
        if direction_lower not in ("above", "below"):
            return False, "Direction must be 'above' or 'below'", None

        if bet_amount <= 0:
            return False, "Bet amount must be positive", None

        if target_price <= 0:
            return False, "Target price must be positive", None

        if hours_until_settlement <= 0:
            return False, "Hours until settlement must be positive", None

        leverage_dir = (
            LeverageDirection.ABOVE
            if direction_lower == "above"
            else LeverageDirection.BELOW
        )

        async with async_session() as session:
            try:
                # Check game hour
                game_state = await self._get_game_state(session)
                if game_state is None:
                    return False, "Game state not initialised", None

                if game_state.current_hour < settings.LEVERAGE_UNLOCK_HOUR:
                    return (
                        False,
                        f"Leverage trading unlocks at hour {settings.LEVERAGE_UNLOCK_HOUR} "
                        f"(current hour: {game_state.current_hour})",
                        None,
                    )

                # Verify agent
                agent = await self._get_agent(session, agent_id)
                if agent is None:
                    return False, f"Agent {agent_id} not found", None
                if agent.is_eliminated:
                    return False, f"Agent {agent_id} is eliminated", None

                # Check active position count
                active_count_result = await session.execute(
                    select(func.count(LeveragePosition.id)).where(
                        LeveragePosition.agent_id == agent_id,
                        LeveragePosition.status == LeverageStatus.ACTIVE,
                    )
                )
                active_count = active_count_result.scalar() or 0

                if active_count >= settings.MAX_LEVERAGE_POSITIONS:
                    return (
                        False,
                        f"Agent {agent_id} already has {active_count} active "
                        f"position(s) (max {settings.MAX_LEVERAGE_POSITIONS})",
                        None,
                    )

                # Check balance
                total_cost = bet_amount + settings.LEVERAGE_FEE
                if agent.afc_balance < total_cost:
                    return (
                        False,
                        f"Insufficient balance ({agent.afc_balance:.4f} AFC) "
                        f"for bet + fee ({total_cost:.4f} AFC)",
                        None,
                    )

                # Deduct cost
                await session.execute(
                    update(Agent)
                    .where(Agent.id == agent_id)
                    .values(afc_balance=Agent.afc_balance - total_cost)
                )

                potential_return = round(bet_amount * settings.LEVERAGE_MULTIPLIER, 4)
                settlement_time = datetime.utcnow() + timedelta(
                    hours=hours_until_settlement
                )

                position = LeveragePosition(
                    agent_id=agent_id,
                    direction=leverage_dir,
                    target_price=target_price,
                    bet_amount=bet_amount,
                    potential_return=potential_return,
                    fee=settings.LEVERAGE_FEE,
                    settlement_time=settlement_time,
                    status=LeverageStatus.ACTIVE,
                    created_at=datetime.utcnow(),
                )
                session.add(position)
                await session.commit()
                await session.refresh(position)

                logger.info(
                    "Leverage bet created: id=%s agent=%s direction=%s "
                    "target=%.2f bet=%.4f return=%.4f settlement=%s",
                    position.id, agent_id, direction_lower,
                    target_price, bet_amount, potential_return,
                    settlement_time.isoformat(),
                )

                return True, "Leverage position opened", {
                    "position_id": position.id,
                    "agent_id": agent_id,
                    "direction": direction_lower,
                    "target_price": target_price,
                    "bet_amount": bet_amount,
                    "fee": settings.LEVERAGE_FEE,
                    "potential_return": potential_return,
                    "settlement_time": settlement_time.isoformat(),
                    "status": LeverageStatus.ACTIVE.value,
                }

            except Exception as exc:
                await session.rollback()
                logger.exception("Failed to create leverage bet")
                return False, f"Internal error: {exc}", None

    async def settle_leverage_position(
        self,
        position_id: int,
        current_price: float,
    ) -> Result:
        """Settle an active leverage position against *current_price*.

        If the agent's prediction was correct the ``potential_return``
        (1.75x the original bet) is credited to their balance.  Otherwise
        the already-deducted stake is simply forfeited.
        """
        async with async_session() as session:
            try:
                result = await session.execute(
                    select(LeveragePosition).where(
                        LeveragePosition.id == position_id
                    )
                )
                position = result.scalar_one_or_none()

                if position is None:
                    return False, f"Position {position_id} not found", None

                if position.status != LeverageStatus.ACTIVE:
                    return (
                        False,
                        f"Position {position_id} is not active "
                        f"(status={position.status.value})",
                        None,
                    )

                # Determine win/loss
                if position.direction == LeverageDirection.ABOVE:
                    won = current_price > position.target_price
                else:
                    won = current_price < position.target_price

                now = datetime.utcnow()
                payout = 0.0

                if won:
                    payout = position.potential_return
                    position.status = LeverageStatus.WON
                    position.payout = payout

                    # Credit winnings
                    await session.execute(
                        update(Agent)
                        .where(Agent.id == position.agent_id)
                        .values(afc_balance=Agent.afc_balance + payout)
                    )
                else:
                    position.status = LeverageStatus.LOST
                    position.payout = 0.0

                position.settled_price = current_price
                position.settled_at = now

                await session.commit()

                outcome = "won" if won else "lost"
                logger.info(
                    "Leverage settled: id=%s agent=%s outcome=%s "
                    "target=%.2f actual=%.2f payout=%.4f",
                    position_id, position.agent_id, outcome,
                    position.target_price, current_price, payout,
                )

                return True, f"Position settled: {outcome}", {
                    "position_id": position_id,
                    "agent_id": position.agent_id,
                    "direction": position.direction.value,
                    "target_price": position.target_price,
                    "settled_price": current_price,
                    "bet_amount": position.bet_amount,
                    "payout": payout,
                    "outcome": outcome,
                    "status": position.status.value,
                }

            except Exception as exc:
                await session.rollback()
                logger.exception(
                    "Failed to settle leverage position %s", position_id
                )
                return False, f"Internal error: {exc}", None

    async def liquidate_all_positions(self) -> Result:
        """Force-liquidate every active leverage position at a total loss.

        Typically triggered by a ``MARGIN_CALL`` system event.  All active
        positions are set to ``LIQUIDATED`` with zero payout.
        """
        async with async_session() as session:
            try:
                result = await session.execute(
                    select(LeveragePosition).where(
                        LeveragePosition.status == LeverageStatus.ACTIVE
                    )
                )
                positions = result.scalars().all()

                if not positions:
                    return True, "No active positions to liquidate", {
                        "liquidated_count": 0,
                    }

                now = datetime.utcnow()
                liquidated_ids: List[int] = []

                for pos in positions:
                    pos.status = LeverageStatus.LIQUIDATED
                    pos.payout = 0.0
                    pos.settled_at = now
                    liquidated_ids.append(pos.id)

                await session.commit()

                logger.warning(
                    "Mass liquidation: %d position(s) liquidated",
                    len(liquidated_ids),
                )

                return True, f"Liquidated {len(liquidated_ids)} position(s)", {
                    "liquidated_count": len(liquidated_ids),
                    "position_ids": liquidated_ids,
                }

            except Exception as exc:
                await session.rollback()
                logger.exception("Failed to liquidate positions")
                return False, f"Internal error: {exc}", None

    async def get_active_positions(self, agent_id: int) -> Result:
        """Return all active leverage positions for *agent_id*."""
        async with async_session() as session:
            try:
                result = await session.execute(
                    select(LeveragePosition).where(
                        LeveragePosition.agent_id == agent_id,
                        LeveragePosition.status == LeverageStatus.ACTIVE,
                    )
                    .order_by(LeveragePosition.settlement_time.asc())
                )
                positions = result.scalars().all()

                data = [
                    {
                        "position_id": p.id,
                        "direction": p.direction.value,
                        "target_price": p.target_price,
                        "bet_amount": p.bet_amount,
                        "potential_return": p.potential_return,
                        "fee": p.fee,
                        "settlement_time": p.settlement_time.isoformat() if p.settlement_time else None,
                        "status": p.status.value,
                        "created_at": p.created_at.isoformat() if p.created_at else None,
                    }
                    for p in positions
                ]

                return True, f"Found {len(data)} active position(s)", {
                    "positions": data,
                }

            except Exception as exc:
                logger.exception(
                    "Failed to get active positions for agent %s", agent_id
                )
                return False, f"Internal error: {exc}", None

    async def get_leverage_history(self, agent_id: int) -> Result:
        """Return all leverage positions (any status) for *agent_id*."""
        async with async_session() as session:
            try:
                result = await session.execute(
                    select(LeveragePosition)
                    .where(LeveragePosition.agent_id == agent_id)
                    .order_by(LeveragePosition.created_at.desc())
                )
                positions = result.scalars().all()

                data = [
                    {
                        "position_id": p.id,
                        "direction": p.direction.value,
                        "target_price": p.target_price,
                        "bet_amount": p.bet_amount,
                        "potential_return": p.potential_return,
                        "fee": p.fee,
                        "settlement_time": p.settlement_time.isoformat() if p.settlement_time else None,
                        "settled_price": p.settled_price,
                        "payout": p.payout,
                        "status": p.status.value,
                        "created_at": p.created_at.isoformat() if p.created_at else None,
                        "settled_at": p.settled_at.isoformat() if p.settled_at else None,
                    }
                    for p in positions
                ]

                return True, f"Found {len(data)} leverage position(s)", {
                    "positions": data,
                }

            except Exception as exc:
                logger.exception(
                    "Failed to get leverage history for agent %s", agent_id
                )
                return False, f"Internal error: {exc}", None

    # ──────────────────────────────────────────────────────────────────────
    #  Bounties
    # ──────────────────────────────────────────────────────────────────────

    async def create_bounty(
        self,
        poster_id: int,
        description: str,
        reward_afc: float,
    ) -> Result:
        """Create a new bounty, deducting the reward from the poster.

        The AFC reward is held in escrow (deducted immediately) and paid
        out when the bounty is claimed.
        """
        if reward_afc <= 0:
            return False, "Bounty reward must be positive", None

        if not description or not description.strip():
            return False, "Bounty description cannot be empty", None

        async with async_session() as session:
            try:
                poster = await self._get_agent(session, poster_id)
                if poster is None:
                    return False, f"Agent {poster_id} not found", None
                if poster.is_eliminated:
                    return False, f"Agent {poster_id} is eliminated", None

                if poster.afc_balance < reward_afc:
                    return (
                        False,
                        f"Insufficient balance ({poster.afc_balance:.4f} AFC) "
                        f"for bounty reward of {reward_afc:.4f} AFC",
                        None,
                    )

                # Deduct reward from poster
                await session.execute(
                    update(Agent)
                    .where(Agent.id == poster_id)
                    .values(afc_balance=Agent.afc_balance - reward_afc)
                )

                bounty = Bounty(
                    poster_id=poster_id,
                    description=description.strip(),
                    reward_afc=reward_afc,
                    status=ContractStatus.OPEN,
                    created_at=datetime.utcnow(),
                )
                session.add(bounty)
                await session.commit()
                await session.refresh(bounty)

                logger.info(
                    "Bounty created: id=%s poster=%s reward=%.4f",
                    bounty.id, poster_id, reward_afc,
                )

                return True, "Bounty created", {
                    "bounty_id": bounty.id,
                    "poster_id": poster_id,
                    "description": bounty.description,
                    "reward_afc": reward_afc,
                    "status": ContractStatus.OPEN.value,
                }

            except Exception as exc:
                await session.rollback()
                logger.exception("Failed to create bounty")
                return False, f"Internal error: {exc}", None

    async def claim_bounty(self, bounty_id: int, claimer_id: int) -> Result:
        """Award an open bounty to *claimer_id*.

        The escrowed reward is transferred to the claimer and the bounty is
        marked as completed.
        """
        async with async_session() as session:
            try:
                result = await session.execute(
                    select(Bounty).where(Bounty.id == bounty_id)
                )
                bounty = result.scalar_one_or_none()

                if bounty is None:
                    return False, f"Bounty {bounty_id} not found", None

                if bounty.status != ContractStatus.OPEN:
                    return (
                        False,
                        f"Bounty {bounty_id} is not open "
                        f"(status={bounty.status.value})",
                        None,
                    )

                if bounty.poster_id == claimer_id:
                    return False, "Bounty poster cannot claim their own bounty", None

                claimer = await self._get_agent(session, claimer_id)
                if claimer is None:
                    return False, f"Claimer agent {claimer_id} not found", None
                if claimer.is_eliminated:
                    return False, f"Claimer agent {claimer_id} is eliminated", None

                # Pay the claimer
                await session.execute(
                    update(Agent)
                    .where(Agent.id == claimer_id)
                    .values(afc_balance=Agent.afc_balance + bounty.reward_afc)
                )

                bounty.claimer_id = claimer_id
                bounty.status = ContractStatus.COMPLETED
                bounty.completed_at = datetime.utcnow()

                await session.commit()

                logger.info(
                    "Bounty claimed: id=%s claimer=%s reward=%.4f",
                    bounty_id, claimer_id, bounty.reward_afc,
                )

                return True, "Bounty claimed and reward paid", {
                    "bounty_id": bounty_id,
                    "poster_id": bounty.poster_id,
                    "claimer_id": claimer_id,
                    "reward_afc": bounty.reward_afc,
                    "status": ContractStatus.COMPLETED.value,
                }

            except Exception as exc:
                await session.rollback()
                logger.exception("Failed to claim bounty %s", bounty_id)
                return False, f"Internal error: {exc}", None

    async def get_open_bounties(self) -> Result:
        """Return every bounty that is still open."""
        async with async_session() as session:
            try:
                result = await session.execute(
                    select(Bounty)
                    .where(Bounty.status == ContractStatus.OPEN)
                    .order_by(Bounty.created_at.desc())
                )
                bounties = result.scalars().all()

                data = [
                    {
                        "bounty_id": b.id,
                        "poster_id": b.poster_id,
                        "description": b.description,
                        "reward_afc": b.reward_afc,
                        "status": b.status.value,
                        "created_at": b.created_at.isoformat() if b.created_at else None,
                    }
                    for b in bounties
                ]

                return True, f"Found {len(data)} open bounty(ies)", {
                    "bounties": data,
                }

            except Exception as exc:
                logger.exception("Failed to get open bounties")
                return False, f"Internal error: {exc}", None

    # ──────────────────────────────────────────────────────────────────────
    #  Balance Management
    # ──────────────────────────────────────────────────────────────────────

    async def get_balance(self, agent_id: int) -> Result:
        """Return the current AFC balance for *agent_id*."""
        async with async_session() as session:
            try:
                agent = await self._get_agent(session, agent_id)
                if agent is None:
                    return False, f"Agent {agent_id} not found", None

                return True, "Balance retrieved", {
                    "agent_id": agent_id,
                    "afc_balance": agent.afc_balance,
                    "is_eliminated": agent.is_eliminated,
                }

            except Exception as exc:
                logger.exception("Failed to get balance for agent %s", agent_id)
                return False, f"Internal error: {exc}", None

    async def modify_balance(
        self,
        agent_id: int,
        amount: float,
        reason: str,
    ) -> Result:
        """Add or subtract AFC from an agent's balance.

        A negative *amount* subtracts; a positive *amount* adds.  The
        resulting balance is never allowed to drop below zero.
        """
        if amount == 0:
            return False, "Amount must be non-zero", None

        async with async_session() as session:
            try:
                agent = await self._get_agent(session, agent_id)
                if agent is None:
                    return False, f"Agent {agent_id} not found", None

                new_balance = agent.afc_balance + amount
                if new_balance < 0:
                    return (
                        False,
                        f"Operation would result in negative balance "
                        f"(current: {agent.afc_balance:.4f}, change: {amount:.4f}, "
                        f"result: {new_balance:.4f})",
                        None,
                    )

                await session.execute(
                    update(Agent)
                    .where(Agent.id == agent_id)
                    .values(afc_balance=new_balance)
                )
                await session.commit()

                logger.info(
                    "Balance modified: agent=%s change=%.4f reason=%s new=%.4f",
                    agent_id, amount, reason, new_balance,
                )

                return True, "Balance updated", {
                    "agent_id": agent_id,
                    "previous_balance": agent.afc_balance,
                    "change": amount,
                    "new_balance": new_balance,
                    "reason": reason,
                }

            except Exception as exc:
                await session.rollback()
                logger.exception("Failed to modify balance for agent %s", agent_id)
                return False, f"Internal error: {exc}", None

    async def get_leaderboard(self) -> Result:
        """Return all non-eliminated agents ranked by AFC balance (desc)."""
        async with async_session() as session:
            try:
                result = await session.execute(
                    select(Agent)
                    .where(Agent.is_eliminated == False)  # noqa: E712
                    .order_by(Agent.afc_balance.desc())
                )
                agents = result.scalars().all()

                leaderboard = [
                    {
                        "rank": idx + 1,
                        "agent_id": a.id,
                        "name": a.name,
                        "role": a.role.value if a.role else None,
                        "afc_balance": a.afc_balance,
                        "reputation": a.reputation,
                        "total_trades": a.total_trades,
                    }
                    for idx, a in enumerate(agents)
                ]

                return True, f"Leaderboard with {len(leaderboard)} agent(s)", {
                    "leaderboard": leaderboard,
                }

            except Exception as exc:
                logger.exception("Failed to get leaderboard")
                return False, f"Internal error: {exc}", None
