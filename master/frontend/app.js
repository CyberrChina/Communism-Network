const API_BASE = window.NETWORK_API_BASE || "../backend/api";
let csrfToken = "";

async function api(path, options = {}) {
  const method = (options.method || "GET").toUpperCase();
  const headers = { "Content-Type": "application/json", ...(options.headers || {}) };
  if (csrfToken && method !== "GET") headers["X-CSRF-Token"] = csrfToken;
  const response = await fetch(`${API_BASE}${path}`, {
    credentials: "same-origin",
    headers,
    ...options,
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || "The network request failed.");
  if (data.csrf_token) csrfToken = data.csrf_token;
  return data;
}

function setFormBusy(form, busy) {
  const button = form.querySelector("button[type=submit]");
  if (!button) return;
  button.disabled = busy;
  button.textContent = busy ? "Working..." : button.dataset.label;
}

function showError(form, message) {
  const target = form.querySelector(".form-error");
  target.textContent = message;
  target.hidden = false;
}

function bindAuthForm() {
  const form = document.querySelector("[data-auth-form]");
  if (!form) return;
  const button = form.querySelector("button[type=submit]");
  button.dataset.label = button.textContent;
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    form.querySelector(".form-error").hidden = true;
    const data = Object.fromEntries(new FormData(form));
    if (form.dataset.auth === "signup" && data.password !== data.confirm_password) {
      showError(form, "Passwords do not match.");
      return;
    }
    setFormBusy(form, true);
    try {
      await api(form.dataset.auth === "signup" ? "/auth/signup" : "/auth/login", { method: "POST", body: JSON.stringify(data) });
      window.location.href = "index.html";
    } catch (error) {
      showError(form, error.message);
      setFormBusy(form, false);
    }
  });
}

function roomCard(room, action) {
  const card = document.createElement("article");
  card.className = "room-card";
  const joined = room.joined || action === "open";
  card.innerHTML = `<div><h3></h3><p></p></div><div class="room-actions"><span class="status">${joined ? "Joined room" : "Public room"}</span><button class="button small ${joined ? "primary" : "ghost"}">${joined ? "Open room" : "Join room"}</button></div>`;
  card.querySelector("h3").textContent = room.room_name || room.name || "Unnamed room";
  card.querySelector("p").textContent = room.server_address || room.ip_address || "Room backend unavailable";
  const button = card.querySelector("button");
  button.addEventListener("click", async () => {
    if (joined) {
      window.location.href = `room.html?room=${encodeURIComponent(room.room_id || room.id)}`;
      return;
    }
    button.disabled = true;
    try {
      await api("/servers/join", { method: "POST", body: JSON.stringify({ room_id: room.room_id }) });
      card.replaceWith(roomCard({ ...room, joined: true }, "open"));
      loadJoinedRooms();
    } catch (error) {
      button.disabled = false;
      showToast(error.message);
    }
  });
  return card;
}

async function loadJoinedRooms() {
  const container = document.querySelector("[data-joined-rooms]");
  if (!container) return;
  try {
    const data = await api("/servers/joined");
    container.replaceChildren();
    if (!data.servers.length) {
      container.innerHTML = '<div class="empty">You have not joined a room yet. Visit Discover to find one.</div>';
      return;
    }
    data.servers.forEach((server) => container.appendChild(roomCard({ room_id: server.room_id || server.id, room_name: server.name || "Joined room", server_address: server.server_address || `${server.ip_address}:${server.port}` }, "open")));
  } catch (error) {
    container.innerHTML = `<div class="empty">${error.message}</div>`;
  }
}

async function loadDiscoverRooms() {
  const container = document.querySelector("[data-discover-rooms]");
  if (!container) return;
  try {
    const data = await api("/servers/discover");
    container.replaceChildren();
    if (!data.rooms.length) {
      container.innerHTML = '<div class="empty">No room servers have registered yet.</div>';
      return;
    }
    data.rooms.forEach((room) => container.appendChild(roomCard(room)));
  } catch (error) {
    container.innerHTML = `<div class="empty">${error.message}</div>`;
  }
}

async function bindLogout() {
  const button = document.querySelector("[data-logout]");
  if (!button) return;
  button.addEventListener("click", async () => { await api("/auth/logout", { method: "POST" }); window.location.href = "login.html"; });
}

function showToast(message) {
  const toast = document.createElement("div");
  toast.className = "toast";
  toast.textContent = message;
  document.body.appendChild(toast);
  setTimeout(() => toast.remove(), 3200);
}

bindAuthForm();
bindLogout();
api("/auth/me").catch(() => {});
loadJoinedRooms();
loadDiscoverRooms();
