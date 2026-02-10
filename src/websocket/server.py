"""WebSocket server handler for real-time connections."""

import asyncio
import json
import logging

import websockets

from src.config.settings import settings
from src.websocket.broadcaster import broadcaster

logger = logging.getLogger(__name__)


async def ws_handler(websocket, path=None):
    """Handle a WebSocket connection."""
    is_admin = False
    try:
        # Wait for initial auth message
        try:
            raw = await asyncio.wait_for(websocket.recv(), timeout=10)
            msg = json.loads(raw)
            if msg.get("type") == "auth":
                if msg.get("secret") == settings.ADMIN_SECRET:
                    is_admin = True
                    await websocket.send(json.dumps({"type": "auth", "status": "admin"}))
                else:
                    await websocket.send(json.dumps({"type": "auth", "status": "observer"}))
            else:
                await websocket.send(json.dumps({"type": "auth", "status": "observer"}))
        except asyncio.TimeoutError:
            await websocket.send(json.dumps({"type": "auth", "status": "observer"}))

        await broadcaster.register(websocket, is_admin=is_admin)
        logger.info(f"WS connected: admin={is_admin}")

        # Keep connection alive and handle subscription messages
        async for message in websocket:
            try:
                data = json.loads(message)
                msg_type = data.get("type", "")

                if msg_type == "subscribe":
                    channel = data.get("channel", "")
                    await broadcaster.subscribe(websocket, channel)

                elif msg_type == "unsubscribe":
                    channel = data.get("channel", "")
                    await broadcaster.unsubscribe(websocket, channel)

                elif msg_type == "ping":
                    await websocket.send(json.dumps({"type": "pong"}))

            except json.JSONDecodeError:
                pass

    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        await broadcaster.unregister(websocket)
        logger.info("WS disconnected")


async def start_ws_server():
    """Start the WebSocket server."""
    server = await websockets.serve(
        ws_handler,
        "0.0.0.0",
        settings.WS_PORT,
        ping_interval=30,
        ping_timeout=10,
    )
    logger.info(f"WebSocket server started on ws://0.0.0.0:{settings.WS_PORT}")
    return server
