"""
Reputation engine for the AfterCoin game simulation.

Tracks, modifies, and queries agent reputation scores. Every mutation is
persisted to the ``reputation_logs`` table and the owning ``Agent`` row is
updated atomically inside the same database transaction.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import List, Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.config.settings import settings
from src.db.database import async_session
from src.models.models import Agent, ReputationLog

logger = logging.getLogger(__name__)


class ReputationEngine:
    """Async engine that manages agent reputation within the AfterCoin game.

    All public methods that touch the database open their own session and
    commit before returning, so callers do not need to manage transactions.
    """

    # ── Badge thresholds (highest match wins) ─────────────────────────────

    _BADGE_TIERS: list[tuple[int, str]] = [
        (80, "VERIFIED"),
        (30, "NORMAL"),
        (10, "UNTRUSTED"),
    ]
    _BADGE_FLOOR: str = "PARIAH"

    # ── Core helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _clamp(value: int) -> int:
        """Clamp *value* to the ``[REP_MIN, REP_MAX]`` range."""
        return max(settings.REP_MIN, min(settings.REP_MAX, value))

    # ── Primary mutation ──────────────────────────────────────────────────

    async def modify_reputation(
        self,
        agent_id: int,
        change: int,
        reason: str,
        session: Optional[AsyncSession] = None,
    ) -> int:
        """Apply a reputation *change* to *agent_id*.

        Steps:
        1. Fetch the agent's current reputation.
        2. Compute the new score (clamped to 0-100).
        3. Write a ``ReputationLog`` entry.
        4. Update the ``Agent.reputation`` column.
        5. Commit the transaction.

        Parameters
        ----------
        agent_id:
            Primary key of the target agent.
        change:
            Signed integer delta (positive = gain, negative = loss).
        reason:
            Human-readable explanation stored in the log.
        session:
            Optional externally-managed session.  When provided the caller is
            responsible for committing / rolling back.

        Returns
        -------
        int
            The new (clamped) reputation score.
        """
        own_session = session is None
        sess: AsyncSession = session or async_session()

        try:
            # Fetch current reputation
            result = await sess.execute(
                select(Agent.reputation).where(Agent.id == agent_id)
            )
            row = result.scalar_one_or_none()
            if row is None:
                raise ValueError(f"Agent {agent_id} not found")

            current_reputation: int = row
            new_reputation = self._clamp(current_reputation + change)

            # Persist the log entry
            log_entry = ReputationLog(
                agent_id=agent_id,
                change=change,
                reason=reason,
                new_value=new_reputation,
                created_at=datetime.utcnow(),
            )
            sess.add(log_entry)

            # Update the agent row
            await sess.execute(
                update(Agent)
                .where(Agent.id == agent_id)
                .values(reputation=new_reputation)
            )

            if own_session:
                await sess.commit()

            logger.info(
                "Agent %s reputation: %d -> %d (%+d, %s)",
                agent_id,
                current_reputation,
                new_reputation,
                change,
                reason,
            )

            return new_reputation

        except Exception:
            if own_session:
                await sess.rollback()
            raise

        finally:
            if own_session:
                await sess.close()

    # ── Queries ───────────────────────────────────────────────────────────

    async def get_reputation(self, agent_id: int) -> int:
        """Return the current reputation score for *agent_id*.

        Raises ``ValueError`` if the agent does not exist.
        """
        async with async_session() as sess:
            result = await sess.execute(
                select(Agent.reputation).where(Agent.id == agent_id)
            )
            value = result.scalar_one_or_none()
            if value is None:
                raise ValueError(f"Agent {agent_id} not found")
            return value

    @staticmethod
    def get_reputation_badge(reputation: int) -> str:
        """Map a numeric reputation to its badge string.

        Thresholds (checked from highest to lowest):
            >= 80  -> ``"VERIFIED"``
            >= 30  -> ``"NORMAL"``
            >= 10  -> ``"UNTRUSTED"``
            <  10  -> ``"PARIAH"``
        """
        for threshold, badge in ReputationEngine._BADGE_TIERS:
            if reputation >= threshold:
                return badge
        return ReputationEngine._BADGE_FLOOR

    async def get_reputation_history(
        self,
        agent_id: int,
        limit: int = 20,
    ) -> List[ReputationLog]:
        """Return the most recent reputation changes for *agent_id*.

        Results are ordered newest-first, capped at *limit* rows.
        """
        async with async_session() as sess:
            result = await sess.execute(
                select(ReputationLog)
                .where(ReputationLog.agent_id == agent_id)
                .order_by(ReputationLog.created_at.desc())
                .limit(limit)
            )
            return list(result.scalars().all())

    # ── Convenience mutators (one per game event) ─────────────────────────

    async def apply_trade_success(self, agent_id: int) -> int:
        """Reward a successfully completed trade."""
        return await self.modify_reputation(
            agent_id, settings.REP_TRADE_SUCCESS, "trade_success"
        )

    async def apply_upvote(self, agent_id: int) -> int:
        """Reward receiving an upvote on a post."""
        return await self.modify_reputation(
            agent_id, settings.REP_UPVOTE, "upvote_received"
        )

    async def apply_downvote(self, agent_id: int) -> int:
        """Penalise receiving a downvote on a post."""
        return await self.modify_reputation(
            agent_id, settings.REP_DOWNVOTE, "downvote_received"
        )

    async def apply_tip_given(self, agent_id: int) -> int:
        """Reward the agent for giving a tip."""
        return await self.modify_reputation(
            agent_id, settings.REP_TIP, "tip_given"
        )

    async def apply_bounty_complete(self, agent_id: int) -> int:
        """Reward completing a bounty."""
        return await self.modify_reputation(
            agent_id, settings.REP_BOUNTY_COMPLETE, "bounty_complete"
        )

    async def apply_alliance_loyalty(self, agent_id: int) -> int:
        """Reward loyalty to an alliance."""
        return await self.modify_reputation(
            agent_id, settings.REP_ALLIANCE_LOYAL, "alliance_loyalty"
        )

    async def apply_scam_confirmed(self, agent_id: int) -> int:
        """Penalise a confirmed scam."""
        return await self.modify_reputation(
            agent_id, settings.REP_SCAM_CONFIRMED, "scam_confirmed"
        )

    async def apply_betrayal(self, agent_id: int) -> int:
        """Penalise betraying an alliance."""
        return await self.modify_reputation(
            agent_id, settings.REP_BETRAYAL, "betrayal"
        )

    async def apply_blackmail_exposed(self, agent_id: int) -> int:
        """Penalise being exposed as a blackmailer."""
        return await self.modify_reputation(
            agent_id, settings.REP_BLACKMAIL_EXPOSED, "blackmail_exposed"
        )

    async def apply_fake_news(self, agent_id: int) -> int:
        """Penalise spreading fake news."""
        return await self.modify_reputation(
            agent_id, settings.REP_FAKE_NEWS, "fake_news"
        )

    async def apply_hit_target(self, agent_id: int) -> int:
        """Penalise being the executor of a hit contract."""
        return await self.modify_reputation(
            agent_id, settings.REP_HIT_TARGET, "hit_target"
        )
