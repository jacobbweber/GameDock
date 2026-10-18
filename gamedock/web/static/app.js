// Plain vanilla JS UI: Dashboard (deployed servers) and Catalog (game templates).
// All text is inserted with textContent (never innerHTML) so catalog files can't inject markup.
"use strict";

const POLL_MS = 3000;
const busy = new Set();          // server ids with an action in flight
let servers = [];
let catalog = [];

const $ = (id) => document.getElementById(id);

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node[k] = v;
  }
  for (const c of children) if (c !== null && c !== undefined && c !== false) node.append(c);
  return node;
}

function showError(msg) {
  const box = $("error");
  box.hidden = !msg;
  box.textContent = msg || "";
}

async function api(path, method = "GET", body = undefined) {
  const opts = { method };
  if (body !== undefined) {
    opts.headers = { "Content-Type": "application/json" };
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(path, opts);
  let data = null;
  try { data = await res.json(); } catch (_) { /* no body */ }
  if (!res.ok) throw new Error((data && data.error) || `${res.status} ${res.statusText}`);
  return data;
}

const portText = (ports) => ports.length ? ports.map((p) => `${p.host}/${p.protocol}`).join(", ") : "none";

// ------------------------------------------------------------------ dashboard
function serverCard(s) {
  const isBusy = busy.has(s.id);
  const running = s.status === "running";
  const label = isBusy ? "working" : s.status.replace("_", " ");
  const badgeClass = isBusy ? "busy" : s.status;

  const btn = (text, cls, onclick, disabled, title) =>
    el("button", { class: cls, textContent: text, disabled: disabled || isBusy, title: title || "", onclick });

  return el("article", { class: "card", id: `card-${s.id}` },
    el("div", { class: "card-head" },
      el("h2", { textContent: s.name }),
      el("span", { class: `badge ${badgeClass}`, textContent: label })),
    el("p", { class: "desc", textContent: s.game_name }),
    el("div", { class: "meta" }, "Image: ", el("code", { textContent: s.image })),
    el("div", { class: "meta", textContent: `Host ports: ${portText(s.ports)}` }),
    s.status === "error" && s.detail ? el("div", { class: "meta", textContent: s.detail }) : null,
    el("div", { class: "actions" },
      btn("Start", "start", () => act(s.id, "start"), running),
      btn("Stop", "stop", () => act(s.id, "stop"), !running),
      btn("Restart", "", () => act(s.id, "restart"), !running),
      el("span", { class: "spacer" }),
      btn("Edit", "secondary", () => openEdit(s), running, running ? "Stop the server to edit it" : ""),
      btn("Delete", "danger", () => openDelete(s), running, running ? "Stop the server to delete it" : "")));
}

function renderServers() {
  $("servers").replaceChildren(...servers.map(serverCard));
  $("empty").hidden = servers.length > 0;
}

async function act(id, action) {
  busy.add(id);
  showError("");
  renderServers();
  try {
    await api(`/api/servers/${encodeURIComponent(id)}/${action}`, "POST");
  } catch (e) {
    showError(`Could not ${action} ${id}: ${e.message}`);
  } finally {
    busy.delete(id);
    await refresh();
  }
}

// -------------------------------------------------------------------- catalog
function catalogCard(e) {
  return el("article", { class: "card", id: `catalog-${e.id}` },
    el("div", { class: "card-head" },
      el("h2", { textContent: e.name }),
      el("button", { class: "primary", textContent: "Deploy", onclick: () => openDeploy(e) })),
    el("p", { class: "desc", textContent: e.description }),
    el("div", { class: "meta" }, "Image: ", el("code", { textContent: e.image })),
    el("div", { class: "meta", textContent: `Default ports: ${portText(e.ports)}` }),
    e.notes ? el("p", { class: "note", textContent: e.notes }) : null);
}

function renderCatalog() {
  $("catalog").replaceChildren(...catalog.map(catalogCard));
}

// -------------------------------------------------------------------- dialogs
function openDialog(...content) {
  const d = $("dialog");
  d.replaceChildren(...content);
  if (!d.open) d.showModal();
  return d;
}
const closeDialog = () => { const d = $("dialog"); if (d.open) d.close(); };

function fieldInput(f, value, editing) {
  const input = el("input", {
    type: f.secret ? "password" : "text",
    name: f.name,
    value: value || "",
    required: f.required && !(editing && f.secret && f.is_set),
    autocomplete: f.secret ? "new-password" : "off",
    placeholder: f.secret && editing && f.is_set ? "(unchanged)" : "",
  });
  input.dataset.env = f.name;
  return el("label", { class: "field" },
    el("span", {}, f.label || f.name, f.required ? el("span", { class: "req", textContent: " *" }) : null),
    input,
    f.description ? el("span", { class: "hint", textContent: f.description }) : null);
}

// Builds the shared deploy/edit form. `ports` = [{protocol, container, host}], `fields` = env field defs.
function buildServerForm({ title, submitText, name, nameEditable = true, ports, fields, values, editing, showStart, onSubmit }) {
  const err = el("div", { class: "form-error", hidden: true });
  const nameInput = el("input", { type: "text", name: "name", value: name, required: true, maxLength: 64, disabled: !nameEditable });
  const portInputs = ports.map((p) => {
    const input = el("input", { type: "number", min: 1, max: 65535, value: p.host, required: true });
    input.dataset.protocol = p.protocol;
    input.dataset.container = p.container;
    return el("div", { class: "portrow" },
      el("span", { class: "lbl", textContent: `Container ${p.container}/${p.protocol} \u2192 host port` }), input);
  });
  const start = showStart ? el("input", { type: "checkbox", id: "start-after" }) : null;
  const submit = el("button", { type: "submit", class: "primary", textContent: submitText });

  const form = el("form", { method: "dialog" },
    el("h3", { textContent: title }),
    el("label", { class: "field" }, el("span", {}, "Name", el("span", { class: "req", textContent: " *" })), nameInput),
    ports.length ? el("h4", { textContent: "Host ports" }) : null,
    ...portInputs,
    fields.length ? el("h4", { textContent: "Settings" }) : null,
    ...fields.map((f) => fieldInput(f, values[f.name], editing)),
    start ? el("label", { class: "check" }, start, "Start after deploy") : null,
    err,
    el("div", { class: "dialog-actions" },
      el("button", { type: "button", class: "secondary", textContent: "Cancel", onclick: closeDialog }),
      submit));

  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    err.hidden = true;
    submit.disabled = true;
    const env = {};
    form.querySelectorAll("input[data-env]").forEach((i) => { env[i.dataset.env] = i.value; });
    const body = {
      name: nameInput.value,
      ports: [...form.querySelectorAll(".portrow input")].map((i) => ({
        protocol: i.dataset.protocol, container: Number(i.dataset.container), host: Number(i.value) })),
      env,
    };
    try {
      await onSubmit(body, start ? start.checked : false);
      closeDialog();
    } catch (e) {
      err.textContent = e.message;
      err.hidden = false;
    } finally {
      submit.disabled = false;
    }
  });
  return form;
}

function openDeploy(entry) {
  const values = Object.fromEntries(entry.fields.map((f) => [f.name, f.default]));
  openDialog(buildServerForm({
    title: `Deploy ${entry.name}`, submitText: "Deploy", name: entry.name,
    ports: entry.ports, fields: entry.fields, values, editing: false, showStart: true,
    onSubmit: async (body, startAfter) => {
      await api("/api/servers", "POST", { game_id: entry.id, start: startAfter, ...body });
      await refresh();
      location.hash = "#/";
    },
  }));
}

function openEdit(s) {
  const values = Object.fromEntries(s.env.map((f) => [f.name, f.value]));
  openDialog(buildServerForm({
    title: `Edit ${s.name}`, submitText: "Save", name: s.name,
    ports: s.ports, fields: s.env, values, editing: true, showStart: false,
    onSubmit: async (body) => {
      await api(`/api/servers/${encodeURIComponent(s.id)}`, "PATCH", body);
      await refresh();
    },
  }));
}

function openDelete(s) {
  const err = el("div", { class: "form-error", hidden: true });
  const second = el("input", { type: "checkbox", id: "confirm-data" });
  const secondRow = el("label", { class: "check", hidden: true }, second,
    "Yes, I understand this permanently deletes saves and config.");
  const wipe = el("input", { type: "checkbox", id: "delete-data" });
  const del = el("button", { type: "submit", class: "danger", textContent: "Delete server" });
  const sync = () => {
    secondRow.hidden = !wipe.checked;
    del.disabled = wipe.checked && !second.checked;
  };
  wipe.addEventListener("change", sync);
  second.addEventListener("change", sync);

  const form = el("form", { method: "dialog" },
    el("h3", { textContent: `Delete ${s.name}?` }),
    el("p", { textContent: "This removes the server and its container. Its data folder (saves and config) is kept on disk." }),
    el("label", { class: "check" }, wipe, "Also delete the data folder"),
    secondRow,
    err,
    el("div", { class: "dialog-actions" },
      el("button", { type: "button", class: "secondary", textContent: "Cancel", onclick: closeDialog }),
      del));
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    err.hidden = true;
    const q = new URLSearchParams({ confirm: "true" });
    if (wipe.checked) { q.set("delete_data", "true"); if (second.checked) q.set("confirm_delete_data", "true"); }
    try {
      await api(`/api/servers/${encodeURIComponent(s.id)}?${q}`, "DELETE");
      closeDialog();
      await refresh();
    } catch (e) {
      err.textContent = e.message;
      err.hidden = false;
    }
  });
  openDialog(form);
}

// -------------------------------------------------------------------- routing
function route() {
  const page = location.hash.startsWith("#/catalog") ? "catalog" : "dashboard";
  $("page-dashboard").hidden = page !== "dashboard";
  $("page-catalog").hidden = page !== "catalog";
  $("nav-dashboard").classList.toggle("active", page === "dashboard");
  $("nav-catalog").classList.toggle("active", page === "catalog");
  showError("");
}

async function refresh() {
  try {
    servers = await api("/api/servers");
    $("conn").textContent = "connected";
    $("conn").className = "conn ok";
    renderServers();
  } catch (e) {
    $("conn").textContent = "disconnected";
    $("conn").className = "conn bad";
  }
}

async function loadCatalog() {
  try {
    catalog = await api("/api/catalog");
    renderCatalog();
  } catch (e) {
    showError(`Could not load the catalog: ${e.message}`);
  }
}

window.addEventListener("hashchange", route);
route();
loadCatalog();
refresh();
setInterval(() => { if (busy.size === 0 && !$("dialog").open) refresh(); }, POLL_MS);
