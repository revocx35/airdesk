"use strict";

const form = document.getElementById("login");
const error = document.getElementById("error");

fetch("/api/login-info").then((r) => r.json()).then((info) => {
  if (!info.has_users) {
    form.hidden = true;
    document.getElementById("setup").hidden = false;
    document.getElementById("setup-cmd").textContent =
      `docker compose exec ${info.app.toLowerCase()} python -m sdrcommon.users add NAME`;
  }
}).catch(() => {});

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  error.textContent = "";
  const button = document.getElementById("submit");
  button.disabled = true;
  try {
    const res = await fetch("/api/login", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-App-Request": "1" },
      body: JSON.stringify({
        username: document.getElementById("username").value.trim(),
        password: document.getElementById("password").value,
      }),
    });
    if (res.ok) {
      location.replace("/");
      return;
    }
    const data = await res.json().catch(() => ({}));
    error.textContent = data.detail || `Sign-in failed (${res.status}).`;
    document.getElementById("password").select();
  } catch {
    error.textContent = "Could not reach the app. Check the connection and try again.";
  } finally {
    button.disabled = false;
  }
});
