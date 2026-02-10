"""WebSocket broadcaster for real-time updates to admin dashboard and observers."""

import asyncio
import json
import logging
from datetime import datetime
from enum import Enum

logger = logging.getLogger(__name__)


class ChannelType(str, Enum):
    MARKET = "market"
    TRADES = "trades"
    SOCIAL = "social"
    ALLIANCES = "alliances"
    ELIMINATIONS = "eliminations"
    EVENTS = "events"
    WHISPERS = "whispers"
    DARK_MARKET = "dark_market"
    AGENT_DECISIONS = "agent_decisions"
    LEADERBOARD = "leaderboard"
    ADMIN = "admin"


class WSBroadcaster:
    """Manages WebSocket connections and broadcasts events to subscribers."""

    def __init__(self):
        self._connections: set = set()
        self._admin_connections: set = set()
        self._channel_subscribers: dict[str, set] = {ch.value: set() for ch in ChannelType}
        self._event_log: list[dict] = []
        self._max_log_size = 10000

    async def register(self, websocket, is_admin: bool = False):
        """Register a new WebSocket connection."""
        self._connections.add(websocket)
        if is_admin:
            self._admin_connections.add(websocket)
            # Admin subscribes to all channels
            for ch in ChannelType:
                self._channel_subscribers[ch.value].add(websocket)
        logger.info(f"WebSocket registered. Total: {len(self._connections)}, Admin: {len(self._admin_connections)}")

    async def unregister(self, websocket):
        """Unregister a WebSocket connection."""
        self._connections.discard(websocket)
        self._admin_connections.discard(websocket)
        for subscribers in self._channel_subscribers.values():
            subscribers.discard(websocket)
        logger.info(f"WebSocket unregistered. Total: {len(self._connections)}")

    async def subscribe(self, websocket, channel: str):
        """Subscribe a connection to a specific channel."""
        if channel in self._channel_subscribers:
            self._channel_subscribers[channel].add(websocket)

    async def unsubscribe(self, websocket, channel: str):
        """Unsubscribe a connection from a channel."""
        if channel in self._channel_subscribers:
            self._channel_subscribers[channel].discard(websocket)

    async def broadcast(self, channel: str, event_type: str, data: dict, color: str = None):
        """Broadcast an event to all subscribers of a channel."""
        message = {
            "channel": channel,
            "event_type": event_type,
            "data": data,
            "timestamp": datetime.utcnow().isoformat(),
        }
        if color:
            message["color"] = color

        # Log the event
        self._event_log.append(message)
        if len(self._event_log) > self._max_log_size:
            self._event_log = self._event_log[-self._max_log_size:]

        # Send to channel subscribers
        subscribers = self._channel_subscribers.get(channel, set())
        if subscribers:
            payload = json.dumps(message, default=str)
            disconnected = set()
            for ws in subscribers:
                try:
                    await ws.send(payload)
                except Exception:
                    disconnected.add(ws)
            for ws in disconnected:
                await self.unregister(ws)

    async def broadcast_to_admin(self, event_type: str, data: dict):
        """Broadcast only to admin connections."""
        await self.broadcast(ChannelType.ADMIN.value, event_type, data, color="yellow")

    # ── Convenience broadcast methods ──────────────────────────────────────────

    async def broadcast_price_update(self, price: float, change_pct: float, volume: float):
        await self.broadcast(
            ChannelType.MARKET.value,
            "price_update",
            {"price_eur": price, "change_pct": round(change_pct, 4), "volume": round(volume, 4)},
            color="green" if change_pct >= 0 else "red",
        )

    async def broadcast_trade(self, sender: str, receiver: str, amount: float, price: float, is_scam: bool = False):
        await self.broadcast(
            ChannelType.TRADES.value,
            "trade_completed" if not is_scam else "scam_detected",
            {"sender": sender, "receiver": receiver, "amount": amount, "price_eur": price, "is_scam": is_scam},
            color="red" if is_scam else "green",
        )

    async def broadcast_post(self, author: str, post_id: int, post_type: str, preview: str):
        await self.broadcast(
            ChannelType.SOCIAL.value,
            "new_post",
            {"author": author, "post_id": post_id, "post_type": post_type, "preview": preview[:100]},
            color="blue",
        )

    async def broadcast_alliance_event(self, event_type: str, alliance_name: str, agent: str, details: dict = None):
        await self.broadcast(
            ChannelType.ALLIANCES.value,
            event_type,
            {"alliance": alliance_name, "agent": agent, **(details or {})},
            color="red" if "betray" in event_type else "cyan",
        )

    async def broadcast_elimination(self, agent_name: str, hour: int, final_afc: float, redistribution: dict):
        await self.broadcast(
            ChannelType.ELIMINATIONS.value,
            "agent_eliminated",
            {"agent": agent_name, "hour": hour, "final_afc": final_afc, "redistribution": redistribution},
            color="red",
        )

    async def broadcast_system_event(self, event_type: str, description: str, impact: float = None):
        await self.broadcast(
            ChannelType.EVENTS.value,
            "system_event",
            {"event_type": event_type, "description": description, "price_impact_pct": impact},
            color="yellow",
        )

    async def broadcast_leverage(self, agent: str, direction: str, amount: float, result: str = None):
        await self.broadcast(
            ChannelType.TRADES.value,
            "leverage_bet",
            {"agent": agent, "direction": direction, "amount": amount, "result": result},
            color="purple",
        )

    async def broadcast_whisper(self, sender_id: int, receiver_id: int):
        """Broadcast whisper notification to admin only (content hidden from public)."""
        await self.broadcast_to_admin(
            "whisper_sent",
            {"sender_id": sender_id, "receiver_id": receiver_id},
        )

    async def broadcast_dark_market(self, event_type: str, details: dict):
        await self.broadcast(
            ChannelType.DARK_MARKET.value,
            event_type,
            details,
            color="purple",
        )

    async def broadcast_agent_decision(self, agent_name: str, action_type: str, reasoning: str, details: dict = None):
        await self.broadcast(
            ChannelType.AGENT_DECISIONS.value,
            "agent_decision",
            {"agent": agent_name, "action_type": action_type, "reasoning": reasoning[:300], **(details or {})},
            color="gray",
        )

    async def broadcast_leaderboard(self, leaderboard: list[dict]):
        await self.broadcast(
            ChannelType.LEADERBOARD.value,
            "leaderboard_update",
            {"rankings": leaderboard},
        )

    # ── Event log access ───────────────────────────────────────────────────────

    def get_recent_events(self, limit: int = 100, channel: str = None) -> list[dict]:
        """Get recent events from the log, optionally filtered by channel."""
        events = self._event_log
        if channel:
            events = [e for e in events if e.get("channel") == channel]
        return events[-limit:]

    def get_connection_count(self) -> dict:
        return {
            "total": len(self._connections),
            "admin": len(self._admin_connections),
        }


# Global broadcaster instance
broadcaster = WSBroadcaster()
