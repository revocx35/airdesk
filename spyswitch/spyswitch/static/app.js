"use strict";

const $ = (id) => document.getElementById(id);
let state = null;
const lastJson = {};

function changed(key, value) {
  const j = JSON.stringify(value);
  if (lastJson[key] === j) return false;
  lastJson[key] = j;
  return true;
}

async function api(path, body) {
  const res = await fetch(path, {
    method: body === undefined ? "GET" : "POST",
    headers: { "Content-Type": "application/json", "X-App-Request": "1" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (res.status === 401) { location.replace("/login"); throw new Error("Signed out."); }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || `Request failed (${res.status})`);
  return data;
}

function msg(text, error = false) {
  $("message").textContent = text || "";
  $("message").classList.toggle("error", error);
}

async function act(path, body) {
  try { render(await api(path, body)); msg(""); } catch (e) { msg(e.message, true); }
}

function el(tag, props = {}, ...kids) {
  const e = document.createElement(tag);
  Object.assign(e, props);
  e.append(...kids.filter((k) => k !== null && k !== undefined));
  return e;
}

const fmtFreq = (hz) => (hz ? `${(hz / 1e6).toFixed(3)} MHz` : "frequency not set yet");
const fmtRate = (b) => (b >= 1e6 ? `${(b / 1e6).toFixed(1)} MB/s` : b >= 1e3 ? `${(b / 1e3).toFixed(0)} kB/s` : `${b} B/s`);
function fmtAgo(t) {
  const s = Math.max(0, Date.now() / 1000 - t);
  if (s < 60) return `${Math.round(s)} s`;
  if (s < 3600) return `${Math.round(s / 60)} min`;
  if (s < 86400) return `${(s / 3600).toFixed(1)} h`;
  return `${Math.round(s / 86400)} d`;
}
const fmtTime = (t) => new Date(t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });

function render(s, force = false) {
  state = s;
  if (force) delete lastJson.servers;
  const servers = $("servers");
  if (changed("servers", s.servers)) servers.replaceChildren(...s.servers.map((sv) => {
    const status = !sv.upstream_ok ? ["down", "SpyServer not answering"] : sv.connections.length ? ["busy", "In use"] : ["", "Free"];
    const card = el("article", { className: "server" },
      el("div", { className: "server-head" },
        el("div", {}, el("div", { className: "server-title", textContent: sv.name }),
          el("div", { className: "port", textContent: `port ${sv.port} → ${sv.upstream}` })),
        el("span", { className: `state ${status[0]}`, textContent: status[1] })));
    if (!sv.connections.length) card.append(el("p", { className: "free", textContent: "No app is connected." }));
    for (const c of sv.connections) {
      const b = el("button", { type: "button", textContent: "Disconnect" });
      b.addEventListener("click", () => act(`/api/connections/${c.id}/disconnect`, {}));
      card.append(el("div", { className: "conn" },
        el("div", { className: "conn-name", textContent: `${c.name}` }),
        el("div", { className: "conn-meta" },
          el("span", { textContent: c.ip }),
          el("span", { textContent: fmtFreq(c.freq) }),
          el("span", { textContent: c.gain === null ? "gain auto" : `gain step ${c.gain}` }),
          el("span", { textContent: c.streaming ? fmtRate(c.rate_down) : "not streaming" }),
          el("span", { textContent: `for ${fmtAgo(c.since)}` })),
        b));
    }
    return card;
  }));

  const names = Object.fromEntries(s.servers.map((sv) => [sv.key, sv.name]));
  const tbody = $("apps").querySelector("tbody");
  $("no-apps").hidden = s.apps.length > 0;
  $("apps").hidden = s.apps.length === 0;
  if (changed("apps", [s.apps.map(({ last_seen, ...rest }) => rest), Math.floor(Date.now() / 30000)])) tbody.replaceChildren(...s.apps.map((a) => {
    const input = el("input", { type: "checkbox", checked: a.allowed, role: "switch" });
    input.setAttribute("aria-label", `Allow ${a.name} from ${a.ip}`);
    input.addEventListener("change", () => act("/api/apps/allowed", { key: a.key, allowed: input.checked }));
    const sw = el("label", { className: "switch" }, input, el("span", { className: "track" }), el("span", { className: "knob" }));
    const status = a.connected.length
      ? el("span", { className: "pill on", textContent: `on ${a.connected.map((k) => names[k] || k).join(", ")}` })
      : el("span", { className: `pill ${a.allowed ? "" : "off"}`, textContent: a.allowed ? "not connected" : "blocked" });
    const forget = el("button", { type: "button", textContent: "Forget", disabled: a.connected.length > 0 });
    forget.addEventListener("click", () => act("/api/apps/forget", { key: a.key }));
    return el("tr", {}, el("td", {}, sw), el("td", { className: "app-name", textContent: a.name }),
      el("td", { className: "mono", textContent: a.ip }), el("td", {}, status),
      el("td", { textContent: `${fmtAgo(a.last_seen)} ago` }), el("td", {}, forget));
  }));
  if (document.activeElement !== $("default-allow")) $("default-allow").checked = s.default_allow;

  if (changed("events", s.events)) $("events").replaceChildren(...s.events.map((e) =>
    el("li", {}, el("time", { textContent: fmtTime(e.t) }), el("span", { className: e.kind, textContent: e.text }))));
}

function connect(delay = 500) {
  const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
  ws.onmessage = (ev) => render(JSON.parse(ev.data));
  ws.onopen = () => { delay = 500; };
  ws.onclose = () => {
    fetch("/api/me").then((r) => {
      if (r.status === 401) location.replace("/login");
      else setTimeout(() => connect(Math.min(delay * 2, 8000)), delay);
    }).catch(() => setTimeout(() => connect(Math.min(delay * 2, 8000)), delay));
  };
}

async function init() {
  $("default-allow").addEventListener("change", () => act("/api/settings", { default_allow: $("default-allow").checked }));
  $("logout").addEventListener("click", async () => { await api("/api/logout", {}).catch(() => {}); location.replace("/login"); });
  api("/api/me").then((me) => { $("who").textContent = `Signed in as ${me.user}`; }).catch(() => {});
  render(await api("/api/state"));
  connect();
  setInterval(() => state && render(state, true), 15000);   // refresh the "for N min" texts
}

init().catch((e) => msg(e.message, true));
