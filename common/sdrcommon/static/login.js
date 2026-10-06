"use strict";

const $ = (id) => document.getElementById(id);

async function post(path, body) {
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-App-Request": "1" },
    body: JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const d = data.detail;
    throw new Error(Array.isArray(d) ? d.map((x) => x.msg).join("; ") : d || `Request failed (${res.status}).`);
  }
  return data;
}

function show(id) {
  for (const x of ["login", "setup", "remote"]) $(x).hidden = x !== id;
  const first = $(id).querySelector("input:not([value]), input");
  if (first) first.focus();
}

fetch("/api/login-info").then((r) => r.json()).then((info) => {
  if (info.has_users) show("login");
  else if (info.setup_allowed) { show("setup"); $("s-password").focus(); }
  else {
    show("remote");
    $("setup-cmd").textContent = `docker compose exec ${info.app.toLowerCase()} python -m sdrcommon.users add NAME`;
  }
}).catch(() => show("login"));

$("login").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("error").textContent = "";
  $("submit").disabled = true;
  try {
    await post("/api/login", { username: $("username").value.trim(), password: $("password").value });
    location.replace("/");
  } catch (err) {
    $("error").textContent = err.message;
    $("password").select();
  } finally {
    $("submit").disabled = false;
  }
});

$("setup").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("s-error").textContent = "";
  if ($("s-password").value !== $("s-repeat").value) {
    $("s-error").textContent = "The passwords do not match.";
    return;
  }
  $("s-submit").disabled = true;
  try {
    await post("/api/setup", { username: $("s-username").value.trim(), password: $("s-password").value });
    location.replace("/");
  } catch (err) {
    $("s-error").textContent = err.message;
    if (/already exists/.test(err.message)) setTimeout(() => location.reload(), 1500);
  } finally {
    $("s-submit").disabled = false;
  }
});
