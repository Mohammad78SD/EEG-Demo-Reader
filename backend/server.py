import asyncio
import struct
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles

from reader import FileReader
from ringbuffer import RingBuffer

BATCH_SIZE = 32  # samples/message, ~64ms at 500Hz
BROADCAST_INTERVAL_S = BATCH_SIZE * 0.002
RING_CAPACITY = 5000  # ~10s window

reader = FileReader()
ring = RingBuffer(RING_CAPACITY, reader.num_channels)
clients: set[WebSocket] = set()
stop_event = threading.Event()
finished_event = threading.Event()
done_sent = False
producer_thread: threading.Thread | None = None
replay_lock = threading.Lock()
producer_lock = threading.Lock()
producer_started = False


def _producer_loop():
    reader.run(on_sample=ring.push, stop_event=stop_event)
    finished_event.set()


def _start_producer():
    global producer_thread
    producer_thread = threading.Thread(target=_producer_loop, daemon=True)
    producer_thread.start()


def _ensure_producer_started():
    """Starts playback on first client connection, not at server boot —
    so every session sees row 0 through the end at a true fixed cadence,
    instead of joining a clock that's already been running since boot."""
    global producer_started
    with producer_lock:
        if not producer_started:
            producer_started = True
            _start_producer()


async def _broadcaster_loop():
    global done_sent
    start = time.monotonic()
    tick = 0
    while True:
        tick += 1
        target = start + tick * BROADCAST_INTERVAL_S
        sleep_for = target - time.monotonic()
        if sleep_for > 0:
            await asyncio.sleep(sleep_for)
        if not clients:
            continue
        rows = ring.latest(BATCH_SIZE)
        dead = []
        if rows.shape[0] == BATCH_SIZE:
            frame = struct.pack("<d", time.time()) + rows.tobytes()
            for ws in clients:
                try:
                    await ws.send_bytes(frame)
                except Exception:
                    dead.append(ws)
        if finished_event.is_set() and not done_sent:
            done_sent = True
            for ws in clients:
                try:
                    await ws.send_text("DONE")
                except Exception:
                    dead.append(ws)
        for ws in dead:
            clients.discard(ws)


@asynccontextmanager
async def lifespan(app: FastAPI):
    broadcaster_task = asyncio.create_task(_broadcaster_loop())
    yield
    stop_event.set()
    broadcaster_task.cancel()
    producer_thread.join(timeout=1)


app = FastAPI(lifespan=lifespan)


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    await websocket.accept()
    clients.add(websocket)
    _ensure_producer_started()
    if finished_event.is_set():
        await websocket.send_text("DONE")
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        clients.discard(websocket)


@app.get("/api/channels")
async def get_channels():
    return {"channels": reader.channels, "batch_size": BATCH_SIZE}


@app.post("/api/replay")
async def replay():
    global done_sent, producer_started
    with replay_lock:
        stop_event.set()
        if producer_thread is not None:
            await asyncio.to_thread(producer_thread.join, 2)
        stop_event.clear()
        finished_event.clear()
        done_sent = False
        ring.reset()
        producer_started = False
    return {"status": "replaying"}


dist_dir = Path(__file__).parent.parent / "frontend" / "dist"
if dist_dir.exists():
    app.mount("/", StaticFiles(directory=dist_dir, html=True), name="static")
