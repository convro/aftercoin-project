"""Whisper system - anonymous messaging between agents."""

from datetime import datetime

from sqlalchemy import select, func, and_

from src.db.database import async_session
from src.models.models import Agent, Whisper
from src.config.settings import settings


class WhisperEngine:
    """Handles anonymous messaging between agents. Costs 0.2 AFC per whisper."""

    async def send_whisper(
        self, sender_id: int, receiver_id: int, content: str
    ) -> tuple[bool, str, dict | None]:
        """Send an anonymous whisper to another agent. Max 200 chars, costs 0.2 AFC."""
        if len(content) > 200:
            return False, "Whisper content exceeds 200 character limit.", None

        if sender_id == receiver_id:
            return False, "Cannot whisper to yourself.", None

        async with async_session() as session:
            sender = await session.get(Agent, sender_id)
            receiver = await session.get(Agent, receiver_id)

            if not sender or sender.is_eliminated:
                return False, "Sender not found or eliminated.", None
            if not receiver or receiver.is_eliminated:
                return False, "Receiver not found or eliminated.", None

            cost = settings.WHISPER_COST
            if sender.afc_balance < cost:
                return False, f"Insufficient balance. Need {cost} AFC.", None

            sender.afc_balance -= cost
            sender.afc_balance = round(sender.afc_balance, 4)

            whisper = Whisper(
                sender_id=sender_id,
                receiver_id=receiver_id,
                content=content,
                cost=cost,
                created_at=datetime.utcnow(),
            )
            session.add(whisper)
            await session.commit()
            await session.refresh(whisper)

            return True, "Whisper sent anonymously.", {
                "whisper_id": whisper.id,
                "cost": cost,
                "remaining_balance": sender.afc_balance,
            }

    async def get_received_whispers(
        self, agent_id: int, unread_only: bool = False, limit: int = 50
    ) -> list[dict]:
        """Get whispers received by an agent. Sender identity hidden."""
        async with async_session() as session:
            query = select(Whisper).where(Whisper.receiver_id == agent_id)
            if unread_only:
                query = query.where(Whisper.is_read == False)  # noqa: E712
            query = query.order_by(Whisper.created_at.desc()).limit(limit)
            result = await session.execute(query)
            whispers = result.scalars().all()

            return [
                {
                    "whisper_id": w.id,
                    "content": w.content,
                    "is_read": w.is_read,
                    "received_at": w.created_at.isoformat(),
                }
                for w in whispers
            ]

    async def mark_read(self, whisper_id: int, agent_id: int) -> bool:
        """Mark a whisper as read."""
        async with async_session() as session:
            whisper = await session.get(Whisper, whisper_id)
            if not whisper or whisper.receiver_id != agent_id:
                return False
            whisper.is_read = True
            await session.commit()
            return True

    async def mark_all_read(self, agent_id: int) -> int:
        """Mark all unread whispers as read for an agent. Returns count marked."""
        async with async_session() as session:
            query = select(Whisper).where(
                and_(Whisper.receiver_id == agent_id, Whisper.is_read == False)  # noqa: E712
            )
            result = await session.execute(query)
            whispers = result.scalars().all()
            count = 0
            for w in whispers:
                w.is_read = True
                count += 1
            await session.commit()
            return count

    async def get_sent_whispers(self, agent_id: int, limit: int = 50) -> list[dict]:
        """Get whispers sent by an agent (admin/intel view only)."""
        async with async_session() as session:
            query = (
                select(Whisper)
                .where(Whisper.sender_id == agent_id)
                .order_by(Whisper.created_at.desc())
                .limit(limit)
            )
            result = await session.execute(query)
            whispers = result.scalars().all()

            return [
                {
                    "whisper_id": w.id,
                    "receiver_id": w.receiver_id,
                    "content": w.content,
                    "cost": w.cost,
                    "sent_at": w.created_at.isoformat(),
                }
                for w in whispers
            ]

    async def get_all_whispers_for_agent(self, agent_id: int) -> list[dict]:
        """Get ALL whispers involving an agent (sent and received). For intel tier 3."""
        async with async_session() as session:
            query = (
                select(Whisper)
                .where(
                    (Whisper.sender_id == agent_id) | (Whisper.receiver_id == agent_id)
                )
                .order_by(Whisper.created_at.desc())
            )
            result = await session.execute(query)
            whispers = result.scalars().all()

            return [
                {
                    "whisper_id": w.id,
                    "direction": "sent" if w.sender_id == agent_id else "received",
                    "other_agent_id": w.receiver_id
                    if w.sender_id == agent_id
                    else w.sender_id,
                    "content": w.content,
                    "timestamp": w.created_at.isoformat(),
                }
                for w in whispers
            ]

    async def get_unread_count(self, agent_id: int) -> int:
        """Get count of unread whispers for an agent."""
        async with async_session() as session:
            query = select(func.count(Whisper.id)).where(
                and_(Whisper.receiver_id == agent_id, Whisper.is_read == False)  # noqa: E712
            )
            result = await session.execute(query)
            return result.scalar() or 0

    async def get_whisper_stats(self, agent_id: int) -> dict:
        """Get whisper statistics for an agent."""
        async with async_session() as session:
            sent_q = select(func.count(Whisper.id)).where(
                Whisper.sender_id == agent_id
            )
            recv_q = select(func.count(Whisper.id)).where(
                Whisper.receiver_id == agent_id
            )
            cost_q = select(func.sum(Whisper.cost)).where(
                Whisper.sender_id == agent_id
            )

            sent = (await session.execute(sent_q)).scalar() or 0
            received = (await session.execute(recv_q)).scalar() or 0
            total_cost = (await session.execute(cost_q)).scalar() or 0.0

            return {
                "sent": sent,
                "received": received,
                "total_cost": round(total_cost, 4),
                "unread": await self.get_unread_count(agent_id),
            }
