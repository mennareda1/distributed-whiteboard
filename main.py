import asyncio
import json
import os
import redis.asyncio as aioredis
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles

app = FastAPI()

REDIS_CHANNEL = "whiteboard"
REDIS_HISTORY = "stroke_history"
MAX_HISTORY   = 5000

local_clients: list[WebSocket] = []

async def get_redis():
    url = os.environ.get("REDIS_URL", "redis://localhost:6379")
    return await aioredis.from_url(url)

# ── Send full history to a newly joined browser ──────────────────────────────
async def send_history(websocket: WebSocket):
    r = await get_redis()
    strokes = await r.lrange(REDIS_HISTORY, 0, -1)
    await r.aclose()
    for stroke in strokes:
        await websocket.send_text(stroke.decode())

# ── Redis listener ────────────────────────────────────────────────────────────
async def redis_listener():
    while True:
        try:
            r = await get_redis()
            pubsub = r.pubsub()
            await pubsub.subscribe(REDIS_CHANNEL)
            print("Redis listener connected and subscribed.")

            while True:
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

                await asyncio.sleep(0.01)

        except Exception as e:
            print(f"Redis listener error: {e}. Retrying in 2 seconds...")
            await asyncio.sleep(2)

@app.on_event("startup")
async def startup():
    asyncio.create_task(redis_listener())

# ── WebSocket endpoint ────────────────────────────────────────────────────────
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    local_clients.append(websocket)
    print(f"Client connected. Local clients: {len(local_clients)}")

    await send_history(websocket)

    r = await get_redis()

    try:
        while True:
            data = await websocket.receive_text()
            parsed = json.loads(data)
            event_type = parsed.get("type")

            # ── Clear all ────────────────────────────────────────────────────
            if event_type == "clear":
                await r.delete(REDIS_HISTORY)
                await r.publish(REDIS_CHANNEL, data)

            # ── Per-user undo ────────────────────────────────────────────────
            elif event_type == "undo":
                # userId sent by the browser — remove their last stroke from Redis
                user_id = parsed.get("userId")
                if user_id:
                    # Scan history from end to find last stroke by this user
                    strokes = await r.lrange(REDIS_HISTORY, 0, -1)
                    target_index = None
                    for i in range(len(strokes) - 1, -1, -1):
                        try:
                            s = json.loads(strokes[i].decode())
                            # Match by userId — skip cursor/clear/undo events
                            if s.get("userId") == user_id and s.get("type") not in ("clear", "undo", "cursor"):
                                target_index = i
                                break
                        except Exception:
                            continue

                    if target_index is not None:
                        # Redis has no delete-by-index — use a tombstone trick:
                        # replace the entry with a null marker then clean the list
                        await r.lset(REDIS_HISTORY, target_index, b"__deleted__")
                        # Remove all tombstones
                        await r.lrem(REDIS_HISTORY, 0, b"__deleted__")

                    # Broadcast a full redraw signal — all clients will replay history
                    redraw_event = json.dumps({"type": "redraw"})
                    await r.publish(REDIS_CHANNEL, redraw_event)

            # ── Cursor movement (laser pointer — not saved to history) ────────
            elif event_type == "cursor":
                await r.publish(REDIS_CHANNEL, data)

            # ── Regular drawing events (strokes, shapes, text) ───────────────
            else:
                await r.rpush(REDIS_HISTORY, data)
                await r.ltrim(REDIS_HISTORY, -MAX_HISTORY, -1)
                await r.publish(REDIS_CHANNEL, data)

    except WebSocketDisconnect:
        local_clients.remove(websocket)
        await r.aclose()
        print(f"Client disconnected. Local clients: {len(local_clients)}")

app.mount("/", StaticFiles(directory="static", html=True), name="static")
