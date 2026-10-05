"use strict";

// ---------------------------------------------------------------------------------------------
// helpers
// ---------------------------------------------------------------------------------------------
const $ = (id) => document.getElementById(id);
const SVGNS = "http://www.w3.org/2000/svg";

function h(tag, attrs = {}, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") e.className = v;
    else if (k === "text") e.textContent = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (k === "dataset") Object.assign(e.dataset, v);
    else e.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) if (kid !== null && kid !== undefined && kid !== false) e.append(kid);
  return e;
}

function svg(tag, attrs = {}, ...kids) {
  const e = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
  e.append(...kids);
  return e;
}

async function api(path, body, method) {
  const res = await fetch(path, {
    method: method || (body === undefined ? "GET" : "POST"),
    headers: { "Content-Type": "application/json", "X-App-Request": "1" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (res.status === 401 && path !== "/api/password") { location.replace("/login"); throw new Error("Signed out."); }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const d = data.detail;
    throw new Error(Array.isArray(d) ? d.map((x) => x.msg).join("; ") : d || `Request failed (${res.status})`);
  }
  return data;
}

let toastTimer;
function toast(text, error = false) {
  const t = $("toast");
  t.textContent = text;
  t.classList.toggle("error", error);
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, error ? 6000 : 2500);
}

async function act(path, body, method) {
  try { const st = await api(path, body, method); if (st && st.radio) applyState(st); return st; }
  catch (e) { toast(e.message, true); return null; }
}

const mhz = (hz, d = 3) => (hz / 1e6).toFixed(d);
const fmtNum = (v, d = 0) => (v === undefined || v === null || v === "" ? "–" : Number(v).toLocaleString(undefined, { maximumFractionDigits: d }));
const fmtClock = (t) => new Date(t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
function fmtAgo(t) {
  const s = Math.max(0, Date.now() / 1000 - t);
  return s < 60 ? `${Math.round(s)} s ago` : s < 3600 ? `${Math.round(s / 60)} min ago` : `${(s / 3600).toFixed(1)} h ago`;
}

// altitude colours, matching the legend gradient (0, 8k, 15k, 25k, 34k, 45k ft)
const ALT_STOPS = [[0, [255, 138, 61]], [8000, [245, 208, 64]], [15000, [60, 207, 123]], [25000, [46, 199, 214]],
  [34000, [63, 140, 255]], [45000, [180, 108, 255]]];
function altColor(alt) {
  if (alt === "ground") return "#9aa3ad";
  if (typeof alt !== "number") return "#c8ced6";
  const a = Math.max(0, Math.min(45000, alt));
  for (let i = 1; i < ALT_STOPS.length; i++) {
    const [h1, c1] = ALT_STOPS[i];
    const [h0, c0] = ALT_STOPS[i - 1];
    if (a <= h1) {
      const t = (a - h0) / (h1 - h0);
      const c = c0.map((v, k) => Math.round(v + (c1[k] - v) * t));
      return `rgb(${c[0]}, ${c[1]}, ${c[2]})`;
    }
  }
  return "rgb(180, 108, 255)";
}

// ---------------------------------------------------------------------------------------------
// state
// ---------------------------------------------------------------------------------------------
const S = {
  aircraft: new Map(), trails: new Map(), markers: new Map(), messages: [], clips: [], state: null,
  selected: null, follow: false, fitted: false, sort: { key: "dist", dir: 1 }, msgSource: "",
  listen: new Set(), spectrum: null, ws: null,
};

// ---------------------------------------------------------------------------------------------
// map
// ---------------------------------------------------------------------------------------------
const map = L.map("map", { zoomControl: false, worldCopyJump: true }).setView([30, 15], 3);
L.control.zoom({ position: "topright" }).addTo(map);
const dark = window.matchMedia("(prefers-color-scheme: dark)");
let tiles = null, tileCfg = null;
function setTiles(cfg) {
  if (cfg) tileCfg = cfg;
  if (!tileCfg) return;
  if (!tiles) {
    // the page sends no Referer by default; tile servers such as OpenStreetMap ask for one
    tiles = L.tileLayer(tileCfg.url, { subdomains: "abcd", maxZoom: 19, attribution: tileCfg.attribution,
      referrerPolicy: "strict-origin-when-cross-origin" }).addTo(map);
  }
  $("map").classList.toggle("dark-tiles", tileCfg.dark_filter && dark.matches);
}
dark.addEventListener("change", () => setTiles());
const trailLayer = L.layerGroup().addTo(map);
let receiverMarker = null;

const JET = "M12 1.5c.7 0 1.2.9 1.2 2.1v5.6l8.3 5v2.2l-8.3-2.6v5.1l2.3 1.7v1.7L12 21.4l-3.5.9v-1.7l2.3-1.7v-5.1l-8.3 2.6v-2.2l8.3-5V3.6c0-1.2.5-2.1 1.2-2.1z";
const LIGHT = "M12 2c.6 0 1 .8 1 1.8V8h8.5v2.2L13 11.4v6l2.6 1.4v1.6L12 19.8l-3.6.6v-1.6L11 17.4v-6l-8.5-1.2V8H11V3.8C11 2.8 11.4 2 12 2z";
const HELI = "M11 3h2v3.2a5 5 0 0 1 3 4.6v3.4a3 3 0 0 1-2.2 2.9L13 21h2.5v1.5h-7V21H11l-.8-3.9A3 3 0 0 1 8 14.2v-3.4a5 5 0 0 1 3-4.6zM3 4.5l18 1.5v1L3 5.5z";
function iconFor(a) {
  const cat = a.category || "";
  const path = cat === "A7" ? HELI : (cat === "A1" || cat === "B1" ? LIGHT : JET);
  const size = { A1: 20, A2: 22, A3: 26, A4: 27, A5: 30, A7: 24 }[cat] || 24;
  return { path, size };
}

function makeMarker(a) {
  const { path, size } = iconFor(a);
  const p = svg("path", { d: path, stroke: "rgba(0,0,0,.55)", "stroke-width": "0.7" });
  const s = svg("svg", { viewBox: "0 0 24 24", width: size, height: size }, p);
  const wrap = h("div", {}, s);
  const marker = L.marker([a.lat, a.lon], {
    icon: L.divIcon({ html: wrap, className: "ac-icon", iconSize: [size, size], iconAnchor: [size / 2, size / 2] }),
    keyboard: false, riseOnHover: true,
  });
  marker.on("click", () => select(a.hex, false));
  marker.bindTooltip("", { direction: "right", offset: [size / 2, 0], className: "ac-label" });
  marker.addTo(map);
  return { marker, svg: s, path: p, iconKey: path };
}

function updateMarkers() {
  const seen = new Set();
  for (const a of S.aircraft.values()) {
    if (typeof a.lat !== "number") continue;
    seen.add(a.hex);
    let m = S.markers.get(a.hex);
    if (m && m.iconKey !== iconFor(a).path) { map.removeLayer(m.marker); m = null; }
    if (!m) { m = makeMarker(a); S.markers.set(a.hex, m); }
    m.marker.setLatLng([a.lat, a.lon]);
    m.svg.style.transform = `rotate(${a.track || 0}deg)`;
    const emerg = (a.emergency && a.emergency !== "none") || ["7500", "7600", "7700"].includes(a.squawk);
    m.path.setAttribute("fill", emerg ? "#ff4d4d" : altColor(a.alt));
    const el = m.marker.getElement();
    if (el) {
      el.classList.toggle("sel", a.hex === S.selected);
      el.classList.toggle("emerg", emerg);
    }
    m.marker.setTooltipContent(a.flight || a.reg || a.hex.toUpperCase());
    if (a.hex === S.selected) m.marker.openTooltip(); else m.marker.closeTooltip();
  }
  for (const [hex, m] of S.markers) if (!seen.has(hex)) { map.removeLayer(m.marker); S.markers.delete(hex); }
  if (!S.fitted && seen.size) {
    const pts = [...S.aircraft.values()].filter((a) => typeof a.lat === "number").map((a) => [a.lat, a.lon]);
    const rx = S.state && S.state.adsb.receiver;
    if (rx) map.setView(rx, 8); else map.fitBounds(pts, { padding: [40, 40], maxZoom: 9 });
    S.fitted = true;
  }
  const rx = S.state && S.state.adsb.receiver;
  if (rx && !receiverMarker) {
    receiverMarker = L.circleMarker(rx, { radius: 5, color: "#4aa8ff", weight: 2, fillOpacity: 0.3 }).addTo(map)
      .bindTooltip("Receiver", { direction: "top" });
  }
}

function drawTrail() {
  trailLayer.clearLayers();
  const t = S.trails.get(S.selected);
  if (!t || t.length < 2) return;
  for (let i = 1; i < t.length; i++) {
    L.polyline([[t[i - 1][0], t[i - 1][1]], [t[i][0], t[i][1]]],
      { color: altColor(t[i][2] || "ground"), weight: 3, opacity: 0.9, interactive: false }).addTo(trailLayer);
  }
}

function addTrailPoint(a) {
  if (typeof a.lat !== "number") return;
  let t = S.trails.get(a.hex);
  if (!t) { t = []; S.trails.set(a.hex, t); }
  const last = t[t.length - 1];
  if (!last || last[0] !== a.lat || last[1] !== a.lon) {
    t.push([a.lat, a.lon, typeof a.alt === "number" ? a.alt : 0]);
    if (t.length > 240) t.shift();
  }
}

// ---------------------------------------------------------------------------------------------
// aircraft list and card
// ---------------------------------------------------------------------------------------------
function msgCount(hex) { return S.messages.reduce((n, m) => n + (m.aircraft === hex), 0); }

function sortValue(a, key) {
  switch (key) {
    case "call": return (a.flight || a.reg || a.hex).toUpperCase();
    case "type": return a.type || "~";
    case "alt": return a.alt === "ground" ? 0 : (typeof a.alt === "number" ? a.alt : -1);
    case "gs": return a.gs ?? -1;
    case "dist": return a.dist_km ?? 1e9;
    case "msgs": return -msgCount(a.hex);
    default: return 0;
  }
}

function renderTable() {
  const q = $("ac-search").value.trim().toUpperCase();
  const onlyPos = $("ac-pos").checked;
  let rows = [...S.aircraft.values()].filter((a) => {
    if (onlyPos && typeof a.lat !== "number") return false;
    if (!q) return true;
    return [a.flight, a.reg, a.type, a.squawk, a.hex, a.desc].some((v) => (v || "").toUpperCase().includes(q));
  });
  const { key, dir } = S.sort;
  rows.sort((x, y) => { const a = sortValue(x, key), b = sortValue(y, key); return (a > b ? 1 : a < b ? -1 : 0) * dir; });
  const tbody = $("ac-table").querySelector("tbody");
  tbody.replaceChildren(...rows.map((a) => {
    const dot = h("span", { class: "altdot" });
    dot.style.background = altColor(a.alt);
    const emerg = ["7500", "7600", "7700"].includes(a.squawk);
    return h("tr", { class: a.hex === S.selected ? "sel" : "", onclick: () => select(a.hex, true), dataset: { hex: a.hex } },
      h("td", {}, dot, h("span", { class: "call", text: a.flight || a.hex.toUpperCase() }), a.reg ? h("span", { class: "reg", text: a.reg }) : null),
      h("td", { text: a.type || "" }),
      h("td", { class: "num" + (emerg ? " emerg" : ""), text: emerg ? `SQ ${a.squawk}` : a.alt === "ground" ? "GND" : fmtNum(a.alt) }),
      h("td", { class: "num", text: fmtNum(a.gs) }),
      h("td", { class: "num", text: fmtNum(a.dist_km, 0) }),
      h("td", { class: "num", text: msgCount(a.hex) || "" }));
  }));
  $("ac-empty").hidden = rows.length > 0;
  $("n-traffic").textContent = S.aircraft.size || "";
  for (const th of $("ac-table").querySelectorAll("th")) {
    th.setAttribute("aria-sort", th.dataset.sort === key ? (dir > 0 ? "ascending" : "descending") : "none");
  }
}

function fact(label, value) { return h("div", {}, h("dt", { text: label }), h("dd", { text: value })); }

function renderCard() {
  const a = S.aircraft.get(S.selected);
  $("card").hidden = !a;
  if (!a) return;
  $("c-call").textContent = a.flight || a.reg || a.hex.toUpperCase();
  $("c-sub").textContent = [a.reg, a.type, a.desc].filter(Boolean).join(" · ") || "Unknown aircraft";
  const vs = typeof a.vr === "number" ? `${a.vr > 0 ? "+" : ""}${fmtNum(a.vr)}` : "–";
  $("c-facts").replaceChildren(
    fact("Altitude", a.alt === "ground" ? "Ground" : `${fmtNum(a.alt)} ft`), fact("V/S fpm", vs),
    fact("Speed", a.gs != null ? `${fmtNum(a.gs)} kt` : "–"), fact("Track", a.track != null ? `${fmtNum(a.track)}°` : "–"),
    fact("Squawk", a.squawk || "–"), fact("Distance", a.dist_km != null ? `${fmtNum(a.dist_km, 1)} km` : "–"),
    fact("Signal", a.rssi != null ? `${fmtNum(a.rssi, 1)} dB` : "–"), fact("Seen", a.seen != null ? `${fmtNum(a.seen, 1)} s` : "–"),
    fact("ICAO", a.hex.toUpperCase() + (a.mlat ? " · MLAT" : "")));
  const msgs = S.messages.filter((m) => m.aircraft === a.hex).slice(0, 30);
  $("c-msg-count").textContent = msgs.length ? `${msgs.length}` : "none yet";
  $("c-msgs").replaceChildren(...msgs.map((m) => msgItem(m, false)));
  $("c-follow").setAttribute("aria-pressed", String(S.follow));
}

function select(hex, pan) {
  S.selected = hex;
  const a = S.aircraft.get(hex);
  if (pan && a && typeof a.lat === "number") map.panTo([a.lat, a.lon]);
  drawTrail();
  renderCard();
  renderTable();
  updateMarkers();
}

// ---------------------------------------------------------------------------------------------
// messages
// ---------------------------------------------------------------------------------------------
function msgItem(m, showWho = true) {
  const who = m.flight || m.reg || (m.hex ? m.hex.toUpperCase() : "?");
  const linked = m.aircraft && S.aircraft.has(m.aircraft);
  const whoEl = linked
    ? h("a", { tabindex: "0", text: who, onclick: () => { select(m.aircraft, true); }, onkeydown: (e) => { if (e.key === "Enter") select(m.aircraft, true); } })
    : h("span", { class: "who", text: who });
  return h("li", { class: "msg" },
    h("div", { class: "msg-head" },
      h("time", { text: fmtClock(m.t) }), h("span", { class: `badge ${m.source}`, text: m.source }),
      showWho ? whoEl : null, m.reg && m.flight ? h("span", { class: "label", text: m.reg }) : null,
      m.label ? h("span", { class: "label", text: `${m.label}${m.label_name ? ` · ${m.label_name}` : ""}` }) : null,
      m.freq_hz ? h("span", { class: "label", text: `${mhz(m.freq_hz)}` }) : null),
    m.text ? h("pre", { text: m.text }) : null);
}

function renderMessages() {
  const q = $("msg-search").value.trim().toUpperCase();
  const list = S.messages.filter((m) => (!S.msgSource || m.source === S.msgSource) &&
    (!q || [m.text, m.flight, m.reg, m.label, m.hex].some((v) => (v || "").toUpperCase().includes(q)))).slice(0, 300);
  $("msg-list").replaceChildren(...list.map((m) => msgItem(m)));
  $("msg-empty").hidden = S.messages.length > 0;
  $("n-msgs").textContent = S.messages.length || "";
}

// ---------------------------------------------------------------------------------------------
// radio
// ---------------------------------------------------------------------------------------------
const HEADPHONES = "M12 3a8 8 0 0 0-8 8v6a3 3 0 0 0 3 3h1v-8H6v-1a6 6 0 0 1 12 0v1h-2v8h1a3 3 0 0 0 3-3v-6a8 8 0 0 0-8-8z";
let chanKey = "";
const meterEls = new Map();

function renderRadio(r) {
  const pill = h("span", { class: `pill ${r.state}`, text: r.state });
  $("r-state").replaceChildren(pill, document.createTextNode(r.state === "running" ? `${mhz(r.config.center_hz)} MHz` : ""));
  $("r-device").textContent = r.device || "";
  $("r-message").textContent = r.message || "";
  const on = r.config.running;
  $("r-toggle").textContent = on ? "Stop radio" : "Start radio";
  $("r-toggle").classList.toggle("primary", !on);
  if (document.activeElement !== $("r-center")) $("r-center").value = mhz(r.config.center_hz);
  if (document.activeElement !== $("r-gain")) { $("r-gain").value = r.config.gain; $("r-gain-out").textContent = r.config.gain; }
  $("keep-clips").checked = r.config.keep_clips;
  $("r-window").textContent = `${mhz(r.window[0], 2)}–${mhz(r.window[1], 2)} MHz`;
  $("n-live").hidden = !(r.state === "running" && r.channels.some((c) => c.open));

  const voice = r.channels.filter((c) => c.kind === "voice");
  const data = r.channels.filter((c) => c.kind !== "voice");
  const key = JSON.stringify([voice.map((c) => [c.id, c.label, c.freq_hz, c.inside]), data.map((c) => [c.id, c.inside]), [...S.listen]]);
  if (key !== chanKey) {
    chanKey = key;
    meterEls.clear();
    $("voice-list").replaceChildren(...voice.map(voiceRow));
    $("data-list").replaceChildren(...data.map(dataRow));
    if (!voice.length) $("voice-list").append(h("li", { class: "hint", text: "No voice channels yet. Add one below, or pick a frequency from Heard recently." }));
  }
  for (const c of r.channels) {
    const m = meterEls.get(c.id);
    if (!m) continue;
    m.bar.style.width = `${Math.max(0, Math.min(100, (c.level_db / 30) * 100))}%`;
    m.row.classList.toggle("open", !!c.open);
  }
  const v = r.vdl2;
  $("vdl2-note").textContent = !v.available ? "VDL2 needs dumpvdl2, which is not installed in this container."
    : v.enabled ? `VDL2 decoder running on ${v.freqs.map((f) => mhz(f)).join(", ")} MHz · ${v.decoded} frames decoded${v.dropped ? ` · ${v.dropped} blocks dropped (CPU busy)` : ""}`
    : "Move the window to 136.2–137.0 MHz (VDL2 preset) to decode VDL2.";
  $("activity").replaceChildren(...r.activity.slice(0, 16).map((a) => {
    const known = r.channels.find((c) => Math.abs(c.freq_hz - a.freq_hz) < 6e3);
    return h("li", {}, h("button", {
      type: "button", title: known ? `${known.label || "Channel"} · last ${fmtAgo(a.last)}` : `Add ${mhz(a.freq_hz)} MHz as a channel`,
      onclick: () => { if (!known) { $("add-freq").value = mhz(a.freq_hz); $("add-label").focus(); } },
    }, mhz(a.freq_hz), h("small", { text: known ? (known.label || known.kind.toUpperCase()) : `${a.count}×` })));
  }));
  if (!r.activity.length) $("activity").append(h("li", { class: "hint", text: r.state === "running" ? "Listening… frequencies with signals show up here." : "Start the radio to look for activity." }));
}

function voiceRow(c) {
  const listening = S.listen.has(c.id);
  const bar = h("span");
  const btn = h("button", { type: "button", class: "listen", "aria-pressed": String(listening), disabled: !c.inside,
    "aria-label": `Listen to ${c.label || mhz(c.freq_hz)}`, onclick: () => toggleListen(c.id) },
  svg("svg", { viewBox: "0 0 24 24", fill: "currentColor" }, svg("path", { d: HEADPHONES })));
  const del = h("button", { type: "button", class: "ghost small", "aria-label": `Remove ${c.label || mhz(c.freq_hz)}`, text: "✕",
    onclick: () => act(`/api/channels/${encodeURIComponent(c.id)}`, undefined, "DELETE") });
  const row = h("li", { class: `chan${c.inside ? "" : " outside"}` }, btn,
    h("div", {}, h("span", { class: "name", text: c.label || "Voice" }), " ", h("span", { class: "freq", text: `${mhz(c.freq_hz)} MHz${c.inside ? "" : " · outside window"}` })),
    h("div", { class: "tools" }, del), h("div", { class: "meter" }, bar));
  meterEls.set(c.id, { bar, row });
  return row;
}

function dataRow(c) {
  const bar = h("span");
  const row = h("li", { class: `chan ${c.kind}${c.inside ? "" : " outside"}` },
    h("span", { class: `kind ${c.kind}`, text: c.kind.toUpperCase() }),
    h("div", {}, h("span", { class: "freq", text: `${mhz(c.freq_hz)} MHz${c.inside ? "" : " · outside window"}` })),
    h("div", {}), h("div", { class: "meter" }, bar));
  meterEls.set(c.id, { bar, row });
  return row;
}

function drawSpectrum() {
  const c = $("spectrum");
  const w = c.clientWidth, ht = c.clientHeight, dpr = window.devicePixelRatio || 1;
  if (!w) return;
  if (c.width !== Math.round(w * dpr)) { c.width = Math.round(w * dpr); c.height = Math.round(ht * dpr); }
  const g = c.getContext("2d");
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  g.clearRect(0, 0, w, ht);
  const css = getComputedStyle(document.documentElement);
  const col = (n) => css.getPropertyValue(n).trim();
  const sp = S.spectrum, r = S.state && S.state.radio;
  if (!sp || !r || r.state !== "running") {
    g.fillStyle = col("--muted");
    g.font = `12px ${col("--ui")}`;
    g.fillText(r && r.state === "running" ? "Waiting for the spectrum…" : "Radio is off", 10, ht / 2);
    return;
  }
  const lo = sp.center_hz - sp.span_hz / 2, span = sp.span_hz;
  const x = (f) => ((f - lo) / span) * w;
  const floor = -60;
  g.fillStyle = col("--raised");
  g.fillRect(x(r.window[0]), 0, x(r.window[1]) - x(r.window[0]), ht);
  g.beginPath();
  sp.db.forEach((v, i) => {
    const px = (i / (sp.db.length - 1)) * w, py = 6 + Math.min(1, Math.max(0, v / floor)) * (ht - 20);
    i ? g.lineTo(px, py) : g.moveTo(px, py);
  });
  g.strokeStyle = col("--accent");
  g.lineWidth = 1;
  g.stroke();
  for (const ch of r.channels) {
    if (!ch.inside) continue;
    g.fillStyle = col(ch.kind === "voice" ? "--voice" : ch.kind === "acars" ? "--acars" : "--vdl2");
    g.fillRect(x(ch.freq_hz) - 1, ht - 14, 2, 8);
  }
  g.fillStyle = col("--muted");
  g.font = `10px ${col("--mono")}`;
  const ticks = [lo + span * 0.1, sp.center_hz, lo + span * 0.9];
  ticks.forEach((t, i) => {
    const label = mhz(t, 2);
    const tw = g.measureText(label).width;
    g.fillText(label, i === 0 ? x(t) : i === 2 ? x(t) - tw : x(t) - tw / 2, ht - 2);
  });
}

// ---------------------------------------------------------------------------------------------
// audio: live listening and recorded transmissions
// ---------------------------------------------------------------------------------------------
let audioCtx = null, gainNode = null, playAt = 0;
function ensureAudio() {
  if (!audioCtx) {
    audioCtx = new AudioContext();
    gainNode = audioCtx.createGain();
    gainNode.gain.value = Number($("volume").value);
    gainNode.connect(audioCtx.destination);
  }
  if (audioCtx.state === "suspended") audioCtx.resume();
}

function playPcm(buf) {
  if (!audioCtx || !S.listen.size) return;
  const pcm = new Int16Array(buf.slice(1));
  if (!pcm.length) return;
  const ab = audioCtx.createBuffer(1, pcm.length, 12000);
  const ch = ab.getChannelData(0);
  for (let i = 0; i < pcm.length; i++) ch[i] = pcm[i] / 32768;
  const src = audioCtx.createBufferSource();
  src.buffer = ab;
  src.connect(gainNode);
  const now = audioCtx.currentTime;
  if (playAt < now + 0.03 || playAt > now + 0.7) playAt = now + 0.12;      // small jitter buffer, never far behind
  src.start(playAt);
  playAt += ab.duration;
}

function toggleListen(id) {
  ensureAudio();
  if (S.listen.has(id)) S.listen.delete(id); else S.listen.add(id);
  if (S.ws && S.ws.readyState === 1) S.ws.send(JSON.stringify({ listen: [...S.listen] }));
  if (S.state) renderRadio(S.state.radio);
}

let playing = null;
function renderClips() {
  const labels = new Map(((S.state && S.state.radio.channels) || []).map((c) => [c.id, c.label]));
  $("clips").replaceChildren(...S.clips.slice(0, 120).map((c) => {
    const btn = h("button", { type: "button", class: "small", text: playing && playing.id === c.id ? "■" : "▶",
      "aria-label": `Play transmission at ${fmtClock(c.start)}`, onclick: () => playClip(c) });
    return h("li", { class: `clip${playing && playing.id === c.id ? " playing" : ""}` },
      h("time", { text: fmtClock(c.start) }),
      h("span", { class: "what" }, labels.get(c.channel) || "Voice", h("small", { text: `${mhz(c.freq_hz)} · ${c.duration.toFixed(1)} s` })),
      btn);
  }));
  $("clips-empty").hidden = S.clips.length > 0;
}

function playClip(c) {
  if (playing) { playing.audio.pause(); const same = playing.id === c.id; playing = null; renderClips(); if (same) return; }
  const audio = new Audio(`/api/clips/${c.id}.wav`);
  audio.volume = Math.min(1, Number($("volume").value));
  playing = { id: c.id, audio };
  audio.addEventListener("ended", () => { playing = null; renderClips(); });
  audio.play().catch(() => toast("That recording is no longer kept.", true));
  renderClips();
}

// ---------------------------------------------------------------------------------------------
// live updates
// ---------------------------------------------------------------------------------------------
function applyState(st) {
  S.state = st;
  if (!tiles && st.tiles) setTiles(st.tiles);
  const a = st.adsb;
  const chipA = $("chip-adsb");
  chipA.className = `chip ${a.ok ? "ok" : "bad"}`;
  chipA.querySelector(".t").textContent = a.ok ? `ADS-B · ${a.aircraft} aircraft` : "ADS-B offline";
  chipA.title = a.ok ? `${a.with_position} with position` : a.error;
  const r = st.radio;
  const chipR = $("chip-radio");
  chipR.className = `chip ${r.state === "running" ? "ok" : r.state === "error" ? "bad" : r.state === "connecting" ? "warn" : ""}`;
  chipR.querySelector(".t").textContent = r.state === "running" ? `Radio · ${mhz(r.config.center_hz)} MHz` : r.state === "error" ? "Radio problem" : r.config.running ? "Radio connecting" : "Radio off";
  chipR.title = r.message || r.device || "";
  const total = Object.values(st.messages).reduce((x, y) => x + y, 0);
  $("chip-msgs").querySelector(".t").textContent = `Messages · ${total}`;
  if (!$("presets").children.length) {
    $("presets").replaceChildren(...Object.entries(st.presets).map(([k, label]) =>
      h("button", { type: "button", "aria-pressed": "false", title: label, text: k.toUpperCase(), onclick: () => act("/api/radio/preset", { name: k }) })));
  }
  renderRadio(r);
}

function setAircraft(list) {
  const fresh = new Map();
  for (const a of list) { fresh.set(a.hex, a); addTrailPoint(a); }
  S.aircraft = fresh;
  for (const hex of [...S.trails.keys()]) if (!fresh.has(hex)) S.trails.delete(hex);
  if (S.follow && S.selected && fresh.get(S.selected) && typeof fresh.get(S.selected).lat === "number") {
    const a = fresh.get(S.selected);
    map.panTo([a.lat, a.lon], { animate: true });
  }
  updateMarkers();
  if (S.selected) drawTrail();
  renderTable();
  renderCard();
}

function connect(delay = 500) {
  const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
  ws.binaryType = "arraybuffer";
  S.ws = ws;
  ws.onopen = () => { delay = 500; if (S.listen.size) ws.send(JSON.stringify({ listen: [...S.listen] })); };
  ws.onmessage = (ev) => {
    if (typeof ev.data !== "string") { playPcm(ev.data); return; }
    const m = JSON.parse(ev.data);
    if (m.type === "hello") {
      S.trails = new Map(Object.entries(m.trails));
      S.messages = m.messages;
      S.clips = m.clips;
      applyState(m.state);
      setAircraft(m.aircraft);
      renderMessages();
      renderClips();
    } else if (m.type === "state") {
      applyState(m.state);
    } else if (m.type === "aircraft") {
      setAircraft(m.aircraft);
    } else if (m.type === "spectrum") {
      S.spectrum = m.spectrum;
      drawSpectrum();
    } else if (m.type === "message") {
      S.messages.unshift(m.message);
      if (S.messages.length > 1000) S.messages.length = 1000;
      renderMessages();
      if (m.message.aircraft === S.selected) renderCard();
    } else if (m.type === "clip") {
      S.clips.unshift(m.clip);
      if (S.clips.length > 300) S.clips.length = 300;
      renderClips();
    }
  };
  ws.onclose = () => {
    fetch("/api/me").then((r) => {
      if (r.status === 401) location.replace("/login");
      else setTimeout(() => connect(Math.min(delay * 2, 8000)), delay);
    }).catch(() => setTimeout(() => connect(Math.min(delay * 2, 8000)), delay));
  };
}

// ---------------------------------------------------------------------------------------------
// wiring
// ---------------------------------------------------------------------------------------------
function showTab(name) {
  for (const t of ["traffic", "radio", "messages"]) {
    $(`tab-${t}`).setAttribute("aria-selected", String(t === name));
    $(`pane-${t}`).hidden = t !== name;
  }
  if (name === "radio") drawSpectrum();
  try { localStorage.setItem("airdesk-tab", name); } catch { /* storage may be unavailable */ }
}

function init() {
  for (const t of ["traffic", "radio", "messages"]) $(`tab-${t}`).addEventListener("click", () => showTab(t));
  let saved = "traffic";
  try { saved = localStorage.getItem("airdesk-tab") || "traffic"; } catch { /* ignore */ }
  showTab(saved);

  $("ac-search").addEventListener("input", renderTable);
  $("ac-pos").addEventListener("change", renderTable);
  for (const th of $("ac-table").querySelectorAll("th")) {
    th.addEventListener("click", () => {
      S.sort = { key: th.dataset.sort, dir: S.sort.key === th.dataset.sort ? -S.sort.dir : 1 };
      renderTable();
    });
  }
  $("c-close").addEventListener("click", () => { S.selected = null; S.follow = false; trailLayer.clearLayers(); renderCard(); renderTable(); updateMarkers(); });
  $("c-follow").addEventListener("click", () => { S.follow = !S.follow; renderCard(); if (S.follow) select(S.selected, true); });
  map.on("dragstart", () => { if (S.follow) { S.follow = false; renderCard(); } });

  $("msg-search").addEventListener("input", renderMessages);
  for (const b of document.querySelectorAll("#pane-messages .segmented button")) {
    b.addEventListener("click", () => {
      S.msgSource = b.dataset.src;
      for (const o of document.querySelectorAll("#pane-messages .segmented button")) o.setAttribute("aria-pressed", String(o === b));
      renderMessages();
    });
  }

  $("r-toggle").addEventListener("click", () => act(S.state && S.state.radio.config.running ? "/api/radio/stop" : "/api/radio/start", {}));
  $("r-center").addEventListener("change", () => act("/api/radio/settings", { center_mhz: Number($("r-center").value) }));
  let gainTimer;
  $("r-gain").addEventListener("input", () => {
    $("r-gain-out").textContent = $("r-gain").value;
    clearTimeout(gainTimer);
    gainTimer = setTimeout(() => act("/api/radio/settings", { gain: Number($("r-gain").value) }), 250);
  });
  $("keep-clips").addEventListener("change", () => act("/api/radio/settings", { keep_clips: $("keep-clips").checked }));
  $("volume").addEventListener("input", () => { if (gainNode) gainNode.gain.value = Number($("volume").value); });
  $("add-chan").addEventListener("submit", async (e) => {
    e.preventDefault();
    const st = await act("/api/channels", { freq_mhz: Number($("add-freq").value), label: $("add-label").value });
    if (st) { $("add-chan").reset(); toast("Channel added."); }
  });
  window.addEventListener("resize", drawSpectrum);

  // account
  api("/api/me").then((me) => { $("who").textContent = me.user; }).catch(() => {});
  $("logout").addEventListener("click", async () => { await api("/api/logout", {}).catch(() => {}); location.replace("/login"); });
  const dlg = $("pw-dialog");
  $("pw-open").addEventListener("click", () => { $("pw-form").reset(); $("pw-error").textContent = ""; dlg.showModal(); });
  $("pw-cancel").addEventListener("click", () => dlg.close());
  $("pw-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    if ($("pw-new").value !== $("pw-repeat").value) { $("pw-error").textContent = "The new passwords do not match."; return; }
    try { await api("/api/password", { current: $("pw-current").value, new: $("pw-new").value }); dlg.close(); toast("Password changed."); }
    catch (ex) { $("pw-error").textContent = ex.message; }
  });

  setInterval(() => { if (S.state) renderClips(); }, 30000);
  connect();
}

init();
