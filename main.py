import asyncio
import json
import os
import redis.asyncio as aioredis
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles

app = FastAPI()

# The key names we'll use inside Redis
REDIS_CHANNEL = "whiteboard"      # pub/sub channel name
REDIS_HISTORY = "stroke_history"  # list name for saving strokes
MAX_HISTORY   = 5000              # max strokes to remember

# Browsers connected to THIS specific server instance
local_clients: list[WebSocket] = []

# ── Redis connection ─────────────────────────────────────────────────────────
async def get_redis():
    url = os.environ.get("REDIS_URL", "redis://localhost:6379")
    return await aioredis.from_url(url)

# ── Send full history to a newly joined browser ──────────────────────────────
async def send_history(websocket: WebSocket):
    """When a browser first connects, replay all saved strokes so they
    see the current board state instead of a blank canvas."""
    r = await get_redis()
    strokes = await r.lrange(REDIS_HISTORY, 0, -1)
    await r.aclose()
    for stroke in strokes:
        await websocket.send_text(stroke.decode())

# ── Background task: listen to Redis and forward to local browsers ────────────
async def redis_listener():
    """Polls Redis for new pub/sub messages every 10ms and forwards
    them to all browsers connected to this server instance.
    Retries automatically if Redis connection drops."""
    while True:                          # outer loop — reconnect if Redis drops
        try:
            r = await get_redis()
            pubsub = r.pubsub()
            await pubsub.subscribe(REDIS_CHANNEL)
            print("Redis listener connected and subscribed.")

            while True:                  # inner loop — keep reading messages
                message = await pubsub.get_message(
                    ignore_subscribe_messages=True,
                    timeout=1.0
                )

                if message is not None:
                    data = message["data"].decode()

                    dead_clients = []
                    for client in local_clients:
                        try:
                            await client.send_text(data)
                        except Exception:
                            dead_clients.append(client)

                    for dead in dead_clients:
                        local_clients.remove(dead)

                await asyncio.sleep(0.01)  # 10ms pause — fast but not CPU-hungry

        except Exception as e:
            print(f"Redis listener error: {e}. Retrying in 2 seconds...")
            await asyncio.sleep(2)

# ── Start the background listener when the server boots ──────────────────────
@app.on_event("startup")
async def startup():
    asyncio.create_task(redis_listener())

# ── WebSocket endpoint ────────────────────────────────────────────────────────
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    local_clients.append(websocket)
    print(f"Client connected. Local clients: {len(local_clients)}")

    # Replay full drawing history so new browser sees the current board
    await send_history(websocket)

    r = await get_redis()

    try:
        while True:
            data = await websocket.receive_text()
            parsed = json.loads(data)

            if parsed.get("type") == "clear":
                # Wipe Redis history so new joiners also get a blank board
                await r.delete(REDIS_HISTORY)
            else:
                # Save stroke to history
                await r.rpush(REDIS_HISTORY, data)
                await r.ltrim(REDIS_HISTORY, -MAX_HISTORY, -1)

            # Always publish — redis_listener delivers it to all browsers
            await r.publish(REDIS_CHANNEL, data)

    except WebSocketDisconnect:
        local_clients.remove(websocket)
        await r.aclose()
        print(f"Client disconnected. Local clients: {len(local_clients)}")

app.mount("/", StaticFiles(directory="static", html=True), name="static")