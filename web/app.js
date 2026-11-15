"use strict";

/* Lunar Landing Site Navigator - front end.
 *
 * No frameworks and no CDN: the map is a canvas, the charts are canvases, and
 * the only thing it talks to is the local Flask API.
 */

const R_MOON = 1737400;           // metres, the sphere everything is measured on
const SUN_RADIUS = 0.266;         // degrees
const D2R = Math.PI / 180;

const C = {
  bg: "#08090b",
  panel: "#14171d",
  grid: "#232936",
  axis: "#4a5262",
  text: "#dfe3ea",
  muted: "#8d95a5",
  dim: "#6f7889",
  sun: "#f5b544",
  earth: "#56c8d8",
  roi: "#7f8fa6",
  roiSel: "#e6eefb",
  sky: "#6d7688",
  good: "#58c08a",
  warn: "#e0a33e",
  bad: "#d9636b",
};

const $ = (sel) => document.querySelector(sel);
const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));

const state = {
  status: null,
  sites: [],
  site: null,
  roi: null,
  observer: null,
  view: { cx: 0, cy: 0, span: 6000 },
  raster: { img: null, bbox: null, key: "", url: null, globe: false, sub: null },
  skyline: null,
  eph: null,
  hover: null,
  busy: false,
  stale: true,
  view3d: false,
  scenarios: [],
  // active step of the scrubber/playback, or null when not playing;
  // timer holds the setInterval id while the play button runs
  playback: { i: null, timer: null, fps: 6, speed: 1, mapTimer: null, mapInterval: null },
  params: { start: "2026-11-14T00:00:00", hours: 24, step_min: 10, mast: 2, maxdist: 12000 },
};
// short alias: every playback function below reads and writes this one
const playback = state.playback;

const dpr = () => Math.min(2.5, window.devicePixelRatio || 1);

/* ------------------------------------------------------------------ helpers */

function parseUTC(iso) {
  let s = String(iso).trim();
  if (s.endsWith("Z")) s = s.slice(0, -1);
  return new Date(s + "Z");
}

function fmtTime(iso, short) {
  const d = parseUTC(iso);
  const iso2 = d.toISOString();
  if (short) return iso2.slice(11, 16);
  return iso2.slice(0, 10) + " " + iso2.slice(11, 16);
}

function fmtDay(iso) {
  const d = parseUTC(iso);
  const months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  return `${d.getUTCDate()} ${months[d.getUTCMonth()]}`;
}

function fmtDuration(hours) {
  if (hours >= 48) return (hours / 24).toFixed(1) + " d";
  if (hours >= 1) return hours.toFixed(1) + " h";
  return Math.round(hours * 60) + " min";
}

function fmtLat(v) {
  return Math.abs(v).toFixed(3) + "° " + (v >= 0 ? "N" : "S");
}
function fmtLon(v) {
  return Math.abs(v).toFixed(3) + "° " + (v >= 0 ? "E" : "W");
}
function fmtNum(v, digits = 0) {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  return v.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

function niceStep(range, target) {
  const raw = range / (target || 5);
  const pow = Math.pow(10, Math.floor(Math.log10(raw)));
  const m = [1, 2, 5, 10].find((k) => k * pow >= raw) || 10;
  return m * pow;
}

const arrMin = (a) => a.reduce((m, v) => (v < m ? v : m), Infinity);
const arrMax = (a) => a.reduce((m, v) => (v > m ? v : m), -Infinity);

async function getJSON(url) {
  const resp = await fetch(url);
  let body;
  try {
    body = await resp.json();
  } catch {
    throw new Error(`server returned ${resp.status}`);
  }
  if (!resp.ok || body.ok === false) throw new Error(body.error || `server returned ${resp.status}`);
  return body;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

function setPill(text, kind, spin = false) {
  const el = $("#status-pill");
  el.className = "pill " + (kind === "ok" ? "pill-ok" : kind === "bad" ? "pill-bad" : "pill-wait");
  el.innerHTML = (spin ? '<span class="spinner"></span>' : "") + escapeHtml(text);
}

/* Every request that changes the answer goes through these two functions, so
   the user always knows whether the numbers on screen are current. */
const BUSY_CARDS = ["#card-horizon", "#card-eph"];

function beginBusy(label) {
  state.busy = true;
  state.stale = true;
  setPill(label || "calculating…", "wait", true);
  $("#progress").hidden = false;
  $("#map-busy").hidden = false;
  $("#last-updated").textContent = "Calculating…";
  const run = $("#run");
  if (run) {
    run.disabled = true;
    run.textContent = "Calculating…";
  }
  const save = $("#save-scenario");
  if (save) save.disabled = true;
  // playback timers keep running across a recompute; the step is preserved
  for (const sel of BUSY_CARDS) {
    const card = $(sel);
    card.classList.add("is-busy");
    const overlay = card.querySelector(".card-loading");
    if (overlay) overlay.hidden = false;
  }
  drawMap(); // adds the dashed ring around the observer
}

function endBusy() {
  state.busy = false;
  $("#progress").hidden = true;
  $("#map-busy").hidden = true;
  const run = $("#run");
  if (run) {
    run.disabled = false;
    run.textContent = "Recompute";
  }
  const save = $("#save-scenario");
  if (save) save.disabled = !state.eph || state.stale;
  for (const sel of BUSY_CARDS) {
    const card = $(sel);
    card.classList.remove("is-busy");
    const overlay = card.querySelector(".card-loading");
    if (overlay) overlay.hidden = true;
  }
  drawMap();
}

/* --------------------------------------------------------------------- map */

const mapCanvas = $("#map");
const mctx = mapCanvas.getContext("2d");

function mapSize() {
  return { W: mapCanvas.width / dpr(), H: mapCanvas.height / dpr() };
}
function pxPerM() {
  return mapSize().W / state.view.span;
}
function worldToScreen(x, y) {
  const { W, H } = mapSize();
  const s = pxPerM();
  return { x: (x - state.view.cx) * s + W / 2, y: (state.view.cy - y) * s + H / 2 };
}
function screenToWorld(sx, sy) {
  const { W, H } = mapSize();
  const s = pxPerM();
  return { x: (sx - W / 2) / s + state.view.cx, y: state.view.cy - (sy - H / 2) / s };
}

function resizeMap() {
  const rect = mapCanvas.parentElement.getBoundingClientRect();
  const d = dpr();
  mapCanvas.width = Math.max(1, Math.round(rect.width * d));
  mapCanvas.height = Math.max(1, Math.round(rect.height * d));
  mapCanvas.style.width = rect.width + "px";
  mapCanvas.style.height = rect.height + "px";
  clampView();
  drawMap();
  scheduleRaster();
}

function clampView() {
  const b = state.status?.dem?.bounds;
  if (!b) return;
  const { W, H } = mapSize();
  const halfW = state.view.span / 2;
  const halfH = (state.view.span * H) / W / 2;
  const demW = b.right - b.left;
  const demH = b.top - b.bottom;
  state.view.cx = 2 * halfW >= demW ? (b.left + b.right) / 2 : clamp(state.view.cx, b.left + halfW, b.right - halfW);
  state.view.cy = 2 * halfH >= demH ? (b.bottom + b.top) / 2 : clamp(state.view.cy, b.bottom + halfH, b.top - halfH);
}

let rasterTimer = null;
function scheduleRaster(delay = 140) {
  clearTimeout(rasterTimer);
  rasterTimer = setTimeout(fetchRaster, delay);
}

let rasterToken = 0;
function globeMode() {
  const b = state.status?.dem?.bounds;
  return b ? state.view.span > (b.right - b.left) * 1.05 : false;
}

async function fetchRaster() {
  const { W, H } = mapSize();
  if (W < 8 || H < 8) return;
  const d = dpr();
  const globe = globeMode();
  // while playing, render small frames: the server keeps pace with the bar
  // and the step the map shows stays near the cursor. Pausing or scrubbing
  // returns to full resolution (the cache key changes with it).
  const cap = playback.timer !== null ? 640 : 2048;
  const w = clamp(Math.round(W * d), 64, cap);
  const h = clamp(Math.round((w * H) / W), 64, cap);
  const v = state.view;
  const eph = state.eph;
  // while playing, the CURRENT step lights the scene: raster mode re-shades the
  // hillshade, globe mode also moves the day/night line to the step's time
  // the bar position lights the scene: step 0 at rest, the scrub step
  // when seeking. The generic 315/35 studio light washed the terrain
  // out white; the real Sun is used as soon as we have it.
  const pb = eph && Array.isArray(eph.sun_az) && eph.sun_az.length
    ? Math.min(playback.i ?? 0, eph.sun_az.length - 1) : null;
  // the selected date matters in globe mode: it sets the day/night line
  let tHour = globe ? Math.round(parseUTC(state.params.start) / 36e5) : 0;
  if (globe && pb !== null && eph && Array.isArray(eph.t)) tHour = pb;
  let key = [globe ? "g" : "r", Math.round(v.cx), Math.round(v.cy),
             Math.round(v.span), w, h, tHour].join(",");
  if (pb !== null && eph) {
    // fold the step's lighting into the key so playback frames never collide
    key += "," + Math.round(eph.sun_az[pb]) + "," + Math.round(eph.sun_el[pb]);
  }
  if (key === state.raster.key) return;

  const token = ++rasterToken;
  const q = new URLSearchParams({
    cx: v.cx.toFixed(1), cy: v.cy.toFixed(1), span: v.span.toFixed(1), w, h,
  });
  if (pb !== null && eph) {
    q.set("sun_az", eph.sun_az[pb]);
    q.set("sun_el", eph.sun_el[pb]);
    if (globe && Array.isArray(eph.t)) q.set("t", eph.t[pb]);
  } else if (globe) {
    q.set("t", state.params.start);
    if (eph && Array.isArray(eph.sun_az) && eph.sun_az.length) {
      q.set("sun_az", eph.sun_az[0].toFixed(1));
      q.set("sun_el", eph.sun_el[0].toFixed(1));
    }
  }
  try {
    const resp = await fetch((globe ? "/api/globe?" : "/api/raster?") + q);
    if (!resp.ok) throw new Error("raster " + resp.status);
    const bbox = (resp.headers.get("X-BBox") || "").split(",").map(Number);
    const sub = {
      sun: (resp.headers.get("X-Sun-Subpoint") || "").split(",").map(Number),
      earth: (resp.headers.get("X-Earth-Subpoint") || "").split(",").map(Number),
    };
    const blob = await resp.blob();
    const url = URL.createObjectURL(blob);
    const img = new Image();
    await new Promise((res, rej) => {
      img.onload = res;
      img.onerror = () => rej(new Error("image decode failed"));
      img.src = url;
    });
    if (token !== rasterToken) {
      URL.revokeObjectURL(url);
      return;
    }
    if (state.raster.url) URL.revokeObjectURL(state.raster.url);
    state.raster = { img, bbox, key, url, globe, sub };
    // feed the 3D view, if it exists, the fresh body sub-points
    if (globe3d && sub.sun.length === 2 && sub.earth.length === 2) globe3d.update(sub.sun, sub.earth);
    drawMap();
  } catch (err) {
    console.warn("raster:", err.message);
  }
}

function drawMap() {
  const d = dpr();
  const { W, H } = mapSize();
  mctx.setTransform(d, 0, 0, d, 0, 0);
  mctx.clearRect(0, 0, W, H);
  mctx.fillStyle = C.bg;
  mctx.fillRect(0, 0, W, H);

  // terrain
  const r = state.raster;
  if (r.img && r.bbox && r.bbox.length === 4) {
    const p0 = worldToScreen(r.bbox[0], r.bbox[3]);
    const p1 = worldToScreen(r.bbox[2], r.bbox[1]);
    mctx.imageSmoothingEnabled = true;
    mctx.imageSmoothingQuality = "high";
    mctx.drawImage(r.img, p0.x, p0.y, p1.x - p0.x, p1.y - p0.y);
  }

  // labelled Sun/Earth markers, only in the zoomed-out globe view
  if (r.img && r.globe && r.sub) drawBodies(W, H, r.sub);

  // regions
  const s = pxPerM();
  for (const roi of state.site?.rois || []) {
    const pts = roi.ring.map(([x, y]) => worldToScreen(x, y));
    const xs = pts.map((p) => p.x);
    const ys = pts.map((p) => p.y);
    const wpx = Math.max(...xs) - Math.min(...xs);
    const hpx = Math.max(...ys) - Math.min(...ys);
    if (Math.max(wpx, hpx) < 3) continue;

    const selected = state.roi && roi.id === state.roi.id;
    mctx.beginPath();
    pts.forEach((p, i) => (i ? mctx.lineTo(p.x, p.y) : mctx.moveTo(p.x, p.y)));
    mctx.closePath();
    mctx.fillStyle = selected ? "rgba(230,238,251,0.22)" : "rgba(127,143,166,0.13)";
    mctx.fill();
    mctx.lineWidth = selected ? 2 : 1.2;
    mctx.strokeStyle = selected ? C.roiSel : C.roi;
    mctx.stroke();

    if (Math.max(wpx, hpx) > 22) {
      const c0 = worldToScreen(roi.x, roi.y);
      mctx.font = "11px ui-monospace, monospace";
      mctx.textAlign = "center";
      mctx.textBaseline = "middle";
      mctx.fillStyle = selected ? "#ffffff" : "#c3ccda";
      mctx.fillText("R" + roi.index, c0.x, c0.y);
    }
  }

  // observer
  if (state.observer) {
    const p = worldToScreen(state.observer.x, state.observer.y);
    if (state.busy) {
      mctx.save();
      mctx.strokeStyle = "rgba(245,181,68,0.85)";
      mctx.setLineDash([4, 4]);
      mctx.lineWidth = 1.4;
      mctx.beginPath();
      mctx.arc(p.x, p.y, 16, 0, Math.PI * 2);
      mctx.stroke();
      mctx.restore();
    }
    mctx.save();
    mctx.strokeStyle = "#ffffff";
    mctx.lineWidth = 1.5;
    mctx.beginPath();
    mctx.arc(p.x, p.y, 6, 0, Math.PI * 2);
    mctx.stroke();
    mctx.beginPath();
    mctx.moveTo(p.x - 12, p.y); mctx.lineTo(p.x - 7, p.y);
    mctx.moveTo(p.x + 7, p.y);  mctx.lineTo(p.x + 12, p.y);
    mctx.moveTo(p.x, p.y - 12); mctx.lineTo(p.x, p.y - 7);
    mctx.moveTo(p.x, p.y + 7);  mctx.lineTo(p.x, p.y + 12);
    mctx.stroke();
    mctx.fillStyle = "rgba(255,255,255,0.9)";
    mctx.beginPath();
    mctx.arc(p.x, p.y, 2, 0, Math.PI * 2);
    mctx.fill();
    mctx.restore();
  }

  // scale bar
  drawScaleBar(W, H, s);

  // meridian hint: +y is the 0 degree meridian in this projection
  mctx.font = "10.5px ui-monospace, monospace";
  mctx.textAlign = "right";
  mctx.fillStyle = "rgba(160,170,186,0.55)";
  if (state.raster.globe) {
    mctx.fillText("southern hemisphere · global texture · zoom in for terrain detail", W - 12, H - 44);
  } else {
    mctx.fillText("0°E ↑   90°E →", W - 12, H - 44);
  }
}

function bodyMapXY(subLL) {
  // south-polar stereographic forward projection of a lat/lon sub-point
  const rho = 2 * R_MOON * Math.tan(Math.PI / 4 + (subLL[0] * D2R) / 2);
  return { x: rho * Math.sin(subLL[1] * D2R), y: rho * Math.cos(subLL[1] * D2R) };
}

function drawBodies(W, H, sub) {
  const bodies = [
    { name: "Sun", ll: sub.sun, color: C.sun },
    { name: "Earth", ll: sub.earth, color: C.earth },
  ];
  for (const b of bodies) {
    if (!Array.isArray(b.ll) || b.ll.length !== 2 || !Number.isFinite(b.ll[0])) continue;
    const m = bodyMapXY(b.ll);
    let p = worldToScreen(m.x, m.y);
    const margin = 34;
    const inside = p.x > margin && p.x < W - margin && p.y > margin && p.y < H - margin;
    if (!inside) {
      // pin to the frame edge along the same bearing from the view centre
      const c0 = { x: W / 2, y: H / 2 };
      const dx = p.x - c0.x;
      const dy = p.y - c0.y;
      const kx = dx === 0 ? Infinity : (W / 2 - margin) / Math.abs(dx);
      const ky = dy === 0 ? Infinity : (H / 2 - margin) / Math.abs(dy);
      const k = Math.min(kx, ky);
      p = { x: c0.x + dx * k, y: c0.y + dy * k };
    }
    mctx.save();
    mctx.shadowColor = b.color;
    mctx.shadowBlur = inside ? 14 : 6;
    mctx.fillStyle = b.color;
    mctx.beginPath();
    mctx.arc(p.x, p.y, inside ? 8 : 5, 0, Math.PI * 2);
    mctx.fill();
    mctx.restore();
    mctx.strokeStyle = b.color;
    mctx.lineWidth = 1.2;
    mctx.beginPath();
    mctx.arc(p.x, p.y, inside ? 12 : 8, 0, Math.PI * 2);
    mctx.stroke();
    mctx.font = "600 12px ui-sans-serif, system-ui, sans-serif";
    mctx.textAlign = "center";
    mctx.textBaseline = "bottom";
    mctx.fillStyle = "rgba(8,9,11,0.9)"; // readability against the lit surface
    mctx.fillText(b.name, p.x + 1, p.y - 14);
    mctx.fillStyle = b.color;
    mctx.fillText(b.name, p.x, p.y - 15);
  }
}

function drawScaleBar(W, H, s) {
  const target = W * 0.22; // px
  const raw = target / s; // metres
  const pow = Math.pow(10, Math.floor(Math.log10(raw)));
  const mult = [1, 2, 5, 10].find((m) => m * pow >= raw) || 10;
  const len = mult * pow;
  const px = len * s;
  const x = W - 16 - px;
  const y = H - 22;

  mctx.save();
  mctx.strokeStyle = "rgba(223,227,234,0.8)";
  mctx.fillStyle = "rgba(223,227,234,0.8)";
  mctx.lineWidth = 1.2;
  mctx.beginPath();
  mctx.moveTo(x, y - 5); mctx.lineTo(x, y);
  mctx.lineTo(x + px, y); mctx.lineTo(x + px, y - 5);
  mctx.stroke();
  mctx.font = "11px ui-monospace, monospace";
  mctx.textAlign = "center";
  mctx.textBaseline = "alphabetic";
  const label = len >= 1000 ? len / 1000 + " km" : len + " m";
  mctx.fillText(label, x + px / 2, y - 8);
  mctx.restore();
}

/* ------------------------------------------------------------- map events */

let drag = null;

mapCanvas.addEventListener("mousedown", (e) => {
  drag = { x: e.clientX, y: e.clientY, cx: state.view.cx, cy: state.view.cy, moved: 0 };
  mapCanvas.classList.add("dragging");
});

window.addEventListener("mousemove", (e) => {
  if (drag) {
    const dx = e.clientX - drag.x;
    const dy = e.clientY - drag.y;
    drag.moved = Math.max(drag.moved, Math.hypot(dx, dy));
    const s = pxPerM();
    state.view.cx = drag.cx - dx / s;
    state.view.cy = drag.cy + dy / s;
    clampView();
    drawMap();
    scheduleRaster(90);
  }
});

window.addEventListener("mouseup", (e) => {
  if (!drag) return;
  const wasClick = drag.moved < 4;
  mapCanvas.classList.remove("dragging");
  drag = null;
  if (wasClick && e.target === mapCanvas) {
    const rect = mapCanvas.getBoundingClientRect();
    const w = screenToWorld(e.clientX - rect.left, e.clientY - rect.top);
    moveObserver(w.x, w.y, true);
  }
});

/* Trackpads: two-finger scroll PANS (content follows the fingers, like
   every map app); a pinch (ctrl+wheel) or a real mouse wheel ZOOMS.
   Pixel-mode deltas with small steps are the trackpad signature; line/page
   modes and big jumps are a mouse wheel. */
let wheelZAccum = 0;
let wheelZPt = null;
let wheelRaf = null;
function applyWheelZoom() {
  wheelRaf = null;
  if (!wheelZPt) return;
  const pt = wheelZPt;
  const dy = clamp(wheelZAccum, -120, 120); // cap per frame; pinch deltas add up
  wheelZAccum = 0;
  if (dy === 0) return;
  const before = screenToWorld(pt.x, pt.y);
  const factor = Math.exp(dy * 0.0022);
  state.view.span = clamp(state.view.span * factor, 600, 8_000_000);
  const after = screenToWorld(pt.x, pt.y);
  state.view.cx += before.x - after.x;
  state.view.cy += before.y - after.y;
  clampView();
  drawMap();
  scheduleRaster();
}
mapCanvas.addEventListener(
  "wheel",
  (e) => {
    e.preventDefault();
    const rect = mapCanvas.getBoundingClientRect();
    const pt = { x: e.clientX - rect.left, y: e.clientY - rect.top };
    let d = e.deltaY;
    if (e.deltaMode === 1) d *= 16;        // lines (Firefox)
    else if (e.deltaMode === 2) d *= 100;  // pages
    const discrete = e.deltaMode !== 0 || Math.abs(d) >= 50;
    if (e.ctrlKey || discrete) {
      // zoom: every delta counts, applied once per frame. A mouse notch is
      // ~100; pinch deltas are tiny, so they are amplified to match.
      wheelZAccum += discrete ? d : d * 4;
      wheelZPt = pt;
      if (wheelRaf === null) wheelRaf = requestAnimationFrame(applyWheelZoom);
    } else {
      // two-finger scroll: pan
      const s = pxPerM();
      state.view.cx -= e.deltaX / s;
      state.view.cy += e.deltaY / s;
      clampView();
      drawMap();
      scheduleRaster();
    }
  },
  { passive: false }
);

mapCanvas.addEventListener("mousemove", (e) => {
  const rect = mapCanvas.getBoundingClientRect();
  const w = screenToWorld(e.clientX - rect.left, e.clientY - rect.top);
  const ll = xyToLonLat(w.x, w.y);
  $("#cursor-readout").textContent = `${fmtLat(ll.lat)}  ${fmtLon(ll.lon)}  ·  ${fmtNum(w.x)} / ${fmtNum(w.y)} m`;
});

mapCanvas.addEventListener("mouseleave", () => {
  $("#cursor-readout").textContent = "—";
});

/* Exact inverse of the DEM's south polar stereographic (checked against pyproj
   in scripts/selfcheck.py, so the browser and the server cannot disagree). */
function xyToLonLat(x, y) {
  const rho = Math.hypot(x, y);
  if (rho === 0) return { lon: 0, lat: -90 };
  const lat = (2 * Math.atan(rho / (2 * R_MOON)) - Math.PI / 2) / D2R;
  const lon = Math.atan2(x, y) / D2R;
  return { lon, lat };
}

/* The same projection, forwards, for typed coordinates. */
function lonLatToXY(lat, lon) {
  const rho = 2 * R_MOON * Math.tan(Math.PI / 4 + (lat * D2R) / 2);
  return { x: rho * Math.sin(lon * D2R), y: rho * Math.cos(lon * D2R) };
}

/* ---------------------------------------------------------------- charts */

function sizeCanvas(canvas, cssH) {
  const cssW = canvas.clientWidth || canvas.parentElement.clientWidth;
  const d = dpr();
  const w = Math.max(120, Math.round(cssW * d));
  const h = Math.round(cssH * d);
  if (canvas.width !== w || canvas.height !== h) {
    canvas.width = w;
    canvas.height = h;
  }
  canvas.style.height = cssH + "px";
  const ctx = canvas.getContext("2d");
  ctx.setTransform(d, 0, 0, d, 0, 0);
  ctx.clearRect(0, 0, cssW, cssH);
  return { ctx, W: cssW, H: cssH };
}

function drawSkyline() {
  const canvas = $("#skyline");
  const { ctx, W, H } = sizeCanvas(canvas, 280);
  const sky = state.skyline;
  if (!sky) {
    placeholder(ctx, W, H, "no horizon yet");
    return;
  }

  const cx = W / 2;
  const cy = H / 2 + 4;
  const R = Math.min(W, H) / 2 - 26;
  const elMin = Math.min(-6, Math.floor(arrMin(sky.elev)) - 1);
  const maxEl = Math.max(6, Math.ceil(sky.max_elev) + 2);
  const rOf = (el) => ((el - elMin) / (maxEl - elMin)) * R;

  ctx.font = "10px ui-monospace, monospace";
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";

  // rings
  for (let el = Math.ceil(elMin / 5) * 5; el <= maxEl; el += 5) {
    const r = rOf(el);
    if (r < 2) continue;
    ctx.beginPath();
    ctx.arc(cx, cy, r, 0, Math.PI * 2);
    ctx.strokeStyle = el === 0 ? "#3b4454" : C.grid;
    ctx.lineWidth = el === 0 ? 1.2 : 0.8;
    ctx.stroke();
    if (el % 10 === 0) {
      ctx.fillStyle = C.dim;
      ctx.textAlign = "left";
      ctx.fillText(el + "°", cx + 3, cy - r);
      ctx.textAlign = "center";
    }
  }

  // spokes + cardinal labels
  const labels = [["N", 0], ["E", 90], ["S", 180], ["W", 270]];
  for (const [name, az] of labels) {
    const a = (az - 90) * D2R;
    ctx.beginPath();
    ctx.moveTo(cx, cy);
    ctx.lineTo(cx + Math.cos(a) * R, cy + Math.sin(a) * R);
    ctx.strokeStyle = "#20262f";
    ctx.lineWidth = 0.8;
    ctx.stroke();
    ctx.fillStyle = C.dim;
    ctx.fillText(name, cx + Math.cos(a) * (R + 12), cy + Math.sin(a) * (R + 12));
  }

  // horizon polygon
  const n = sky.az.length;
  ctx.beginPath();
  for (let i = 0; i < n; i++) {
    const a = (sky.az[i] - 90) * D2R;
    const r = Math.max(0, rOf(sky.elev[i]));
    const x = cx + Math.cos(a) * r;
    const y = cy + Math.sin(a) * r;
    if (i === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  }
  ctx.closePath();
  ctx.fillStyle = "rgba(127,143,166,0.22)";
  ctx.fill();
  ctx.strokeStyle = "#aab6c8";
  ctx.lineWidth = 1.3;
  ctx.stroke();

  // highest obstacle
  const aMax = (sky.max_az - 90) * D2R;
  const rMax = rOf(sky.max_elev);
  ctx.beginPath();
  ctx.arc(cx + Math.cos(aMax) * rMax, cy + Math.sin(aMax) * rMax, 3, 0, Math.PI * 2);
  ctx.fillStyle = "#ffffff";
  ctx.fill();

  // current Sun and Earth directions
  const cur = currentAngles();
  if (cur) {
    for (const [key, colour, label] of [["sun", C.sun, "Sun"], ["earth", C.earth, "Earth"]]) {
      const v = cur[key];
      const a = (v.az - 90) * D2R;
      const el = clamp(v.el, elMin, maxEl);
      const r = rOf(el);
      const x = cx + Math.cos(a) * r;
      const y = cy + Math.sin(a) * r;
      ctx.beginPath();
      ctx.arc(x, y, 4.5, 0, Math.PI * 2);
      ctx.fillStyle = colour;
      ctx.fill();
      ctx.strokeStyle = "#0b0d11";
      ctx.lineWidth = 1;
      ctx.stroke();
      ctx.fillStyle = colour;
      ctx.font = "10px ui-monospace, monospace";
      ctx.fillText(label, x, y - 10);
    }
  }
}

function currentAngles() {
  const e = state.eph;
  if (!e || !e.t) return null;
  let i = 0;
  // while scrubbing or playing, the skyline markers follow the playback step
  if (playback.i !== null && playback.i < e.t.length) i = playback.i;
  else if (state.hover !== null && state.hover < e.t.length) i = state.hover;
  return {
    sun: { el: e.sun_el[i], az: e.sun_az[i] },
    earth: { el: e.earth_el[i], az: e.earth_az[i] },
    t: e.t[i],
  };
}

/* First moment the state flips, measured from the start of the window.
   This is the "how long until sunset / Earthset" answer. */
function nextEventInfo(e, body) {
  const flags = body === "sun" ? e.lit : e.earth_vis;
  if (!flags || flags.length < 2) return null;
  const up = !!flags[0];
  for (let i = 1; i < flags.length; i++) {
    if (!!flags[i] !== up) {
      return {
        up,
        event: up ? "set" : "rise",
        hours: (parseUTC(e.t[i]) - parseUTC(e.t[0])) / 3.6e6,
        at: e.t[i],
      };
    }
  }
  return { up, event: null, hours: null, at: null };
}

function eventText(info, body) {
  const name = body === "sun" ? "Sun" : "Earth";
  if (!info) return "—";
  if (!info.event) return `no ${name.toLowerCase()}${info.up ? "set" : "rise"} in window`;
  return `${name}${info.event} in ${fmtDuration(info.hours)}`;
}

function renderNowcast() {
  const box = $("#nowcast");
  if (!box) return;
  const e = state.eph;
  if (!e) {
    box.innerHTML = "";
    return;
  }
  const sun = nextEventInfo(e, "sun");
  const earth = nextEventInfo(e, "earth");
  const sunUp = !!e.lit[0];
  const earthUp = !!e.earth_vis[0];

  const sunNext = sun && sun.event
    ? `Sun${sun.event} in ${fmtDuration(sun.hours)}`
    : (sunUp ? "no sunset in this window" : "no sunrise in this window");
  const earthNext = earth && earth.event
    ? `Earth${earth.event} in ${fmtDuration(earth.hours)}`
    : (earthUp ? "no earthset in this window" : "no earthrise in this window");

  box.innerHTML =
    `<div class="now-item" title="State at the start of the selected window">
       <span class="k">Sun</span>
       <span class="v ${sunUp ? "good" : "warn"}">${sunUp ? "sunlit" : "in shadow"} · ${e.sun_el[0].toFixed(2)}°</span>
       <span class="sub">${sunNext}</span>
     </div>
     <div class="now-item" title="State at the start of the selected window">
       <span class="k">Earth</span>
       <span class="v ${earthUp ? "good" : "warn"}">${earthUp ? "visible" : "hidden"} · ${e.earth_el[0].toFixed(2)}°</span>
       <span class="sub">${earthNext}</span>
     </div>`;
}

function placeholder(ctx, W, H, text) {
  ctx.fillStyle = C.dim;
  ctx.font = "12px ui-sans-serif, system-ui, sans-serif";
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.fillText(text, W / 2, H / 2);
}

function drawTimeline() {
  const canvas = $("#timeline");
  const { ctx, W, H } = sizeCanvas(canvas, 226);
  const e = state.eph;
  if (!e || !e.t || e.t.length < 2) {
    placeholder(ctx, W, H, "no ephemeris yet");
    return;
  }

  const padL = 40, padR = 10, padT = 10, padB = 40;
  const plotW = W - padL - padR;
  const plotH = H - padT - padB;
  const n = e.t.length;

  const lo = Math.min(arrMin(e.sun_el), arrMin(e.earth_el), arrMin(e.sun_sky), arrMin(e.earth_sky), -2);
  const hi = Math.max(arrMax(e.sun_el), arrMax(e.earth_el), arrMax(e.sun_sky), arrMax(e.earth_sky), 2);
  const yMin = Math.floor(lo - 1);
  const yMax = Math.ceil(hi + 1);
  const xOf = (i) => padL + (i / (n - 1)) * plotW;
  const yOf = (v) => padT + ((yMax - v) / (yMax - yMin)) * plotH;

  // lit / visible background bands
  ctx.save();
  ctx.beginPath();
  ctx.rect(padL, padT, plotW, plotH);
  ctx.clip();

  for (const [flags, colour] of [[e.lit, "rgba(245,181,68,0.09)"], [e.earth_vis, "rgba(86,200,216,0.09)"]]) {
    let start = null;
    for (let i = 0; i < n; i++) {
      if (flags[i] && start === null) start = i;
      if ((!flags[i] || i === n - 1) && start !== null) {
        const end = flags[i] ? i : i - 1;
        ctx.fillStyle = colour;
        ctx.fillRect(xOf(start), padT, xOf(end) - xOf(start) + 1, plotH);
        start = null;
      }
    }
  }
  ctx.restore();

  // grid
  const step = niceStep(yMax - yMin, 5);
  ctx.font = "10px ui-monospace, monospace";
  ctx.textAlign = "right";
  ctx.textBaseline = "middle";
  for (let v = Math.ceil(yMin / step) * step; v <= yMax; v += step) {
    const y = yOf(v);
    ctx.beginPath();
    ctx.moveTo(padL, y);
    ctx.lineTo(padL + plotW, y);
    ctx.strokeStyle = v === 0 ? "#3b4454" : C.grid;
    ctx.lineWidth = v === 0 ? 1.2 : 0.7;
    ctx.stroke();
    ctx.fillStyle = C.dim;
    ctx.fillText(v + "°", padL - 6, y);
  }

  // time axis
  const spanHours = (parseUTC(e.t[n - 1]) - parseUTC(e.t[0])) / 3.6e6;
  const ticks = Math.max(3, Math.min(7, Math.floor(plotW / 90)));
  ctx.textAlign = "center";
  ctx.textBaseline = "top";
  for (let k = 0; k <= ticks; k++) {
    const i = Math.round((k / ticks) * (n - 1));
    const x = xOf(i);
    ctx.beginPath();
    ctx.moveTo(x, padT + plotH);
    ctx.lineTo(x, padT + plotH + 4);
    ctx.strokeStyle = C.axis;
    ctx.lineWidth = 0.8;
    ctx.stroke();
    ctx.fillStyle = C.dim;
    ctx.textAlign = k === 0 ? "left" : k === ticks ? "right" : "center";
    const label = spanHours <= 48 ? fmtTime(e.t[i], true) : fmtDay(e.t[i]);
    ctx.fillText(label, x, padT + plotH + 7);
  }

  // horizon thresholds + elevations
  const line = (arr, colour, width, dash) => {
    ctx.save();
    ctx.beginPath();
    ctx.setLineDash(dash || []);
    for (let i = 0; i < n; i++) {
      const x = xOf(i);
      const y = yOf(arr[i]);
      i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
    }
    ctx.strokeStyle = colour;
    ctx.lineWidth = width;
    ctx.stroke();
    ctx.restore();
  };

  line(e.sun_sky, "rgba(245,181,68,0.45)", 1, [3, 3]);
  line(e.earth_sky, "rgba(86,200,216,0.45)", 1, [3, 3]);
  line(e.sun_el, C.sun, 1.7);
  line(e.earth_el, C.earth, 1.7);

  // hover crosshair
  if (state.hover !== null && state.hover < n) {
    const x = xOf(state.hover);
    ctx.beginPath();
    ctx.moveTo(x, padT);
    ctx.lineTo(x, padT + plotH);
    ctx.strokeStyle = "rgba(223,227,234,0.45)";
    ctx.lineWidth = 1;
    ctx.stroke();
    for (const [arr, colour] of [[e.sun_el, C.sun], [e.earth_el, C.earth]]) {
      ctx.beginPath();
      ctx.arc(x, yOf(arr[state.hover]), 3, 0, Math.PI * 2);
      ctx.fillStyle = colour;
      ctx.fill();
    }
  }

  // playback cursor: a vertical line at the active step, with a dot where the
  // Sun sits. With playback.i null nothing is drawn, exactly as before.
  if (playback.i !== null && playback.i < n) {
    const x = xOf(playback.i);
    ctx.beginPath();
    ctx.moveTo(x, padT);
    ctx.lineTo(x, padT + plotH);
    ctx.strokeStyle = C.sun;
    ctx.lineWidth = 1;
    ctx.stroke();
    for (const [arr, colour] of [[e.sun_el, C.sun], [e.earth_el, C.earth]]) {
      ctx.beginPath();
      ctx.arc(x, yOf(arr[playback.i]), 3, 0, Math.PI * 2);
      ctx.fillStyle = colour;
      ctx.fill();
    }
  }
}

/* --------------------------------------------------------------- readouts */

function statHTML(items) {
  return items
    .map(([k, v, cls]) => `<div class="stat"><span class="k">${k}</span><span class="v ${cls || ""}">${v}</span></div>`)
    .join("");
}

function renderSiteCard() {
  const o = state.observer;
  if (!o) return;
  $("#v-roi").textContent = state.roi ? `R${state.roi.index}` : "custom point";
  $("#v-site").textContent = state.site ? `${state.site.id} · ${state.site.name}` : "—";
  $("#v-lat").textContent = fmtLat(o.lat);
  $("#v-lon").textContent = fmtLon(o.lon);
  $("#v-height").textContent = o.height === null || o.height === undefined ? "—" : fmtNum(o.height, 1) + " m";
  $("#v-area").textContent = state.roi ? fmtNum(state.roi.area_km2, 4) + " km²" : "—";
  $("#v-xy").textContent = `${fmtNum(o.x)} / ${fmtNum(o.y)} m`;
  const r = Math.hypot(o.x, o.y);
  $("#v-pole").textContent = fmtNum(r / 1000, 2) + " km";

  // keep the coordinate boxes in step, unless the user is typing in them
  const latEl = $("#coord-lat");
  const lonEl = $("#coord-lon");
  if (latEl && document.activeElement !== latEl) latEl.value = o.lat.toFixed(3);
  if (lonEl && document.activeElement !== lonEl) lonEl.value = o.lon.toFixed(3);
}

function renderSkyline() {
  drawSkyline();
  const s = state.skyline;
  if (!s) return;

  // A direction is only "blocked" if the ridge is higher than the Sun ever
  // gets. Comparing against 0 degrees would call a ridge-top site fully
  // blocked, which is the opposite of the truth.
  const sunMax = state.eph?.stats?.sun_max_el;
  const blockedShare =
    sunMax === undefined ? null : s.elev.filter((v) => v > sunMax).length / s.elev.length;

  $("#skyline-stats").innerHTML = statHTML([
    ["highest obstacle", `${s.max_elev.toFixed(2)}° @ ${Math.round(s.max_az)}°`, s.max_elev > 5 ? "warn" : ""],
    ["mean horizon", s.mean_elev.toFixed(2) + "°"],
    [
      "above Sun's max",
      blockedShare === null ? "—" : Math.round(blockedShare * 100) + "%",
      blockedShare === null ? "" : blockedShare > 0 ? "warn" : "good",
    ],
    ["observer height", fmtNum(s.observer_height, 1) + " m"],
  ]);

  let note =
    `Horizon computed out to ${s.maxdist / 1000} km from a ${s.mast} m mast. ` +
    `Highest ridge is ${s.max_elev.toFixed(2)}° at ${Math.round(s.max_az)}°. `;
  if (blockedShare === null) {
    note += "";
  } else if (blockedShare === 0) {
    note += `The Sun (peaking at ${sunMax.toFixed(2)}°) clears the terrain in every direction.`;
  } else {
    note += `${Math.round(blockedShare * 100)}% of directions stay in permanent shadow ` +
      `because the ridge rises above the Sun's highest point (${sunMax.toFixed(2)}°).`;
  }
  $("#skyline-note").textContent = note;
}

function renderEphemeris() {
  drawTimeline();
  const e = state.eph;
  if (!e) return;
  renderNowcast();
  const st = e.stats;
  $("#eph-stats").innerHTML = statHTML([
    ["sunlit", st.sunlit_pct.toFixed(1) + "%", st.sunlit_pct > 50 ? "good" : "warn"],
    ["earth visible", st.earth_vis_pct.toFixed(1) + "%", st.earth_vis_pct > 50 ? "good" : "warn"],
    ["sunlit + linked", st.both_pct.toFixed(1) + "%"],
    ["longest sunlit run", fmtDuration(st.longest_sunlit_h)],
    ["sun elevation", `${st.sun_min_el.toFixed(1)}° … ${st.sun_max_el.toFixed(1)}°`],
    ["earth elevation", `${st.earth_min_el.toFixed(1)}° … ${st.earth_max_el.toFixed(1)}°`],
    ["sampling", `${st.step_min} min × ${st.samples}`],
  ]);

  const rows = e.transitions.slice(0, 8).map((tr) => {
    const what = tr.what === "sun" ? "Sun" : "Earth";
    const dir = tr.to === "up" ? "rises above terrain" : "sets behind terrain";
    return `<div class="tr"><b>${fmtTime(tr.t)}</b><span>${what} ${dir}</span></div>`;
  });
  $("#transitions").innerHTML = rows.join("") ||
    `<div class="tr"><span>No horizon crossings here. Near the poles the Sun can stay up ` +
    `for weeks — try a date near an equinox (March or September) to watch it cross.</span></div>`;
}


/* ------------------------------------------------------------- playback */

/* Sub-point for a body at one ephemeris step, from the observer-relative look
   angles in state.eph: starting at the observer's lat/lon, walk the great
   circle along the body's azimuth for 90 - elevation degrees (the same
   geo.destination formula the server uses). Because the look angles come from
   a FIXED observer, this matches exactly what the 2D charts show; for an
   observer far from the sub-point it is only an approximation of the body's
   true direction, but over a window it is consistent with the charts, which
   is what the playback needs. */
function subPointFromAngles(elDeg, azDeg) {
  const lat1 = state.observer.lat * D2R;
  const lon1 = state.observer.lon * D2R;
  const th = azDeg * D2R;
  const dl = (90 - elDeg) * D2R;
  const lat2 = Math.asin(Math.sin(lat1) * Math.cos(dl) + Math.cos(lat1) * Math.sin(dl) * Math.cos(th));
  const lon2 = lon1 + Math.atan2(
    Math.sin(th) * Math.sin(dl) * Math.cos(lat1),
    Math.cos(dl) - Math.sin(lat1) * Math.sin(lat2)
  );
  return [lat2 / D2R, lon2 / D2R];
}

function renderPlayback() {
  const btn = $("#play");
  const slider = $("#scrub");
  const out = $("#scrub-readout");
  const e = state.eph;
  const usable = e && e.t && e.t.length >= 2;
  btn.disabled = slider.disabled = !usable;
  if (!usable) {
    slider.max = 0;
    slider.value = 0;
    out.textContent = "—";
    return;
  }
  slider.max = e.t.length - 1;
  slider.value = playback.i ?? 0;
  if (playback.i === null) {
    out.textContent = "—";
    return;
  }
  const i = playback.i;
  const deg = (v) => (v >= 0 ? "+" : "−") + Math.abs(v).toFixed(1) + "°";
  out.textContent =
    `${fmtTime(e.t[i], true)} · Sun ${deg(e.sun_el[i])} ${e.lit[i] ? "up" : "hidden"}` +
    ` · Earth ${deg(e.earth_el[i])} ${e.earth_vis[i] ? "visible" : "hidden"}`;
}

function setPlayback(i) {
  const e = state.eph;
  if (!e || !e.t || !e.t.length) return;
  playback.i = clamp(Math.round(i), 0, e.t.length - 1);
  drawTimeline();  // re-render with the cursor line at the new step
  drawSkyline();   // skyline markers follow the same step via currentAngles()
  renderPlayback();
  // re-light the map for the new step. While PLAYING, the map interval in
  // startPlaybackTimer() owns this (a debounce here would be reset by every
  // tick and starve - the map would only move when paused). While scrubbing
  // a paused bar, a short debounce bundles fast drags.
  if (playback.timer === null) {
    clearTimeout(playback.mapTimer);
    playback.mapTimer = setTimeout(scheduleRaster, 200);
  }
  // move the 3D bodies only when they are on screen; the sub-point maths is
  // pure and costs nothing to skip
  if (state.view3d && globe3d) {
    globe3d.update(
      subPointFromAngles(e.sun_el[playback.i], e.sun_az[playback.i]),
      subPointFromAngles(e.earth_el[playback.i], e.earth_az[playback.i])
    );
  }
}

function stopPlaybackTimer() {
  if (playback.timer === null) return;
  clearInterval(playback.timer);
  playback.timer = null;
  if (playback.mapInterval !== null) {
    clearInterval(playback.mapInterval);
    playback.mapInterval = null;
  }
  $("#play").textContent = "▶ Play";
  // render the step we paused on right away, not 200 ms from now
  scheduleRaster();
}

/* One code path for (re)arming the tick interval, so the speed control can
   restart it mid-run and the play button can start it fresh. */
function startPlaybackTimer() {
  if (playback.timer !== null) clearInterval(playback.timer);
  $("#play").textContent = "⏸ Pause";
  playback.timer = setInterval(() => {
    const n = state.eph.t.length;
    setPlayback(playback.i >= n - 1 ? 0 : playback.i + 1); // loop at the end
  }, 1000 / (playback.fps * playback.speed));
  // the map is re-rendered server-side, so it follows at its own slower
  // cadence while the charts and the 3D bodies run at full frame rate
  if (playback.mapInterval === null) {
    playback.mapInterval = setInterval(() => scheduleRaster(), 400);
  }
}

function togglePlayback() {
  if (playback.timer !== null) {
    stopPlaybackTimer(); // pausing keeps playback.i so the cursor stays put
    return;
  }
  const e = state.eph;
  if (!e || !e.t || e.t.length < 2) return;
  if (playback.i === null) playback.i = 0;
  startPlaybackTimer();
}

/* Leave playback entirely: stop the timer, clear the cursor, redraw clean. */
function stopPlayback() {
  stopPlaybackTimer();
  clearTimeout(playback.mapTimer);
  playback.i = null;
  drawTimeline();
  drawSkyline();
  renderPlayback();
}

function bindPlaybackUI() {
  $("#play").addEventListener("click", togglePlayback);
  $("#playback-speed").addEventListener("change", (e) => {
    playback.speed = Number(e.target.value) || 1;
    // if the timer is running, re-arm it so the new speed applies immediately
    if (playback.timer !== null) startPlaybackTimer();
  });
  $("#scrub").addEventListener("input", (e) => {
    stopPlaybackTimer(); // dragging the scrubber pauses, but keeps the step
    setPlayback(Number(e.target.value));
  });
  renderPlayback();
}

/* ------------------------------------------------------------- scenarios */

const SCENARIO_KEY = "lunar.scenarios.v1";
const SCENARIO_MAX = 6;

function scenarioKey(sc) {
  return [sc.x.toFixed(1), sc.y.toFixed(1), sc.start, sc.hours, sc.mast, sc.maxdist].join("|");
}

function captureScenario() {
  const o = state.observer;
  const e = state.eph;
  if (!o || !e || !state.skyline || state.stale) return null;
  const st = e.stats;
  const sc = {
    label: state.roi ? `${state.site.id} · R${state.roi.index}` : "Custom point",
    site_id: state.site?.id || null,
    roi_id: state.roi?.id || null,
    x: o.x, y: o.y, lon: o.lon, lat: o.lat, height: o.height,
    start: state.params.start,
    hours: state.params.hours,
    step_min: state.params.step_min,
    mast: state.params.mast,
    maxdist: state.params.maxdist,
    sunlit: st.sunlit_pct,
    earth: st.earth_vis_pct,
    both: st.both_pct,
    longest: st.longest_sunlit_h,
    sun_min: st.sun_min_el,
    sun_max: st.sun_max_el,
    earth_min: st.earth_min_el,
    earth_max: st.earth_max_el,
    sun_next: nextEventInfo(e, "sun"),
    earth_next: nextEventInfo(e, "earth"),
  };
  sc.key = scenarioKey(sc);
  return sc;
}

function persistScenarios() {
  try {
    localStorage.setItem(SCENARIO_KEY, JSON.stringify(state.scenarios));
  } catch { /* private mode or full storage */ }
}

function restoreScenarios() {
  try {
    const raw = localStorage.getItem(SCENARIO_KEY);
    if (!raw) return;
    const arr = JSON.parse(raw);
    if (!Array.isArray(arr)) return;
    state.scenarios = arr
      .filter((s) => s && Number.isFinite(s.x) && Number.isFinite(s.sunlit))
      .slice(0, SCENARIO_MAX)
      .map((s) => ({ ...s, key: s.key || scenarioKey(s) }));
  } catch {
    state.scenarios = [];
  }
}

function saveScenario() {
  const sc = captureScenario();
  if (!sc) {
    setPill("nothing to save yet", "bad");
    return;
  }
  const i = state.scenarios.findIndex((s) => s.key === sc.key);
  if (i >= 0) state.scenarios[i] = sc;
  else {
    state.scenarios.push(sc);
    if (state.scenarios.length > SCENARIO_MAX) state.scenarios.shift();
  }
  persistScenarios();
  renderScenarios();

  const btn = $("#save-scenario");
  btn.textContent = i >= 0 ? "Updated ✓" : "Saved ✓";
  clearTimeout(saveScenario._t);
  saveScenario._t = setTimeout(() => { btn.textContent = "Save current"; }, 1400);
}

function renderScenarios() {
  const list = $("#scenario-list");
  const compareBtn = $("#open-compare");
  if (!list || !compareBtn) return;
  if (!state.scenarios.length) {
    list.innerHTML =
      `<p class="note muted">No saved scenarios yet. Set a site and date, then press “Save current”.</p>`;
    compareBtn.disabled = true;
    return;
  }
  list.innerHTML = state.scenarios.map((s) => `
    <div class="scenario">
      <div class="scenario-main">
        <b>${escapeHtml(s.label || "Scenario")}</b>
        <span>${fmtTime(s.start)} · sunlit ${Number(s.sunlit).toFixed(0)}% · Earth ${Number(s.earth).toFixed(0)}%</span>
      </div>
      <div class="scenario-btns">
        <button data-load="${escapeHtml(s.key)}" title="Load this scenario">Load</button>
        <button data-del="${escapeHtml(s.key)}" class="danger" title="Remove this scenario">×</button>
      </div>
    </div>`).join("");
  compareBtn.disabled = state.scenarios.length < 2;
}

function loadScenario(sc) {
  const site = state.sites.find((s) => s.id === sc.site_id) || state.sites[0];
  if (!site) return;

  state.site = site;
  $("#site-select").value = site.id;
  fillRoiSelect(site);

  state.params = {
    start: sc.start,
    hours: Number(sc.hours),
    step_min: Number(sc.step_min),
    mast: Number(sc.mast),
    maxdist: Number(sc.maxdist),
  };
  $("#start").value = String(sc.start).slice(0, 16);
  $("#hours").value = String(sc.hours);
  $("#step").value = String(sc.step_min);
  $("#mast").value = String(sc.mast);
  $("#maxdist").value = String(sc.maxdist);

  state.roi = sc.roi_id ? site.rois.find((r) => r.id === sc.roi_id) || null : null;
  $("#roi-select").value = state.roi ? state.roi.id : "custom";
  state.observer = {
    x: Number(sc.x), y: Number(sc.y),
    lon: Number(sc.lon), lat: Number(sc.lat),
    height: Number(sc.height),
  };

  state.view.cx = state.observer.x;
  state.view.cy = state.observer.y;
  if (state.view.span > 60_000) state.view.span = 8_000;
  clampView();
  drawMap();
  scheduleRaster(0);

  closeCompare();
  renderSiteCard();
  runAnalysis();
}

function bestColumn(values) {
  let best = -Infinity;
  let idx = -1;
  values.forEach((v, i) => {
    if (Number.isFinite(v) && v > best) { best = v; idx = i; }
  });
  if (idx < 0) return -1;
  // only mark a winner if it is unique, otherwise the colour means nothing
  return values.filter((v) => v === best).length === 1 ? idx : -1;
}

function renderCompareTable() {
  const body = $("#compare-body");
  const scs = state.scenarios;
  if (!body) return;
  if (scs.length < 2) {
    body.innerHTML = `<p class="lead">Save at least two scenarios to compare them.</p>`;
    return;
  }

  const rows = [
    ["Point", (s) => `${fmtLat(s.lat)}<br>${fmtLon(s.lon)}`],
    ["Start (UTC)", (s) => fmtTime(s.start)],
    ["Window", (s) => `${(Number(s.hours) / 24).toFixed(1)} d @ ${s.step_min} min`],
    ["Mast / horizon", (s) => `${s.mast} m / ${Number(s.maxdist) / 1000} km`],
    ["Sunlit", (s) => `${Number(s.sunlit).toFixed(1)}%`, "sunlit"],
    ["Earth visible", (s) => `${Number(s.earth).toFixed(1)}%`, "earth"],
    ["Sunlit + linked", (s) => `${Number(s.both).toFixed(1)}%`, "both"],
    ["Longest sunlit run", (s) => fmtDuration(Number(s.longest)), "longest"],
    ["Sun elevation", (s) => `${Number(s.sun_min).toFixed(1)}° … ${Number(s.sun_max).toFixed(1)}°`],
    ["Earth elevation", (s) => `${Number(s.earth_min).toFixed(1)}° … ${Number(s.earth_max).toFixed(1)}°`],
    ["Next Sun event", (s) => eventText(s.sun_next, "sun")],
    ["Next Earth event", (s) => eventText(s.earth_next, "earth")],
  ];

  const head = `<thead><tr><th>Metric</th>${scs.map((s) => `
    <th>${escapeHtml(s.label || "Scenario")}<span>${fmtTime(s.start)}</span>
      <button class="load-btn" data-load="${escapeHtml(s.key)}">Load</button>
    </th>`).join("")}</tr></thead>`;

  const bodyRows = rows.map(([name, fn, key]) => {
    const best = key ? bestColumn(scs.map((s) => Number(s[key]))) : -1;
    return `<tr><td>${name}</td>${scs.map((s, i) =>
      `<td class="${i === best ? "best" : ""}">${fn(s)}</td>`).join("")}</tr>`;
  }).join("");

  body.innerHTML =
    `<div class="compare-wrap">
       <table class="compare-table">${head}<tbody>${bodyRows}</tbody></table>
     </div>
     <p class="compare-note">Green marks the best value in each row. Times are measured
     from each scenario's own start. Scroll sideways if the table is wider than the window.</p>`;
}

function openCompare() {
  if (state.scenarios.length < 2) return;
  renderCompareTable();
  $("#compare-modal").hidden = false;
}

function closeCompare() {
  const modal = $("#compare-modal");
  if (modal) modal.hidden = true;
}

function bindScenarioUI() {
  $("#save-scenario").addEventListener("click", saveScenario);
  $("#open-compare").addEventListener("click", openCompare);
  $("#compare-close").addEventListener("click", closeCompare);
  $("#compare-modal").querySelector(".modal-backdrop").addEventListener("click", closeCompare);

  $("#scenario-list").addEventListener("click", (e) => {
    const load = e.target.closest("[data-load]");
    if (load) {
      const sc = state.scenarios.find((s) => s.key === load.dataset.load);
      if (sc) loadScenario(sc);
      return;
    }
    const del = e.target.closest("[data-del]");
    if (del) {
      state.scenarios = state.scenarios.filter((s) => s.key !== del.dataset.del);
      persistScenarios();
      renderScenarios();
    }
  });

  $("#compare-body").addEventListener("click", (e) => {
    const load = e.target.closest("[data-load]");
    if (!load) return;
    const sc = state.scenarios.find((s) => s.key === load.dataset.load);
    if (sc) loadScenario(sc);
  });

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !$("#compare-modal").hidden) closeCompare();
  });
}

/* --------------------------------------------------------------- actions */

function moveObserver(x, y, fromClick) {
  const dem = state.status?.dem?.bounds;
  if (dem && (x < dem.left || x > dem.right || y < dem.bottom || y > dem.top)) return;

  const ll = xyToLonLat(x, y);
  state.observer = { x, y, lon: ll.lon, lat: ll.lat, height: null };
  if (fromClick) {
    state.roi = null;
    const sel = $("#roi-select");
    if (sel) sel.value = "custom";
  }
  renderSiteCard();

  // fill in the exact height from the terrain file
  getJSON(`/api/point?x=${x.toFixed(1)}&y=${y.toFixed(1)}`)
    .then((p) => {
      if (!state.observer || state.observer.x !== x || state.observer.y !== y) return;
      state.observer.height = p.height;
      state.observer.lon = p.lon;
      state.observer.lat = p.lat;
      renderSiteCard();
    })
    .catch(() => {});

  runAnalysis();
}

let runToken = 0;
async function runAnalysis() {
  if (!state.observer) return;
  const token = ++runToken;
  const p = state.params;
  const o = state.observer;

  beginBusy("calculating…");
  const started = performance.now();

  const base = {
    x: o.x.toFixed(1),
    y: o.y.toFixed(1),
    maxdist: p.maxdist,
    mast: p.mast,
    az_step: 1,
  };

  try {
    const [sky, eph] = await Promise.all([
      getJSON("/api/skyline?" + new URLSearchParams(base)),
      getJSON(
        "/api/ephemeris?" +
          new URLSearchParams({
            ...base,
            start: p.start,
            hours: p.hours,
            step_min: p.step_min,
            min_earth_elev: 0,
          })
      ),
    ]);
    if (token !== runToken) return;

    // keep playback across the recompute: carry the bar position over as
    // a fraction of the window, so a new site or date continues the
    // animation seamlessly instead of resetting to 00:00
    const prev = state.eph;
    const frac = playback.i !== null && prev && prev.t && prev.t.length > 1
      ? playback.i / (prev.t.length - 1)
      : null;
    state.skyline = sky;
    state.eph = eph;
    if (frac !== null) {
      playback.i = clamp(Math.round(frac * (eph.t.length - 1)), 0, eph.t.length - 1);
      setPlayback(playback.i);
    }
    renderPlayback(); // now the scrubber is live for the new data
    scheduleRaster(); // re-light the map with the new window first step
    state.stale = false;
    state.hover = null;

    if (eph.site) {
      state.observer.lon = eph.site.lon;
      state.observer.lat = eph.site.lat;
      state.observer.height = eph.site.height;
    }
    renderSiteCard();
    renderSkyline();
    renderEphemeris();
    setPill("ready", "ok");
    const ms = Math.round(performance.now() - started);
    const clock = new Date().toISOString().slice(11, 19);
    $("#last-updated").textContent = `Updated ${clock} UTC · calculated in ${ms} ms`;
  } catch (err) {
    if (token !== runToken) return;
    console.error(err);
    setPill(err.message.slice(0, 60), "bad");
    $("#last-updated").textContent = "";
  } finally {
    if (token === runToken) {
      endBusy();
    }
  }
}

function fitRegion() {
  const rois = state.site?.rois || [];
  if (!rois.length) return;
  let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
  for (const r of rois) {
    for (const [x, y] of r.ring) {
      minX = Math.min(minX, x); maxX = Math.max(maxX, x);
      minY = Math.min(minY, y); maxY = Math.max(maxY, y);
    }
  }
  const w = maxX - minX, h = maxY - minY;
  const { W, H } = mapSize();
  const spanForW = w / 0.6;
  const spanForH = (h / 0.6) * (W / H);
  state.view.cx = (minX + maxX) / 2;
  state.view.cy = (minY + maxY) / 2;
  // 20 m/px data: below ~4 km the picture is pure upscaling
  state.view.span = clamp(Math.max(spanForW, spanForH, 4000), 600, 8_000_000);
  clampView();
  drawMap();
  scheduleRaster(0);
}

function defaultView() {
  const rois = state.site?.rois || [];
  if (!rois.length) return fitMap();
  let cx = 0, cy = 0;
  for (const r of rois) { cx += r.x; cy += r.y; }
  state.view.cx = cx / rois.length;
  state.view.cy = cy / rois.length;
  state.view.span = 8_000; // wide enough that the 20 m terrain is not upscaled to mush
  clampView();
  drawMap();
  scheduleRaster(0);
}

function fitMap() {
  const b = state.status?.dem?.bounds;
  if (!b) return;
  state.view.cx = (b.left + b.right) / 2;
  state.view.cy = (b.bottom + b.top) / 2;
  state.view.span = (b.right - b.left) * 1.02;
  clampView();
  drawMap();
  scheduleRaster(0);
}

/* ------------------------------------------------------------------- init */

function readParams() {
  const raw = $("#start").value;
  state.params.start = raw.length === 16 ? raw + ":00" : raw;
  state.params.hours = Number($("#hours").value);
  state.params.step_min = Number($("#step").value);
  state.params.mast = Number($("#mast").value);
  state.params.maxdist = Number($("#maxdist").value);
}

function fillSiteSelect() {
  const sel = $("#site-select");
  sel.innerHTML = "";
  for (const s of state.sites) {
    const opt = document.createElement("option");
    opt.value = s.id;
    opt.textContent = `${s.id} — ${s.name}`;
    sel.appendChild(opt);
  }
}

function fillRoiSelect(site) {
  const sel = $("#roi-select");
  sel.innerHTML = "";
  for (const r of site.rois) {
    const opt = document.createElement("option");
    opt.value = r.id;
    opt.textContent = `R${r.index} · ${r.area_km2.toFixed(3)} km² · ${fmtLat(r.lat)}`;
    sel.appendChild(opt);
  }
  const custom = document.createElement("option");
  custom.value = "custom";
  custom.textContent = "custom point";
  sel.appendChild(custom);
  sel.value = site.rois.length ? site.rois[0].id : "custom";
}

function selectRoi(roi) {
  state.roi = roi;
  if (!roi) return;
  state.observer = { x: roi.x, y: roi.y, lon: roi.lon, lat: roi.lat, height: roi.height };
  renderSiteCard();
  drawMap();
  runAnalysis();
}

function bindControls() {
  $("#site-select").addEventListener("change", (e) => {
    const site = state.sites.find((s) => s.id === e.target.value);
    if (!site) return;
    state.site = site;
    fillRoiSelect(site);
    fitRegion();
    selectRoi(site.rois[0] || null);
  });

  $("#roi-select").addEventListener("change", (e) => {
    if (e.target.value === "custom") return;
    const roi = state.site?.rois.find((r) => r.id === e.target.value);
    if (roi) selectRoi(roi);
  });

  for (const id of ["#start", "#hours", "#step", "#mast", "#maxdist"]) {
    $(id).addEventListener("change", () => {
      readParams();
      runAnalysis();
    });
  }

  $("#run").addEventListener("click", () => {
    readParams();
    runAnalysis();
  });

  $("#fit-region").addEventListener("click", fitRegion);
  $("#fit-map").addEventListener("click", fitMap);
  bindPlaybackUI();

  $("#view-3d").addEventListener("click", () => {
    state.view3d = !state.view3d;
    if (state.view3d) {
      // first switch loads the module on demand; if the vendor files were
      // never downloaded the import throws and we stay in a working 2D view
      init3D().catch((err) => {
        console.warn("3d:", err);
        state.view3d = false;
        setPill("3D unavailable (vendor files missing?)", "warn");
        applyViewMode(); // un-highlight the button, keep the 2D view
      });
    } else {
      applyViewMode();
    }
  });

  $("#recenter").addEventListener("click", () => {
    if (state.view3d) {
      globe3d.recenter();
      return;
    }
    state.view.cx = 0;
    state.view.cy = 0;
    if (state.view.span > 8_000_000 || state.view.span < 608_000) state.view.span = 608_000;
    clampView();
    drawMap();
    scheduleRaster();
  });
}

/* ------------------------------------------------------------ 3D view mode */

let globe3d = null;

async function init3D() {
  if (globe3d) {
    // module already loaded and constructed: just switch the view back on
    applyViewMode();
    return;
  }
  const canvas = $("#map3d");
  const mod = await import("./globe3d.js");
  globe3d = new mod.Globe3D(canvas);
  const sub = state.raster.sub;
  const haveSub = sub && sub.sun.length === 2 && Number.isFinite(sub.sun[0]) &&
                  sub.earth.length === 2 && Number.isFinite(sub.earth[0]);
  if (haveSub) {
    globe3d.update(sub.sun, sub.earth);
  } else {
    // never shown the globe view yet: get real sub-points for this date
    globe3d.refreshFromServer(state.params.start).catch((e) =>
      console.warn("3d: sub-points unavailable:", e.message)
    );
  }
  applyViewMode();
}

function applyViewMode() {
  const map2d = $("#map"), map3d = $("#map3d");
  const on = state.view3d;
  map2d.hidden = on;
  map3d.hidden = !on;
  $("#view-3d").classList.toggle("active", on);
  if (on) {
    globe3d.show();
    globe3d.recenter();
    // while playback is active, put the bodies on the current step instead of
    // the server sub-points that came with the last globe raster
    if (playback.i !== null) setPlayback(playback.i);
  } else { globe3d && globe3d.hide(); }
  // controls that belong to the 2D map only
  $("#fit-region").disabled = on;
  $("#fit-map").disabled = on;
}

function bindCoordForm() {
  const form = $("#coord-form");
  if (!form) return;
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    const latRaw = $("#coord-lat").value.trim();
    const lonRaw = $("#coord-lon").value.trim();
    if (!latRaw || !lonRaw) {
      setPill("enter both latitude and longitude", "bad");
      return;
    }
    const lat = Number(latRaw);
    const lon = Number(lonRaw);

    if (!Number.isFinite(lat) || !Number.isFinite(lon)) {
      setPill("enter both latitude and longitude", "bad");
      return;
    }
    if (lat > 0 || lat < -90) {
      setPill("latitude must be between -90 and 0", "bad");
      return;
    }
    if (lon < -180 || lon > 180) {
      setPill("longitude must be between -180 and 180", "bad");
      return;
    }

    const { x, y } = lonLatToXY(lat, lon);
    const b = state.status?.dem?.bounds;
    if (b && (x < b.left || x > b.right || y < b.bottom || y > b.top)) {
      setPill("that point is outside the terrain file (about 80°S–90°S)", "bad");
      return;
    }

    state.view.cx = x;
    state.view.cy = y;
    if (state.view.span > 40_000) state.view.span = 8_000;
    clampView();
    drawMap();
    scheduleRaster(0);
    moveObserver(x, y, true);
  });
}

function bindTimelineHover() {
  const canvas = $("#timeline");
  const tip = $("#tooltip");

  canvas.addEventListener("mousemove", (ev) => {
    const e = state.eph;
    if (!e || !e.t) return;
    const rect = canvas.getBoundingClientRect();
    const W = rect.width;
    const padL = 40, padR = 10;
    const plotW = W - padL - padR;
    const mx = ev.clientX - rect.left;
    const frac = (mx - padL) / plotW;
    if (frac < -0.02 || frac > 1.02) {
      hideHover();
      return;
    }
    const i = clamp(Math.round(frac * (e.t.length - 1)), 0, e.t.length - 1);
    state.hover = i;
    drawTimeline();
    drawSkyline();

    const lit = e.lit[i] ? "sunlit" : "in shadow";
    const vis = e.earth_vis[i] ? "Earth visible" : "Earth hidden";
    tip.innerHTML =
      `<div class="mono">${fmtTime(e.t[i])} UTC</div>` +
      `<div>Sun ${e.sun_el[i].toFixed(2)}° · horizon ${e.sun_sky[i].toFixed(2)}°</div>` +
      `<div>Earth ${e.earth_el[i].toFixed(2)}° · horizon ${e.earth_sky[i].toFixed(2)}°</div>` +
      `<div>${lit} · ${vis}</div>`;
    tip.hidden = false;
    const tw = tip.offsetWidth || 220;
    tip.style.left = clamp(ev.clientX + 14, 8, window.innerWidth - tw - 8) + "px";
    tip.style.top = Math.max(8, ev.clientY - 70) + "px";
  });

  canvas.addEventListener("mouseleave", hideHover);

  function hideHover() {
    tip.hidden = true;
    if (state.hover !== null) {
      state.hover = null;
      drawTimeline();
      drawSkyline();
    }
  }
}

function bindHelp() {
  const modal = $("#help-modal");
  const seen = (() => {
    try { return localStorage.getItem("lunar.help.seen") === "1"; } catch { return true; }
  })();
  const markSeen = () => {
    try { localStorage.setItem("lunar.help.seen", "1"); } catch { /* private mode */ }
  };
  const open = () => {
    modal.hidden = false;
    $("#help-close").focus();
  };
  const close = () => {
    modal.hidden = true;
    markSeen();
    $("#help-open").focus();
  };
  $("#help-open").addEventListener("click", open);
  $("#help-close").addEventListener("click", close);
  modal.querySelector(".modal-backdrop").addEventListener("click", close);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !modal.hidden) close();
  });
  // First visit: explain the page. Later visits: only via the button or ?help.
  // ?help=0 suppresses it (useful for screenshots and embeds).
  const param = new URLSearchParams(location.search).get("help");
  if (param === "1" || (!seen && param !== "0")) open();
}

async function init() {
  setPill("loading…", "wait", true);
  bindControls();
  bindCoordForm();
  bindTimelineHover();
  bindHelp();
  bindScenarioUI();
  restoreScenarios();
  renderScenarios();
  readParams();

  let status;
  try {
    status = await getJSON("/api/status");
  } catch (err) {
    setPill("backend offline", "bad");
    $("#data-note").textContent = "Could not reach the server: " + err.message;
    return;
  }
  state.status = status;

  const dem = status.dem;
  const data = await getJSON("/api/sites");
  state.sites = data.sites;

  if (status.warnings?.length) {
    console.warn("data warnings:", status.warnings);
  }

  $("#data-note").innerHTML =
    `<b>${dem.filename}</b><br>` +
    `${fmtNum(dem.width)} × ${fmtNum(dem.height)} px at ${dem.res_m} m/px<br>` +
    `${fmtNum(dem.bytes / 1e9, 2)} GB · ${dem.overviews.length} overview levels<br>` +
    `${status.kernels.map((k) => k.name).join(", ")}` +
    (status.coverage ? `<br>valid ${status.coverage.start} → ${status.coverage.end} UTC` : "") +
    (status.warnings?.length
      ? `<br><span style="color:${C.warn}">${status.warnings.join("<br>")}</span>`
      : "");

  resizeMap();
  fillSiteSelect();

  if (!state.sites.length) {
    setPill("no ROI files", "bad");
    return;
  }

  const site = state.sites[0];
  state.site = site;
  $("#site-select").value = site.id;
  fillRoiSelect(site);
  defaultView();
  selectRoi(site.rois[0] || null);

  window.addEventListener("resize", () => {
    clearTimeout(window.__rz);
    window.__rz = setTimeout(() => {
      resizeMap();
      if (globe3d && state.view3d) globe3d.resize();
      drawSkyline();
      drawTimeline();
    }, 120);
  });
}

init();
