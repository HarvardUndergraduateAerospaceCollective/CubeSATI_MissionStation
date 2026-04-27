/* ═══════════════════════════════════════════════
   Mission Control — Dashboard JS
   Leaflet map + Chart.js panels + SocketIO live push
   ═══════════════════════════════════════════════ */

(function () {
  "use strict";

  window.addEventListener("error", function (e) {
    console.error("Dashboard error:", e.message, e.filename, e.lineno);
    var el = document.getElementById("db-count");
    if (el) el.textContent = "JS ERROR: " + e.message + " L" + e.lineno;
  });

  // ── Constants ──
  const TRACK_REFRESH_MS = 5_000;
  const GLOBE_PATH_REFRESH_MS = 300_000;
  const PANEL_REFRESH_MS = 10_000;
  const STATUS_REFRESH_MS = 1_000;
  const MET_TICK_MS = 1_000;
  const PANEL_COLORS = ["#33ff00", "#00ff88", "#ffb000", "#ff6600", "#ff2244"];
  const FSM_PLACEHOLDER = "AWAITING DATA";

  const AXIS_LABEL_COLOR = "#7a9a5a";
  const AXIS_TICK_COLOR = "#7a9a5a";
  const GRID_COLOR = "rgba(51, 255, 0, 0.06)";
  const AXIS_FONT = { family: "monospace" };

  // ── State ──
  let startTime = Date.now();
  let blinkOn = true;
  let lastApproach = null;
  let nextGlobePathUpdateAt = 0;

  // ──────────────────────────────────────────
  // Leaflet map
  // ──────────────────────────────────────────

  const map = L.map("map", {
    center: [0, 0],
    zoom: 2.5,
    minZoom: 1,
    maxZoom: 6,
    worldCopyJump: true,
    zoomControl: false,
  });

  L.tileLayer(
    "https://{s}.basemaps.cartocdn.com/dark_nolabels/{z}/{x}/{y}{r}.png",
    {
      attribution: '&copy; <a href="https://carto.com/">CARTO</a>',
      subdomains: "abcd",
      maxZoom: 19,
    }
  ).addTo(map);

  let trackLines = L.layerGroup().addTo(map);

  const satIcon = L.divIcon({
    className: "sat-icon",
    html: '<svg width="18" height="18"><circle cx="9" cy="9" r="7" fill="#ff2200" stroke="#ffb000" stroke-width="1.5"/></svg>',
    iconSize: [18, 18],
    iconAnchor: [9, 9],
  });
  const satMarker = L.marker([0, 0], { icon: satIcon, zIndex: 1000 }).addTo(map);

  satMarker.bindTooltip("HUCSAT", {
    permanent: true,
    direction: "right",
    offset: [10, -4],
    className: "sat-tooltip",
  });

  const startIcon = L.divIcon({
    className: "sat-icon",
    html: '<svg width="12" height="12"><rect x="1" y="1" width="10" height="10" rx="2" fill="#ffb000" stroke="white" stroke-width="0.8" transform="rotate(45,6,6)"/></svg>',
    iconSize: [12, 12],
    iconAnchor: [6, 6],
  });
  const startMarker = L.marker([0, 0], { icon: startIcon, zIndex: 999 }).addTo(map);

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

  const tooltipStyle = document.createElement("style");
  tooltipStyle.textContent = `
    .sat-tooltip {
      background: transparent; border: none; box-shadow: none;
      color: #ffb000; font-family: monospace; font-size: 14px; font-weight: bold;
    }
    .harvard-tooltip {
      background: transparent; border: none; box-shadow: none;
      color: crimson; font-family: monospace; font-size: 13px; font-weight: bold;
    }
  `;
  document.head.appendChild(tooltipStyle);


  // ──────────────────────────────────────────
  // Globe.gl 3D view
  // ──────────────────────────────────────────

  let globeViz = null;
  (function initGlobe() {
    const el = document.getElementById("globe");
    if (!el || typeof Globe === "undefined") return;

    globeViz = Globe({ animateIn: true })(el)
      .globeImageUrl("//unpkg.com/three-globe/example/img/earth-dark.jpg")
      .bumpImageUrl("//unpkg.com/three-globe/example/img/earth-topology.png")
      .backgroundColor("rgba(0,0,0,0)")
      .showGraticules(true)
      .showAtmosphere(true)
      .atmosphereColor("#33ff00")
      .atmosphereAltitude(0.12)
      .pathsData([])
      .pathPoints(function (seg) { return seg; })
      .pathPointLat(function (p) { return p[0]; })
      .pathPointLng(function (p) { return p[1]; })
      .pathColor(function () { return "#33ff00"; })
      .pathStroke(1.5)
      .pathDashLength(1)
      .pathDashGap(0)
      .pointsData([])
      .pointColor(function () { return "#ff2200"; })
      .pointAltitude(0.02)
      .pointRadius(0.4)
      .pointOfView({ lat: 20, lng: -74, altitude: 2 });

    var ro = new ResizeObserver(function () {
      globeViz.width(el.clientWidth).height(el.clientHeight);
    });
    ro.observe(el);
  })();

  const approachCanvas = document.getElementById("approach-polar");
  const approachCtx = approachCanvas ? approachCanvas.getContext("2d") : null;

  // Dynamic refresh state for approach tracking
  let approachFastMode = false;
  let approachFastTimer = null;
  let fastModeExitAt = null;


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
            borderWidth: 2,
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
              title: { display: true, text: "Time (min)", color: AXIS_LABEL_COLOR, font: { size: 12, ...AXIS_FONT } },
              ticks: { color: AXIS_TICK_COLOR, font: { size: 11, ...AXIS_FONT }, maxTicksLimit: 5 },
              grid: { color: GRID_COLOR },
            },
            y: {
              display: true,
              ticks: { color: AXIS_TICK_COLOR, font: { size: 11, ...AXIS_FONT }, maxTicksLimit: 5 },
              grid: { color: GRID_COLOR },
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

  function isMissingFSMValue(v) {
    return v === undefined || v === null || v === "" || v === "—";
  }

  function formatEta(totalSeconds) {
    const sec = Math.max(0, Math.round(Number(totalSeconds) || 0));
    const hh = String(Math.floor(sec / 3600)).padStart(2, "0");
    const mm = String(Math.floor((sec % 3600) / 60)).padStart(2, "0");
    const ss = String(sec % 60).padStart(2, "0");
    return hh + ":" + mm + ":" + ss;
  }

  function drawApproachPolar(pred) {
    if (!approachCanvas || !approachCtx) return;

    const rect = approachCanvas.getBoundingClientRect();
    if (rect.width < 2 || rect.height < 2) return;

    const dpr = window.devicePixelRatio || 1;
    const pixW = Math.max(2, Math.round(rect.width * dpr));
    const pixH = Math.max(2, Math.round(rect.height * dpr));
    if (approachCanvas.width !== pixW || approachCanvas.height !== pixH) {
      approachCanvas.width = pixW;
      approachCanvas.height = pixH;
    }

    approachCtx.setTransform(dpr, 0, 0, dpr, 0, 0);
    approachCtx.clearRect(0, 0, rect.width, rect.height);

    const w = rect.width;
    const h = rect.height;
    const cx = w / 2;
    const cy = h / 2;
    const radius = Math.max(10, Math.min(w, h) * 0.40);

    approachCtx.strokeStyle = "rgba(51, 255, 0, 0.22)";
    approachCtx.lineWidth = 1;

    for (const el of [0, 30, 60, 90]) {
      const rr = ((90 - el) / 90) * radius;
      approachCtx.beginPath();
      approachCtx.arc(cx, cy, rr, 0, Math.PI * 2);
      approachCtx.stroke();
    }

    for (const az of [0, 45, 90, 135, 180, 225, 270, 315]) {
      const rad = az * Math.PI / 180;
      const x = cx + radius * Math.sin(rad);
      const y = cy - radius * Math.cos(rad);
      approachCtx.beginPath();
      approachCtx.moveTo(cx, cy);
      approachCtx.lineTo(x, y);
      approachCtx.stroke();
    }

    approachCtx.fillStyle = "#7a9a5a";
    approachCtx.font = "10px monospace";
    approachCtx.textAlign = "center";
    approachCtx.textBaseline = "middle";

    const labelRadius = radius + 14;
    const azLabels = [
      { az: 0, text: "N 0°" },
      { az: 45, text: "45°" },
      { az: 90, text: "E 90°" },
      { az: 135, text: "135°" },
      { az: 180, text: "S 180°" },
      { az: 225, text: "225°" },
      { az: 270, text: "W 270°" },
      { az: 315, text: "315°" },
    ];
    for (const lbl of azLabels) {
      const rad = lbl.az * Math.PI / 180;
      const lx = cx + labelRadius * Math.sin(rad);
      const ly = cy - labelRadius * Math.cos(rad);
      approachCtx.fillText(lbl.text, lx, ly);
    }

    approachCtx.textAlign = "left";
    for (const elTick of [0, 30, 60]) {
      const rrTick = ((90 - elTick) / 90) * radius;
      approachCtx.fillText(elTick + "°", cx + rrTick + 4, cy - 1);
    }
    approachCtx.textAlign = "center";
    approachCtx.fillText("90°", cx, cy - 10);

    if (!pred || typeof pred.az_deg !== "number" || typeof pred.el_deg !== "number") return;

    const path = Array.isArray(pred.path) ? pred.path : [];
    if (pred.visible && path.length > 1) {
      const pathPts = [];
      for (const pt of path) {
        const azPt = Number(pt.az);
        const elPt = Number(pt.el);
        if (!isFinite(azPt) || !isFinite(elPt)) continue;

        const elClamped = Math.max(0, Math.min(90, elPt));
        const rrPt = ((90 - elClamped) / 90) * radius;
        const azPtRad = azPt * Math.PI / 180;
        const xPt = cx + rrPt * Math.sin(azPtRad);
        const yPt = cy - rrPt * Math.cos(azPtRad);
        pathPts.push({ x: xPt, y: yPt });
      }

      if (pathPts.length > 1) {
        // Dim-to-bright path: AOS -> LOS gradient to show pass direction.
        const denom = Math.max(1, pathPts.length - 1);
        for (let i = 1; i < pathPts.length; i++) {
          const t = i / denom;
          const r = Math.round(255 + (51 - 255) * t);
          const g = Math.round(176 + (255 - 176) * t);
          const alpha = 0.25 + 0.75 * t;
          const width = 1.2 + 1.4 * t;
          approachCtx.strokeStyle = "rgba(" + r + ", " + g + ", 0, " + alpha.toFixed(3) + ")";
          approachCtx.lineWidth = width;
          approachCtx.beginPath();
          approachCtx.moveTo(pathPts[i - 1].x, pathPts[i - 1].y);
          approachCtx.lineTo(pathPts[i].x, pathPts[i].y);
          approachCtx.stroke();
        }

        const startPt = pathPts[0];
        const endPt = pathPts[pathPts.length - 1];
        approachCtx.fillStyle = "rgba(255, 176, 0, 0.55)";
        approachCtx.beginPath();
        approachCtx.arc(startPt.x, startPt.y, 2.5, 0, Math.PI * 2);
        approachCtx.fill();

        approachCtx.fillStyle = "#33ff00";
        approachCtx.beginPath();
        approachCtx.arc(endPt.x, endPt.y, 3.5, 0, Math.PI * 2);
        approachCtx.fill();
      }
    }

    // Mark closest approach point.
    const az = pred.az_deg;
    const el = pred.el_deg;
    const clampedEl = Math.max(0, Math.min(90, el));
    const rr = ((90 - clampedEl) / 90) * radius;
    const azRad = az * Math.PI / 180;
    const px = cx + rr * Math.sin(azRad);
    const py = cy - rr * Math.cos(azRad);

    const color = pred.visible ? "#33ff00" : "#ff6600";
    approachCtx.fillStyle = color;
    approachCtx.beginPath();
    approachCtx.arc(px, py, 4.5, 0, Math.PI * 2);
    approachCtx.fill();

    approachCtx.strokeStyle = "#ffffff";
    approachCtx.lineWidth = 1;
    approachCtx.stroke();
  }

  async function refreshTrack() {
    try {
      const data = await fetchJSON("/api/track");
      trackLines.clearLayers();
      for (const seg of data.segments) {
        L.polyline(seg, {
          color: "#33ff00",
          weight: 2,
          opacity: 0.85,
        }).addTo(trackLines);
      }
      if (data.current) satMarker.setLatLng(data.current);
      if (data.start) startMarker.setLatLng(data.start);
      if (globeViz) {
        const now = Date.now();
        if (now >= nextGlobePathUpdateAt) {
          globeViz.pathsData(data.segments || []);
          nextGlobePathUpdateAt = now + GLOBE_PATH_REFRESH_MS;
        }
        if (data.current) {
          globeViz.pointsData([{ lat: data.current[0], lng: data.current[1] }]);
        }
      }
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
          color: AXIS_LABEL_COLOR,
          font: { size: 12, ...AXIS_FONT },
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
        "INC: " + s.inc + "°   " +
        "ECC: " + s.ecc + "   " +
        "PERIOD: " + s.period_min + " min   " +
        "ORBITS: " + s.n_orbits;
      document.getElementById("db-count").textContent = "DB: " + s.n_pkts + " pkts";

      if (s.fsm_state !== undefined) {
        const stateEl = document.getElementById("fsm-state");
        const deplEl = document.getElementById("fsm-depl");
        const uptimeEl = document.getElementById("fsm-uptime");
        if (stateEl) {
          stateEl.textContent = isMissingFSMValue(s.fsm_state)
            ? FSM_PLACEHOLDER
            : String(s.fsm_state);
        }
        if (deplEl) {
          deplEl.textContent = isMissingFSMValue(s.fsm_depl)
            ? FSM_PLACEHOLDER
            : String(s.fsm_depl);
        }
        if (uptimeEl) {
          const ut = s.fsm_uptime;
          if (!isMissingFSMValue(ut)) {
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
            uptimeEl.textContent = FSM_PLACEHOLDER;
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
          borderColor: "#33ff00",
          backgroundColor: "rgba(51, 255, 0, 0.08)",
          borderWidth: 2,
          pointRadius: 4,
          pointBackgroundColor: "#33ff00",
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
            title: { display: true, text: "Packet #", color: AXIS_LABEL_COLOR, font: { size: 12, ...AXIS_FONT } },
            ticks: { color: AXIS_TICK_COLOR, font: { size: 11, ...AXIS_FONT }, maxTicksLimit: 8 },
            grid: { color: GRID_COLOR },
          },
          y: {
            display: true,
            title: { display: true, text: "State", color: AXIS_LABEL_COLOR, font: { size: 12, ...AXIS_FONT } },
            ticks: {
              color: AXIS_TICK_COLOR,
              font: { size: 11, ...AXIS_FONT },
              callback: function (value) {
                return fsmChart._stateNames ? (fsmChart._stateNames[value] || value) : value;
              },
            },
            grid: { color: GRID_COLOR },
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

      const stateSet = [...new Set(data.history.map(h => String(h.fsm_state)))];
      const stateMap = {};
      stateSet.forEach((s, i) => { stateMap[s] = i; });

      const labels = data.history.map((_, i) => i + 1);
      const values = data.history.map(h => stateMap[String(h.fsm_state)]);
      const stateLabels = data.history.map(h =>
        "State: " + h.fsm_state + "  Depl: " + h.fsm_depl + "  Uptime: " + h.uptime
      );

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
        labels: ["+Y", "−X", "−Y", "+X", "N/A"],
        datasets: [{
          data: [0, 0, 0, 0, 0],
          backgroundColor: ["#33ff00", "#ffb000", "#ff6600", "#00ff88", "#1a1a1a"],
          borderColor: "#0a0c0a",
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
              color: "#7a9a5a",
              font: { size: 12, family: "monospace" },
              boxWidth: 12,
              padding: 8,
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

      const labelEl = document.getElementById("best-dir-current");
      if (labelEl) labelEl.textContent = data.latest_label;
    } catch (e) {
      console.error("Best-dir fetch failed:", e);
    }
  }

  async function refreshHarvardApproach() {
    if (!approachCanvas) return;

    const awaiting = document.getElementById("awaiting-placeholder");
    try {
      const data = await fetchJSON("/api/harvard_approach");
      if (!data || typeof data.az_deg !== "number" || typeof data.el_deg !== "number") {
        if (awaiting) awaiting.classList.remove("hidden");
        if (awaiting) awaiting.textContent = "AWAITING DATA";
        lastApproach = null;
        drawApproachPolar(null);
        return;
      }
      lastApproach = data;
      drawApproachPolar(lastApproach);

      if (awaiting) {
        if (data.visible) {
          awaiting.classList.add("hidden");
        } else {
          awaiting.textContent = "NO LINE OF SIGHT";
          awaiting.classList.remove("hidden");
        }
      }
    } catch (e) {
      console.error("Harvard approach fetch failed:", e);
      if (awaiting) awaiting.classList.remove("hidden");
      if (awaiting) awaiting.textContent = "AWAITING DATA";
      lastApproach = null;
      drawApproachPolar(null);
    }
  }

  async function refreshNextApproaches() {
    const awaitingFeed = document.getElementById("awaiting-feed");
    try {
      const data = await fetchJSON("/api/next_approaches");

      if (!data.current_pass) {
        if (awaitingFeed) awaitingFeed.classList.remove("hidden");
        setApproachVal("current-aos", "--:--:--");
        setApproachVal("current-cpa", "--:--:--");
        setApproachVal("current-los", "--:--:--");
        setApproachVal("current-min", "--- km");
        for (let i = 0; i < 3; i++) {
          const el = document.getElementById("upcoming-" + i);
          if (el) el.textContent = "--";
        }
        updateApproachFastMode(null);
        return;
      }

      if (awaitingFeed) awaitingFeed.classList.add("hidden");

      const cp = data.current_pass;
      setApproachVal("current-aos", formatEta(cp.aos_eta_sec));
      setApproachVal("current-cpa", formatEta(cp.cpa_eta_sec));
      setApproachVal("current-los", formatEta(cp.los_eta_sec));
      setApproachVal("current-min", cp.min_range_km.toFixed(1) + " km");

      const upcoming = data.upcoming || [];
      for (let i = 0; i < 3; i++) {
        const el = document.getElementById("upcoming-" + i);
        if (!el) continue;
        if (i < upcoming.length) {
          const p = upcoming[i];
          el.textContent =
            "AOS " + formatEta(p.aos_eta_sec) +
            "  CPA " + formatEta(p.cpa_eta_sec) +
            "  LOS " + formatEta(p.los_eta_sec) +
            "  MIN " + p.min_range_km.toFixed(1) + " km";
        } else {
          el.textContent = "--";
        }
      }

      updateApproachFastMode(cp);
    } catch (e) {
      console.error("Next approaches fetch failed:", e);
    }
  }

  function setApproachVal(id, text) {
    const el = document.getElementById(id);
    if (!el) return;
    const span = el.querySelector(".approach-val");
    if (span) { span.textContent = text; }
  }

  function updateApproachFastMode(currentPass) {
    if (!currentPass) {
      exitFastMode();
      return;
    }

    var aosEta = currentPass.aos_eta_sec;

    if (aosEta <= 300) {
      fastModeExitAt = null;
      enterFastMode();
    } else if (approachFastMode) {
      if (fastModeExitAt === null) {
        fastModeExitAt = Date.now() + 120000;
      }
      if (Date.now() >= fastModeExitAt) {
        exitFastMode();
      }
    }
  }

  function enterFastMode() {
    if (approachFastMode) return;
    approachFastMode = true;
    approachFastTimer = setInterval(function () {
      refreshHarvardApproach();
      refreshNextApproaches();
    }, 1000);
  }

  function exitFastMode() {
    if (!approachFastMode) return;
    approachFastMode = false;
    fastModeExitAt = null;
    if (approachFastTimer) {
      clearInterval(approachFastTimer);
      approachFastTimer = null;
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
        refreshPanels();
        refreshFSM();
        refreshBestDir();
        refreshHarvardApproach();
        refreshNextApproaches();
      });
    } catch (e) {
      // SocketIO not available — no problem, we poll
    }
  }


  // ──────────────────────────────────────────
  // Bootstrap
  // ──────────────────────────────────────────

  fetchJSON("/api/status").then(function (s) {
    startTime = Date.now() - s.elapsed * 1000;
  }).catch(function () {});

  refreshTrack();
  refreshPanels();
  refreshStatus();
  refreshFSM();
  refreshBestDir();
  refreshHarvardApproach();
  refreshNextApproaches();

  setInterval(refreshTrack, TRACK_REFRESH_MS);
  setInterval(refreshPanels, PANEL_REFRESH_MS);
  setInterval(refreshStatus, STATUS_REFRESH_MS);
  setInterval(tickMET, MET_TICK_MS);
  setInterval(refreshFSM, PANEL_REFRESH_MS);
  setInterval(refreshBestDir, PANEL_REFRESH_MS);
  setInterval(refreshHarvardApproach, PANEL_REFRESH_MS);
  setInterval(refreshNextApproaches, PANEL_REFRESH_MS);

  function scheduleMapResize() {
    if (scheduleMapResize._timer) {
      clearTimeout(scheduleMapResize._timer);
    }
    scheduleMapResize._timer = setTimeout(function () {
      map.invalidateSize();
      if (lastApproach) drawApproachPolar(lastApproach);
      scheduleMapResize._timer = null;
    }, 150);
  }

  setTimeout(scheduleMapResize, 200);
  window.addEventListener("resize", scheduleMapResize);
  window.addEventListener("orientationchange", scheduleMapResize);

})();
