import uPlot from "uplot";
import "uplot/dist/uPlot.min.css";

const BATCH_SIZE = 16;
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
      show: i < DEFAULT_VISIBLE,
    });
  });
  const opts: uPlot.Options = {
    width: window.innerWidth,
    height: window.innerHeight - 80,
    series,
    scales: { x: { time: false, range: [0, WINDOW_MS] } },
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
    checkbox.checked = i < DEFAULT_VISIBLE;
    checkbox.addEventListener("change", () => {
      plot?.setSeries(i + 1, { show: checkbox.checked });
    });

    label.appendChild(checkbox);
    label.appendChild(document.createTextNode(name));
    pickerEl.appendChild(label);
  });
}

function pushBatch(batch: Float32Array) {
  // batch is BATCH_SIZE * numChannels, row-major (row i = sample i, cols = channels)
  for (let i = 0; i < BATCH_SIZE; i++) {
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
  plot.setData([xs, ...sweepBuffers]);
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

    pushBatch(samples);
    msgCount++;
    sampleCount += BATCH_SIZE;
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
