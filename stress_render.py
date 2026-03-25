#!/usr/bin/env python3
"""
Stress Test — Browser Rendering Load Test Page

Serves a self-contained HTML page that measures how long the browser takes
to render each dashboard component.  Open this on the Pi's Chromium in
kiosk mode to see real rendering numbers.

Usage:
    python stress_render.py                          # default, port 5001
    python stress_render.py --host 0.0.0.0           # accessible from LAN
    python stress_render.py --server http://10.0.0.5:5000  # point at remote server

Then open http://localhost:5001 in the Pi's browser.

The page will repeatedly:
  1. Fetch /api/track and redraw the Leaflet polyline  → measure draw time
  2. Fetch /api/panels and update all 4 Chart.js charts → measure draw time
  3. Report frame timing via requestAnimationFrame      → detect jank
"""

import argparse
import sys

try:
    from flask import Flask, Response
except ImportError:
    print("pip install flask  (should already be installed)")
    sys.exit(1)


def build_app(server_url):
    app = Flask(__name__)

    @app.route("/")
    def index():
        return Response(PAGE_HTML.replace("__SERVER__", server_url),
                        content_type="text/html")

    return app


PAGE_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Render Stress Test</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.7/dist/chart.umd.min.js"></script>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: #0a0e17; color: #0f0; font-family: 'Courier New', monospace;
         font-size: 13px; }
  #log { position: fixed; top: 0; right: 0; width: 420px; height: 100vh;
         overflow-y: auto; background: rgba(0,0,0,0.85); padding: 10px;
         z-index: 9999; border-left: 1px solid #333; }
  #log h3 { color: #0ff; margin-bottom: 6px; }
  #log .warn { color: #ff0; }
  #log .fail { color: #f44; }
  #log .ok   { color: #0f0; }
  #map { position: fixed; top: 0; left: 0; width: calc(100% - 420px); height: 50vh; }
  #charts { position: fixed; bottom: 0; left: 0; width: calc(100% - 420px); height: 50vh;
            display: grid; grid-template-columns: 1fr 1fr; grid-template-rows: 1fr 1fr;
            gap: 4px; padding: 4px; }
  #charts canvas { width: 100%; height: 100%; background: #111; }
  .summary { background: #111; padding: 8px; margin-top: 8px; border: 1px solid #333; }
  .summary td { padding: 2px 8px; }
</style>
</head>
<body>

<div id="map"></div>
<div id="charts">
  <canvas id="c0"></canvas>
  <canvas id="c1"></canvas>
  <canvas id="c2"></canvas>
  <canvas id="c3"></canvas>
</div>

<div id="log">
  <h3>RENDER STRESS TEST</h3>
  <div id="output"></div>
  <div class="summary" id="summary" style="display:none">
    <h3>SUMMARY</h3>
    <table id="summary-table"></table>
  </div>
</div>

<script>
const SERVER = "__SERVER__";
const ROUNDS = 30;          // how many fetch+render cycles
const DELAY_MS = 2000;      // delay between rounds (simulates poll interval)

const out = document.getElementById("output");
const results = { track_fetch: [], track_render: [], panel_fetch: [], panel_render: [], fps: [] };

function log(msg, cls="ok") {
  const d = document.createElement("div");
  d.className = cls;
  d.textContent = msg;
  out.appendChild(d);
  out.scrollTop = out.scrollHeight;
}

// ── Leaflet map ──
const map = L.map("map", { zoomControl: false }).setView([0, 0], 2);
L.tileLayer("https://{s}.basemaps.cartocdn.com/dark_nolabels/{z}/{x}/{y}{r}.png",
  { maxZoom: 18 }).addTo(map);
let trackLayer = L.layerGroup().addTo(map);

// ── Chart.js charts ──
const charts = [];
["Altitude", "Signal", "Temperature", "Power"].forEach((label, i) => {
  charts.push(new Chart(document.getElementById("c" + i), {
    type: "line",
    data: { labels: [], datasets: [{ label, data: [], borderColor: "#0f0",
            borderWidth: 1, pointRadius: 0 }] },
    options: { animation: false, responsive: true, maintainAspectRatio: false,
               scales: { x: { display: false }, y: { display: true,
               ticks: { color: "#666", font: { size: 9 } },
               grid: { color: "#222" } } },
               plugins: { legend: { display: false } } },
  }));
});

// ── FPS counter via rAF ──
let frameCount = 0, lastFpsTime = performance.now();
function countFrames() {
  frameCount++;
  requestAnimationFrame(countFrames);
}
requestAnimationFrame(countFrames);
setInterval(() => {
  const now = performance.now();
  const fps = frameCount / ((now - lastFpsTime) / 1000);
  results.fps.push(fps);
  frameCount = 0;
  lastFpsTime = now;
}, 1000);

// ── Benchmark helpers ──
async function fetchJSON(url) {
  const t0 = performance.now();
  const resp = await fetch(url);
  const data = await resp.json();
  return { data, ms: performance.now() - t0 };
}

async function benchTrack() {
  const { data, ms: fetchMs } = await fetchJSON(SERVER + "/api/track");
  results.track_fetch.push(fetchMs);

  const t0 = performance.now();
  trackLayer.clearLayers();
  if (data.segments) {
    data.segments.forEach(seg => {
      L.polyline(seg, { color: "#00ff88", weight: 2 }).addTo(trackLayer);
    });
  }
  if (data.current) L.circleMarker(data.current, { radius: 5, color: "#ff0" }).addTo(trackLayer);
  const renderMs = performance.now() - t0;
  results.track_render.push(renderMs);
  return { fetchMs, renderMs };
}

async function benchPanels() {
  const { data, ms: fetchMs } = await fetchJSON(SERVER + "/api/panels");
  results.panel_fetch.push(fetchMs);

  const t0 = performance.now();
  if (Array.isArray(data)) {
    data.forEach((panel, i) => {
      if (i < charts.length && panel.x) {
        charts[i].data.labels = panel.x;
        charts[i].data.datasets[0].data = panel.y;
        charts[i].update();
      }
    });
  }
  const renderMs = performance.now() - t0;
  results.panel_render.push(renderMs);
  return { fetchMs, renderMs };
}

// ── Main loop ──
async function run() {
  log(`Starting ${ROUNDS} rounds (${DELAY_MS}ms apart)...`);
  log(`Server: ${SERVER}`);
  log("");

  for (let r = 0; r < ROUNDS; r++) {
    const { fetchMs: tf, renderMs: tr } = await benchTrack();
    const { fetchMs: pf, renderMs: pr } = await benchPanels();

    const cls = (tf + tr > 1000 || pf + pr > 1000) ? "warn" : "ok";
    log(`[${r+1}/${ROUNDS}] track: ${tf.toFixed(0)}+${tr.toFixed(0)}ms  `
      + `panels: ${pf.toFixed(0)}+${pr.toFixed(0)}ms`, cls);

    await new Promise(ok => setTimeout(ok, DELAY_MS));
  }

  showSummary();
}

function pct(arr, p) {
  const s = [...arr].sort((a,b) => a-b);
  const i = Math.floor(s.length * p / 100);
  return s[Math.min(i, s.length-1)];
}
function avg(arr) { return arr.reduce((a,b)=>a+b, 0) / arr.length; }

function showSummary() {
  const el = document.getElementById("summary");
  el.style.display = "block";
  const tb = document.getElementById("summary-table");

  const rows = [
    ["Metric", "Avg", "p95", "Max"],
    ["Track fetch", avg(results.track_fetch), pct(results.track_fetch,95), Math.max(...results.track_fetch)],
    ["Track render", avg(results.track_render), pct(results.track_render,95), Math.max(...results.track_render)],
    ["Panel fetch", avg(results.panel_fetch), pct(results.panel_fetch,95), Math.max(...results.panel_fetch)],
    ["Panel render", avg(results.panel_render), pct(results.panel_render,95), Math.max(...results.panel_render)],
    ["FPS", avg(results.fps), pct(results.fps,5), Math.min(...results.fps)],
  ];

  rows.forEach((row, i) => {
    const tr = document.createElement("tr");
    row.forEach((cell, j) => {
      const td = document.createElement("td");
      td.textContent = (typeof cell === "number") ? cell.toFixed(1) + (i < 5 ? " ms" : "") : cell;
      if (i > 0 && j > 0) {
        const v = parseFloat(cell);
        if (i < 5 && v > 500) td.style.color = "#f44";
        else if (i < 5 && v > 200) td.style.color = "#ff0";
        else td.style.color = "#0f0";
      }
      tr.appendChild(td);
    });
    tb.appendChild(tr);
  });

  // Overall verdict
  const totalP95 = pct(results.track_fetch, 95) + pct(results.track_render, 95);
  const avgFps = avg(results.fps);
  log("");
  if (totalP95 < 500 && avgFps > 30) {
    log("VERDICT: Browser handles this comfortably", "ok");
  } else if (totalP95 < 1500 && avgFps > 15) {
    log("VERDICT: Acceptable, some jank likely", "warn");
  } else {
    log("VERDICT: Browser is struggling — upgrade needed", "fail");
  }
}

run();
</script>
</body>
</html>
"""


def main():
    parser = argparse.ArgumentParser(description="Serve the browser rendering stress test page")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5001)
    parser.add_argument("--server", default="http://localhost:5000",
                        help="URL of the running web_server.py instance")
    args = parser.parse_args()

    app = build_app(args.server)
    print(f"Render stress test at http://{args.host}:{args.port}")
    print(f"Targeting dashboard server at {args.server}")
    print("Open the URL in the Pi's Chromium to measure real rendering performance.\n")
    app.run(host=args.host, port=args.port)


if __name__ == "__main__":
    main()
