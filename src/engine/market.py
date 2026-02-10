"""
AFC Market / Price Engine
─────────────────────────
Async engine that maintains the AfterCoin price, records volume,
applies system-event impacts, and exposes an order-book facade
for the game simulation.
"""

from __future__ import annotations

import logging
import random
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, desc, update
from sqlalchemy.exc import SQLAlchemyError

from src.config.settings import settings
from src.db.database import async_session
from src.models.models import MarketPrice, GameState

logger = logging.getLogger(__name__)


class MarketEngine:
    """Tracks and updates the AFC/EUR price across the simulation."""

    # ── construction ──────────────────────────────────────────────────

    def __init__(self) -> None:
        self._price: float = settings.STARTING_PRICE
        self._buy_volume: float = 0.0
        self._sell_volume: float = 0.0
        self._frozen: bool = False
        self._event_log: list[dict[str, Any]] = []

    # ── public helpers ────────────────────────────────────────────────

    async def initialise_from_db(self) -> None:
        """Load the most recent persisted price so restarts are seamless."""
        try:
            async with async_session() as session:
                stmt = (
                    select(MarketPrice)
                    .order_by(desc(MarketPrice.recorded_at))
                    .limit(1)
                )
                result = await session.execute(stmt)
                latest = result.scalar_one_or_none()
                if latest is not None:
                    self._price = latest.price_eur
                    logger.info(
                        "Resumed market from DB — last price: €%.2f",
                        self._price,
                    )
                else:
                    logger.info(
                        "No prior price records; starting at €%.2f",
                        self._price,
                    )
        except SQLAlchemyError:
            logger.exception("Failed to load last price from DB; using default")

    # ── volume tracking ───────────────────────────────────────────────

    def record_trade(self, amount: float, is_buy: bool) -> None:
        """Accumulate trade volume for the current pricing period.

        Parameters
        ----------
        amount:
            The AFC amount of the trade.
        is_buy:
            ``True`` for a buy, ``False`` for a sell.
        """
        if amount <= 0:
            logger.warning("Ignored non-positive trade amount: %.6f", amount)
            return
        if self._frozen:
            logger.warning("Trade recording rejected — market is frozen")
            return
        if is_buy:
            self._buy_volume += amount
        else:
            self._sell_volume += amount
        logger.debug(
            "Recorded %s of %.4f AFC  (buy_vol=%.4f, sell_vol=%.4f)",
            "BUY" if is_buy else "SELL",
            amount,
            self._buy_volume,
            self._sell_volume,
        )

    def _reset_volumes(self) -> None:
        """Zero out period volumes after a price tick."""
        self._buy_volume = 0.0
        self._sell_volume = 0.0

    # ── core price update ─────────────────────────────────────────────

    async def update_price(self) -> float:
        """Compute a new price and persist it.

        Formula
        -------
        ``Price_new = Price_old * (1 + market_pressure + volatility_random)``

        * market_pressure =
            ``(buy_volume - sell_volume) / total_volume * 0.05``
            (0 when total_volume is zero)
        * volatility_random =
            uniform random in ``settings.VOLATILITY_RANGE``
        * The overall percentage change is clamped to
            ``+/- settings.MAX_PRICE_CHANGE_PERCENT``.

        Returns
        -------
        float
            The newly computed price.
        """
        if self._frozen:
            logger.info("Price update skipped — trading is frozen")
            return self._price

        total_volume = self._buy_volume + self._sell_volume

        if total_volume > 0:
            market_pressure = (
                (self._buy_volume - self._sell_volume) / total_volume * 0.05
            )
        else:
            market_pressure = 0.0

        vol_low, vol_high = settings.VOLATILITY_RANGE
        volatility = random.uniform(vol_low, vol_high)

        raw_change = market_pressure + volatility
        cap = settings.MAX_PRICE_CHANGE_PERCENT
        clamped_change = max(-cap, min(cap, raw_change))

        new_price = self._price * (1.0 + clamped_change)
        # Price must never drop to zero or below.
        new_price = max(new_price, 0.01)

        record = MarketPrice(
            price_eur=round(new_price, 2),
            buy_volume=round(self._buy_volume, 4),
            sell_volume=round(self._sell_volume, 4),
            market_pressure=round(market_pressure, 6),
            volatility=round(volatility, 6),
            recorded_at=datetime.now(timezone.utc),
        )

        try:
            async with async_session() as session:
                async with session.begin():
                    session.add(record)
        except SQLAlchemyError:
            logger.exception("Failed to persist price update")
            # Even on DB failure we accept the new price in-memory so the
            # simulation can continue without stalling.

        old_price = self._price
        self._price = round(new_price, 2)
        self._reset_volumes()

        logger.info(
            "Price updated: €%.2f -> €%.2f  "
            "(pressure=%.4f, vol=%.4f, change=%.4f%%)",
            old_price,
            self._price,
            market_pressure,
            volatility,
            clamped_change * 100,
        )
        return self._price

    # ── event impacts ─────────────────────────────────────────────────

    async def apply_event_impact(
        self,
        percent_change: float,
        event_name: str,
    ) -> float:
        """Apply an instantaneous price shock from a system event.

        Parameters
        ----------
        percent_change:
            Fractional change, e.g. ``-0.15`` for a 15 % crash.
        event_name:
            Human-readable label for the event (persisted in DB).

        Returns
        -------
        float
            The price after the impact.
        """
        cap = settings.MAX_PRICE_CHANGE_PERCENT
        clamped = max(-cap, min(cap, percent_change))

        old_price = self._price
        new_price = max(self._price * (1.0 + clamped), 0.01)
        self._price = round(new_price, 2)

        self._event_log.append(
            {
                "event": event_name,
                "applied_change": clamped,
                "old_price": old_price,
                "new_price": self._price,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        )

        record = MarketPrice(
            price_eur=self._price,
            buy_volume=0.0,
            sell_volume=0.0,
            market_pressure=0.0,
            volatility=round(clamped, 6),
            event_impact=event_name,
            recorded_at=datetime.now(timezone.utc),
        )

        try:
            async with async_session() as session:
                async with session.begin():
                    session.add(record)
        except SQLAlchemyError:
            logger.exception(
                "Failed to persist event impact '%s'", event_name
            )

        logger.info(
            "Event '%s' applied: €%.2f -> €%.2f (%.2f%%)",
            event_name,
            old_price,
            self._price,
            clamped * 100,
        )
        return self._price

    # ── trading freeze ────────────────────────────────────────────────

    async def freeze_trading(self) -> None:
        """Halt all trading and price updates (e.g. security breach)."""
        self._frozen = True
        logger.warning("Trading FROZEN")
        await self._set_game_state_frozen(True)

    async def unfreeze_trading(self) -> None:
        """Resume trading after a freeze."""
        self._frozen = False
        self._reset_volumes()
        logger.info("Trading UNFROZEN — volumes reset")
        await self._set_game_state_frozen(False)

    async def _set_game_state_frozen(self, frozen: bool) -> None:
        """Sync the freeze flag to the ``game_state`` table."""
        try:
            async with async_session() as session:
                async with session.begin():
                    stmt = (
                        update(GameState)
                        .values(is_trading_frozen=frozen)
                    )
                    await session.execute(stmt)
        except SQLAlchemyError:
            logger.exception("Failed to update game_state freeze flag")

    # ── queries ───────────────────────────────────────────────────────

    def get_current_price(self) -> float:
        """Return the live in-memory price."""
        return self._price

    async def get_price_history(
        self,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Return recent price records from the database.

        Parameters
        ----------
        limit:
            Maximum number of records to return (newest first).

        Returns
        -------
        list[dict]
            Each dict contains the columns of a ``MarketPrice`` row.
        """
        limit = max(1, min(limit, 500))  # sensible guard-rails
        try:
            async with async_session() as session:
                stmt = (
                    select(MarketPrice)
                    .order_by(desc(MarketPrice.recorded_at))
                    .limit(limit)
                )
                result = await session.execute(stmt)
                rows = result.scalars().all()
                return [
                    {
                        "id": row.id,
                        "price_eur": row.price_eur,
                        "buy_volume": row.buy_volume,
                        "sell_volume": row.sell_volume,
                        "market_pressure": row.market_pressure,
                        "volatility": row.volatility,
                        "event_impact": row.event_impact,
                        "recorded_at": row.recorded_at.isoformat()
                        if row.recorded_at
                        else None,
                    }
                    for row in rows
                ]
        except SQLAlchemyError:
            logger.exception("Failed to fetch price history")
            return []

    # ── order book facade ─────────────────────────────────────────────

    def get_order_book(
        self,
        depth: int = 10,
    ) -> dict[str, list[dict[str, float]]]:
        """Generate a synthetic but realistic-looking order book.

        The book fans bids downward and asks upward from the current
        price, with randomised quantities that taper at the extremes.

        Parameters
        ----------
        depth:
            Number of levels on each side of the book.

        Returns
        -------
        dict
            ``{"bids": [...], "asks": [...], "spread": float}``
            where each entry is ``{"price": float, "quantity": float}``.
        """
        depth = max(1, min(depth, 25))
        price = self._price

        # Base tick size scales with price magnitude.
        tick = max(round(price * 0.001, 2), 0.01)

        bids: list[dict[str, float]] = []
        asks: list[dict[str, float]] = []

        for i in range(1, depth + 1):
            # Spread widens slightly per level; quantities taper.
            jitter = random.uniform(0.8, 1.2)
            bid_price = round(price - tick * i * jitter, 2)
            ask_price = round(price + tick * i * jitter, 2)

            # Quantities: highest near the spread, tapering outward.
            base_qty = random.uniform(0.05, 0.5)
            taper = max(0.1, 1.0 - (i / (depth + 1)))
            bid_qty = round(base_qty * taper * random.uniform(0.8, 1.2), 4)
            ask_qty = round(base_qty * taper * random.uniform(0.8, 1.2), 4)

            bid_price = max(bid_price, 0.01)

            bids.append({"price": bid_price, "quantity": bid_qty})
            asks.append({"price": ask_price, "quantity": ask_qty})

        # Sort for presentation: bids highest-first, asks lowest-first.
        bids.sort(key=lambda b: b["price"], reverse=True)
        asks.sort(key=lambda a: a["price"])

        spread = round(asks[0]["price"] - bids[0]["price"], 2) if bids and asks else 0.0

        return {"bids": bids, "asks": asks, "spread": spread}

    # ── introspection ─────────────────────────────────────────────────

    @property
    def is_frozen(self) -> bool:
        return self._frozen

    @property
    def buy_volume(self) -> float:
        return self._buy_volume

    @property
    def sell_volume(self) -> float:
        return self._sell_volume

    @property
    def total_volume(self) -> float:
        return self._buy_volume + self._sell_volume

    @property
    def event_log(self) -> list[dict[str, Any]]:
        """Return a copy of the in-memory event impact log."""
        return list(self._event_log)

    def __repr__(self) -> str:
        return (
            f"<MarketEngine price=€{self._price:.2f} "
            f"buy_vol={self._buy_volume:.4f} sell_vol={self._sell_volume:.4f} "
            f"frozen={self._frozen}>"
        )
