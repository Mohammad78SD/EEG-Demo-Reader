import asyncio
import struct
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

import numpy as np
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles

from reader import FileReader

BATCH_SIZE = 32  # samples/message, ~64ms at 500Hz
QUEUE_MAXSIZE = 5000  # only fills if nobody is connected to drain it

reader = FileReader()
sample_queue: asyncio.Queue = None  # created in lifespan, once the loop exists
event_loop: asyncio.AbstractEventLoop = None
clients: set[WebSocket] = set()
stop_event = threading.Event()
finished_event = threading.Event()
done_sent = False
producer_thread: threading.Thread | None = None
replay_lock = threading.Lock()
producer_lock = threading.Lock()
producer_started = False


def _safe_put(item):
    try:
        sample_queue.put_nowait(item)
    except asyncio.QueueFull:
        pass  # nobody's listening; drop rather than block the producer


def _on_sample(row: np.ndarray):
    """Runs on the producer thread; hands the sample straight to the asyncio
    queue instead of a ring buffer the broadcaster polls on its own clock.
    A queue guarantees every sample is delivered exactly once, in order, no
    matter how the producer's 2ms loop and the broadcaster's send loop drift
    against each other — the old "grab whatever's most recent" approach
    silently dropped or duplicated samples whenever those two clocks drifted,
    which is unacceptable for a device meant to show every EEG sample."""
    event_loop.call_soon_threadsafe(_safe_put, row.copy())


_END_OF_STREAM = object()  # sentinel; queue order guarantees it lands after the last real sample


def _producer_loop():
    reader.run(on_sample=_on_sample, stop_event=stop_event)
    finished_event.set()
    event_loop.call_soon_threadsafe(_safe_put, _END_OF_STREAM)


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
    """Drains the queue in strict FIFO order and ships exactly BATCH_SIZE
    samples per frame — no polling, no clock to drift, so nothing is ever
    skipped or resent. Runs continuously (even with zero clients) so the
    queue never backs up while producing with nobody watching.

    End-of-stream is signaled by _END_OF_STREAM being enqueued *after* the
    last real sample (same producer thread, same queue -> guaranteed order).
    Checking a separately-set threading.Event here instead would race: the
    flag can flip before that last sample has actually been dequeued, which
    made an earlier version of this loop hang waiting for data that would
    never come."""
    global done_sent
    batch: list[np.ndarray] = []
    while True:
        item = await sample_queue.get()
        if item is _END_OF_STREAM:
            if batch:
                frame = struct.pack("<d", time.time()) + np.stack(batch).tobytes()
                batch = []
                dead = []
                for ws in clients:
                    try:
                        await ws.send_bytes(frame)
                    except Exception:
                        dead.append(ws)
                for ws in dead:
                    clients.discard(ws)
            if not done_sent:
                done_sent = True
                dead = []
                for ws in clients:
                    try:
                        await ws.send_text("DONE")
                    except Exception:
                        dead.append(ws)
                for ws in dead:
                    clients.discard(ws)
            continue

        batch.append(item)
        if len(batch) < BATCH_SIZE:
            continue
        frame = struct.pack("<d", time.time()) + np.stack(batch).tobytes()
        batch = []
        dead = []
        for ws in clients:
            try:
                await ws.send_bytes(frame)
            except Exception:
                dead.append(ws)
        for ws in dead:
            clients.discard(ws)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global sample_queue, event_loop
    event_loop = asyncio.get_running_loop()
    sample_queue = asyncio.Queue(maxsize=QUEUE_MAXSIZE)
    broadcaster_task = asyncio.create_task(_broadcaster_loop())
    yield
    stop_event.set()
    broadcaster_task.cancel()
    if producer_thread is not None:
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
        while not sample_queue.empty():
            sample_queue.get_nowait()
        producer_started = False
    return {"status": "replaying"}


dist_dir = Path(__file__).parent.parent / "frontend" / "dist"
if dist_dir.exists():
    app.mount("/", StaticFiles(directory=dist_dir, html=True), name="static")
