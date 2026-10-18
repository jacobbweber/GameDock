// Plain vanilla JS dashboard. Polls the API and renders one card per server.
// All text is inserted with textContent (never innerHTML) so definitions can't inject markup.
"use strict";

const POLL_MS = 3000;
const busy = new Set();      // server ids with an action in flight
let servers = [];

const $ = (id) => document.getElementById(id);

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === "class") node.className = v;
    else if (k === "onclick") node.addEventListener("click", v);
    else node[k] = v;
  }
  for (const c of children) node.append(c);
  return node;
}

function showError(msg) {
  const box = $("error");
  box.hidden = !msg;
  box.textContent = msg || "";
}

async function api(path, method = "GET") {
  const res = await fetch(path, { method });
  let body = null;
  try { body = await res.json(); } catch (_) { /* ignore */ }
  if (!res.ok) throw new Error((body && body.error) || `${res.status} ${res.statusText}`);
  return body;
}

function card(s) {
  const isBusy = busy.has(s.id);
  const running = s.status === "running";
  const label = isBusy ? "working" : s.status.replace("_", " ");
  const badgeClass = isBusy ? "busy" : s.status;

  const ports = s.ports.length
    ? s.ports.map((p) => `${p.host}/${p.protocol}`).join(", ")
    : "none";

  const btn = (text, cls, action, disabled) =>
    el("button", { class: cls, textContent: text, disabled: disabled || isBusy,
                   onclick: () => act(s.id, action) });

  return el("article", { class: "card", id: `card-${s.id}` },
    el("div", { class: "card-head" },
      el("h2", { textContent: s.name }),
      el("span", { class: `badge ${badgeClass}`, textContent: label })),
    s.description ? el("p", { class: "desc", textContent: s.description }) : "",
    el("div", { class: "meta" }, "Image: ", el("code", { textContent: s.image })),
    el("div", { class: "meta", textContent: `Ports: ${ports}` }),
    s.status === "error" && s.detail ? el("div", { class: "meta", textContent: s.detail }) : "",
    el("div", { class: "actions" },
      btn("Start", "start", "start", running),
      btn("Stop", "stop", "stop", !running),
      btn("Restart", "", "restart", !running)));
}

function render() {
  const root = $("servers");
  root.replaceChildren(...servers.map(card));
  $("empty").hidden = servers.length > 0;
}

async function refresh() {
  try {
    servers = await api("/api/servers");
    $("conn").textContent = "connected";
    $("conn").className = "conn ok";
    render();
  } catch (e) {
    $("conn").textContent = "disconnected";
    $("conn").className = "conn bad";
  }
}

async function act(id, action) {
  busy.add(id);
  showError("");
  render();
  try {
    await api(`/api/servers/${encodeURIComponent(id)}/${action}`, "POST");
  } catch (e) {
    showError(`Could not ${action} ${id}: ${e.message}`);
  } finally {
    busy.delete(id);
    await refresh();
  }
}

refresh();
setInterval(() => { if (busy.size === 0) refresh(); }, POLL_MS);
