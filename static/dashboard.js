/* ═══════════════════════════════════════════════
   Mission Control — Dashboard JS
   Leaflet map + Chart.js panels + SocketIO live push
   ═══════════════════════════════════════════════ */

(function () {
  "use strict";

  // Global error handler — logs to a visible element if present
  window.addEventListener("error", function (e) {
    console.error("Dashboard error:", e.message, e.filename, e.lineno);
    var el = document.getElementById("db-count");
    if (el) el.textContent = "JS ERROR: " + e.message + " L" + e.lineno;
  });

  // ── Constants ──
  const TRACK_REFRESH_MS = 5_000;    // ground-track poll interval
  const PANEL_REFRESH_MS = 10_000;   // side-panel poll interval
  const STATUS_REFRESH_MS = 1_000;   // HUD status poll interval
  const MET_TICK_MS = 1_000;         // local MET clock tick
  const PANEL_COLORS = ["#00ccff", "#00ffcc", "#cc44ff", "#ffcc00", "#ff4400"];

  // ── State ──
  let startTime = Date.now();
  let blinkOn = true;

  // ──────────────────────────────────────────
  // Leaflet map
  // ──────────────────────────────────────────

  const map = L.map("map", {
    center: [0, 0],
    zoom: 2,
    minZoom: 1,
    maxZoom: 6,
    worldCopyJump: true,
    zoomControl: false,
  });

  // Dark tile layer (CartoDB dark_nolabels — free, no key needed)
  L.tileLayer(
    "https://{s}.basemaps.cartocdn.com/dark_nolabels/{z}/{x}/{y}{r}.png",
    {
      attribution: '&copy; <a href="https://carto.com/">CARTO</a>',
      subdomains: "abcd",
      maxZoom: 19,
    }
  ).addTo(map);

  // Ground-track polyline group
  let trackLines = L.layerGroup().addTo(map);

  // Satellite marker (red circle)
  const satIcon = L.divIcon({
    className: "sat-icon",
    html: '<svg width="18" height="18"><circle cx="9" cy="9" r="7" fill="#ff3333" stroke="white" stroke-width="1.5"/></svg>',
    iconSize: [18, 18],
    iconAnchor: [9, 9],
  });
  const satMarker = L.marker([0, 0], { icon: satIcon, zIndex: 1000 }).addTo(map);

  // Satellite label
  satMarker.bindTooltip("HUCSAT", {
    permanent: true,
    direction: "right",
    offset: [10, -4],
    className: "sat-tooltip",
  });

  // Start marker (blue diamond)
  const startIcon = L.divIcon({
    className: "sat-icon",
    html: '<svg width="12" height="12"><rect x="1" y="1" width="10" height="10" rx="2" fill="#00ccff" stroke="white" stroke-width="0.8" transform="rotate(45,6,6)"/></svg>',
    iconSize: [12, 12],
    iconAnchor: [6, 6],
  });
  const startMarker = L.marker([0, 0], { icon: startIcon, zIndex: 999 }).addTo(map);

  // Harvard marker (crimson)
  const harvardIcon = L.divIcon({
    className: "sat-icon",
    html: '<svg width="14" height="14"><circle cx="7" cy="7" r="5" fill="crimson" stroke="white" stroke-width="0.8"/></svg>',
    iconSize: [14, 14],
    iconAnchor: [7, 7],
  });
  L.marker([42.3736, -71.1097], { icon: harvardIcon })
    .addTo(map)
    .bindTooltip("Harvard", {
      permanent: true,
      direction: "right",
      offset: [8, -4],
      className: "harvard-tooltip",
    });

  // Inject tooltip styles
  const tooltipStyle = document.createElement("style");
  tooltipStyle.textContent = `
    .sat-tooltip {
      background: transparent; border: none; box-shadow: none;
      color: #ff6666; font-family: monospace; font-size: 12px; font-weight: bold;
    }
    .harvard-tooltip {
      background: transparent; border: none; box-shadow: none;
      color: crimson; font-family: monospace; font-size: 11px; font-weight: bold;
    }
  `;
  document.head.appendChild(tooltipStyle);


  // ──────────────────────────────────────────
  // Chart.js panels
  // ──────────────────────────────────────────

  const charts = [];
  try {
    for (let i = 0; i < 5; i++) {
      const el = document.getElementById("chart-" + i);
      if (!el) { charts.push(null); continue; }
      const ctx = el.getContext("2d");
      charts.push(new Chart(ctx, {
        type: "line",
        data: {
          labels: [],
          datasets: [{
            data: [],
            borderColor: PANEL_COLORS[i],
            borderWidth: 1.2,
            pointRadius: 0,
            tension: 0.1,
            fill: false,
          }],
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          animation: false,
          plugins: { legend: { display: false } },
          scales: {
            x: {
              display: true,
              title: { display: true, text: "Time (min)", color: "#667788", font: { size: 9, family: "monospace" } },
              ticks: { color: "#667788", font: { size: 16 }, maxTicksLimit: 5 },
              grid: { color: "rgba(255,255,255,0.06)" },
            },
            y: {
              display: true,
              ticks: { color: "#667788", font: { size: 16 }, maxTicksLimit: 5 },
              grid: { color: "rgba(255,255,255,0.06)" },
            },
          },
        },
      }));
    }
  } catch (chartErr) {
    console.error("Chart init failed:", chartErr);
  }


  // ──────────────────────────────────────────
  // Data fetching
  // ──────────────────────────────────────────

  async function fetchJSON(url) {
    const resp = await fetch(url);
    if (!resp.ok) throw new Error(resp.statusText);
    return resp.json();
  }

  async function refreshTrack() {
    try {
      const data = await fetchJSON("/api/track");
      trackLines.clearLayers();
      for (const seg of data.segments) {
        L.polyline(seg, {
          color: "#00ccff",
          weight: 2,
          opacity: 0.85,
        }).addTo(trackLines);
      }
      if (data.current) satMarker.setLatLng(data.current);
      if (data.start) startMarker.setLatLng(data.start);
    } catch (e) {
      console.error("Track fetch failed:", e);
    }
  }

  async function refreshPanels() {
    try {
      const data = await fetchJSON("/api/panels");
      data.panels.forEach(function (p, i) {
        const chart = charts[i];
        if (!chart) return;
        const awaiting = document.getElementById("awaiting-" + i);
        if (p.x.length === 0) {
          if (awaiting) awaiting.classList.remove("hidden");
          return;
        }
        if (awaiting) awaiting.classList.add("hidden");
        chart.data.labels = p.x;
        chart.data.datasets[0].data = p.y;
        chart.data.datasets[0].borderColor = p.color;
        chart.options.scales.y.title = {
          display: true,
          text: p.ylabel,
          color: "#8899aa",
          font: { size: 9, family: "monospace" },
        };
        chart.update();
      });
    } catch (e) {
      console.error("Panel fetch failed:", e);
    }
  }

  async function refreshStatus() {
    try {
      const s = await fetchJSON("/api/status");
      document.getElementById("hud-text").textContent =
        "ALT: " + s.alt_km + " km   " +
        "INC: " + s.inc + "\u00b0   " +
        "ECC: " + s.ecc + "   " +
        "PERIOD: " + s.period_min + " min   " +
        "ORBITS: " + s.n_orbits;
      document.getElementById("db-count").textContent = "DB: " + s.n_pkts + " pkts";

      // FSM state HUD
      if (s.fsm_state !== undefined) {
        const stateEl = document.getElementById("fsm-state");
        const deplEl = document.getElementById("fsm-depl");
        const uptimeEl = document.getElementById("fsm-uptime");
        if (stateEl) stateEl.textContent = s.fsm_state || "\u2014";
        if (deplEl) deplEl.textContent = String(s.fsm_depl ?? "\u2014");
        if (uptimeEl) {
          const ut = s.fsm_uptime;
          if (ut !== "\u2014" && ut !== "" && ut !== undefined) {
            const secs = Number(ut);
            if (!isNaN(secs)) {
              const hh = String(Math.floor(secs / 3600)).padStart(2, "0");
              const mm = String(Math.floor((secs % 3600) / 60)).padStart(2, "0");
              const ss = String(Math.floor(secs % 60)).padStart(2, "0");
              uptimeEl.textContent = hh + ":" + mm + ":" + ss;
            } else {
              uptimeEl.textContent = String(ut);
            }
          } else {
            uptimeEl.textContent = "\u2014";
          }
        }
      }
    } catch (e) {
      // Silently ignore — will retry next tick
    }
  }


  // ──────────────────────────────────────────
  // MET clock (runs locally, no server round-trip)
  // ──────────────────────────────────────────

  function tickMET() {
    const elapsed = Math.floor((Date.now() - startTime) / 1000);
    const h = String(Math.floor(elapsed / 3600)).padStart(2, "0");
    const m = String(Math.floor((elapsed % 3600) / 60)).padStart(2, "0");
    const s = String(elapsed % 60).padStart(2, "0");
    document.getElementById("met-clock").textContent = "MET " + h + ":" + m + ":" + s;

    // Blink the live dot
    const dot = document.getElementById("live-dot");
    if (dot) {
      blinkOn = !blinkOn;
      dot.style.opacity = blinkOn ? "1" : "0";
    }
  }


  // ──────────────────────────────────────────
  // FSM State Timeline Chart
  // ──────────────────────────────────────────

  const fsmCtx = document.getElementById("fsm-timeline");
  let fsmChart = null;
  try { if (fsmCtx) {
    fsmChart = new Chart(fsmCtx.getContext("2d"), {
      type: "line",
      data: {
        labels: [],
        datasets: [{
          data: [],
          borderColor: "#00ddff",
          backgroundColor: "rgba(0,221,255,0.1)",
          borderWidth: 1.5,
          pointRadius: 3,
          pointBackgroundColor: "#00ddff",
          stepped: "before",
          fill: true,
        }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: false,
        plugins: {
          legend: { display: false },
          tooltip: {
            callbacks: {
              label: function (ctx) {
                return fsmChart._stateLabels ? fsmChart._stateLabels[ctx.dataIndex] || "" : "";
              },
            },
          },
        },
        scales: {
          x: {
            display: true,
            title: { display: true, text: "Packet #", color: "#667788", font: { size: 16, family: "monospace" } },
            ticks: { color: "#667788", font: { size: 7 }, maxTicksLimit: 8 },
            grid: { color: "rgba(255,255,255,0.06)" },
          },
          y: {
            display: true,
            title: { display: true, text: "State", color: "#667788", font: { size: 16, family: "monospace" } },
            ticks: {
              color: "#667788",
              font: { size: 7 },
              callback: function (value) {
                return fsmChart._stateNames ? (fsmChart._stateNames[value] || value) : value;
              },
            },
            grid: { color: "rgba(255,255,255,0.06)" },
          },
        },
      },
    });
    fsmChart._stateLabels = [];
    fsmChart._stateNames = {};
  } } catch (fsmErr) {
    console.error("FSM chart init failed:", fsmErr);
  }

  async function refreshFSM() {
    try {
      const data = await fetchJSON("/api/fsm");
      const awaiting = document.getElementById("awaiting-fsm");
      if (!data.history || data.history.length === 0) {
        if (awaiting) awaiting.classList.remove("hidden");
        return;
      }
      if (awaiting) awaiting.classList.add("hidden");

      if (!fsmChart) return;

      // Build unique state → numeric index mapping
      const stateSet = [...new Set(data.history.map(h => String(h.fsm_state)))];
      const stateMap = {};
      stateSet.forEach((s, i) => { stateMap[s] = i; });

      const labels = data.history.map((_, i) => i + 1);
      const values = data.history.map(h => stateMap[String(h.fsm_state)]);
      const stateLabels = data.history.map(h =>
        "State: " + h.fsm_state + "  Depl: " + h.fsm_depl + "  Uptime: " + h.uptime
      );

      // Reverse map for Y-axis labels
      const stateNames = {};
      for (const [name, idx] of Object.entries(stateMap)) {
        stateNames[idx] = name;
      }

      fsmChart._stateLabels = stateLabels;
      fsmChart._stateNames = stateNames;
      fsmChart.data.labels = labels;
      fsmChart.data.datasets[0].data = values;
      fsmChart.update();
    } catch (e) {
      console.error("FSM fetch failed:", e);
    }
  }


  // ──────────────────────────────────────────
  // Best Direction Doughnut Chart
  // ──────────────────────────────────────────

  const bestDirCtx = document.getElementById("chart-best-dir");
  let bestDirChart = null;
  try { if (bestDirCtx) {
    bestDirChart = new Chart(bestDirCtx.getContext("2d"), {
      type: "doughnut",
      data: {
        labels: ["+Y", "\u2212X", "\u2212Y", "+X", "N/A"],
        datasets: [{
          data: [0, 0, 0, 0, 0],
          backgroundColor: ["#00ccff", "#cc44ff", "#ffcc00", "#ff4400", "#334455"],
          borderColor: "#0a1628",
          borderWidth: 2,
        }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: false,
        cutout: "55%",
        plugins: {
          legend: {
            display: true,
            position: "bottom",
            labels: {
              color: "#8899aa",
              font: { size: 9, family: "monospace" },
              boxWidth: 10,
              padding: 6,
            },
          },
          tooltip: {
            callbacks: {
              label: function (ctx) {
                const total = ctx.dataset.data.reduce(function (a, b) { return a + b; }, 0);
                const pct = total > 0 ? Math.round(ctx.raw / total * 100) : 0;
                return ctx.label + ": " + ctx.raw + " (" + pct + "%)";
              },
            },
          },
        },
      },
    });
  } } catch (bdErr) {
    console.error("Best-dir chart init failed:", bdErr);
  }

  async function refreshBestDir() {
    if (!bestDirChart) return;
    try {
      const data = await fetchJSON("/api/best_dir");
      const awaiting = document.getElementById("awaiting-best-dir");
      const total = data.counts.reduce(function (a, b) { return a + b; }, 0);
      if (total === 0) {
        if (awaiting) awaiting.classList.remove("hidden");
        return;
      }
      if (awaiting) awaiting.classList.add("hidden");

      bestDirChart.data.labels = data.labels;
      bestDirChart.data.datasets[0].data = data.counts;
      bestDirChart.data.datasets[0].backgroundColor = data.colors;
      bestDirChart.update();

      // Update center label with current direction
      const labelEl = document.getElementById("best-dir-current");
      if (labelEl) labelEl.textContent = data.latest_label;
    } catch (e) {
      console.error("Best-dir fetch failed:", e);
    }
  }


  // ──────────────────────────────────────────
  // SocketIO live push (optional)
  // ──────────────────────────────────────────

  if (typeof io !== "undefined") {
    try {
      const socket = io();
      socket.on("new_packets", function (data) {
        document.getElementById("db-count").textContent = "DB: " + data.count + " pkts";
        // Refresh panels immediately when new data arrives
        refreshPanels();
        refreshFSM();
        refreshBestDir();
      });
    } catch (e) {
      // SocketIO not available — no problem, we poll
    }
  }


  // ──────────────────────────────────────────
  // Bootstrap
  // ──────────────────────────────────────────

  // Sync start time with server
  fetchJSON("/api/status").then(function (s) {
    startTime = Date.now() - s.elapsed * 1000;
  }).catch(function () {});

  // Initial data load
  refreshTrack();
  refreshPanels();
  refreshStatus();
  refreshFSM();
  refreshBestDir();

  // Periodic refreshes
  setInterval(refreshTrack, TRACK_REFRESH_MS);
  setInterval(refreshPanels, PANEL_REFRESH_MS);
  setInterval(refreshStatus, STATUS_REFRESH_MS);
  setInterval(tickMET, MET_TICK_MS);
  setInterval(refreshFSM, PANEL_REFRESH_MS);
  setInterval(refreshBestDir, PANEL_REFRESH_MS);

  // Fix map size after layout settles
  setTimeout(function () { map.invalidateSize(); }, 200);

})();
