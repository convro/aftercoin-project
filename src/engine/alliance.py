"""
Alliance engine for the AfterCoin game simulation.

Manages alliance lifecycles (create / join / leave / dissolve), shared
treasuries with staking bonuses, and the betrayal (defection) mechanic
including countdown timers, emergency eject votes, and proportional
treasury redistribution.

Every public method returns a ``(success, message, data)`` tuple so that
callers always receive a uniform contract.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import select, update, and_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.config.settings import settings
from src.db.database import async_session
from src.models.models import Agent, Alliance, AllianceMember, AllianceStatus

logger = logging.getLogger(__name__)

# Type alias for the standard return contract.
Result = tuple[bool, str, Optional[dict]]


class AllianceEngine:
    """Async engine that manages alliances within the AfterCoin game.

    All public methods open their own database session and commit before
    returning, so callers do not need to manage transactions.
    """

    # ──────────────────────────────────────────────────────────────────────
    #  Alliance Lifecycle
    # ──────────────────────────────────────────────────────────────────────

    async def create_alliance(self, founder_id: int, name: str) -> Result:
        """Create a new alliance and add the founder as its first member.

        Parameters
        ----------
        founder_id:
            Primary key of the agent who founds the alliance.
        name:
            Display name for the alliance (must be non-empty).

        Returns
        -------
        Result
            On success the ``data`` dict contains the new ``alliance_id``.
        """
        if not name or not name.strip():
            return False, "Alliance name cannot be empty", None

        async with async_session() as session:
            async with session.begin():
                # Validate founder exists and is not eliminated.
                agent = await self._get_active_agent(session, founder_id)
                if agent is None:
                    return False, f"Agent {founder_id} not found or eliminated", None

                # Create the alliance.
                alliance = Alliance(
                    name=name.strip(),
                    founder_id=founder_id,
                    treasury=0.0,
                    status=AllianceStatus.ACTIVE,
                    created_at=datetime.utcnow(),
                )
                session.add(alliance)
                await session.flush()  # populate alliance.id

                # Add the founder as the first member.
                member = AllianceMember(
                    alliance_id=alliance.id,
                    agent_id=founder_id,
                    contribution=0.0,
                    share_percent=0.0,
                    is_active=True,
                    joined_at=datetime.utcnow(),
                )
                session.add(member)

            logger.info(
                "Alliance '%s' (id=%d) created by agent %d",
                name,
                alliance.id,
                founder_id,
            )
            return True, "Alliance created", {"alliance_id": alliance.id}

    async def join_alliance(self, alliance_id: int, agent_id: int) -> Result:
        """Add an agent as a new member of an existing *ACTIVE* alliance.

        Parameters
        ----------
        alliance_id:
            The alliance to join.
        agent_id:
            The agent who wants to join.
        """
        async with async_session() as session:
            async with session.begin():
                alliance = await self._get_active_alliance(session, alliance_id)
                if alliance is None:
                    return False, f"Alliance {alliance_id} not found or not active", None

                agent = await self._get_active_agent(session, agent_id)
                if agent is None:
                    return False, f"Agent {agent_id} not found or eliminated", None

                # Check the agent is not already an active member.
                existing = await self._get_active_membership(
                    session, alliance_id, agent_id
                )
                if existing is not None:
                    return False, "Agent is already a member of this alliance", None

                member = AllianceMember(
                    alliance_id=alliance_id,
                    agent_id=agent_id,
                    contribution=0.0,
                    share_percent=0.0,
                    is_active=True,
                    joined_at=datetime.utcnow(),
                )
                session.add(member)

            logger.info("Agent %d joined alliance %d", agent_id, alliance_id)
            return True, "Joined alliance", {"alliance_id": alliance_id, "agent_id": agent_id}

    async def leave_alliance(self, alliance_id: int, agent_id: int) -> Result:
        """Remove an agent from an alliance, returning their contribution share.

        The agent cannot leave while they have a pending defection.
        """
        async with async_session() as session:
            async with session.begin():
                alliance = await self._get_active_alliance(session, alliance_id)
                if alliance is None:
                    return False, f"Alliance {alliance_id} not found or not active", None

                member = await self._get_active_membership(
                    session, alliance_id, agent_id
                )
                if member is None:
                    return False, "Agent is not an active member of this alliance", None

                if member.defection_initiated_at is not None:
                    return False, "Cannot leave while a defection is pending", None

                # Compute the agent's share of the treasury.
                share_amount = 0.0
                if alliance.treasury > 0 and member.share_percent > 0:
                    share_amount = round(
                        alliance.treasury * (member.share_percent / 100.0), 6
                    )
                    alliance.treasury = round(alliance.treasury - share_amount, 6)
                    # Credit the agent.
                    agent = await session.get(Agent, agent_id)
                    if agent is not None:
                        agent.afc_balance = round(agent.afc_balance + share_amount, 6)

                # Deactivate the membership.
                member.is_active = False
                member.left_at = datetime.utcnow()

                # Recalculate remaining members' shares.
                await self._recalculate_shares(session, alliance_id)

            logger.info(
                "Agent %d left alliance %d (returned %.4f AFC)",
                agent_id,
                alliance_id,
                share_amount,
            )
            return True, "Left alliance", {
                "alliance_id": alliance_id,
                "agent_id": agent_id,
                "returned_amount": share_amount,
            }

    async def dissolve_alliance(self, alliance_id: int, founder_id: int) -> Result:
        """Founder dissolves the alliance; every member receives their
        proportional share of the treasury.
        """
        async with async_session() as session:
            async with session.begin():
                alliance = await self._get_active_alliance(session, alliance_id)
                if alliance is None:
                    return False, f"Alliance {alliance_id} not found or not active", None

                if alliance.founder_id != founder_id:
                    return False, "Only the founder can dissolve the alliance", None

                # Distribute treasury proportionally.
                distributions: dict[int, float] = {}
                members = await self._get_all_active_members(session, alliance_id)

                if alliance.treasury > 0 and members:
                    for m in members:
                        payout = round(
                            alliance.treasury * (m.share_percent / 100.0), 6
                        )
                        if payout > 0:
                            agent = await session.get(Agent, m.agent_id)
                            if agent is not None:
                                agent.afc_balance = round(
                                    agent.afc_balance + payout, 6
                                )
                                distributions[m.agent_id] = payout

                # Mark all members inactive.
                for m in members:
                    m.is_active = False
                    m.left_at = datetime.utcnow()

                # Update alliance status.
                alliance.status = AllianceStatus.DISSOLVED
                alliance.dissolved_at = datetime.utcnow()
                alliance.treasury = 0.0

            logger.info(
                "Alliance %d dissolved by founder %d — distributed to %d members",
                alliance_id,
                founder_id,
                len(distributions),
            )
            return True, "Alliance dissolved", {
                "alliance_id": alliance_id,
                "distributions": distributions,
            }

    async def get_alliance(self, alliance_id: int) -> Result:
        """Return full details for an alliance including its members."""
        async with async_session() as session:
            stmt = (
                select(Alliance)
                .options(selectinload(Alliance.members))
                .where(Alliance.id == alliance_id)
            )
            result = await session.execute(stmt)
            alliance = result.scalar_one_or_none()
            if alliance is None:
                return False, f"Alliance {alliance_id} not found", None

            members_data = [
                {
                    "agent_id": m.agent_id,
                    "contribution": m.contribution,
                    "share_percent": m.share_percent,
                    "is_active": m.is_active,
                    "defection_initiated_at": (
                        m.defection_initiated_at.isoformat()
                        if m.defection_initiated_at
                        else None
                    ),
                    "joined_at": m.joined_at.isoformat() if m.joined_at else None,
                    "left_at": m.left_at.isoformat() if m.left_at else None,
                }
                for m in alliance.members
            ]

            return True, "Alliance found", {
                "id": alliance.id,
                "name": alliance.name,
                "founder_id": alliance.founder_id,
                "treasury": alliance.treasury,
                "status": alliance.status.value,
                "last_bonus_at": (
                    alliance.last_bonus_at.isoformat()
                    if alliance.last_bonus_at
                    else None
                ),
                "created_at": (
                    alliance.created_at.isoformat()
                    if alliance.created_at
                    else None
                ),
                "dissolved_at": (
                    alliance.dissolved_at.isoformat()
                    if alliance.dissolved_at
                    else None
                ),
                "betrayed_by": alliance.betrayed_by,
                "members": members_data,
            }

    async def get_agent_alliances(self, agent_id: int) -> Result:
        """Return every *active* alliance the agent currently belongs to."""
        async with async_session() as session:
            stmt = (
                select(Alliance)
                .join(AllianceMember, AllianceMember.alliance_id == Alliance.id)
                .where(
                    and_(
                        AllianceMember.agent_id == agent_id,
                        AllianceMember.is_active.is_(True),
                        Alliance.status == AllianceStatus.ACTIVE,
                    )
                )
            )
            result = await session.execute(stmt)
            alliances = result.scalars().all()

            data = [
                {
                    "id": a.id,
                    "name": a.name,
                    "founder_id": a.founder_id,
                    "treasury": a.treasury,
                    "status": a.status.value,
                    "created_at": a.created_at.isoformat() if a.created_at else None,
                }
                for a in alliances
            ]
            return True, f"Found {len(data)} active alliance(s)", {"alliances": data}

    async def list_alliances(self) -> Result:
        """Return all alliances with ACTIVE status."""
        async with async_session() as session:
            stmt = select(Alliance).where(Alliance.status == AllianceStatus.ACTIVE)
            result = await session.execute(stmt)
            alliances = result.scalars().all()

            data = [
                {
                    "id": a.id,
                    "name": a.name,
                    "founder_id": a.founder_id,
                    "treasury": a.treasury,
                    "status": a.status.value,
                    "created_at": a.created_at.isoformat() if a.created_at else None,
                }
                for a in alliances
            ]
            return True, f"{len(data)} active alliance(s)", {"alliances": data}

    # ──────────────────────────────────────────────────────────────────────
    #  Treasury
    # ──────────────────────────────────────────────────────────────────────

    async def contribute_to_treasury(
        self,
        alliance_id: int,
        agent_id: int,
        amount: float,
    ) -> Result:
        """Agent contributes AFC from their wallet to the alliance treasury.

        A flat fee of ``settings.ALLIANCE_FEE`` (0.02 AFC) is deducted from
        the contribution so the treasury receives ``amount - fee``.  The
        agent's wallet is debited the full ``amount``.  The member's
        ``contribution`` tally and everyone's ``share_percent`` are updated.
        """
        fee = settings.ALLIANCE_FEE

        if amount <= 0:
            return False, "Contribution amount must be positive", None
        if amount <= fee:
            return (
                False,
                f"Amount must exceed the {fee} AFC fee",
                None,
            )

        async with async_session() as session:
            async with session.begin():
                alliance = await self._get_active_alliance(session, alliance_id)
                if alliance is None:
                    return False, f"Alliance {alliance_id} not found or not active", None

                member = await self._get_active_membership(
                    session, alliance_id, agent_id
                )
                if member is None:
                    return False, "Agent is not an active member of this alliance", None

                agent = await session.get(Agent, agent_id)
                if agent is None or agent.is_eliminated:
                    return False, f"Agent {agent_id} not found or eliminated", None

                if agent.afc_balance < amount:
                    return (
                        False,
                        f"Insufficient funds: need {amount} AFC, have {agent.afc_balance} AFC",
                        None,
                    )

                net_contribution = round(amount - fee, 6)

                # Debit the full amount from the agent.
                agent.afc_balance = round(agent.afc_balance - amount, 6)

                # Credit the net amount to the treasury.
                alliance.treasury = round(alliance.treasury + net_contribution, 6)

                # Update member contribution tally.
                member.contribution = round(member.contribution + net_contribution, 6)

                # Recalculate every member's share_percent.
                await self._recalculate_shares(session, alliance_id)

            logger.info(
                "Agent %d contributed %.4f AFC to alliance %d "
                "(fee=%.2f, net=%.4f, treasury=%.4f)",
                agent_id,
                amount,
                alliance_id,
                fee,
                net_contribution,
                alliance.treasury,
            )
            return True, "Contribution accepted", {
                "alliance_id": alliance_id,
                "agent_id": agent_id,
                "gross_amount": amount,
                "fee": fee,
                "net_amount": net_contribution,
                "new_treasury": alliance.treasury,
            }

    async def apply_staking_bonus(self, alliance_id: int) -> Result:
        """Add a staking bonus to the alliance treasury.

        The bonus equals ``settings.ALLIANCE_STAKING_BONUS`` (5 %) of the
        current treasury balance.  It should be called every 6 hours.

        A cooldown check ensures that at least 6 hours have elapsed since
        ``last_bonus_at`` (or since alliance creation if no bonus has been
        applied yet).
        """
        bonus_rate = settings.ALLIANCE_STAKING_BONUS
        cooldown = timedelta(hours=6)

        async with async_session() as session:
            async with session.begin():
                alliance = await self._get_active_alliance(session, alliance_id)
                if alliance is None:
                    return False, f"Alliance {alliance_id} not found or not active", None

                if alliance.treasury <= 0:
                    return False, "Treasury is empty; no bonus to apply", None

                # Cooldown check.
                reference = alliance.last_bonus_at or alliance.created_at
                now = datetime.utcnow()
                if reference and (now - reference) < cooldown:
                    remaining = cooldown - (now - reference)
                    return (
                        False,
                        f"Bonus on cooldown; {remaining} remaining",
                        None,
                    )

                bonus = round(alliance.treasury * bonus_rate, 6)
                alliance.treasury = round(alliance.treasury + bonus, 6)
                alliance.last_bonus_at = now

            logger.info(
                "Staking bonus applied to alliance %d: +%.4f AFC (treasury=%.4f)",
                alliance_id,
                bonus,
                alliance.treasury,
            )
            return True, "Staking bonus applied", {
                "alliance_id": alliance_id,
                "bonus": bonus,
                "new_treasury": alliance.treasury,
            }

    async def get_treasury_balance(self, alliance_id: int) -> Result:
        """Return the treasury balance and each member's share breakdown."""
        async with async_session() as session:
            alliance = await session.get(Alliance, alliance_id)
            if alliance is None:
                return False, f"Alliance {alliance_id} not found", None

            members = await self._get_all_active_members(session, alliance_id)
            shares = [
                {
                    "agent_id": m.agent_id,
                    "contribution": m.contribution,
                    "share_percent": m.share_percent,
                    "share_amount": round(
                        alliance.treasury * (m.share_percent / 100.0), 6
                    ),
                }
                for m in members
            ]

            return True, "Treasury balance retrieved", {
                "alliance_id": alliance_id,
                "treasury": alliance.treasury,
                "status": alliance.status.value,
                "member_shares": shares,
            }

    # ──────────────────────────────────────────────────────────────────────
    #  Betrayal (Defection)
    # ──────────────────────────────────────────────────────────────────────

    async def initiate_defection(self, alliance_id: int, agent_id: int) -> Result:
        """Start the 2-hour defection countdown for *agent_id*.

        Other alliance members are *not* notified -- they must actively
        check to discover the pending betrayal.
        """
        async with async_session() as session:
            async with session.begin():
                alliance = await self._get_active_alliance(session, alliance_id)
                if alliance is None:
                    return False, f"Alliance {alliance_id} not found or not active", None

                member = await self._get_active_membership(
                    session, alliance_id, agent_id
                )
                if member is None:
                    return False, "Agent is not an active member of this alliance", None

                if member.defection_initiated_at is not None:
                    return False, "Defection already initiated", None

                now = datetime.utcnow()
                member.defection_initiated_at = now
                execute_at = now + timedelta(hours=settings.BETRAYAL_COUNTDOWN_HOURS)

            logger.info(
                "Agent %d initiated defection from alliance %d (executes at %s)",
                agent_id,
                alliance_id,
                execute_at.isoformat(),
            )
            return True, "Defection initiated", {
                "alliance_id": alliance_id,
                "agent_id": agent_id,
                "initiated_at": now.isoformat(),
                "execute_at": execute_at.isoformat(),
            }

    async def cancel_defection(self, alliance_id: int, agent_id: int) -> Result:
        """Cancel a pending defection before the countdown completes."""
        async with async_session() as session:
            async with session.begin():
                member = await self._get_active_membership(
                    session, alliance_id, agent_id
                )
                if member is None:
                    return False, "Agent is not an active member of this alliance", None

                if member.defection_initiated_at is None:
                    return False, "No pending defection to cancel", None

                member.defection_initiated_at = None

            logger.info(
                "Agent %d cancelled defection from alliance %d",
                agent_id,
                alliance_id,
            )
            return True, "Defection cancelled", {
                "alliance_id": alliance_id,
                "agent_id": agent_id,
            }

    async def execute_defection(self, alliance_id: int, agent_id: int) -> Result:
        """Execute a defection after the 2-hour countdown has elapsed.

        Mechanics
        ---------
        * The betrayer steals ``settings.BETRAYAL_STEAL_PERCENT`` (80 %) of
          the treasury.
        * The remaining 20 % is split proportionally among other active
          members based on their ``share_percent``.
        * The alliance is marked ``BETRAYED`` and dissolved.
        * All members are set to inactive.
        * The betrayer's reputation is reduced by
          ``settings.REP_BETRAYAL`` (-25).
        """
        steal_pct = settings.BETRAYAL_STEAL_PERCENT
        countdown = timedelta(hours=settings.BETRAYAL_COUNTDOWN_HOURS)

        async with async_session() as session:
            async with session.begin():
                alliance = await self._get_active_alliance(session, alliance_id)
                if alliance is None:
                    return False, f"Alliance {alliance_id} not found or not active", None

                member = await self._get_active_membership(
                    session, alliance_id, agent_id
                )
                if member is None:
                    return False, "Agent is not an active member of this alliance", None

                if member.defection_initiated_at is None:
                    return False, "No pending defection for this agent", None

                now = datetime.utcnow()
                if (now - member.defection_initiated_at) < countdown:
                    remaining = countdown - (now - member.defection_initiated_at)
                    return (
                        False,
                        f"Countdown not yet complete; {remaining} remaining",
                        None,
                    )

                treasury = alliance.treasury
                stolen = round(treasury * steal_pct, 6)
                remainder = round(treasury - stolen, 6)

                # Credit the betrayer.
                betrayer = await session.get(Agent, agent_id)
                if betrayer is None:
                    return False, f"Agent {agent_id} not found", None
                betrayer.afc_balance = round(betrayer.afc_balance + stolen, 6)

                # Distribute the remainder among other active members.
                all_members = await self._get_all_active_members(session, alliance_id)
                others = [m for m in all_members if m.agent_id != agent_id]
                distributions: dict[int, float] = {}

                if others and remainder > 0:
                    # Build share totals for the non-betrayer members.
                    total_other_share = sum(m.share_percent for m in others)
                    for m in others:
                        if total_other_share > 0:
                            payout = round(
                                remainder * (m.share_percent / total_other_share), 6
                            )
                        else:
                            # Equal split if everyone has 0 share.
                            payout = round(remainder / len(others), 6)
                        if payout > 0:
                            other_agent = await session.get(Agent, m.agent_id)
                            if other_agent is not None:
                                other_agent.afc_balance = round(
                                    other_agent.afc_balance + payout, 6
                                )
                                distributions[m.agent_id] = payout

                # Mark all members inactive.
                for m in all_members:
                    m.is_active = False
                    m.left_at = now
                    m.defection_initiated_at = None

                # Update alliance.
                alliance.status = AllianceStatus.BETRAYED
                alliance.dissolved_at = now
                alliance.betrayed_by = agent_id
                alliance.treasury = 0.0

                # Reputation penalty for betrayer.
                betrayer.reputation = max(
                    settings.REP_MIN,
                    betrayer.reputation + settings.REP_BETRAYAL,
                )

            logger.warning(
                "BETRAYAL: Agent %d stole %.4f AFC from alliance %d "
                "(remainder %.4f split among %d members)",
                agent_id,
                stolen,
                alliance_id,
                remainder,
                len(distributions),
            )
            return True, "Defection executed", {
                "alliance_id": alliance_id,
                "betrayer_id": agent_id,
                "stolen_amount": stolen,
                "remainder": remainder,
                "distributions": distributions,
                "reputation_penalty": settings.REP_BETRAYAL,
            }

    async def check_pending_defections(self) -> Result:
        """Scan all active alliances for defections whose 2-hour countdown
        has expired and execute them automatically.
        """
        countdown = timedelta(hours=settings.BETRAYAL_COUNTDOWN_HOURS)
        now = datetime.utcnow()
        executed: list[dict] = []

        async with async_session() as session:
            stmt = (
                select(AllianceMember)
                .join(Alliance, Alliance.id == AllianceMember.alliance_id)
                .where(
                    and_(
                        AllianceMember.is_active.is_(True),
                        AllianceMember.defection_initiated_at.isnot(None),
                        Alliance.status == AllianceStatus.ACTIVE,
                    )
                )
            )
            result = await session.execute(stmt)
            pending = result.scalars().all()

        for member in pending:
            if (now - member.defection_initiated_at) >= countdown:
                success, msg, data = await self.execute_defection(
                    member.alliance_id, member.agent_id
                )
                executed.append({
                    "alliance_id": member.alliance_id,
                    "agent_id": member.agent_id,
                    "success": success,
                    "message": msg,
                    "data": data,
                })

        logger.info(
            "check_pending_defections: %d pending, %d executed",
            len(pending),
            len(executed),
        )
        return True, f"Checked defections: {len(executed)} executed", {
            "executed": executed,
        }

    async def emergency_vote_eject(
        self,
        alliance_id: int,
        target_id: int,
        voter_ids: list[int],
    ) -> Result:
        """Members vote to eject *target_id* before a defection completes.

        A simple majority of **all** active members (excluding the target)
        is required.  On success the target is removed and receives only
        their original ``contribution`` back (not their proportional share).
        """
        async with async_session() as session:
            async with session.begin():
                alliance = await self._get_active_alliance(session, alliance_id)
                if alliance is None:
                    return False, f"Alliance {alliance_id} not found or not active", None

                target_member = await self._get_active_membership(
                    session, alliance_id, target_id
                )
                if target_member is None:
                    return False, "Target is not an active member of this alliance", None

                all_members = await self._get_all_active_members(session, alliance_id)

                # Eligible voters are active members excluding the target.
                eligible = [m for m in all_members if m.agent_id != target_id]
                eligible_ids = {m.agent_id for m in eligible}

                # Filter voter_ids to only those who are eligible.
                valid_votes = [v for v in voter_ids if v in eligible_ids]
                majority_needed = (len(eligible) // 2) + 1

                if len(valid_votes) < majority_needed:
                    return (
                        False,
                        f"Insufficient votes: {len(valid_votes)}/{majority_needed} needed",
                        {
                            "valid_votes": len(valid_votes),
                            "majority_needed": majority_needed,
                            "eligible_voters": len(eligible),
                        },
                    )

                # Eject target -- return only their original contribution.
                refund = round(target_member.contribution, 6)
                if refund > alliance.treasury:
                    refund = round(alliance.treasury, 6)

                if refund > 0:
                    alliance.treasury = round(alliance.treasury - refund, 6)
                    target_agent = await session.get(Agent, target_id)
                    if target_agent is not None:
                        target_agent.afc_balance = round(
                            target_agent.afc_balance + refund, 6
                        )

                target_member.is_active = False
                target_member.left_at = datetime.utcnow()
                target_member.defection_initiated_at = None

                # Recalculate remaining shares.
                await self._recalculate_shares(session, alliance_id)

            logger.info(
                "Agent %d ejected from alliance %d by vote "
                "(%d/%d votes, refund=%.4f AFC)",
                target_id,
                alliance_id,
                len(valid_votes),
                len(eligible),
                refund,
            )
            return True, "Target ejected by majority vote", {
                "alliance_id": alliance_id,
                "ejected_agent_id": target_id,
                "refund": refund,
                "votes_for": len(valid_votes),
                "votes_needed": majority_needed,
                "eligible_voters": len(eligible),
            }

    # ──────────────────────────────────────────────────────────────────────
    #  Internal helpers
    # ──────────────────────────────────────────────────────────────────────

    @staticmethod
    async def _get_active_agent(
        session: AsyncSession, agent_id: int
    ) -> Optional[Agent]:
        """Return the ``Agent`` if it exists and is not eliminated."""
        agent = await session.get(Agent, agent_id)
        if agent is None or agent.is_eliminated:
            return None
        return agent

    @staticmethod
    async def _get_active_alliance(
        session: AsyncSession, alliance_id: int
    ) -> Optional[Alliance]:
        """Return the ``Alliance`` if it exists and has ACTIVE status."""
        alliance = await session.get(Alliance, alliance_id)
        if alliance is None or alliance.status != AllianceStatus.ACTIVE:
            return None
        return alliance

    @staticmethod
    async def _get_active_membership(
        session: AsyncSession,
        alliance_id: int,
        agent_id: int,
    ) -> Optional[AllianceMember]:
        """Return the active ``AllianceMember`` row for the pair, or None."""
        stmt = select(AllianceMember).where(
            and_(
                AllianceMember.alliance_id == alliance_id,
                AllianceMember.agent_id == agent_id,
                AllianceMember.is_active.is_(True),
            )
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    @staticmethod
    async def _get_all_active_members(
        session: AsyncSession, alliance_id: int
    ) -> list[AllianceMember]:
        """Return every active member row for the given alliance."""
        stmt = select(AllianceMember).where(
            and_(
                AllianceMember.alliance_id == alliance_id,
                AllianceMember.is_active.is_(True),
            )
        )
        result = await session.execute(stmt)
        return list(result.scalars().all())

    @staticmethod
    async def _recalculate_shares(
        session: AsyncSession, alliance_id: int
    ) -> None:
        """Recompute ``share_percent`` for all active members of an alliance.

        Each member's share is their contribution divided by total
        contributions, expressed as a percentage.  If total contributions
        are zero every active member receives an equal share.
        """
        stmt = select(AllianceMember).where(
            and_(
                AllianceMember.alliance_id == alliance_id,
                AllianceMember.is_active.is_(True),
            )
        )
        result = await session.execute(stmt)
        members = list(result.scalars().all())

        if not members:
            return

        total_contribution = sum(m.contribution for m in members)

        if total_contribution > 0:
            for m in members:
                m.share_percent = round(
                    (m.contribution / total_contribution) * 100.0, 4
                )
        else:
            equal_share = round(100.0 / len(members), 4)
            for m in members:
                m.share_percent = equal_share
