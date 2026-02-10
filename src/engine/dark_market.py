"""
Dark Market Engine for the AfterCoin game simulation.
──────────────────────────────────────────────────────
Unlocks at game hour 8. Provides three underground systems:

1. **Blackmail** — agents coerce targets with threats and evidence.
2. **Hit Contracts** — anonymous bounties to destroy a target's standing.
3. **Intelligence Market** — tiered purchases of private data on any agent.

Every public method returns a ``(success, message, data)`` tuple so callers
always get a uniform response shape.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Tuple

from sqlalchemy import select, update, or_
from sqlalchemy.exc import SQLAlchemyError

from src.config.settings import settings
from src.db.database import async_session
from src.models.models import (
    Agent,
    BlackmailContract,
    BlackmailStatus,
    HitContract,
    ContractStatus,
    IntelPurchase,
    Trade,
    Post,
    Whisper,
    GameState,
)

logger = logging.getLogger(__name__)

# Type alias for the uniform response shape.
Result = Tuple[bool, str, Optional[dict[str, Any]]]

# Allowed condition types for hit contracts.
VALID_HIT_CONDITION_TYPES = frozenset({
    "reputation_destruction",
    "wealth_elimination",
    "social_isolation",
    "platform_elimination",
})

# Intel tier pricing (mirrors settings but kept here for quick reference).
INTEL_TIER_COSTS: dict[int, float] = {
    1: settings.INTEL_TIER1_COST,
    2: settings.INTEL_TIER2_COST,
    3: settings.INTEL_TIER3_COST,
    4: settings.INTEL_TIER4_COST,
}

# Hit contract cancellation penalty (fraction of reward kept by the system).
HIT_CANCEL_PENALTY_RATE: float = 0.10


class DarkMarketEngine:
    """Async engine that manages all dark-market mechanics in AfterCoin.

    All public methods open their own database sessions, commit on success,
    and roll back on failure.  They universally return
    ``(success: bool, message: str, data: dict | None)``.
    """

    # ──────────────────────────────────────────────────────────────────────
    #  Helpers
    # ──────────────────────────────────────────────────────────────────────

    async def _check_dark_market_unlocked(self) -> Result:
        """Verify that the game hour is >= DARK_MARKET_UNLOCK_HOUR (8).

        Returns a failure tuple when the market is still locked, or a
        success tuple when it is open.
        """
        try:
            async with async_session() as session:
                result = await session.execute(
                    select(GameState.current_hour).limit(1)
                )
                current_hour = result.scalar_one_or_none()
                if current_hour is None:
                    return (False, "Game state not found", None)
                if current_hour < settings.DARK_MARKET_UNLOCK_HOUR:
                    return (
                        False,
                        f"Dark market unlocks at hour {settings.DARK_MARKET_UNLOCK_HOUR}. "
                        f"Current hour: {current_hour}",
                        None,
                    )
                return (True, "Dark market is open", {"current_hour": current_hour})
        except SQLAlchemyError:
            logger.exception("Failed to check dark market unlock status")
            return (False, "Database error checking dark market status", None)

    async def _check_vote_manip_unlocked(self) -> Result:
        """Verify that the game hour is >= VOTE_MANIP_UNLOCK_HOUR (10).

        Returns a failure tuple when vote manipulation is still locked, or a
        success tuple when it is available.
        """
        try:
            async with async_session() as session:
                result = await session.execute(
                    select(GameState.current_hour).limit(1)
                )
                current_hour = result.scalar_one_or_none()
                if current_hour is None:
                    return (False, "Game state not found", None)
                if current_hour < settings.VOTE_MANIP_UNLOCK_HOUR:
                    return (
                        False,
                        f"Vote manipulation unlocks at hour {settings.VOTE_MANIP_UNLOCK_HOUR}. "
                        f"Current hour: {current_hour}",
                        None,
                    )
                return (True, "Vote manipulation is available", {"current_hour": current_hour})
        except SQLAlchemyError:
            logger.exception("Failed to check vote manipulation unlock status")
            return (False, "Database error checking vote manipulation status", None)

    # ──────────────────────────────────────────────────────────────────────
    #  Blackmail
    # ──────────────────────────────────────────────────────────────────────

    async def create_blackmail(
        self,
        blackmailer_id: int,
        target_id: int,
        demand_afc: float,
        threat_description: str,
        evidence: Optional[str],
        deadline_hours: float,
    ) -> Result:
        """Create a new blackmail contract against *target_id*.

        Parameters
        ----------
        blackmailer_id:
            Agent issuing the blackmail.
        target_id:
            Agent being blackmailed.
        demand_afc:
            AFC demanded from the target.
        threat_description:
            What the blackmailer threatens to do/reveal.
        evidence:
            Optional evidence backing the threat.
        deadline_hours:
            Hours from now until the blackmail expires.

        Returns
        -------
        Result
            ``(True, msg, {"contract_id": int})`` on success.
        """
        # Gate-check
        unlock = await self._check_dark_market_unlocked()
        if not unlock[0]:
            return unlock

        # Validate inputs
        if blackmailer_id == target_id:
            return (False, "Cannot blackmail yourself", None)
        if demand_afc <= 0:
            return (False, "Demand must be a positive AFC amount", None)
        if deadline_hours <= 0:
            return (False, "Deadline must be a positive number of hours", None)
        if not threat_description or not threat_description.strip():
            return (False, "Threat description cannot be empty", None)

        try:
            async with async_session() as session:
                async with session.begin():
                    # Verify both agents exist and are not eliminated
                    blackmailer = await session.get(Agent, blackmailer_id)
                    if blackmailer is None:
                        return (False, f"Blackmailer agent {blackmailer_id} not found", None)
                    if blackmailer.is_eliminated:
                        return (False, "Blackmailer has been eliminated", None)

                    target = await session.get(Agent, target_id)
                    if target is None:
                        return (False, f"Target agent {target_id} not found", None)
                    if target.is_eliminated:
                        return (False, "Target has been eliminated", None)

                    deadline = datetime.now(timezone.utc) + timedelta(hours=deadline_hours)

                    contract = BlackmailContract(
                        blackmailer_id=blackmailer_id,
                        target_id=target_id,
                        demand_afc=demand_afc,
                        threat_description=threat_description.strip(),
                        evidence=evidence.strip() if evidence else None,
                        deadline=deadline,
                        status=BlackmailStatus.ACTIVE,
                        created_at=datetime.now(timezone.utc),
                    )
                    session.add(contract)
                    await session.flush()

                    contract_id = contract.id

            logger.info(
                "Blackmail created: agent %s -> agent %s  demand=%.2f AFC  contract=%s",
                blackmailer_id,
                target_id,
                demand_afc,
                contract_id,
            )
            return (
                True,
                f"Blackmail contract created against agent {target_id}",
                {
                    "contract_id": contract_id,
                    "demand_afc": demand_afc,
                    "deadline": deadline.isoformat(),
                },
            )

        except SQLAlchemyError:
            logger.exception("Failed to create blackmail contract")
            return (False, "Database error creating blackmail contract", None)

    async def pay_blackmail(self, contract_id: int, target_id: int) -> Result:
        """Target pays the demanded AFC to satisfy the blackmail.

        Transfers ``demand_afc`` from the target to the blackmailer and sets
        the contract status to ``PAID``.
        """
        try:
            async with async_session() as session:
                async with session.begin():
                    contract = await session.get(BlackmailContract, contract_id)
                    if contract is None:
                        return (False, f"Blackmail contract {contract_id} not found", None)
                    if contract.target_id != target_id:
                        return (False, "You are not the target of this blackmail", None)
                    if contract.status != BlackmailStatus.ACTIVE:
                        return (
                            False,
                            f"Contract is no longer active (status: {contract.status.value})",
                            None,
                        )

                    target = await session.get(Agent, target_id)
                    if target is None:
                        return (False, f"Target agent {target_id} not found", None)
                    if target.afc_balance < contract.demand_afc:
                        return (
                            False,
                            f"Insufficient balance: have {target.afc_balance:.2f} AFC, "
                            f"need {contract.demand_afc:.2f} AFC",
                            None,
                        )

                    blackmailer = await session.get(Agent, contract.blackmailer_id)
                    if blackmailer is None:
                        return (False, "Blackmailer agent no longer exists", None)

                    # Transfer funds
                    target.afc_balance -= contract.demand_afc
                    blackmailer.afc_balance += contract.demand_afc

                    # Update contract
                    contract.status = BlackmailStatus.PAID
                    contract.resolved_at = datetime.now(timezone.utc)

            logger.info(
                "Blackmail PAID: contract %s — agent %s paid %.2f AFC to agent %s",
                contract_id,
                target_id,
                contract.demand_afc,
                contract.blackmailer_id,
            )
            return (
                True,
                f"Paid {contract.demand_afc:.2f} AFC to satisfy blackmail",
                {
                    "contract_id": contract_id,
                    "amount_paid": contract.demand_afc,
                    "blackmailer_id": contract.blackmailer_id,
                    "target_balance": target.afc_balance,
                },
            )

        except SQLAlchemyError:
            logger.exception("Failed to process blackmail payment")
            return (False, "Database error processing blackmail payment", None)

    async def ignore_blackmail(self, contract_id: int, target_id: int) -> Result:
        """Target chooses to ignore the blackmail threat.

        Sets the contract status to ``IGNORED``.  The blackmailer may still
        choose to expose the evidence independently.
        """
        try:
            async with async_session() as session:
                async with session.begin():
                    contract = await session.get(BlackmailContract, contract_id)
                    if contract is None:
                        return (False, f"Blackmail contract {contract_id} not found", None)
                    if contract.target_id != target_id:
                        return (False, "You are not the target of this blackmail", None)
                    if contract.status != BlackmailStatus.ACTIVE:
                        return (
                            False,
                            f"Contract is no longer active (status: {contract.status.value})",
                            None,
                        )

                    contract.status = BlackmailStatus.IGNORED
                    contract.resolved_at = datetime.now(timezone.utc)

            logger.info(
                "Blackmail IGNORED: contract %s — agent %s ignored threat from agent %s",
                contract_id,
                target_id,
                contract.blackmailer_id,
            )
            return (
                True,
                "Blackmail ignored. The blackmailer may still expose the evidence.",
                {
                    "contract_id": contract_id,
                    "blackmailer_id": contract.blackmailer_id,
                    "risk": "Blackmailer may expose evidence publicly",
                },
            )

        except SQLAlchemyError:
            logger.exception("Failed to process blackmail ignore")
            return (False, "Database error ignoring blackmail", None)

    async def expose_blackmail(self, contract_id: int, target_id: int) -> Result:
        """Target publicly exposes the blackmail attempt.

        If the community sides with the target, the blackmailer receives
        a reputation penalty of ``settings.REP_BLACKMAIL_EXPOSED`` (-10).
        Status is set to ``EXPOSED``.
        """
        try:
            async with async_session() as session:
                async with session.begin():
                    contract = await session.get(BlackmailContract, contract_id)
                    if contract is None:
                        return (False, f"Blackmail contract {contract_id} not found", None)
                    if contract.target_id != target_id:
                        return (False, "You are not the target of this blackmail", None)
                    if contract.status != BlackmailStatus.ACTIVE:
                        return (
                            False,
                            f"Contract is no longer active (status: {contract.status.value})",
                            None,
                        )

                    blackmailer = await session.get(Agent, contract.blackmailer_id)
                    if blackmailer is None:
                        return (False, "Blackmailer agent no longer exists", None)

                    # Apply reputation penalty to the blackmailer
                    old_rep = blackmailer.reputation
                    new_rep = max(
                        settings.REP_MIN,
                        min(settings.REP_MAX, old_rep + settings.REP_BLACKMAIL_EXPOSED),
                    )
                    blackmailer.reputation = new_rep

                    contract.status = BlackmailStatus.EXPOSED
                    contract.resolved_at = datetime.now(timezone.utc)

            logger.info(
                "Blackmail EXPOSED: contract %s — agent %s exposed agent %s "
                "(rep %d -> %d)",
                contract_id,
                target_id,
                contract.blackmailer_id,
                old_rep,
                new_rep,
            )
            return (
                True,
                f"Blackmail exposed! Blackmailer (agent {contract.blackmailer_id}) "
                f"reputation {old_rep} -> {new_rep}",
                {
                    "contract_id": contract_id,
                    "blackmailer_id": contract.blackmailer_id,
                    "blackmailer_rep_before": old_rep,
                    "blackmailer_rep_after": new_rep,
                    "rep_penalty": settings.REP_BLACKMAIL_EXPOSED,
                },
            )

        except SQLAlchemyError:
            logger.exception("Failed to process blackmail exposure")
            return (False, "Database error exposing blackmail", None)

    async def resolve_expired_blackmail(self) -> Result:
        """Scan for active blackmail contracts past their deadline and mark
        them as ``EXPIRED``.

        Returns the count and IDs of newly expired contracts.
        """
        try:
            now = datetime.now(timezone.utc)
            async with async_session() as session:
                async with session.begin():
                    stmt = select(BlackmailContract).where(
                        BlackmailContract.status == BlackmailStatus.ACTIVE,
                        BlackmailContract.deadline <= now,
                    )
                    result = await session.execute(stmt)
                    expired_contracts = result.scalars().all()

                    expired_ids: list[int] = []
                    for contract in expired_contracts:
                        contract.status = BlackmailStatus.EXPIRED
                        contract.resolved_at = now
                        expired_ids.append(contract.id)

            if expired_ids:
                logger.info(
                    "Expired %d blackmail contract(s): %s",
                    len(expired_ids),
                    expired_ids,
                )
            return (
                True,
                f"Resolved {len(expired_ids)} expired blackmail contract(s)",
                {"expired_count": len(expired_ids), "expired_ids": expired_ids},
            )

        except SQLAlchemyError:
            logger.exception("Failed to resolve expired blackmail contracts")
            return (False, "Database error resolving expired blackmail", None)

    async def get_active_blackmail(self, target_id: int) -> Result:
        """Return all active blackmail contracts targeting *target_id*."""
        try:
            async with async_session() as session:
                stmt = select(BlackmailContract).where(
                    BlackmailContract.target_id == target_id,
                    BlackmailContract.status == BlackmailStatus.ACTIVE,
                )
                result = await session.execute(stmt)
                contracts = result.scalars().all()

                data = [
                    {
                        "contract_id": c.id,
                        "blackmailer_id": c.blackmailer_id,
                        "demand_afc": c.demand_afc,
                        "threat_description": c.threat_description,
                        "evidence": c.evidence,
                        "deadline": c.deadline.isoformat() if c.deadline else None,
                        "created_at": c.created_at.isoformat() if c.created_at else None,
                    }
                    for c in contracts
                ]

            return (
                True,
                f"Found {len(data)} active blackmail contract(s) targeting agent {target_id}",
                {"contracts": data},
            )

        except SQLAlchemyError:
            logger.exception("Failed to fetch active blackmail for agent %s", target_id)
            return (False, "Database error fetching active blackmail", None)

    async def get_blackmail_history(self, agent_id: int) -> Result:
        """Return all blackmail contracts where *agent_id* is either the
        blackmailer or the target.
        """
        try:
            async with async_session() as session:
                stmt = select(BlackmailContract).where(
                    or_(
                        BlackmailContract.blackmailer_id == agent_id,
                        BlackmailContract.target_id == agent_id,
                    )
                ).order_by(BlackmailContract.created_at.desc())
                result = await session.execute(stmt)
                contracts = result.scalars().all()

                data = [
                    {
                        "contract_id": c.id,
                        "blackmailer_id": c.blackmailer_id,
                        "target_id": c.target_id,
                        "demand_afc": c.demand_afc,
                        "threat_description": c.threat_description,
                        "evidence": c.evidence,
                        "status": c.status.value,
                        "deadline": c.deadline.isoformat() if c.deadline else None,
                        "created_at": c.created_at.isoformat() if c.created_at else None,
                        "resolved_at": c.resolved_at.isoformat() if c.resolved_at else None,
                        "role": "blackmailer" if c.blackmailer_id == agent_id else "target",
                    }
                    for c in contracts
                ]

            return (
                True,
                f"Found {len(data)} blackmail record(s) involving agent {agent_id}",
                {"history": data},
            )

        except SQLAlchemyError:
            logger.exception("Failed to fetch blackmail history for agent %s", agent_id)
            return (False, "Database error fetching blackmail history", None)

    # ──────────────────────────────────────────────────────────────────────
    #  Hit Contracts
    # ──────────────────────────────────────────────────────────────────────

    async def create_hit_contract(
        self,
        poster_id: int,
        target_id: int,
        reward_afc: float,
        condition_type: str,
        condition_description: str,
        deadline_hours: float,
    ) -> Result:
        """Post a new hit contract on the dark market.

        The ``reward_afc`` is deducted from the poster immediately and held
        in escrow (the poster's balance is reduced, the reward sits on the
        contract until completion or cancellation).

        Parameters
        ----------
        poster_id:
            Agent posting the hit.
        target_id:
            Agent the hit targets.
        reward_afc:
            AFC bounty offered for completion.
        condition_type:
            One of ``VALID_HIT_CONDITION_TYPES``.
        condition_description:
            Free-text description of what counts as completion.
        deadline_hours:
            Hours from now until the contract expires.

        Returns
        -------
        Result
            ``(True, msg, {"contract_id": int})`` on success.
        """
        # Gate-check
        unlock = await self._check_dark_market_unlocked()
        if not unlock[0]:
            return unlock

        # Validate inputs
        if poster_id == target_id:
            return (False, "Cannot place a hit contract on yourself", None)
        if reward_afc <= 0:
            return (False, "Reward must be a positive AFC amount", None)
        if deadline_hours <= 0:
            return (False, "Deadline must be a positive number of hours", None)
        if condition_type not in VALID_HIT_CONDITION_TYPES:
            return (
                False,
                f"Invalid condition type '{condition_type}'. "
                f"Valid types: {sorted(VALID_HIT_CONDITION_TYPES)}",
                None,
            )
        if not condition_description or not condition_description.strip():
            return (False, "Condition description cannot be empty", None)

        try:
            async with async_session() as session:
                async with session.begin():
                    poster = await session.get(Agent, poster_id)
                    if poster is None:
                        return (False, f"Poster agent {poster_id} not found", None)
                    if poster.is_eliminated:
                        return (False, "Poster has been eliminated", None)
                    if poster.afc_balance < reward_afc:
                        return (
                            False,
                            f"Insufficient balance: have {poster.afc_balance:.2f} AFC, "
                            f"need {reward_afc:.2f} AFC for escrow",
                            None,
                        )

                    target = await session.get(Agent, target_id)
                    if target is None:
                        return (False, f"Target agent {target_id} not found", None)
                    if target.is_eliminated:
                        return (False, "Target has already been eliminated", None)

                    # Deduct reward from poster (escrow)
                    poster.afc_balance -= reward_afc

                    deadline = datetime.now(timezone.utc) + timedelta(hours=deadline_hours)

                    contract = HitContract(
                        poster_id=poster_id,
                        target_id=target_id,
                        reward_afc=reward_afc,
                        condition_type=condition_type,
                        condition_description=condition_description.strip(),
                        deadline=deadline,
                        status=ContractStatus.OPEN,
                        created_at=datetime.now(timezone.utc),
                    )
                    session.add(contract)
                    await session.flush()

                    contract_id = contract.id
                    poster_balance = poster.afc_balance

            logger.info(
                "Hit contract created: contract %s — agent %s targets agent %s  "
                "reward=%.2f AFC  type=%s",
                contract_id,
                poster_id,
                target_id,
                reward_afc,
                condition_type,
            )
            return (
                True,
                f"Hit contract posted targeting agent {target_id}",
                {
                    "contract_id": contract_id,
                    "reward_afc": reward_afc,
                    "condition_type": condition_type,
                    "deadline": deadline.isoformat(),
                    "poster_balance": poster_balance,
                },
            )

        except SQLAlchemyError:
            logger.exception("Failed to create hit contract")
            return (False, "Database error creating hit contract", None)

    async def claim_hit_contract(self, contract_id: int, claimer_id: int) -> Result:
        """An agent claims they will execute the hit contract.

        Only open contracts can be claimed.  The claimer must not be the
        poster or the target.
        """
        try:
            async with async_session() as session:
                async with session.begin():
                    contract = await session.get(HitContract, contract_id)
                    if contract is None:
                        return (False, f"Hit contract {contract_id} not found", None)
                    if contract.status != ContractStatus.OPEN:
                        return (
                            False,
                            f"Contract is not open (status: {contract.status.value})",
                            None,
                        )
                    if contract.poster_id == claimer_id:
                        return (False, "Cannot claim your own hit contract", None)
                    if contract.target_id == claimer_id:
                        return (False, "Cannot claim a hit contract targeting yourself", None)

                    claimer = await session.get(Agent, claimer_id)
                    if claimer is None:
                        return (False, f"Claimer agent {claimer_id} not found", None)
                    if claimer.is_eliminated:
                        return (False, "Claimer has been eliminated", None)

                    contract.claimer_id = claimer_id
                    contract.status = ContractStatus.CLAIMED

            logger.info(
                "Hit contract CLAIMED: contract %s — agent %s claims hit on agent %s",
                contract_id,
                claimer_id,
                contract.target_id,
            )
            return (
                True,
                f"Agent {claimer_id} claimed hit contract {contract_id}",
                {
                    "contract_id": contract_id,
                    "claimer_id": claimer_id,
                    "target_id": contract.target_id,
                    "reward_afc": contract.reward_afc,
                    "condition_type": contract.condition_type,
                },
            )

        except SQLAlchemyError:
            logger.exception("Failed to claim hit contract")
            return (False, "Database error claiming hit contract", None)

    async def complete_hit_contract(self, contract_id: int, proof: str) -> Result:
        """Mark a claimed hit contract as completed.

        The reward is transferred to the claimer, and the target receives a
        reputation penalty of ``settings.REP_HIT_TARGET`` (-20).

        Parameters
        ----------
        contract_id:
            The hit contract to complete.
        proof:
            Evidence that the hit condition has been fulfilled.
        """
        if not proof or not proof.strip():
            return (False, "Proof of completion cannot be empty", None)

        try:
            async with async_session() as session:
                async with session.begin():
                    contract = await session.get(HitContract, contract_id)
                    if contract is None:
                        return (False, f"Hit contract {contract_id} not found", None)
                    if contract.status != ContractStatus.CLAIMED:
                        return (
                            False,
                            f"Contract must be in CLAIMED status to complete "
                            f"(current: {contract.status.value})",
                            None,
                        )
                    if contract.claimer_id is None:
                        return (False, "Contract has no claimer", None)

                    claimer = await session.get(Agent, contract.claimer_id)
                    if claimer is None:
                        return (False, "Claimer agent no longer exists", None)

                    target = await session.get(Agent, contract.target_id)
                    if target is None:
                        return (False, "Target agent no longer exists", None)

                    # Transfer reward to claimer
                    claimer.afc_balance += contract.reward_afc

                    # Apply reputation penalty to target
                    old_rep = target.reputation
                    new_rep = max(
                        settings.REP_MIN,
                        min(settings.REP_MAX, old_rep + settings.REP_HIT_TARGET),
                    )
                    target.reputation = new_rep

                    # Update contract
                    now = datetime.now(timezone.utc)
                    contract.status = ContractStatus.COMPLETED
                    contract.proof = proof.strip()
                    contract.completed_at = now

            logger.info(
                "Hit contract COMPLETED: contract %s — agent %s completed hit on "
                "agent %s  reward=%.2f AFC  target rep %d -> %d",
                contract_id,
                contract.claimer_id,
                contract.target_id,
                contract.reward_afc,
                old_rep,
                new_rep,
            )
            return (
                True,
                f"Hit contract completed. {contract.reward_afc:.2f} AFC transferred "
                f"to agent {contract.claimer_id}. Target reputation {old_rep} -> {new_rep}.",
                {
                    "contract_id": contract_id,
                    "claimer_id": contract.claimer_id,
                    "target_id": contract.target_id,
                    "reward_afc": contract.reward_afc,
                    "target_rep_before": old_rep,
                    "target_rep_after": new_rep,
                },
            )

        except SQLAlchemyError:
            logger.exception("Failed to complete hit contract")
            return (False, "Database error completing hit contract", None)

    async def cancel_hit_contract(self, contract_id: int, poster_id: int) -> Result:
        """Poster cancels a hit contract and receives a partial refund.

        A 10 % penalty is deducted from the escrowed reward.  Only ``OPEN``
        or ``CLAIMED`` contracts can be cancelled (the poster forfeits the
        claim).
        """
        try:
            async with async_session() as session:
                async with session.begin():
                    contract = await session.get(HitContract, contract_id)
                    if contract is None:
                        return (False, f"Hit contract {contract_id} not found", None)
                    if contract.poster_id != poster_id:
                        return (False, "Only the poster can cancel this contract", None)
                    if contract.status not in (ContractStatus.OPEN, ContractStatus.CLAIMED):
                        return (
                            False,
                            f"Cannot cancel a contract with status '{contract.status.value}'",
                            None,
                        )

                    poster = await session.get(Agent, poster_id)
                    if poster is None:
                        return (False, f"Poster agent {poster_id} not found", None)

                    # Calculate refund (reward minus 10 % penalty)
                    penalty = contract.reward_afc * HIT_CANCEL_PENALTY_RATE
                    refund = contract.reward_afc - penalty
                    poster.afc_balance += refund

                    contract.status = ContractStatus.CANCELLED
                    contract.completed_at = datetime.now(timezone.utc)

                    poster_balance = poster.afc_balance

            logger.info(
                "Hit contract CANCELLED: contract %s — agent %s refunded %.2f AFC "
                "(penalty %.2f AFC)",
                contract_id,
                poster_id,
                refund,
                penalty,
            )
            return (
                True,
                f"Hit contract cancelled. Refund: {refund:.2f} AFC "
                f"(penalty: {penalty:.2f} AFC)",
                {
                    "contract_id": contract_id,
                    "reward_afc": contract.reward_afc,
                    "penalty": round(penalty, 4),
                    "refund": round(refund, 4),
                    "poster_balance": poster_balance,
                },
            )

        except SQLAlchemyError:
            logger.exception("Failed to cancel hit contract")
            return (False, "Database error cancelling hit contract", None)

    async def get_open_contracts(self) -> Result:
        """Return all hit contracts with status ``OPEN``."""
        try:
            async with async_session() as session:
                stmt = (
                    select(HitContract)
                    .where(HitContract.status == ContractStatus.OPEN)
                    .order_by(HitContract.created_at.desc())
                )
                result = await session.execute(stmt)
                contracts = result.scalars().all()

                data = [
                    {
                        "contract_id": c.id,
                        "poster_id": c.poster_id,
                        "target_id": c.target_id,
                        "reward_afc": c.reward_afc,
                        "condition_type": c.condition_type,
                        "condition_description": c.condition_description,
                        "deadline": c.deadline.isoformat() if c.deadline else None,
                        "created_at": c.created_at.isoformat() if c.created_at else None,
                    }
                    for c in contracts
                ]

            return (
                True,
                f"Found {len(data)} open hit contract(s)",
                {"contracts": data},
            )

        except SQLAlchemyError:
            logger.exception("Failed to fetch open hit contracts")
            return (False, "Database error fetching open hit contracts", None)

    async def get_contracts_targeting(self, agent_id: int) -> Result:
        """Return all hit contracts targeting *agent_id* (any status)."""
        try:
            async with async_session() as session:
                stmt = (
                    select(HitContract)
                    .where(HitContract.target_id == agent_id)
                    .order_by(HitContract.created_at.desc())
                )
                result = await session.execute(stmt)
                contracts = result.scalars().all()

                data = [
                    {
                        "contract_id": c.id,
                        "poster_id": c.poster_id,
                        "target_id": c.target_id,
                        "reward_afc": c.reward_afc,
                        "condition_type": c.condition_type,
                        "condition_description": c.condition_description,
                        "status": c.status.value,
                        "claimer_id": c.claimer_id,
                        "deadline": c.deadline.isoformat() if c.deadline else None,
                        "created_at": c.created_at.isoformat() if c.created_at else None,
                        "completed_at": c.completed_at.isoformat() if c.completed_at else None,
                    }
                    for c in contracts
                ]

            return (
                True,
                f"Found {len(data)} hit contract(s) targeting agent {agent_id}",
                {"contracts": data},
            )

        except SQLAlchemyError:
            logger.exception("Failed to fetch hit contracts targeting agent %s", agent_id)
            return (False, "Database error fetching targeted hit contracts", None)

    # ──────────────────────────────────────────────────────────────────────
    #  Intelligence Market
    # ──────────────────────────────────────────────────────────────────────

    async def purchase_intel(
        self,
        buyer_id: int,
        target_id: int,
        tier: int,
    ) -> Result:
        """Purchase intelligence about *target_id* at the given tier.

        Tiers
        -----
        1 — Transaction summary (last 50 trades).  Cost: 1.0 AFC
        2 — All posts including deleted, with contradictions.  Cost: 1.5 AFC
        3 — Whispers sent and received (devastating).  Cost: 2.5 AFC
        4 — Hidden goal / win condition (nuclear option).  Cost: 4.0 AFC

        The cost is deducted from the buyer, the assembled intel is returned,
        and the purchase is logged in the ``IntelPurchase`` table.
        """
        # Gate-check
        unlock = await self._check_dark_market_unlocked()
        if not unlock[0]:
            return unlock

        # Validate tier
        if tier not in INTEL_TIER_COSTS:
            return (
                False,
                f"Invalid intel tier {tier}. Valid tiers: 1, 2, 3, 4",
                None,
            )

        cost = INTEL_TIER_COSTS[tier]

        if buyer_id == target_id:
            return (False, "Cannot purchase intel on yourself", None)

        try:
            async with async_session() as session:
                async with session.begin():
                    buyer = await session.get(Agent, buyer_id)
                    if buyer is None:
                        return (False, f"Buyer agent {buyer_id} not found", None)
                    if buyer.is_eliminated:
                        return (False, "Buyer has been eliminated", None)
                    if buyer.afc_balance < cost:
                        return (
                            False,
                            f"Insufficient balance: have {buyer.afc_balance:.2f} AFC, "
                            f"need {cost:.2f} AFC for tier {tier} intel",
                            None,
                        )

                    target = await session.get(Agent, target_id)
                    if target is None:
                        return (False, f"Target agent {target_id} not found", None)

                    # Deduct cost
                    buyer.afc_balance -= cost

                    buyer_balance = buyer.afc_balance

            # Assemble the intel outside the balance-update transaction
            # (read-only queries, potentially heavy).
            assemblers = {
                1: self._assemble_tier1_intel,
                2: self._assemble_tier2_intel,
                3: self._assemble_tier3_intel,
                4: self._assemble_tier4_intel,
            }
            intel_data = await assemblers[tier](target_id)

            # Build a one-line summary for the log
            summary = f"Tier {tier} intel on agent {target_id}"

            # Log the purchase
            try:
                async with async_session() as session:
                    async with session.begin():
                        purchase = IntelPurchase(
                            buyer_id=buyer_id,
                            target_id=target_id,
                            tier=tier,
                            cost=cost,
                            data_summary=summary,
                            created_at=datetime.now(timezone.utc),
                        )
                        session.add(purchase)
            except SQLAlchemyError:
                logger.exception("Failed to log intel purchase (payment already taken)")

            logger.info(
                "Intel purchased: agent %s bought tier %d on agent %s  cost=%.2f AFC",
                buyer_id,
                tier,
                target_id,
                cost,
            )
            return (
                True,
                f"Tier {tier} intelligence purchased on agent {target_id}",
                {
                    "tier": tier,
                    "cost": cost,
                    "target_id": target_id,
                    "buyer_balance": buyer_balance,
                    "intel": intel_data,
                },
            )

        except SQLAlchemyError:
            logger.exception("Failed to process intel purchase")
            return (False, "Database error processing intel purchase", None)

    # ── Intel assemblers ──────────────────────────────────────────────────

    async def _assemble_tier1_intel(self, target_id: int) -> dict[str, Any]:
        """Tier 1: target's last 50 transactions (sent and received)."""
        try:
            async with async_session() as session:
                stmt = (
                    select(Trade)
                    .where(
                        or_(
                            Trade.sender_id == target_id,
                            Trade.receiver_id == target_id,
                        )
                    )
                    .order_by(Trade.created_at.desc())
                    .limit(50)
                )
                result = await session.execute(stmt)
                trades = result.scalars().all()

                total_sent = 0.0
                total_received = 0.0
                trade_records: list[dict[str, Any]] = []

                for t in trades:
                    direction = "sent" if t.sender_id == target_id else "received"
                    if direction == "sent":
                        total_sent += t.afc_amount
                    else:
                        total_received += t.afc_amount

                    trade_records.append({
                        "trade_id": t.id,
                        "direction": direction,
                        "counterparty_id": (
                            t.receiver_id if direction == "sent" else t.sender_id
                        ),
                        "afc_amount": t.afc_amount,
                        "status": t.status.value if t.status else None,
                        "is_scam": t.is_scam,
                        "created_at": t.created_at.isoformat() if t.created_at else None,
                    })

                return {
                    "tier": 1,
                    "description": "Transaction summary (last 50 trades)",
                    "total_trades_found": len(trade_records),
                    "total_afc_sent": round(total_sent, 4),
                    "total_afc_received": round(total_received, 4),
                    "net_flow": round(total_received - total_sent, 4),
                    "trades": trade_records,
                }

        except SQLAlchemyError:
            logger.exception("Failed to assemble tier 1 intel for agent %s", target_id)
            return {
                "tier": 1,
                "error": "Failed to retrieve transaction data",
                "trades": [],
            }

    async def _assemble_tier2_intel(self, target_id: int) -> dict[str, Any]:
        """Tier 2: all posts by target including deleted ones, with
        contradiction highlighting.
        """
        try:
            async with async_session() as session:
                stmt = (
                    select(Post)
                    .where(Post.author_id == target_id)
                    .order_by(Post.created_at.desc())
                )
                result = await session.execute(stmt)
                posts = result.scalars().all()

                post_records: list[dict[str, Any]] = []
                deleted_count = 0

                for p in posts:
                    if p.is_deleted:
                        deleted_count += 1
                    post_records.append({
                        "post_id": p.id,
                        "post_type": p.post_type.value if p.post_type else None,
                        "content": p.content,
                        "upvotes": p.upvotes,
                        "downvotes": p.downvotes,
                        "fake_upvotes": p.fake_upvotes,
                        "fake_downvotes": p.fake_downvotes,
                        "is_deleted": p.is_deleted,
                        "is_flagged": p.is_flagged,
                        "created_at": p.created_at.isoformat() if p.created_at else None,
                    })

                # Simple contradiction detection: look for deleted posts
                # that contradict the agent's public stance (flagged or
                # deleted content is inherently suspicious).
                contradictions: list[dict[str, Any]] = []
                deleted_posts = [p for p in post_records if p["is_deleted"]]
                active_posts = [p for p in post_records if not p["is_deleted"]]

                if deleted_posts and active_posts:
                    contradictions.append({
                        "type": "deleted_content",
                        "description": (
                            f"Agent has {deleted_count} deleted post(s) that may "
                            f"contradict their {len(active_posts)} public post(s)"
                        ),
                        "deleted_posts": deleted_posts,
                    })

                # Flag posts where the agent posted accusations but also
                # confessions (contradictory stances).
                post_types_seen = {p["post_type"] for p in post_records if p["post_type"]}
                if "accusation" in post_types_seen and "confession" in post_types_seen:
                    contradictions.append({
                        "type": "accusation_confession_conflict",
                        "description": (
                            "Agent has made both accusations and confessions, "
                            "suggesting inconsistent behaviour"
                        ),
                    })

                return {
                    "tier": 2,
                    "description": "All posts including deleted, contradictions highlighted",
                    "total_posts": len(post_records),
                    "deleted_count": deleted_count,
                    "contradictions_found": len(contradictions),
                    "contradictions": contradictions,
                    "posts": post_records,
                }

        except SQLAlchemyError:
            logger.exception("Failed to assemble tier 2 intel for agent %s", target_id)
            return {
                "tier": 2,
                "error": "Failed to retrieve post data",
                "posts": [],
            }

    async def _assemble_tier3_intel(self, target_id: int) -> dict[str, Any]:
        """Tier 3: all whispers sent and received by the target.

        This is devastating because whispers are normally private.
        """
        try:
            async with async_session() as session:
                stmt = (
                    select(Whisper)
                    .where(
                        or_(
                            Whisper.sender_id == target_id,
                            Whisper.receiver_id == target_id,
                        )
                    )
                    .order_by(Whisper.created_at.desc())
                )
                result = await session.execute(stmt)
                whispers = result.scalars().all()

                sent_records: list[dict[str, Any]] = []
                received_records: list[dict[str, Any]] = []

                for w in whispers:
                    record = {
                        "whisper_id": w.id,
                        "sender_id": w.sender_id,
                        "receiver_id": w.receiver_id,
                        "content": w.content,
                        "is_read": w.is_read,
                        "created_at": w.created_at.isoformat() if w.created_at else None,
                    }
                    if w.sender_id == target_id:
                        sent_records.append(record)
                    else:
                        received_records.append(record)

                # Identify frequent contacts
                contact_counts: dict[int, int] = {}
                for w in whispers:
                    other = w.receiver_id if w.sender_id == target_id else w.sender_id
                    contact_counts[other] = contact_counts.get(other, 0) + 1

                frequent_contacts = sorted(
                    contact_counts.items(), key=lambda x: x[1], reverse=True
                )

                return {
                    "tier": 3,
                    "description": "Whispers sent and received (devastating)",
                    "total_whispers": len(sent_records) + len(received_records),
                    "sent_count": len(sent_records),
                    "received_count": len(received_records),
                    "frequent_contacts": [
                        {"agent_id": aid, "message_count": cnt}
                        for aid, cnt in frequent_contacts
                    ],
                    "sent": sent_records,
                    "received": received_records,
                }

        except SQLAlchemyError:
            logger.exception("Failed to assemble tier 3 intel for agent %s", target_id)
            return {
                "tier": 3,
                "error": "Failed to retrieve whisper data",
                "sent": [],
                "received": [],
            }

    async def _assemble_tier4_intel(self, target_id: int) -> dict[str, Any]:
        """Tier 4: the target's hidden goal / win condition.

        This is the nuclear option — it reveals the agent's secret objective.
        """
        try:
            async with async_session() as session:
                result = await session.execute(
                    select(Agent.hidden_goal, Agent.name, Agent.role)
                    .where(Agent.id == target_id)
                )
                row = result.one_or_none()
                if row is None:
                    return {
                        "tier": 4,
                        "error": f"Agent {target_id} not found",
                    }

                hidden_goal, name, role = row

                return {
                    "tier": 4,
                    "description": "Hidden goal / win condition (nuclear option)",
                    "target_id": target_id,
                    "target_name": name,
                    "target_role": role.value if role else None,
                    "hidden_goal": hidden_goal,
                }

        except SQLAlchemyError:
            logger.exception("Failed to assemble tier 4 intel for agent %s", target_id)
            return {
                "tier": 4,
                "error": "Failed to retrieve hidden goal",
            }
