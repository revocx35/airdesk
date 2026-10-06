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
  aircraft: new Map(), trails: new Map(), markers: new Map(), messages: [], recent: [], state: null,
  selected: null, follow: false, fitted: false, sort: { key: "dist", dir: 1 }, msgSource: "",
  listen: new Set(), spectrum: null, ws: null,
  channels: [], chStatus: "alive", chSelected: null, chHours: 24, chHistory: null, chBucket: null,
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

function fmtBytes(b) {
  return b >= 1e9 ? `${(b / 1e9).toFixed(1)} GB` : b >= 1e6 ? `${(b / 1e6).toFixed(0)} MB` : `${Math.round(b / 1e3)} kB`;
}
function fmtDur(s) {
  if (s < 60) return `${s.toFixed(s < 10 ? 1 : 0)} s`;
  const m = Math.floor(s / 60);
  return m < 60 ? `${m} min ${Math.round(s % 60)} s` : `${Math.floor(m / 60)} h ${m % 60} min`;
}

function listenButton(id, label, enabled = true) {
  return h("button", { type: "button", class: "listen", "aria-pressed": String(S.listen.has(id)), disabled: !enabled,
    "aria-label": `Listen to ${label}`, title: S.listen.has(id) ? "Stop listening" : "Listen live",
    onclick: (e) => { e.stopPropagation(); toggleListen(id); } },
  svg("svg", { viewBox: "0 0 24 24", fill: "currentColor" }, svg("path", { d: HEADPHONES })));
}

function renderRadio(r) {
  const pill = h("span", { class: `pill ${r.state}`, text: r.state });
  $("r-state").replaceChildren(pill, document.createTextNode(r.state === "running" ? `${mhz((r.window[0] + r.window[1]) / 2)} MHz` : ""));
  $("r-device").textContent = r.device || "";
  $("r-message").textContent = r.message || "";
  const on = r.config.running;
  $("r-toggle").textContent = on ? "Stop radio" : "Start radio";
  $("r-toggle").classList.toggle("primary", !on);
  const scan = r.config.mode === "scan";
  for (const b of $("mode").querySelectorAll("button")) b.setAttribute("aria-pressed", String(b.dataset.mode === r.config.mode));
  $("scan-box").hidden = !scan;
  $("fixed-box").hidden = scan;
  if (document.activeElement !== $("r-center")) $("r-center").value = mhz(r.config.center_hz);
  if (document.activeElement !== $("r-gain")) { $("r-gain").value = r.config.gain; $("r-gain-out").textContent = r.config.gain; }
  $("keep-clips").checked = r.config.keep_clips;
  $("rec-usage").textContent = `${fmtBytes(r.recordings.bytes)} of recordings, kept for ${r.recordings.days} days.`;
  $("r-window").textContent = `${mhz(r.window[0], 2)}–${mhz(r.window[1], 2)} MHz`;
  $("n-live").hidden = !(r.state === "running" && r.channels.some((c) => c.open));
  $("n-chans").textContent = r.counts.alive || "";
  if (scan) renderScan(r);

  const voice = r.channels.filter((c) => c.kind === "voice" && c.inside);
  const data = r.channels.filter((c) => c.kind !== "voice");
  const key = JSON.stringify([voice.map((c) => [c.id, c.label, c.freq_hz]), data.map((c) => [c.id, c.inside]), [...S.listen]]);
  if (key !== chanKey) {
    chanKey = key;
    meterEls.clear();
    $("voice-list").replaceChildren(...voice.map(voiceRow));
    $("data-list").replaceChildren(...data.map(dataRow));
    if (!voice.length) {
      $("voice-list").append(h("li", { class: "hint", text: r.state === "running"
        ? "No known voice channels in this window yet. New ones are added as soon as someone talks."
        : "Start the radio to listen." }));
    }
  }
  for (const c of r.channels) {
    const m = meterEls.get(c.id);
    if (!m) continue;
    m.bar.style.width = `${Math.max(0, Math.min(100, (c.level_db / 30) * 100))}%`;
    m.row.classList.toggle("open", !!c.open);
  }
  const v = r.vdl2;
  $("vdl2-note").textContent = !v.available ? "VDL2 needs dumpvdl2, which is not installed in this container."
    : v.enabled ? `VDL2 decoder running on ${v.freqs.map((f) => mhz(f)).join(", ")} MHz · ${v.decoded} messages${v.dropped ? ` · ${v.dropped} blocks dropped (CPU busy)` : ""}`
    : scan ? "VDL2 and ACARS are decoded while the scanner is on their segments." : "Move the window to 136.2–137.0 MHz (VDL2 preset) to decode VDL2.";
}

let segEls = [];
function renderScan(r) {
  const sc = r.scanner, segs = sc.segments;
  const cur = segs.find((s) => s.idx === sc.current);
  $("scan-status").replaceChildren(...(r.state !== "running"
    ? [document.createTextNode("Start the radio to scan the airband.")]
    : cur ? [document.createTextNode("Listening to "), h("b", { text: `${mhz(cur.lo, 1)}–${mhz(cur.hi, 1)} MHz` }),
      document.createTextNode(` for ${Math.round(sc.dwell_s)} s${sc.holding ? " · held while you listen" : ""}`)]
      : [document.createTextNode("Moving…")]));
  if (segEls.length !== segs.length) {
    segEls = segs.map((s) => {
      const fill = h("span", { class: "fill" });
      const el = h("div", { class: "seg", role: "listitem", tabindex: "0" }, fill);
      const show = () => showSegTip(el, s.idx);
      el.addEventListener("pointerenter", show);
      el.addEventListener("focus", show);
      el.addEventListener("pointerleave", () => { $("scan-tip").hidden = true; });
      el.addEventListener("blur", () => { $("scan-tip").hidden = true; });
      return { el, fill };
    });
    $("scan-strip").replaceChildren(...segEls.map((x) => x.el));
  }
  const maxShare = Math.max(...segs.map((s) => s.share), 1e-9);
  segs.forEach((s, i) => {
    const { el, fill } = segEls[i];
    fill.style.opacity = String(0.12 + 0.88 * (s.share / maxShare));     // one hue, light to dark with share
    el.classList.toggle("cur", s.idx === sc.current && r.state === "running");
    el.setAttribute("aria-label", `${mhz(s.lo, 1)} to ${mhz(s.hi, 1)} MHz: ${s.rate} transmissions per hour, ${Math.round(s.share * 100)} % of listening time`);
  });
}

function showSegTip(el, idx) {
  const s = S.state && S.state.radio.scanner.segments.find((x) => x.idx === idx);
  if (!s) return;
  const tip = $("scan-tip");
  tip.replaceChildren(h("strong", { text: `${fmtNum(s.rate, 1)} transmissions / h` }),
    h("span", { text: `${mhz(s.lo, 1)}–${mhz(s.hi, 1)} MHz` }),
    h("span", { text: `${Math.round(s.share * 100)} % of listening time` }),
    h("span", { text: s.last_visit ? `last visit ${fmtAgo(s.last_visit)}` : "not visited yet" }));
  tip.hidden = false;
  const box = $("scan-box").getBoundingClientRect(), r = el.getBoundingClientRect();
  tip.style.left = `${Math.max(0, Math.min(r.left - box.left, box.width - tip.offsetWidth))}px`;
  tip.style.top = `${r.bottom - box.top + 10}px`;
}

function voiceRow(c) {
  const bar = h("span");
  const name = c.source === "provisional" ? "New channel?" : (c.label || "Voice");
  const row = h("li", { class: "chan" }, listenButton(c.id, name, c.source !== "provisional"),
    h("div", {}, h("span", { class: "name", text: name }), " ", h("span", { class: "freq", text: `${mhz(c.freq_hz)} MHz` })),
    h("div", { class: "tools" }), h("div", { class: "meter" }, bar));
  meterEls.set(c.id, { bar, row });
  return row;
}

function dataRow(c) {
  const bar = h("span");
  const row = h("li", { class: `chan ${c.kind}${c.inside ? "" : " outside"}` },
    h("span", { class: `kind ${c.kind}`, text: c.kind.toUpperCase() }),
    h("div", {}, h("span", { class: "freq", text: `${mhz(c.freq_hz)} MHz${c.inside ? "" : " · not in window"}` })),
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
  [lo + span * 0.1, sp.center_hz, lo + span * 0.9].forEach((t, i) => {
    const label = mhz(t, 2), tw = g.measureText(label).width;
    g.fillText(label, i === 0 ? x(t) : i === 2 ? x(t) - tw : x(t) - tw / 2, ht - 2);
  });
}

// ---------------------------------------------------------------------------------------------
// channels: list, details, transmission timeline
// ---------------------------------------------------------------------------------------------
let chTimer = null;
function scheduleChannels(delay = 2500) {
  if (chTimer) return;
  chTimer = setTimeout(() => { chTimer = null; if (!$("pane-channels").hidden) loadChannels(); }, delay);
}

async function loadChannels() {
  try { S.channels = await api("/api/channels"); } catch { return; }
  renderChannels();
  if (S.chSelected) loadHistory();
}

function sparkline(counts) {
  const max = Math.max(...counts, 1), w = 72, ht = 18, bw = w / counts.length;
  return svg("svg", { class: "sparkline", width: w, height: ht, viewBox: `0 0 ${w} ${ht}`, "aria-hidden": "true" },
    ...counts.map((c, i) => {
      const bh = c ? Math.max(2, (c / max) * ht) : 1;
      return svg("rect", { x: (i * bw + 0.5).toFixed(1), y: (ht - bh).toFixed(1), width: (bw - 1).toFixed(1), height: bh.toFixed(1),
        class: i === counts.length - 1 ? "now" : "" });
    }));
}

function renderChannels() {
  const q = $("ch-search").value.trim().toLowerCase();
  const alive = S.channels.filter((c) => c.status === "alive"), dead = S.channels.filter((c) => c.status === "dead");
  $("n-alive").textContent = alive.length || "";
  $("n-dead").textContent = dead.length || "";
  let rows = S.chStatus === "all" ? S.channels : S.chStatus === "alive" ? alive : dead;
  if (q) rows = rows.filter((c) => (c.label || "").toLowerCase().includes(q) || mhz(c.freq_hz).includes(q));
  rows = [...rows].sort((a, b) => (a.status === b.status ? 0 : a.status === "alive" ? -1 : 1) || b.rate - a.rate ||
    (b.last_heard || 0) - (a.last_heard || 0));
  const views = new Map(((S.state && S.state.radio.channels) || []).map((v) => [v.id, v]));
  $("ch-list").replaceChildren(...rows.map((c) => {
    const name = c.label || "Unnamed";
    const live = views.get(c.id);
    const row = h("li", { class: `ch-row${c.id === S.chSelected ? " sel" : ""}${c.status === "dead" ? " dead" : ""}${live && live.open ? " open" : ""}`,
      tabindex: "0", onclick: () => selectChannel(c.id), onkeydown: (e) => { if (e.key === "Enter") selectChannel(c.id); } },
    c.status === "alive" ? listenButton(c.id, name) : h("span"),
    h("div", {}, h("span", { class: "name", text: name }),
      c.status === "dead" ? h("span", { class: "tag dead", text: "dead" }) : null,
      c.pinned ? h("span", { class: "tag pinned", text: "pinned" }) : c.source === "detected" ? h("span", { class: "tag", text: "found" }) : null),
    h("div", { class: "spark" }, h("span", { class: "rate" }, fmtNum(c.rate, 1), h("small", { text: " /h" })), sparkline(c.last_24h)),
    h("div", { class: "meta" }, h("span", { text: `${mhz(c.freq_hz)} MHz` }),
      h("span", { text: c.last_heard ? `heard ${fmtAgo(c.last_heard)}` : "not heard yet" }),
      h("span", { text: `${c.tx_24h} in 24 h` })));
    return row;
  }));
  $("ch-empty").hidden = rows.length > 0;
  $("ch-detail").hidden = !S.chSelected;
}

function selectChannel(id) {
  S.chSelected = id;
  S.chBucket = null;
  S.chHistory = null;
  renderChannels();
  loadHistory();
}

async function loadHistory() {
  const id = S.chSelected;
  if (!id) return;
  try {
    const hist = await api(`/api/channels/${encodeURIComponent(id)}/history?hours=${S.chHours}`);
    if (id !== S.chSelected) return;
    S.chHistory = hist;
  } catch (e) {
    if (/No such channel/.test(e.message)) { S.chSelected = null; renderChannels(); }
    return;
  }
  renderDetail();
}

function renderDetail() {
  const hist = S.chHistory;
  if (!hist) return;
  const c = hist.channel;
  if (document.activeElement !== $("d-label")) $("d-label").value = c.label || "";
  $("d-sub").textContent = `${mhz(c.freq_hz)} MHz · ${c.source === "detected" ? "found by the scanner" : "added by hand"}${c.status === "dead" ? " · no transmissions for over a day" : ""}`;
  $("d-pin").setAttribute("aria-pressed", String(!!c.pinned));
  $("d-pin").textContent = c.pinned ? "Pinned" : "Pin";
  $("d-pin").title = c.pinned ? "Kept even when silent" : "Keep this channel even if it goes silent for a day";
  $("d-listen").setAttribute("aria-pressed", String(S.listen.has(c.id)));
  $("d-listen").textContent = S.listen.has(c.id) ? "Stop listening" : "Listen";
  $("d-listen").disabled = c.status !== "alive";
  const n = hist.transmissions.length;
  $("d-stats").replaceChildren(
    fact("Activeness", `${fmtNum(c.rate, 1)} / h`), fact(S.chHours > 24 ? "In 7 days" : "In 24 h", String(n)),
    fact("Last heard", c.last_heard ? fmtAgo(c.last_heard) : "never"),
    fact("Listened", fmtDur(c.listened_s)), fact("All time", String(c.tx_count)), fact("Status", c.status));
  for (const b of $("d-range").querySelectorAll("button")) b.setAttribute("aria-pressed", String(Number(b.dataset.hours) === S.chHours));
  $("d-chart-title").textContent = S.chHours > 24 ? "Transmissions per 2 hours" : "Transmissions per 30 minutes";
  drawTimeline();
  renderDetailRecordings();
}

function bucketing() {
  const hist = S.chHistory, size = S.chHours > 24 ? 7200 : 1800, n = Math.round(S.chHours * 3600 / size);
  const end = Math.ceil(hist.until / size) * size, start = end - n * size;
  const counts = Array(n).fill(0), talk = Array(n).fill(0), cover = Array(n).fill(0);
  for (const t of hist.transmissions) {
    const i = Math.floor((t.start - start) / size);
    if (i >= 0 && i < n) { counts[i]++; talk[i] += t.duration; }
  }
  for (const [a, b] of hist.coverage) {
    for (let i = Math.max(0, Math.floor((a - start) / size)); i < n && start + i * size < b; i++) {
      const lo = Math.max(a, start + i * size), hi = Math.min(b, start + (i + 1) * size);
      if (hi > lo) cover[i] += (hi - lo) / size;
    }
  }
  return { size, n, start, end, counts, talk, cover };
}

function niceStep(max) {
  if (max <= 4) return 1;
  const raw = max / 3, p = 10 ** Math.floor(Math.log10(raw));
  return [1, 2, 5, 10].map((m) => m * p).find((s) => s >= raw);
}

function drawTimeline() {
  const box = $("d-chart");
  const W = Math.max(260, box.clientWidth || 360), H = 156;
  const B = bucketing();
  const pad = { l: 28, r: 6, t: 10, b: 38 }, pw = W - pad.l - pad.r, ph = H - pad.t - pad.b, bw = pw / B.n;
  const step = niceStep(Math.max(...B.counts, 1)), top = Math.max(step, Math.ceil(Math.max(...B.counts, 1) / step) * step);
  const y = (v) => pad.t + ph - (v / top) * ph, xt = (t) => pad.l + ((t - B.start) / (B.end - B.start)) * pw;
  const root = svg("svg", { viewBox: `0 0 ${W} ${H}`, height: H, role: "group", "aria-label": $("d-chart-title").textContent });
  for (let v = 0; v <= top; v += step) {
    root.append(svg("line", { class: "grid", x1: pad.l, x2: W - pad.r, y1: y(v), y2: y(v) }),
      svg("text", { class: "tick", x: pad.l - 6, y: y(v) + 3, "text-anchor": "end" }, document.createTextNode(String(v))));
  }
  const barW = Math.min(24, Math.max(1, bw - 2));                  // 2 px surface gap between neighbours
  for (let i = 0; i < B.n; i++) {
    const g = svg("g", { class: `b${S.chBucket === i ? " picked" : ""}`, tabindex: "0", role: "button",
      "aria-label": `${tipLabel(B, i)}: ${B.counts[i]} transmissions` });
    g.append(svg("rect", { class: "hit", x: pad.l + i * bw, y: pad.t, width: bw, height: ph }));
    if (B.counts[i]) {
      const x0 = pad.l + i * bw + (bw - barW) / 2, y0 = y(B.counts[i]), y1 = y(0), r = Math.min(4, barW / 2, y1 - y0);
      g.append(svg("path", { class: "bar", d: `M${x0},${y1}V${y0 + r}Q${x0},${y0} ${x0 + r},${y0}H${x0 + barW - r}Q${x0 + barW},${y0} ${x0 + barW},${y0 + r}V${y1}Z` }));
    }
    const show = () => showChartTip(B, i, pad.l + (i + 0.5) * bw, W);
    g.addEventListener("pointerenter", show);
    g.addEventListener("focus", show);
    g.addEventListener("pointerleave", hideChartTip);
    g.addEventListener("blur", hideChartTip);
    g.addEventListener("click", () => pickBucket(i));
    g.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); pickBucket(i); } });
    root.append(g);
  }
  const cy = pad.t + ph + 6;                                         // listening coverage under the axis
  root.append(svg("rect", { class: "covbg", x: pad.l, y: cy, width: pw, height: 4, rx: 2 }));
  for (const [a, b] of S.chHistory.coverage) {
    const x0 = xt(Math.max(a, B.start)), x1 = xt(Math.min(b, B.end));
    if (x1 > x0) root.append(svg("rect", { class: "cov", x: x0, y: cy, width: Math.max(1, x1 - x0), height: 4, rx: 1 }));
  }
  const labels = [];
  if (S.chHours <= 24) {
    for (let t = Math.ceil(B.start / 21600) * 21600; t <= B.end; t += 21600) labels.push([t, new Date(t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })]);
  } else {
    const d = new Date(B.start * 1000); d.setHours(24, 0, 0, 0);
    for (let t = d.getTime() / 1000; t < B.end; t += 86400) labels.push([t, new Date(t * 1000).toLocaleDateString([], { weekday: "short" })]);
  }
  for (const [t, text] of labels) {
    const x = xt(t);
    if (x < pad.l + 8 || x > W - pad.r - 8) continue;
    root.append(svg("text", { class: "tick", x, y: H - 8, "text-anchor": "middle" }, document.createTextNode(text)));
  }
  if (!B.counts.some(Boolean)) {
    root.append(svg("text", { class: "empty", x: pad.l + pw / 2, y: pad.t + ph / 2, "text-anchor": "middle" },
      document.createTextNode("No transmissions in this period")));
  }
  const tip = h("div", { id: "chart-tip", class: "tooltip", role: "tooltip", hidden: true });
  box.replaceChildren(root, tip);
}

function tipLabel(B, i) {
  const a = new Date((B.start + i * B.size) * 1000), b = new Date((B.start + (i + 1) * B.size) * 1000);
  const t = (d) => d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  return `${S.chHours > 24 ? `${a.toLocaleDateString([], { weekday: "short" })} ` : ""}${t(a)}–${t(b)}`;
}

function showChartTip(B, i, x, W) {
  const tip = $("chart-tip");
  if (!tip) return;
  const cov = Math.min(1, B.cover[i]);
  tip.replaceChildren(h("strong", { text: `${B.counts[i]} transmission${B.counts[i] === 1 ? "" : "s"}` }),
    h("span", { text: tipLabel(B, i) }),
    B.counts[i] ? h("span", { text: `${fmtDur(B.talk[i])} of talk` }) : null,
    h("span", { text: cov > 0 ? `listened ${Math.round(cov * 100)} % of the time` : "not listened to" }));
  tip.hidden = false;
  tip.style.left = `${Math.max(0, Math.min(x - tip.offsetWidth / 2, W - tip.offsetWidth))}px`;
  tip.style.top = "0px";
}
function hideChartTip() { const t = $("chart-tip"); if (t) t.hidden = true; }

function pickBucket(i) {
  S.chBucket = S.chBucket === i ? null : i;
  drawTimeline();
  renderDetailRecordings();
}

function renderDetailRecordings() {
  const hist = S.chHistory;
  let list = hist.transmissions;
  const B = bucketing();
  if (S.chBucket !== null) {
    const a = B.start + S.chBucket * B.size, b = a + B.size;
    list = list.filter((t) => t.start >= a && t.start < b);
    $("d-rec-filter").textContent = `Showing ${tipLabel(B, S.chBucket)}`;
  }
  $("d-rec-filter").hidden = S.chBucket === null;
  $("d-showall").hidden = S.chBucket === null;
  const withDay = S.chHours > 24;
  $("d-recs").replaceChildren(...list.slice(0, 300).map((t) => recordingItem(t, null, withDay)));
  $("d-recs-empty").hidden = list.length > 0;
}

// ---------------------------------------------------------------------------------------------
// audio: live listening and recordings
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
  chanKey = "";
  if (S.state) renderRadio(S.state.radio);
  renderChannels();
  if (S.chHistory) renderDetail();
}

let playing = null;
function recordingItem(t, label, withDay = false) {
  const isPlaying = playing && playing.id === t.id;
  const when = new Date(t.start * 1000);
  const btn = h("button", { type: "button", class: "small", text: isPlaying ? "■" : "▶", disabled: !t.id,
    "aria-label": `${isPlaying ? "Stop" : "Play"} recording from ${when.toLocaleString()}`, onclick: () => playRecording(t) });
  return h("li", { class: `clip${isPlaying ? " playing" : ""}` },
    h("time", { text: withDay ? `${when.toLocaleDateString([], { weekday: "short" })} ${fmtClock(t.start)}` : fmtClock(t.start) }),
    h("span", { class: "what" }, label === null ? "" : (label || "Voice"),
      h("small", { text: `${label === null ? "" : `${mhz(t.freq_hz)} · `}${t.duration.toFixed(1)} s` })),
    btn);
}

function renderClips() {
  $("clips").replaceChildren(...S.recent.slice(0, 60).map((t) => recordingItem(t, t.label)));
  $("clips-empty").hidden = S.recent.length > 0;
}

function playRecording(t) {
  if (playing) {
    playing.audio.pause();
    const same = playing.id === t.id;
    playing = null;
    renderClips();
    if (S.chHistory) renderDetailRecordings();
    if (same) return;
  }
  const audio = new Audio(`/api/recordings/${t.id}.wav`);
  audio.volume = Math.min(1, Number($("volume").value));
  playing = { id: t.id, audio };
  audio.addEventListener("ended", () => { playing = null; renderClips(); if (S.chHistory) renderDetailRecordings(); });
  audio.play().catch(() => toast("That recording is no longer kept.", true));
  renderClips();
  if (S.chHistory) renderDetailRecordings();
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
  const center = (r.window[0] + r.window[1]) / 2;
  chipR.querySelector(".t").textContent = r.state === "running" ? `${r.config.mode === "scan" ? "Scanning" : "Radio"} · ${mhz(center)} MHz`
    : r.state === "error" ? "Radio problem" : r.config.running ? "Radio connecting" : "Radio off";
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
      S.recent = m.transmissions;
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
    } else if (m.type === "transmission") {
      S.recent.unshift(m.transmission);
      if (S.recent.length > 300) S.recent.length = 300;
      renderClips();
      scheduleChannels();
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
const TABS = ["traffic", "radio", "channels", "messages"];
function showTab(name) {
  for (const t of TABS) {
    $(`tab-${t}`).setAttribute("aria-selected", String(t === name));
    $(`pane-${t}`).hidden = t !== name;
  }
  if (name === "radio") drawSpectrum();
  if (name === "channels") loadChannels();
  if (name !== "channels" && document.querySelector(".layout").classList.contains("wide")) setWide(false);
  try { localStorage.setItem("airdesk-tab", name); } catch { /* storage may be unavailable */ }
}

function setWide(on) {
  document.querySelector(".layout").classList.toggle("wide", on);
  $("ch-expand").setAttribute("aria-pressed", String(on));
  $("ch-expand").textContent = on ? "Collapse" : "Expand";
  setTimeout(() => { map.invalidateSize(); if (S.chHistory) drawTimeline(); }, 50);
}

function init() {
  for (const t of TABS) $(`tab-${t}`).addEventListener("click", () => showTab(t));
  let saved = "traffic";
  try { saved = localStorage.getItem("airdesk-tab") || "traffic"; } catch { /* ignore */ }
  showTab(TABS.includes(saved) ? saved : "traffic");

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

  // radio
  $("r-toggle").addEventListener("click", () => act(S.state && S.state.radio.config.running ? "/api/radio/stop" : "/api/radio/start", {}));
  for (const b of $("mode").querySelectorAll("button")) b.addEventListener("click", () => act("/api/radio/mode", { mode: b.dataset.mode }));
  $("r-center").addEventListener("change", () => act("/api/radio/settings", { center_mhz: Number($("r-center").value) }));
  let gainTimer;
  $("r-gain").addEventListener("input", () => {
    $("r-gain-out").textContent = $("r-gain").value;
    clearTimeout(gainTimer);
    gainTimer = setTimeout(() => act("/api/radio/settings", { gain: Number($("r-gain").value) }), 250);
  });
  $("keep-clips").addEventListener("change", () => act("/api/radio/settings", { keep_clips: $("keep-clips").checked }));
  $("volume").addEventListener("input", () => { if (gainNode) gainNode.gain.value = Number($("volume").value); });
  window.addEventListener("resize", () => { drawSpectrum(); if (S.chHistory && !$("pane-channels").hidden) drawTimeline(); });

  // channels
  for (const b of $("ch-filter").querySelectorAll("button")) {
    b.addEventListener("click", () => {
      S.chStatus = b.dataset.status;
      for (const o of $("ch-filter").querySelectorAll("button")) o.setAttribute("aria-pressed", String(o === b));
      renderChannels();
    });
  }
  $("ch-search").addEventListener("input", renderChannels);
  $("ch-expand").addEventListener("click", () => setWide(!document.querySelector(".layout").classList.contains("wide")));
  $("add-chan").addEventListener("submit", async (e) => {
    e.preventDefault();
    const st = await act("/api/channels", { freq_mhz: Number($("add-freq").value), label: $("add-label").value });
    if (st) { $("add-chan").reset(); toast("Channel added."); loadChannels(); }
  });
  const saveLabel = async () => {
    const c = S.chHistory && S.chHistory.channel;
    if (!c || $("d-label").value.trim() === (c.label || "")) return;
    if (await act(`/api/channels/${encodeURIComponent(c.id)}`, { label: $("d-label").value.trim() })) { toast("Renamed."); loadChannels(); }
  };
  $("d-label").addEventListener("change", saveLabel);
  $("d-label").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); $("d-label").blur(); } });
  $("d-pin").addEventListener("click", async () => {
    const c = S.chHistory && S.chHistory.channel;
    if (c && await act(`/api/channels/${encodeURIComponent(c.id)}`, { pinned: !c.pinned })) loadChannels();
  });
  $("d-listen").addEventListener("click", () => { if (S.chSelected) toggleListen(S.chSelected); });
  $("d-delete").addEventListener("click", async () => {
    const c = S.chHistory && S.chHistory.channel;
    if (!c || !window.confirm(`Delete ${c.label || mhz(c.freq_hz) + " MHz"} and all its recordings?`)) return;
    if (await act(`/api/channels/${encodeURIComponent(c.id)}`, undefined, "DELETE")) {
      S.listen.delete(c.id);
      S.chSelected = null;
      S.chHistory = null;
      loadChannels();
    }
  });
  $("d-close").addEventListener("click", () => { S.chSelected = null; S.chHistory = null; renderChannels(); });
  for (const b of $("d-range").querySelectorAll("button")) {
    b.addEventListener("click", () => { S.chHours = Number(b.dataset.hours); S.chBucket = null; loadHistory(); });
  }
  $("d-showall").addEventListener("click", () => { S.chBucket = null; drawTimeline(); renderDetailRecordings(); });

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

  setInterval(() => { if (S.state) renderClips(); if (!$("pane-channels").hidden) loadChannels(); }, 20000);
  connect();
}

init();
