import uPlot from "uplot";
import "uplot/dist/uPlot.min.css";

const SAMPLE_INTERVAL_MS = 2; // 500Hz
const WINDOW = 2500; // fixed 5s sweep window (2500 * 2ms)
const WINDOW_MS = WINDOW * SAMPLE_INTERVAL_MS;
const DEFAULT_VISIBLE = 3; // channels checked on load

const metricsEl = document.getElementById("metrics-text")!;
const chartEl = document.getElementById("chart")!;
const pickerEl = document.getElementById("picker")!;
const replayBtn = document.getElementById("replay") as HTMLButtonElement;

let numChannels = 32;
let channelNames: string[] = [];
let channelColors: string[] = [];

// Fixed-position sweep buffers: index = position within the current 5s window.
// On wrap (position reaches WINDOW) the whole window clears and redraws from x=0.
let sweepBuffers: Float64Array[] = [];
let xs: Float64Array = new Float64Array(0);
let position = 0;
let sampleCounter = 0;

let plot: uPlot | null = null;
let finished = false;
let dirty = false;

const VISIBILITY_KEY = "eeg-dashboard-visible-channels";
let savedVisibility: Record<string, boolean> = {};

function loadVisibility(): Record<string, boolean> {
  try {
    return JSON.parse(localStorage.getItem(VISIBILITY_KEY) ?? "{}");
  } catch {
    return {};
  }
}

function saveVisibility() {
  localStorage.setItem(VISIBILITY_KEY, JSON.stringify(savedVisibility));
}

function isVisible(name: string, i: number): boolean {
  return savedVisibility[name] ?? i < DEFAULT_VISIBLE;
}

let msgCount = 0;
let sampleCount = 0;
let lastMetricsUpdate = performance.now();
let lastLatencyMs = 0;

// Distinguishable on a light background: fixed saturation/lightness, spread hue.
function distinctColor(i: number, n: number): string {
  const hue = Math.round((i * 360) / n);
  return `hsl(${hue}, 70%, 38%)`;
}

function initBuffers(n: number) {
  numChannels = n;
  sweepBuffers = Array.from({ length: n }, () => new Float64Array(WINDOW).fill(NaN));
  xs = new Float64Array(WINDOW);
  for (let i = 0; i < WINDOW; i++) xs[i] = i * SAMPLE_INTERVAL_MS;
  position = 0;
}

function initChart() {
  channelColors = channelNames.map((_, i) => distinctColor(i, channelNames.length));
  const series: uPlot.Series[] = [{ label: "Time (ms)" }];
  channelNames.forEach((name, i) => {
    series.push({
      label: name,
      stroke: channelColors[i],
      width: 1.25,
      points: { show: false },
      show: isVisible(name, i),
    });
  });
  const opts: uPlot.Options = {
    width: window.innerWidth,
    height: window.innerHeight - 80,
    series,
    scales: { x: { time: false, range: [0, WINDOW_MS] }, y: { range: [-60, 60] } },
    axes: [
      { stroke: "#333", grid: { stroke: "#ddd" }, label: "Time (ms)" },
      { stroke: "#333", grid: { stroke: "#ddd" } },
    ],
  };
  plot = new uPlot(opts, [xs, ...sweepBuffers], chartEl);
  window.addEventListener("resize", () => {
    plot?.setSize({ width: window.innerWidth, height: window.innerHeight - 80 });
  });
}

function initPicker() {
  channelNames.forEach((name, i) => {
    const label = document.createElement("label");
    label.style.color = channelColors[i];

    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.checked = isVisible(name, i);
    checkbox.addEventListener("change", () => {
      plot?.setSeries(i + 1, { show: checkbox.checked });
      savedVisibility[name] = checkbox.checked;
      saveVisibility();
    });

    label.appendChild(checkbox);
    label.appendChild(document.createTextNode(name));
    pickerEl.appendChild(label);
  });
}

function pushBatch(batch: Float32Array, n: number) {
  // batch is n * numChannels, row-major (row i = sample i, cols = channels).
  // n is usually BATCH_SIZE but the final message of a run may be a shorter
  // trailing partial batch — trust the actual payload size, not a constant.
  for (let i = 0; i < n; i++) {
    if (position === WINDOW) {
      for (const buf of sweepBuffers) buf.fill(NaN);
      position = 0;
    }
    for (let ch = 0; ch < numChannels; ch++) {
      sweepBuffers[ch][position] = batch[i * numChannels + ch];
    }
    position++;
    sampleCounter++;
  }
}

function redraw() {
  if (!plot) return;
  // Both axes are pinned (x: fixed window, y: fixed [-60,60]), so skip
  // uPlot's default full rescale + axis/gridline relayout on every call.
  // setData(data, false) skips its own internal commit (it only fires on
  // the rescale path), so an explicit redraw(false, false) is required to
  // actually repaint — rebuildPaths=false skips re-deriving the scale too,
  // going straight to the cheap path: just re-stroke the lines.
  plot.setData([xs, ...sweepBuffers], false);
  plot.redraw(false, false);
}

function updateMetrics(latencyMs: number) {
  lastLatencyMs = latencyMs;
  const now = performance.now();
  if (now - lastMetricsUpdate >= 1000) {
    const secs = (now - lastMetricsUpdate) / 1000;
    const msgsPerSec = (msgCount / secs).toFixed(1);
    const samplesPerSec = (sampleCount / secs).toFixed(1);
    metricsEl.textContent = `${msgsPerSec} msg/s | ${samplesPerSec} samples/s | latency ~${lastLatencyMs.toFixed(1)}ms`;
    msgCount = 0;
    sampleCount = 0;
    lastMetricsUpdate = now;
  }
}

replayBtn.addEventListener("click", async () => {
  replayBtn.disabled = true;
  await fetch("/api/replay", { method: "POST" });
  location.reload();
});

async function main() {
  const res = await fetch("/api/channels");
  const info = await res.json();
  channelNames = info.channels;
  savedVisibility = loadVisibility();
  initBuffers(channelNames.length);
  initChart();
  initPicker();

  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.binaryType = "arraybuffer";

  ws.onopen = () => {
    metricsEl.textContent = "connected, waiting for data...";
  };
  ws.onclose = () => {
    if (!finished) metricsEl.textContent = "disconnected";
  };
  ws.onmessage = (ev) => {
    if (typeof ev.data === "string") {
      if (ev.data === "DONE") {
        finished = true;
        metricsEl.textContent = `playback complete — ${sampleCounter} samples`;
      }
      return;
    }
    if (finished) return;
    const buf = ev.data as ArrayBuffer;
    const view = new DataView(buf);
    const serverTime = view.getFloat64(0, true);
    const latencyMs = Date.now() - serverTime * 1000;
    const samples = new Float32Array(buf, 8);
    const n = samples.length / numChannels;

    pushBatch(samples, n);
    msgCount++;
    sampleCount += n;
    dirty = true;
    updateMetrics(latencyMs);
  };

  // Redraw at whatever rate the browser can actually sustain, independent of
  // message arrival rate. If a frame takes longer than one tick, later
  // messages just get coalesced into the next redraw instead of queueing up.
  function renderLoop() {
    if (dirty) {
      redraw();
      dirty = false;
    }
    requestAnimationFrame(renderLoop);
  }
  requestAnimationFrame(renderLoop);
}

main();
