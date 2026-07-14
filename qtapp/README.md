# EEG Qt Dashboard — Architecture & Performance Notes

This document explains, from the ground up, how data moves from a 2ms hardware
tick to pixels on screen in this app, every tunable constant along the way,
every bug found and fixed, and every performance measurement taken so far
(dev laptop + real Raspberry Pi 4B). It's written as a learning reference,
not just a changelog — read top to bottom to build the full mental model.

---

## 1. The base fact everything else is built on

**Hardware samples at 500Hz = one sample every 2ms.** This number is fixed —
it comes from the device, not from us. Every other timing constant in this
app is a *design choice* layered on top of that one fixed fact.

Right now the "hardware" is `reader.py` playing back a recorded file
(`EEG3840 Sine.txt`, 8600 rows × 32 channels) at true 2ms cadence using a
drift-corrected loop:
can
```python
target = start + tick * SAMPLE_INTERVAL_S
sleep_for = target - time.perf_counter()
if sleep_for > 0:
    time.sleep(sleep_for)
```

This is *not* "sleep 2ms, repeat" — that would drift (each `sleep()` call has
scheduling overhead that compounds over millions of ticks). Instead, every
tick computes where it *should* be relative to the original start time and
sleeps exactly enough to land there. Sample #500,000 is still exactly
2ms × 500,000 after start, no matter how long the app has run. This matters
for a medical device — the recorded/logged sample clock never drifts, even
if the display briefly does (see §6).

Later, when `FileReader` is swapped for a `SocketReader` reading real
hardware over TCP, this timing guarantee comes from the hardware itself
instead — nothing downstream changes.

---

## 2. Three independent clocks

The whole performance story is about keeping these three clocks decoupled
from each other on purpose.

| Clock | Period | Owner | Job |
|---|---|---|---|
| **Ingest** | 2ms (500Hz) | `reader.py`, on the worker thread | Produce one real sample |
| **Batch/chunk** | `BATCH_SIZE × 2ms` = 32ms (~31/s) | `worker.py` | Group samples before crossing to the GUI thread |
| **Redraw** | `REDRAW_MS` (currently 33ms ≈ 30fps, tuned via `QTimer`) | `main.py`, on the GUI thread | Decide when to paint |

None of these has to equal any other. That's the point.

### Why decouple ingest from redraw at all

A monitor can't show more than its own refresh rate (typically 60Hz on the
Pi's HDMI output — real number checkable with `xrandr | grep '\*'`).
Repainting on every single sample (500/sec) would mean computing full-window
redraws far faster than any screen could ever display them — pure wasted
CPU, since GPU vsync (see §7) throws away frames the display can't show
anyway. So paint rate is matched to *the eye/screen*, not *the sensor*.

### Concrete timeline (current constants: `BATCH_SIZE=16`, `REDRAW_MS=33`)

```
time(ms)   Ingest (samples)              Batch (chunk)          Redraw (paint)
0          sample 1 starts...
2,4,...30  samples trickling in @2ms
32         —                              chunk #1 ready (1-16)
33                                                                TICK → dirty? YES → draw buffer (1-16)
34-63      samples trickling in
64         —                              chunk #2 ready (17-32)
66                                                                TICK → dirty? YES → draw buffer (1-32)
67-95      samples trickling in
96         —                              chunk #3 ready (33-48)
99                                                                TICK → dirty? YES → draw buffer (1-48)
```

Two metronomes, ticking independently, that happen to be *close* in period
(32ms vs 33ms) only because the constants were chosen that way — not because
anything forces them together. If a redraw tick finds no new chunk since the
last tick, it just skips (see `dirty` flag, §5).

### Why this can't accumulate drift over a long session

A natural worry: "if these two clocks are independent, won't they drift
apart over time — e.g. 1 second of lag after 1000 seconds?"

**No**, for two separate reasons:

1. The *data* never drifts — §1's drift-corrected ingest loop guarantees
   sample N is always at true time `N × 2ms`, forever.
2. The *redraw* step never processes a backlog — every tick paints
   "whatever the buffer currently holds," then forgets it drew anything.
   It never says "now let me catch up on the chunk I owed you 3 ticks ago."
   So the worst-case lag between "a sample exists" and "it's visible on
   screen" is bounded by **one redraw interval** (~33ms), and that bound is
   the same at t=1s and at t=1000s. It does not compound.

Compounding lag *would* happen under a different design — see §5.3 for the
"direct-connect" alternative that was considered and rejected.

---

## 3. Component map

```
reader.py          — FileReader: owns the data file, plays it back at true
                      2ms cadence, calls on_sample(row) once per tick.
                      Runs on a QThread (via ProducerWorker), knows nothing
                      about Qt widgets or batching.

worker.py           — ProducerWorker(QThread): wraps FileReader.run().
                      Accumulates BATCH_SIZE=16 samples, emits one
                      batch_ready signal per chunk (~31/s, not 500/s).
                      Emits finished_playback once the file is exhausted.

sweepbuffer.py       — SweepBuffer: plain numpy, no Qt. Owns the fixed-size
                      (num_channels × WINDOW) array that represents "what's
                      currently on screen." push() writes new samples;
                      on wrap it does a full clear (no erase-ahead trail —
                      confirmed display preference). Lives on the main
                      thread, fed only by the batch_ready slot, so no locks
                      needed anywhere.

main.py              — MainWindow: builds the UI (channel picker, plot,
                      metrics, replay button), owns the QTimer that drives
                      redraws, and is the only place that touches
                      pyqtgraph curve objects.
```

### Threading rule (the one Qt rule that matters most here)

**Never touch a widget from a thread that isn't the GUI thread.** `worker.py`
runs on a background `QThread` and must never call `curve.setData(...)`
directly. The only safe way to hand data across is a `Signal` — when a
signal from a worker thread is connected to a slot on a main-thread object,
Qt automatically queues the call onto the main thread's event loop instead
of running it immediately. That queuing is what makes `batch_ready.emit(...)`
safe even though it's called from a different OS thread than the one that
eventually processes it.

---

## 4. The buffer — `SweepBuffer`

Fixed-size sweep window, one row per channel, full clear on wrap:

```python
self.data = np.full((num_channels, window), np.nan, dtype=np.float32)
self.xs = np.arange(window, dtype=np.float32) * sample_interval_ms
self.position = 0

def push_until_full(self, batch):
    """Writes rows until the window fills, then stops WITHOUT clearing.
    Returns the unwritten leftover rows if it filled mid-batch (None if
    the whole batch fit). Caller is responsible for flushing a draw of
    the completed window, then calling reset() + pushing the leftover —
    see §6.5 for why the clear can't happen inside this function."""
    for i, row in enumerate(batch):
        if self.position == self.window:
            return batch[i:]
        self.data[:, self.position] = row
        self.position += 1
    return None
```

```
Sweep window (WINDOW=2500 samples = 5s @ 2ms, before the 15s test):

 position→                                                    window edge
 |███████████████████████████░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░|
 0                          ^cursor                         2500
                       (real data)      (NaN — unwritten tail)

 at wrap: entire buffer → NaN, cursor resets to 0, drawing restarts blank.
```

`connect="finite"` in the curve's `setData()` call is what makes the NaN
tail invisible instead of drawing a stray line back to sample 0 — pyqtgraph
breaks the line at any NaN.

---

## 5. The drawing pipeline, step by step

```
FileReader.run()  (worker thread, every 2ms)
        │  on_sample(row)
        ▼
ProducerWorker._on_sample()  (worker thread)
        │  accumulates 16 rows
        │  batch_ready.emit(np.stack(rows))   ← queued cross-thread signal
        ▼
MainWindow._on_batch()  (main thread, ~31/s)
        │  self.sweep.push_until_full(batch)  ← cheap, O(16) numpy writes
        │  self.dirty = True                    (see §6.5 if it returns leftover)
        ▼
   [buffer just sits there until the next tick]
        ▼
QTimer → MainWindow._redraw()  (main thread, every REDRAW_MS)
        │  if not dirty: return               ← cheap no-op most of the time
        │  for each VISIBLE curve:
        │      curve.setData(xs, sweep.data[ch], connect="finite")
        │        │
        │        ├─ pyqtgraph downsampling ('peak' method): full window
        │        │  re-scanned every call, picks min/max per pixel-column
        │        │  so spikes survive even when 2500+ points map to ~800
        │        │  screen pixels. This re-scan is the expensive part —
        │        │  it costs the same every tick regardless of how many
        │        │  of those points are actually new.
        │        │
        │        └─ paint: CPU rasterizer or GPU (§7), draws the polyline
        ▼
   pixels on screen
```

### 5.1 Why writing to the buffer is cheap but drawing is expensive

This distinction is the key to the whole performance story:

- **Buffer write** (`push`): O(16) — copy 16 floats per channel. Trivial
  even at 500Hz.
- **Draw** (`setData` → downsample → paint): O(window × visible channels)
  — re-processes the *entire* window every single call, not just the new
  samples. There is no "append a few points" API in this rendering model.

### 5.2 Why the `dirty` flag + timer (not "draw on every chunk") — the backlog argument

An earlier version of this design was considered: connect `batch_ready`
directly to a slot that calls `setData()` immediately, no `QTimer` at all.
Rejected, for a concrete reason — **backlog under load**:

```
Direct-connect (draw on every chunk), when one draw call runs long:

 t=0ms   chunk#1 arrives → draw starts (expensive)
 t=0-50  draw is slow this time (system hiccup) — still running
 t=32ms  chunk#2 arrives. Its draw call gets QUEUED (Qt can't run two
         things on one thread at once) — waits for chunk#1's draw to finish
 t=50ms  chunk#1's draw finishes → chunk#2's draw starts immediately
 t=64ms  chunk#3 arrives → also queued behind whatever's running
```

Draws pile up faster than they finish. Worst case, the app spends its
entire CPU budget drawing *outdated* states in rapid succession, forever
behind — visible as stutter/freeze that never recovers on its own.

```
Timer + dirty flag (what this app actually does), same hiccup:

 t=0ms   chunk#1 arrives → buffer updated (cheap) → dirty=True
 t=32ms  chunk#2 arrives → buffer updated (cheap) → dirty already True
 t=33ms  TICK → dirty=True → draw buffer's CURRENT state (chunks 1+2
         merged) → dirty=False
 t=64ms  chunk#3, #4 arrive → buffer updated → dirty=True
 t=66ms  TICK → draw current state (includes everything since last paint)
```

No matter how many chunks arrive between ticks — 1 or 10 — the timer only
ever issues **one** expensive draw call per tick, always showing the latest
state. Backlog cannot build up, because nothing is ever queued waiting to
be drawn individually. This is also *why* long-session drift can't
accumulate (§2) — there's structurally no queue for lag to hide in.

### 5.3 Attribute glossary — every tunable constant

| Constant | Current value | Meaning | Tradeoff if changed |
|---|---|---|---|
| `SAMPLE_INTERVAL_MS` | 2 (500Hz) | Fixed by hardware | Not really tunable — this is the ground truth |
| `BATCH_SIZE` (`worker.py`) | 16 → 32ms/chunk, ~31 chunks/s | How much data crosses the thread boundary per signal | Smaller = lower latency but more signal-emission overhead + couples paint cadence closer to ingest if the timer is ever removed; larger = cheaper but coarser |
| `WINDOW` | 2500 (5s) *(was tested at 7500 = 15s on the Pi, §6)* | Sweep window length in samples | Bigger window = more points per draw = more downsample cost per tick |
| `REDRAW_MS` | 33 (~30fps) *(tested at 16ms — see §6.3 warning)* | How often the paint timer fires | Lower = smoother motion, more CPU; higher = choppier, less CPU. Should track real display refresh (§7.3) |
| `DEFAULT_VISIBLE` | 3 | Channels checked on first launch | Cosmetic default only, persisted via `QSettings` after that |
| `USE_OPENGL` | `True` (flipped from `False` during this investigation, §6.2) | CPU rasterizer vs GPU-accelerated painting | See §7 |

---

## 6. Bugs found and fixed (chronological log)

### 6.1 `RuntimeWarning: overflow encountered in cast`
**Cause:** `plot.setXRange(0, self.sweep.xs[-1])` passed a `numpy.float32`
into a pyqtgraph internal comparison against its large sentinel range
values — float32's smaller range triggered a spurious overflow warning.
**Fix:** cast to plain Python `float`: `plot.setXRange(0, float(self.sweep.xs[-1]))`.

### 6.2 Corrupted axis range + phantom checkbox auto-checking
**Symptom:** screenshot showed a wildly wrong axis range (X: -400000–800000),
an empty chart, and several channel checkboxes checked with no clicks made.
**Investigation:** dumped `QSettings` (confirmed real persisted values, not
a screenshot artifact), added a temporary debug print in the visibility
handler, ran fully headless — **zero prints fired**. Proved the toggling
wasn't app logic at all.
**Root cause:** my own test method — using `osascript ... activate` to bring
the window forward for a screenshot delivered stray real mouse/keyboard
input to a `PlotWidget` that still had default pan/zoom/right-click/
autorange-button interactivity enabled.
**Fix:** locked the plot down (`setMouseEnabled(False, False)`,
`setMenuEnabled(False)`, `hideButtons()`) — fixes the likely trigger *and*
matches the intended design anyway (a fixed-range operator dashboard
shouldn't let a stray drag rescale it).

### 6.3 White background readability + `useOpenGL` flip + `REDRAW_MS` experiment
Background changed to white (`plot.setBackground("w")`), channel hue
lightness darkened (140→100) to stay readable against white instead of
black. Separately, `useOpenGL` was flipped `False → True` and tested on the
Pi (§7.2) — CPU dropped from a sometimes-100% peak to a steady ~85%, no
visual glitches. `REDRAW_MS` was also tested at 16ms directly on the Pi,
uncommitted, and is flagged as a real risk once measured (§6.4) — not a
pure win the way it looked in the abstract.

### 6.4 Measured redraw cost invalidates the current `REDRAW_MS=16` experiment
Direct instrumentation on the Pi (§7.4) showed each `_redraw()` call with
all 32 channels visible costs **~27-31ms**. Against `REDRAW_MS=33` that's
tight but survives. Against the `REDRAW_MS=16` value currently sitting
uncommitted on the Pi, **every single redraw call exceeds its own tick
interval** — this recreates the exact backlog risk described in §5.2, just
via a different route (timer firing faster than the work can complete,
rather than draw-per-chunk). **Open action:** revert `REDRAW_MS` back
toward 33+ if full 32-channel viewing is a real use case, not just the
3-default-channel case.

### 6.5 Wrap-boundary sample loss — found and fixed

**Found by asking:** "is there any missed chunks in drawing state?" Traced
through the old `push()` and found a real, guaranteed-to-happen-every-wrap
case, distinct from the harmless coalescing behavior in §5.2/§2.

**The mechanism:** the old `push()` wrote rows into the buffer *and*
cleared it on wrap inside the same function call, with no paint able to
happen in between (painting only ever happens on the `QTimer` tick, never
synchronously inside a buffer write). So if a batch happened to straddle
the wrap boundary, the rows that completed the old window got overwritten
by `fill(nan)` moments later, in the same `push()` call, before any
`_redraw()` tick ever saw them:

```
position=2490 before a batch arrives (10 slots left, window=2500)

row0..row9   → fill positions 2490..2499 (completes the window)
row10        → position==2500 → fill(nan) WIPES 2490..2499 (just written!)
             → position=0, write row10 at position 0
row11..row15 → fill positions 1..5
```

Bounded to at most `BATCH_SIZE - 1` (15 samples, ~30ms) lost, exactly once
per wrap cycle. Purely a *display* artifact — `reader.py`'s own sample
clock (§1) is untouched, so nothing is lost from a recording/logging
standpoint if this data is ever timestamped for clinical review separately
from the live view.

**Fix implemented:** split "write" from "clear" into two steps, with a
forced synchronous paint in between, so the completed window always gets
shown at least once before being wiped:

```python
# sweepbuffer.py
def push_until_full(self, batch):
    for i, row in enumerate(batch):
        if self.position == self.window:
            return batch[i:]          # stop — do NOT clear yet
        self.data[:, self.position] = row
        self.position += 1
    return None
```

```python
# main.py
def _on_batch(self, batch):
    leftover = self.sweep.push_until_full(batch)
    self.dirty = True
    if leftover is not None:
        self._redraw()          # guarantee the completed window is painted
        self.sweep.reset()      # only now is it safe to wipe
        self.sweep.push_until_full(leftover)
        self.dirty = True
```

Cost: one extra synchronous `_redraw()` call per wrap cycle only (every
`WINDOW × 2ms`, i.e. every 5s at the current default) — not a per-tick
cost, negligible. Verified with a full 8600-sample local run (multiple
wraps at `WINDOW=2500`), no crash, no regression.

---

## 7. CPU / RAM / GPU limitations

### 7.1 CPU — the real bottleneck, not RAM

The 85-100% CPU spikes observed are **compute-bound, not memory-bound**.
Confirmed by explicitly checking: moving from a Pi 3B (1GB) assumption to
the real target, a Pi 4B with 8GB RAM, does **not** change the CPU picture —
downsampling 32 curves costs the same CPU cycles regardless of how much
free RAM sits idle. RAM only matters if it runs out entirely and forces
swap (§7.5) — under normal operation here it's a non-factor.

The cost scales as **visible-channel-count × window-size**, recomputed
fully on every redraw tick that has new data (§5.1). This is inherent to
how pyqtgraph's `setData()` model works, not a bug — CLAUDE.md's own
benchmark plan exists specifically to quantify this at full 32-channel load.

### 7.2 GPU — `useOpenGL`, measured

| | `useOpenGL=False` | `useOpenGL=True` |
|---|---|---|
| Peak CPU (32ch, Pi 4B) | ~100% (single thread) | ~85% |
| Visual artifacts | none observed | none observed |

GPU offloads the actual polyline rasterization from the CPU rasterizer to
the Pi's GPU. Real, measured improvement, no downside found so far. Kept on.

One side effect worth knowing: GL contexts on most platforms **vsync the
buffer swap automatically** — the actual displayed pixels only flip once
per real monitor refresh, no matter how often `setData()`/`update()` is
called. That's a second, GPU-level throttle sitting *underneath* the
`QTimer`, meaning calling `_redraw()` faster than the display's real
refresh rate is doubly wasted (CPU computes a frame the GPU then discards
at the swap).

### 7.3 Display refresh as the real ceiling

Whatever the Pi's attached monitor actually refreshes at (commonly 60Hz,
check with `xrandr | grep '\*'` on the Pi) is a hard ceiling on any benefit
from a faster `REDRAW_MS`. Painting faster than the screen can show is pure
waste. `REDRAW_MS` should be chosen close to (or a clean multiple of) that
real number — not lowered further "for smoothness" once already near it.

Note: this app's `QTimer` is **not** vsync-locked the way a browser's
`requestAnimationFrame` is — rAF gets a callback tied to the actual display
refresh signal for free (browser has direct compositor access); `QTimer`
is a generic wall-clock timer, only approximately matching the real
refresh cadence. True frame-perfect sync exists in Qt only via
`QQuickWindow`'s `frameSwapped`/`beforeRendering` signals, not available on
the plain `QWidget`/`pyqtgraph` path this app uses. Not worth chasing for a
slow-moving medical sweep trace — approximate timing is visually
indistinguishable from perfect sync here.

### 7.4 Real measurements — dev laptop vs Raspberry Pi 4B

Both measured by temporarily instrumenting `_redraw()` with
`time.perf_counter()` around the draw loop, forcing all 32 channels
visible (worst case), then removing the instrumentation afterward.

**macOS dev laptop**, `useOpenGL=True`, 32 channels, 5s window:

| min | avg | p95 | max |
|---|---|---|---|
| 2.51ms | 2.88ms | 3.26ms | 4.10ms |

**Raspberry Pi 4B (real hardware, physical screen, not remote desktop)**,
`useOpenGL=True`, 32 channels, 15s window:

| min | p50 | avg | p95 | max (steady-state) | max (incl. cold-start) |
|---|---|---|---|---|---|
| 24.7ms | 27.3ms | 27.4ms | 28.4ms | 31.2ms | 50.2ms |

The Pi is roughly **9-10× slower** than the dev laptop for the identical
draw workload — expected given the hardware gap (Cortex-A72 embedded SoC
vs a modern laptop CPU/GPU), and the reason CLAUDE.md insists on testing
everything on real hardware rather than trusting dev-machine numbers.

The first 1-2 frames after launch cost noticeably more (43-50ms) — GL
context/shader warm-up and pyqtgraph's internal downsample-buffer
allocation happening for the first time. Steady-state settles quickly
after that.

### 7.5 Theoretical failure mode: a single draw taking ~1 second

Explicitly reasoned through — not observed, but worth documenting since it
was asked directly. The draw algorithm itself is deterministic
(same 27-31ms every call, confirmed across 150+ measured frames on the Pi)
— nothing in this codebase has a path that gets 30× slower under normal
conditions. A multi-hundred-ms-to-1s stall would have to come from outside
the algorithm entirely:

- **SD card I/O stall** — Pi's default storage; cheap/worn SD cards have
  documented multi-hundred-ms to full-second latency spikes during
  internal wear-leveling. Anything doing synchronous disk I/O on the
  system (`journald`, `apt` cron, swap) can stall an unrelated thread.
- **Swap activation** — if RAM pressure ever forces paging, and the swap
  device is the same slow SD card, a page fault mid-`setData()` becomes a
  disk-latency stall instead of a memory-speed one.
- **Thermal throttle event** — check via `vcgencmd get_throttled`
  (expect `0x0`); a non-zero result means the CPU stepped down frequency,
  compounding with the above.
- **Another process saturating all 4 cores** (unattended-upgrades, wifi
  reconnect, log rotation) — starves the GUI thread of scheduling time
  entirely for a while; resumes exactly where it left off once rescheduled.

None of these are defended against in the drawing code itself — they're
system-level. Mitigation is operational: check `vcgencmd get_throttled`
and `free -h` during real benchmark runs (already in CLAUDE.md §8's plan),
not a code change.

---

## 8. Open items / not yet done

- **`REDRAW_MS=16` currently uncommitted on the Pi is unsafe at full
  32-channel load** per the §6.4/§7.4 measurement — needs a decision before
  it ships: revert toward 33, or make it conditional on visible-channel
  count.
- Full CLAUDE.md §8 benchmark plan (sustained run, `vcgencmd get_throttled`,
  `free -h` under full load, thermal behavior over a long soak) not yet
  completed — only short (~20s) targeted timing tests done so far.
- `SocketReader` (real hardware over TCP) not yet built — `FileReader` is
  still the only data source, by design (same interface, swappable later).
- Channel filtering/scaling "options" UI not yet built (CLAUDE.md §6,
  deferred until the stack/perf story is settled).
- PyInstaller packaging, `eglfs` kiosk mode — deferred to final deployment
  per CLAUDE.md §9.
