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
                stroke_id = parsed.get("strokeId")
                if stroke_id:
                    # Find ALL segments belonging to this strokeId and remove them
                    strokes = await r.lrange(REDIS_HISTORY, 0, -1)
                    # Mark every entry with matching strokeId or sid as deleted
                    for i, raw in enumerate(strokes):
                        try:
                            s = json.loads(raw.decode())
                            sid = s.get("sid") or s.get("strokeId")
                            if sid == stroke_id:
                                await r.lset(REDIS_HISTORY, i, b"__deleted__")
                        except Exception:
                            continue
                    await r.lrem(REDIS_HISTORY, 0, b"__deleted__")

                # Fetch the cleaned history and send it inline with redraw
                # so every client gets the authoritative state without reconnecting
                cleaned = await r.lrange(REDIS_HISTORY, 0, -1)
                history_items = []
                for raw in cleaned:
                    try:
                        history_items.append(json.loads(raw.decode()))
                    except Exception:
                        continue

                redraw_event = json.dumps({
                    "type": "redraw",
                    "history": history_items
                })
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
