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
      btn("Restart", "", () => act(s.id, "restart"), !running)),
    el("div", { class: "actions tools" },
      btn("Backups", "secondary", () => openBackups(s.id), false),
      s.config_file_count > 0 ? btn("Config", "secondary", () => openConfig(s.id), false) : null,
      s.update_method !== "none" ? btn("Update", "secondary", () => openUpdate(s.id), false) : null,
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
  d.className = "";
  d.replaceChildren(...content);
  if (!d.open) d.showModal();
  return d;
}
function openWide(...content) {
  const d = openDialog(...content);
  d.className = "wide";
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
    "Yes, delete the saves and config. A backup is taken first, and if it fails nothing is deleted.");
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
    el("label", { class: "check" }, wipe, "Also delete the data folder (a backup is saved first)"),
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
      const r = await api(`/api/servers/${encodeURIComponent(s.id)}?${q}`, "DELETE");
      await refresh();
      if (r.backup) {
        openDialog(el("form", { method: "dialog" }, el("h3", { textContent: "Server deleted" }),
          el("p", { textContent: r.note }),
          el("div", { class: "dialog-actions" }, el("button", { type: "button", class: "primary", textContent: "OK", onclick: closeDialog }))));
      } else {
        closeDialog();
      }
    } catch (e) {
      err.textContent = e.message;
      err.hidden = false;
    }
  });
  openDialog(form);
}

// ------------------------------------------------------- shared confirm / result views
function confirmView({ title, paragraphs, checkboxLabel, confirmText, danger, onConfirm, onBack }) {
  const err = el("div", { class: "form-error", hidden: true });
  const cb = checkboxLabel ? el("input", { type: "checkbox" }) : null;
  const go = el("button", { type: "submit", class: danger ? "danger" : "primary", textContent: confirmText, disabled: !!cb });
  if (cb) cb.addEventListener("change", () => { go.disabled = !cb.checked; });
  const form = el("form", { method: "dialog" },
    el("h3", { textContent: title }),
    ...paragraphs.map((t) => el("p", { textContent: t })),
    cb ? el("label", { class: "check" }, cb, checkboxLabel) : null,
    err,
    el("div", { class: "dialog-actions" },
      el("button", { type: "button", class: "secondary", textContent: "Cancel", onclick: onBack || closeDialog }), go));
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    err.hidden = true;
    go.disabled = true;
    go.textContent = "Working\u2026";
    try {
      await onConfirm();
    } catch (e) {
      err.textContent = e.message;
      err.hidden = false;
      go.disabled = cb ? !cb.checked : false;
      go.textContent = confirmText;
    }
  });
  return form;
}

function resultView(title, lines, onDone) {
  return el("form", { method: "dialog" },
    el("h3", { textContent: title }),
    ...lines.filter(Boolean).map(([text, cls]) => el("p", { class: cls || "", textContent: text })),
    el("div", { class: "dialog-actions" },
      el("button", { type: "button", class: "primary", textContent: "OK", onclick: onDone || closeDialog })));
}

const findServer = (id) => servers.find((s) => s.id === id);
const sid = (id) => encodeURIComponent(id);
const shortId = (v) => (v ? v.replace("sha256:", "").slice(0, 12) : "unknown");
const fmtSize = (n) => n > 1048576 ? `${(n / 1048576).toFixed(1)} MB` : `${Math.max(1, Math.round(n / 1024))} KB`;
const fmtTime = (iso) => new Date(iso).toLocaleString();

// -------------------------------------------------------------------- backups panel
async function openBackups(id, message) {
  await refresh();
  const s = findServer(id);
  if (!s) return closeDialog();
  const running = s.status === "running";
  let data;
  try { data = await api(`/api/servers/${sid(id)}/backups`); }
  catch (e) { return openDialog(resultView("Backups", [[e.message, "warn"]])); }

  const live = el("input", { type: "checkbox", id: "live-backup" });
  const msg = el("div", { class: message ? "okmsg" : "okmsg", hidden: !message, textContent: message || "" });
  const err = el("div", { class: "form-error", hidden: true });
  const create = el("button", { type: "button", class: "primary", textContent: "Create backup" });
  create.addEventListener("click", async () => {
    create.disabled = true; create.textContent = "Backing up\u2026"; err.hidden = true;
    try {
      const r = await api(`/api/servers/${sid(id)}/backups`, "POST", { live: live.checked });
      const extra = r.warnings.length ? " " + r.warnings.join(" ") : "";
      await openBackups(id, `Backup ${r.backup.name} created. ${r.note}${extra}`);
    } catch (e) { err.textContent = e.message; err.hidden = false; create.disabled = false; create.textContent = "Create backup"; }
  });

  const rows = data.backups.map((b) => el("tr", {},
    el("td", {}, el("code", { textContent: b.name })),
    el("td", {}, el("span", { class: `kind ${b.kind === "manual" ? "" : "auto"}`, textContent: b.kind === "manual" ? "manual" : b.kind.replace("pre-", "auto: before ") })),
    el("td", { textContent: fmtTime(b.created_at) }),
    el("td", { textContent: fmtSize(b.size) }),
    el("td", { class: "rowactions" },
      el("button", { class: "secondary", textContent: "Restore", disabled: running,
        title: running ? "Stop the server to restore" : "", onclick: () => confirmRestore(id, b) }),
      el("button", { class: "danger", textContent: "Delete", onclick: () => confirmDeleteBackup(id, b) }))));

  openWide(el("div", { class: "panel" },
    el("h3", { textContent: `Backups \u2013 ${s.name}` }),
    el("p", { class: "hint", textContent: `Backups of this server's data folder. The newest ${data.retention} are kept; older ones are removed automatically.` }),
    running ? el("p", { class: "warn", textContent: "This server is running. By default it is stopped for the backup (so the files are consistent) and started again afterwards." }) : null,
    running ? el("label", { class: "check" }, live, "Back up without stopping (live) \u2013 files may be inconsistent") : null,
    el("div", { class: "toolbar" }, create),
    msg, err,
    data.backups.length
      ? el("table", { class: "list" },
          el("thead", {}, el("tr", {}, ...["Backup", "Type", "Created", "Size", ""].map((h) => el("th", { textContent: h })))),
          el("tbody", {}, ...rows))
      : el("p", { class: "empty-small", textContent: "No backups yet." }),
    el("div", { class: "dialog-actions" },
      el("button", { type: "button", class: "danger", textContent: "Wipe data\u2026", disabled: running,
        title: running ? "Stop the server first" : "", onclick: () => confirmWipe(id) }),
      el("span", { class: "spacer" }),
      el("button", { type: "button", class: "secondary", textContent: "Close", onclick: closeDialog }))));
}

function confirmRestore(id, b) {
  openDialog(confirmView({
    title: "Restore this backup?",
    paragraphs: [
      `Backup ${b.name} (${fmtTime(b.created_at)}) will replace the server's current data.`,
      "Before anything is replaced, a safety backup of the current data is taken automatically. If that fails, nothing is restored.",
    ],
    checkboxLabel: "Yes, replace the current data with this backup.",
    confirmText: "Restore", danger: true,
    onBack: () => openBackups(id),
    onConfirm: async () => {
      const r = await api(`/api/servers/${sid(id)}/backups/${encodeURIComponent(b.name)}/restore?confirm=true`, "POST");
      await openBackups(id, r.note + (r.warnings.length ? " " + r.warnings.join(" ") : ""));
    },
  }));
}

function confirmDeleteBackup(id, b) {
  openDialog(confirmView({
    title: "Delete this backup?",
    paragraphs: [`${b.name} will be permanently deleted. This cannot be undone.`],
    confirmText: "Delete backup", danger: true,
    onBack: () => openBackups(id),
    onConfirm: async () => {
      await api(`/api/servers/${sid(id)}/backups/${encodeURIComponent(b.name)}?confirm=true`, "DELETE");
      await openBackups(id, `Deleted ${b.name}.`);
    },
  }));
}

function confirmWipe(id) {
  openDialog(confirmView({
    title: "Wipe all data?",
    paragraphs: [
      "This deletes everything in the server's data folder (saves and config). The server and its settings stay.",
      "A backup is taken automatically first. If the backup fails, nothing is deleted.",
    ],
    checkboxLabel: "Yes, delete all of this server's data.",
    confirmText: "Wipe data", danger: true,
    onBack: () => openBackups(id),
    onConfirm: async () => {
      const r = await api(`/api/servers/${sid(id)}/wipe-data?confirm=true`, "POST");
      await openBackups(id, r.note);
    },
  }));
}

// -------------------------------------------------------------------- image update
async function openUpdate(id) {
  await refresh();
  const s = findServer(id);
  if (!s) return;
  let info;
  try { info = await api(`/api/servers/${sid(id)}/image`); }
  catch (e) { return openDialog(resultView("Update", [[e.message, "warn"]])); }
  const running = s.status === "running";
  openDialog(confirmView({
    title: `Update ${s.name}?`,
    paragraphs: [
      `This downloads the newest version of ${info.image} and recreates the container from it. Your data and settings are kept.`,
      `Current image id: ${shortId(info.image_id)}.`,
      running
        ? "The server is running: it will be stopped (with time to save), backed up, updated and started again."
        : "The server is stopped and will stay stopped.",
      "A backup of the data is taken automatically before the update. If the backup fails, the update is cancelled. If the image is already the newest, nothing is changed.",
    ],
    confirmText: "Update", danger: false,
    onConfirm: async () => {
      const r = await api(`/api/servers/${sid(id)}/update-image?confirm=true`, "POST");
      await refresh();
      openDialog(resultView(r.changed ? "Updated" : "Already up to date", [
        [r.note],
        [`Image id before: ${shortId(r.before)}`],
        [`Image id after: ${shortId(r.after)}`],
        ...r.warnings.map((w) => [w, "warn"]),
      ]));
    },
  }));
}

// -------------------------------------------------------------------- config editor
async function openConfig(id) {
  await refresh();
  const s = findServer(id);
  if (!s) return;
  let files;
  try { files = await api(`/api/servers/${sid(id)}/config`); }
  catch (e) { return openDialog(resultView("Config", [[e.message, "warn"]])); }

  const select = el("select", { id: "config-file" }, ...files.map((f) => el("option", { value: f.path, textContent: f.label || f.path })));
  const area = el("textarea", { id: "config-text", spellcheck: false, rows: 18, wrap: "off" });
  const pathLabel = el("div", { class: "meta" });
  const note = el("p", { class: "hint" });
  const err = el("div", { class: "form-error", hidden: true });
  const ok = el("div", { class: "okmsg", hidden: true });
  const save = el("button", { type: "button", class: "primary", textContent: "Save" });
  let current = null;

  async function load() {
    err.hidden = true; ok.hidden = true; save.disabled = true;
    try {
      const r = await api(`/api/servers/${sid(id)}/config/file?path=${encodeURIComponent(select.value)}`);
      current = r;
      area.value = r.content;
      area.disabled = !r.editable_now;
      save.disabled = !r.editable_now;
      pathLabel.replaceChildren("File: ", el("code", { textContent: r.info.path }),
        r.info.backups.length ? ` \u2013 ${r.info.backups.length} automatic .bak cop${r.info.backups.length === 1 ? "y" : "ies"} kept next to it` : "");
      note.textContent = r.note;
    } catch (e) { err.textContent = e.message; err.hidden = false; area.value = ""; area.disabled = true; }
  }
  select.addEventListener("change", load);
  save.addEventListener("click", async () => {
    err.hidden = true; ok.hidden = true; save.disabled = true;
    try {
      const r = await api(`/api/servers/${sid(id)}/config/file`, "PUT", { path: select.value, content: area.value });
      ok.textContent = r.note; ok.hidden = false;
      await load();
      ok.textContent = r.note; ok.hidden = false;
    } catch (e) { err.textContent = e.message; err.hidden = false; save.disabled = false; }
  });

  openWide(el("div", { class: "panel" },
    el("h3", { textContent: `Config \u2013 ${s.name}` }),
    files.length > 1 ? el("label", { class: "field" }, el("span", { textContent: "File" }), select) : select,
    pathLabel, area, note, ok, err,
    el("div", { class: "dialog-actions" },
      el("button", { type: "button", class: "secondary", textContent: "Close", onclick: closeDialog }), save)));
  if (files.length <= 1) select.hidden = true;
  load();
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
