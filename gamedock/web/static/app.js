// Plain vanilla JS UI: Dashboard (deployed servers) and Catalog (game templates).
// All text is inserted with textContent (never innerHTML) so catalog files can't inject markup.
"use strict";

const POLL_MS = 3000;
const busy = new Map();          // server id -> what this page asked for (starting, stopping...), while the request runs
let servers = [];
let catalog = [];
let stats = null;                // latest /api/stats snapshot
let history = { servers: {}, host: [] };
let rankSort = "cpu";            // monitoring table sort: cpu | memory | name
let pollCount = 0;
let serversLoaded = false;      // false until the first answer, so we can show "Loading" instead of "No servers"
let appStatus = null;          // latest /api/status
let connLost = false;

const $ = (id) => document.getElementById(id);

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (k.startsWith("aria-") || k.startsWith("data-") || k === "role") node.setAttribute(k, v);
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

const fmtBytes = (n) => {
  if (n === null || n === undefined) return "n/a";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0, v = n;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return `${v >= 100 || i === 0 ? Math.round(v) : v.toFixed(1)} ${u[i]}`;
};
const fmtRate = (n) => `${fmtBytes(n)}/s`;
const fmtPct = (n) => `${n === null || n === undefined ? "n/a" : n.toFixed(n >= 100 ? 0 : 1)}%`;
function fmtUptime(sec) {
  if (sec === null || sec === undefined) return "\u2013";
  const d = Math.floor(sec / 86400), h = Math.floor(sec % 86400 / 3600), m = Math.floor(sec % 3600 / 60);
  if (d) return `${d}d ${h}h`;
  if (h) return `${h}h ${m}m`;
  if (m) return `${m}m`;
  return `${Math.floor(sec)}s`;
}

// Tiny inline SVG sparkline. values: numbers; max: fixed scale top (else the data's own max).
function sparkline(values, { max, cls = "" } = {}) {
  const W = 96, H = 24, ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.setAttribute("class", `spark ${cls}`);
  svg.setAttribute("aria-hidden", "true");
  if (values.length < 2) return svg;
  const top = Math.max(max || 0, ...values, 0.0001);
  const pts = values.map((v, i) => `${(i / (values.length - 1) * (W - 2) + 1).toFixed(1)},${(H - 2 - (v / top) * (H - 4)).toFixed(1)}`);
  const area = document.createElementNS(ns, "polygon");
  area.setAttribute("points", `1,${H - 1} ${pts.join(" ")} ${W - 1},${H - 1}`);
  area.setAttribute("class", "area");
  const line = document.createElementNS(ns, "polyline");
  line.setAttribute("points", pts.join(" "));
  svg.append(area, line);
  return svg;
}

const portText = (ports) => ports.length ? ports.map((p) => `${p.host}/${p.protocol}`).join(", ") : "none";

// ------------------------------------------------------------------ dashboard
// What GameDock itself is doing right now (also reported by the server, so it survives a page reload).
const ACTIVITY = {
  start: "starting", stop: "stopping", restart: "restarting",
};
const ACTIVITY_LABEL = {
  deploying: "Deploying", starting: "Starting", stopping: "Stopping", restarting: "Restarting",
  "backing up": "Backing up", restoring: "Restoring", updating: "Updating", "wiping data": "Wiping data",
  deleting: "Deleting", "saving settings": "Saving settings", "saving config": "Saving config",
  maintenance: "Maintenance", "updating game": "Updating game",
};
const ACTIVITY_HELP = {
  deploying: "Downloading the game image and creating the server. The first time this can take a few minutes.",
  starting: "Starting the container\u2026",
  stopping: "Stopping. The game gets time to save and shut down cleanly (up to a minute).",
  restarting: "Stopping the game and starting it again\u2026",
  "backing up": "Copying the server's data. A running server is stopped first for a consistent copy and started again afterwards.",
  restoring: "Saving a safety backup, then restoring the chosen backup\u2026",
  updating: "Downloading the newest image, backing up, and recreating the container. This can take several minutes.",
  "wiping data": "Saving a safety backup, then emptying the data folder\u2026",
  deleting: "Removing the server\u2026",
  "saving settings": "Re-creating the container with the new settings\u2026",
  "saving config": "Saving the config file\u2026",
  maintenance: "Running maintenance: backing up, updating the game and its image, and restarting, as chosen. This can take several minutes. Nothing else can be done to this server meanwhile.",
  "updating game": "Stopping the server, backing it up, and starting it again so the game downloads its new files. This can take many minutes; the game cannot be joined until it is ready.",
};

const spinner = () => el("span", { class: "spinner", "aria-hidden": "true" });

// One answer to "what should this tile say?": { kind, label, working, progress (0-100|null), sub }
function serverState(s) {
  const act = busy.get(s.id) || s.activity;
  if (act) {
    return { kind: "busy", label: `${ACTIVITY_LABEL[act] || act}\u2026`, working: true, progress: null, sub: ACTIVITY_HELP[act] || "" };
  }
  if (s.status === "running") {
    if (s.phase === "installing") {
      const p = typeof s.progress === "number" ? s.progress : null;
      return { kind: "installing", label: p === null ? "Installing" : `Installing ${Math.round(p)}%`, working: true, progress: p,
        sub: s.phase_detail || "Downloading and installing game files. It cannot be joined yet." };
    }
    if (s.phase === "starting") {
      return { kind: "starting", label: "Starting", working: true, progress: null,
        sub: s.phase_detail || "The container is up but the game is not ready to join yet." };
    }
    if (s.phase === "unhealthy") return { kind: "unhealthy", label: "Unhealthy", working: false, progress: null, sub: s.phase_detail };
    return { kind: "running", label: "Running", working: false, progress: null, sub: "", ready: s.ready !== false };
  }
  return { kind: s.status, label: s.status.replace("_", " "), working: false, progress: null, sub: "" };
}

function progressBar(st) {
  const known = st.progress !== null && st.progress !== undefined;
  const bar = el("div", { class: `progress ${known ? "" : "indeterminate"}`, role: "progressbar", "aria-label": st.label,
    "aria-valuemin": 0, "aria-valuemax": 100 }, el("div", { class: "fill", style: known ? `width:${Math.max(2, Math.min(100, st.progress))}%` : "" }));
  if (known) bar.setAttribute("aria-valuenow", String(Math.round(st.progress)));
  return bar;
}

function statusBox(st) {
  const box = el("div", { class: `statusbox ${st.kind}` });
  if (st.working) {
    box.append(progressBar(st), el("div", { class: "status-text", textContent: st.sub }));
  } else if (st.kind === "unhealthy") {
    box.append(el("div", { class: "status-text warn", textContent: st.sub }));
  } else if (st.kind === "running" && st.ready) {
    box.append(el("div", { class: "status-text ok", textContent: "\u2713 Ready to join" }));
  }
  box.hidden = !box.childNodes.length;
  return box;
}

function badgeNode(st) {
  return el("span", { class: `badge ${st.working ? "busy" : st.kind}`, "aria-live": "polite" }, st.working ? spinner() : null, st.label);
}

function serverCard(s) {
  const st = serverState(s);
  const isBusy = st.kind === "busy";
  const running = s.status === "running";

  const btn = (text, cls, onclick, disabled, title) =>
    el("button", { class: cls, type: "button", textContent: text, disabled: disabled || isBusy, title: title || "",
      onclick, "aria-label": `${text} ${s.name}` });

  return el("article", { class: `card ${st.working ? "working" : ""}`, id: `card-${s.id}` },
    el("div", { class: "card-head" },
      el("h2", { textContent: s.name }),
      badgeNode(st)),
    el("p", { class: "desc", textContent: s.game_name }),
    statusBox(st),
    el("div", { class: "meta" }, "Image: ", el("code", { textContent: s.image })),
    el("div", { class: "meta", textContent: `Host ports: ${portText(s.ports)}` }),
    gameLine(s),
    updateFlag(s),
    schedLine(s),
    s.detail && s.status !== "not_created" ? el("div", { class: s.status === "error" ? "meta problem" : "meta", textContent: s.detail }) : null,
    liveBlock(s),
    el("div", { class: "actions" },
      btn("Start", "start", () => act(s.id, "start"), running),
      btn("Stop", "stop", () => act(s.id, "stop"), !running),
      btn("Restart", "", () => act(s.id, "restart"), !running)),
    el("div", { class: "actions tools" },
      btn("Logs", "secondary", () => openLogs(s.id), s.status === "not_created"),
      btn("Backups", "secondary", () => openBackups(s.id), false),
      btn("Schedules", "secondary", () => openSchedules(s.id), false),
      s.config_file_count > 0 ? btn("Config", "secondary", () => openConfig(s.id), false) : null,
      s.update_method !== "none" || s.steam_app_id ? btn("Updates", "secondary", () => openUpdate(s.id), false) : null,
      el("span", { class: "spacer" }),
      btn("Edit", "secondary", () => openEdit(s), running, running ? "Stop the server to edit it" : ""),
      btn("Delete", "danger", () => openDelete(s), running, running ? "Stop the server to delete it" : "")));
}

function failedDeployCard(job) {
  return el("article", { class: "card failed", id: `card-deploy-${job.key}` },
    el("div", { class: "card-head" },
      el("h2", { textContent: job.name }),
      el("span", { class: "badge error", textContent: "Deploy failed" })),
    el("p", { class: "desc", textContent: job.entry.name }),
    el("div", { class: "statusbox failed" }, el("div", { class: "status-text warn", textContent: job.error })),
    el("div", { class: "actions" },
      el("button", { class: "primary", type: "button", textContent: "Back to the form", onclick: () => { deployJobs.delete(job.key); renderServers(); openDeploy(job.entry); } }),
      el("button", { class: "secondary", type: "button", textContent: "Dismiss", onclick: () => { deployJobs.delete(job.key); renderServers(); } })));
}

function liveBlock(s) {
  const st = stats && stats.servers.find((x) => x.server_id === s.id);
  const pts = (history.servers[s.id] && history.servers[s.id].points) || [];
  if (s.status !== "running") return el("div", { class: "live idle", textContent: "Not running" });
  if (!st || !st.running) return el("div", { class: "live idle", textContent: "Waiting for the first reading\u2026" });
  const cell = (label, value, sub, spark) => el("div", { class: "live-cell" },
    el("div", { class: "live-label", textContent: label }),
    el("div", { class: "live-value", textContent: value }),
    sub ? el("div", { class: "live-sub", textContent: sub }) : null,
    spark);
  return el("div", { class: "live" },
    cell("CPU", fmtPct(st.cpu_percent), `${fmtPct(st.cpu_of_host_percent)} of host`,
      sparkline(pts.map((p) => p.cpu), { max: 100, cls: "cpu" })),
    cell("Memory", fmtBytes(st.mem_used), `${fmtPct(st.mem_of_host_percent)} of host`,
      sparkline(pts.map((p) => p.mem), { cls: "mem" })),
    cell("Network", `\u2193 ${fmtRate(st.net_rx_rate)}`, `\u2191 ${fmtRate(st.net_tx_rate)}`,
      sparkline(pts.map((p) => p.rx_rate + p.tx_rate), { cls: "net" })),
    cell("Uptime", fmtUptime(st.uptime_seconds), null, null));
}

// Tiles are updated in place, not rebuilt, so spinners and progress bars keep animating between polls.
const cardCache = new Map();   // key -> { node, structSig, badgeSig, statusSig, liveSig }
const deployJobs = new Map();  // deploys started from this page: key -> { key, name, entry, state, error }
let jobSeq = 0;

function pendingJobs() {
  // A deploy shows up in the server list itself (activity "deploying") once the server knows about it; until then
  // (or if it failed) this page shows its own tile so there is feedback at once.
  return [...deployJobs.values()].filter((j) => j.state === "failed"
    || !servers.some((x) => x.activity === "deploying" && x.name.toLowerCase() === j.name.toLowerCase()));
}

function liveSig(s) {
  const st = stats && stats.servers.find((x) => x.server_id === s.id);
  const pts = history.servers[s.id] && history.servers[s.id].points;
  return s.status === "running" ? JSON.stringify([st, pts && pts.length, pts && pts.length && pts[pts.length - 1].t]) : "idle";
}

function syncCard(key, s, build) {
  const st = serverState(s);
  const structSig = JSON.stringify([s.name, s.status, s.detail, s.ports, s.image, s.config_file_count, s.update_method,
    s.game_name, st.kind === "busy", s.id, s.env && s.env.length, s.update_available, !!s.activity,
    s.steam_app_id, s.game_version, s.installed_build, s.latest_build, s.game_update_status]);
  const badgeSig = `${st.working ? 1 : 0}|${st.kind}|${st.label}`;
  const statusSig = JSON.stringify([st.kind, st.working, st.progress, st.sub, st.ready]);
  const lsig = liveSig(s);
  let c = cardCache.get(key);
  if (!c || c.structSig !== structSig) {
    c = { node: build(s), structSig, badgeSig, statusSig, liveSig: lsig };
    cardCache.set(key, c);
    return c.node;
  }
  c.node.classList.toggle("working", st.working);
  if (c.badgeSig !== badgeSig) {
    c.node.querySelector(".badge").replaceWith(badgeNode(st));
    c.badgeSig = badgeSig;
  }
  if (c.statusSig !== statusSig) {
    const old = c.node.querySelector(".statusbox");
    const fill = old && old.querySelector(".progress:not(.indeterminate) .fill");
    const prev = JSON.parse(c.statusSig);
    if (fill && st.working && st.kind === prev[0] && st.progress !== null) {
      fill.style.width = `${Math.max(2, Math.min(100, st.progress))}%`;   // same kind of wait: just move the bar
      old.querySelector(".progress").setAttribute("aria-valuenow", String(Math.round(st.progress)));
      old.querySelector(".status-text").textContent = st.sub;
    } else {
      old.replaceWith(statusBox(st));
    }
    c.statusSig = statusSig;
  }
  if (c.liveSig !== lsig) {
    c.node.querySelector(".live").replaceWith(liveBlock(s));
    c.liveSig = lsig;
  }
  const line = c.node.querySelector(".sched-line");   // "next in 5 hours" changes with the clock: update in place
  if (line && line.dataset.sig !== schedText(s) + ((s.schedule || {}).last_problem ? "|problem" : "")) line.replaceWith(schedLine(s));
  return c.node;
}

function renderServers() {
  const wanted = [];
  const keys = new Set();
  for (const j of pendingJobs()) {
    const key = `job-${j.key}`;
    keys.add(key);
    if (j.state === "failed") {
      let c = cardCache.get(key);
      if (!c) { c = { node: failedDeployCard(j) }; cardCache.set(key, c); }
      wanted.push(c.node);
    } else {
      wanted.push(syncCard(key, {
        id: key, name: j.name, game_name: j.entry.name, image: j.entry.image, status: "not_created", ports: [],
        detail: "", activity: "deploying", config_file_count: 0, update_method: "none", env: [] }, serverCard));
    }
  }
  for (const s of servers) {
    keys.add(s.id);
    wanted.push(syncCard(s.id, s, serverCard));
  }
  for (const k of [...cardCache.keys()]) if (!keys.has(k)) cardCache.delete(k);
  const box = $("servers");
  // Touch the page only where something changed (moving a tile restarts its animations).
  [...box.children].forEach((n) => { if (!wanted.includes(n)) n.remove(); });
  wanted.forEach((n, idx) => { if (box.children[idx] !== n) box.insertBefore(n, box.children[idx] || null); });
  $("loading").hidden = serversLoaded;
  $("empty").hidden = !serversLoaded || servers.length > 0 || wanted.length > 0;
}

async function act(id, action) {
  busy.set(id, ACTIVITY[action] || action);
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
      el("button", { class: "primary", type: "button", textContent: "Deploy", "aria-label": `Deploy ${e.name}`, onclick: () => openDeploy(e) })),
    el("p", { class: "desc", textContent: e.description }),
    el("div", { class: "meta" }, "Image: ", el("code", { textContent: e.image })),
    el("div", { class: "meta", textContent: `Default ports: ${portText(e.ports)}` }),
    e.notes ? el("p", { class: "note", textContent: e.notes }) : null);
}

function renderCatalog() {
  $("catalog").replaceChildren(...catalog.map(catalogCard));
  const empty = $("catalog-empty");
  empty.hidden = catalog.length > 0;
  if (!catalog.length) {
    empty.textContent = "No games found. Put a .yaml game file in the catalog folder (see the README, \"Adding a game\"), then reload this page. "
      + "The Health page lists files that could not be read.";
  }
}

// -------------------------------------------------------------------- dialogs
function openDialog(...content) {
  const d = $("dialog");
  d.className = "";
  d.replaceChildren(...content);
  const h = d.querySelector("h3");
  if (h) { h.id = "dialog-title"; d.setAttribute("aria-labelledby", "dialog-title"); }
  else d.removeAttribute("aria-labelledby");
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
    const submitLabel = submit.textContent;
    submit.replaceChildren(spinner(), "Working\u2026");
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
      submit.textContent = submitLabel;
    }
  });
  return form;
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function openDeploy(entry) {
  const values = Object.fromEntries(entry.fields.map((f) => [f.name, f.default]));
  openDialog(buildServerForm({
    title: `Deploy ${entry.name}`, submitText: "Deploy", name: entry.name,
    ports: entry.ports, fields: entry.fields, values, editing: false, showStart: true,
    onSubmit: async (body, startAfter) => {
      // Quick problems (name taken, port in use, missing field) come back within a moment and are shown in this
      // form. If the request is still running after that, deploying is slow (downloading the image): close the
      // form and show a "Deploying" tile on the dashboard at once instead of leaving the user staring at a button.
      const req = api("/api/servers", "POST", { game_id: entry.id, start: startAfter, ...body });
      const outcome = await Promise.race([req.then(() => "done"), sleep(700).then(() => "slow")]);
      if (outcome === "slow") {
        const job = { key: ++jobSeq, name: body.name, entry, state: "deploying", error: "" };
        deployJobs.set(job.key, job);
        location.hash = "#/";
        renderServers();
        req.then(() => deployJobs.delete(job.key), (e) => { job.state = "failed"; job.error = `Could not deploy: ${e.message}`; })
          .finally(() => refresh());
        return;
      }
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
function confirmView({ title, paragraphs, checkboxLabel, confirmText, workingText, danger, onConfirm, onBack, extra }) {
  const err = el("div", { class: "form-error", hidden: true });
  const working = el("div", { class: "statusbox busy", hidden: true, role: "status" },
    el("div", { class: "progress indeterminate" }, el("div", { class: "fill" })),
    el("div", { class: "status-text", textContent: workingText || "Working\u2026" }));
  const cb = checkboxLabel ? el("input", { type: "checkbox" }) : null;
  const go = el("button", { type: "submit", class: danger ? "danger" : "primary", textContent: confirmText, disabled: !!cb });
  if (cb) cb.addEventListener("change", () => { go.disabled = !cb.checked; });
  const form = el("form", { method: "dialog" },
    el("h3", { textContent: title }),
    ...paragraphs.map((t) => el("p", { textContent: t })),
    ...(extra || []),
    cb ? el("label", { class: "check" }, cb, checkboxLabel) : null,
    working, err,
    el("div", { class: "dialog-actions" },
      el("button", { type: "button", class: "secondary", textContent: "Cancel", onclick: onBack || closeDialog }), go));
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    err.hidden = true;
    go.disabled = true;
    go.replaceChildren(spinner(), "Working\u2026");
    working.hidden = false;
    try {
      await onConfirm();
    } catch (e) {
      err.textContent = e.message;
      err.hidden = false;
      go.disabled = cb ? !cb.checked : false;
      go.textContent = confirmText;
      working.hidden = true;
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
  const working = el("div", { class: "statusbox busy", hidden: true, role: "status" },
    el("div", { class: "progress indeterminate" }, el("div", { class: "fill" })),
    el("div", { class: "status-text", textContent: running
      ? "Backing up\u2026 the server is stopped first for a consistent copy and started again afterwards."
      : "Backing up\u2026" }));
  create.addEventListener("click", async () => {
    create.disabled = true; create.replaceChildren(spinner(), "Backing up\u2026"); err.hidden = true; working.hidden = false;
    try {
      const r = await api(`/api/servers/${sid(id)}/backups`, "POST", { live: live.checked });
      const extra = r.warnings.length ? " " + r.warnings.join(" ") : "";
      await openBackups(id, `Backup ${r.backup.name} created. ${r.note}${extra}`);
    } catch (e) { err.textContent = e.message; err.hidden = false; create.disabled = false; create.textContent = "Create backup"; working.hidden = true; }
  });

  const rows = data.backups.map((b) => el("tr", {},
    el("td", {}, el("code", { textContent: b.name })),
    el("td", {}, el("span", { class: `kind ${b.kind === "manual" ? "" : "auto"}`, textContent: b.kind === "manual" ? "manual" : b.kind === "scheduled" ? "scheduled" : b.kind.replace("pre-", "auto: before ") })),
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
    working, msg, err,
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
    workingText: "Saving a safety backup of the current data, then restoring\u2026",
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
    workingText: "Saving a safety backup, then emptying the data folder\u2026",
    onBack: () => openBackups(id),
    onConfirm: async () => {
      const r = await api(`/api/servers/${sid(id)}/wipe-data?confirm=true`, "POST");
      await openBackups(id, r.note);
    },
  }));
}

// -------------------------------------------------------------------- updates dialog (game version + image)
const GAME_BADGE = { update_available: ["Update available", "update"], up_to_date: ["Up to date", "running"],
  could_not_check: ["Could not check", "stopped"], unknown: ["Unknown (no Steam id for this game)", "stopped"] };
const fmtDay = (iso) => { const d = new Date(iso); return isNaN(d) ? "" : d.toLocaleString([], { year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }); };

async function openUpdate(id, message) {
  await refresh();
  const s = findServer(id);
  if (!s) return;
  if (!s.steam_app_id) return openImageUpdate(id, message);   // games without a Steam id: only the image can be updated
  let g;
  try { g = await api(`/api/servers/${sid(id)}/game-update`); }
  catch (e) { return openDialog(resultView("Updates", [[e.message, "warn"]])); }
  const err = el("div", { class: "form-error", hidden: true });
  const [label, cls] = GAME_BADGE[g.status] || GAME_BADGE.unknown;
  const checkBtn = el("button", { type: "button", class: "secondary small", id: "game-check", textContent: "Check now" });
  checkBtn.addEventListener("click", async () => {
    checkBtn.disabled = true; checkBtn.replaceChildren(spinner(), "Checking\u2026");
    try { await api(`/api/servers/${sid(id)}/game-update/check`, "POST"); await refresh(); openUpdate(id); }
    catch (e) { err.textContent = e.message; err.hidden = false; checkBtn.disabled = false; checkBtn.textContent = "Check now"; }
  });
  const row = (k, v) => el("tr", {}, el("th", { textContent: k }), el("td", { textContent: v }));
  const inst = g.installed, late = g.latest;
  const facts = el("table", { class: "about game-facts", id: "game-facts" },
    row("Installed version", g.game_version || (g.running ? "not shown in the log yet" : "shown once the server runs")),
    row("Installed build", inst.build ? `${inst.build}${inst.updated_at ? ` (updated ${fmtDay(inst.updated_at)})` : ""}` : "not found"),
    row("Newest build", late.build ? `${late.build}${late.updated_at ? ` (released ${fmtDay(late.updated_at)})` : ""}${g.branch && g.branch !== "public" ? ` on branch ${g.branch}` : ""}` : "not known"),
    row("Checked", late.checked_at ? fmtDay(late.checked_at) : "not yet"));
  const running = s.status === "running";
  const canUpdate = g.can_update && running && g.status !== "up_to_date" && !s.activity;
  const why = !g.can_update ? "GameDock cannot update this game's files by itself."
    : !running ? "The server is stopped. This game updates itself when it starts, so just start it."
    : g.status === "up_to_date" ? "Nothing to update: the game is already the newest version." : "";
  const updateBtn = el("button", { type: "button", class: "primary", id: "game-update-now", disabled: !canUpdate, title: why,
    textContent: g.status === "update_available" ? "Update game now" : "Update game anyway" });
  updateBtn.addEventListener("click", () => confirmGameUpdate(id, s, g));
  const explain = el("details", { class: "how-updates", open: g.status !== "update_available" },
    el("summary", { textContent: "How game updates work" }),
    el("p", { class: "hint", textContent: "GameDock compares the Steam build number stored in the game's own files with the newest public build number from a public copy of Steam's data. Nothing is downloaded to check, and the answer is kept for an hour." }),
    el("p", { class: "hint", textContent: "Update game now stops the server, backs it up, and starts it again. This game's image downloads the new files while it starts. GameDock then checks that the build number really changed and tells you if it did not." }),
    g.update_note ? el("p", { class: "hint", textContent: g.update_note }) : null);
  const imgBadge = s.update_available === true ? ["Update available", "update"] : s.update_available === false ? ["Up to date", "running"] : ["Not known", "stopped"];
  const image = s.update_method !== "none" ? el("section", { class: "update-section" },
    el("h4", { textContent: "Container image" }),
    el("div", { class: "update-status" }, el("span", { class: `badge ${imgBadge[1]}`, textContent: imgBadge[0] }),
      el("span", { class: "hint", textContent: "The helper program around the game (not the game files). Replacing it keeps your world." }),
      el("button", { type: "button", class: "secondary small", id: "open-image-update", textContent: "Image update\u2026", onclick: () => openImageUpdate(id) }))) : null;
  openWide(el("div", { class: "panel update-panel", id: "update-panel" },
    el("h3", { textContent: `Updates \u2013 ${s.name}` }),
    message ? el("div", { class: "okmsg", textContent: message }) : null,
    el("section", { class: "update-section" },
      el("h4", { textContent: "Game" }),
      el("div", { class: "update-status" }, el("span", { class: `badge ${cls}`, id: "game-status-badge", textContent: label }),
        el("span", { class: "hint", textContent: g.summary }), checkBtn),
      facts, explain),
    image, err,
    el("div", { class: "dialog-actions" },
      el("button", { type: "button", class: "secondary", textContent: "Close", onclick: closeDialog }), el("span", { class: "spacer" }), updateBtn)));
}

function confirmGameUpdate(id, s, g) {
  openDialog(confirmView({
    title: `Update the game on ${s.name}?`,
    paragraphs: [
      g.status === "update_available"
        ? `A newer game version is available (build ${g.installed.build} \u2192 ${g.latest.build}).`
        : "GameDock does not see a newer game version right now; this restarts the server and checks again.",
      "The server will be stopped (with time to save), backed up, and started again. Players are disconnected. While it starts, the game downloads its new files, which can take many minutes.",
      "If the backup fails, nothing is changed. Afterwards GameDock checks that the installed build number changed and tells you plainly if it did not.",
    ],
    checkboxLabel: "Yes, restart the server and update the game.", confirmText: "Update game now", danger: false,
    workingText: "Stopping, backing up, restarting, and waiting for the game to update. This can take many minutes\u2026",
    onBack: () => openUpdate(id),
    onConfirm: async () => {
      busy.set(id, "updating game");
      renderServers();
      let r;
      try { r = await api(`/api/servers/${sid(id)}/update-game?confirm=true`, "POST"); }
      finally { busy.delete(id); await refresh(); }
      const title = r.outcome === "changed" ? "Game updated" : r.outcome === "up_to_date" ? "Already up to date"
        : r.problem ? "The game was not confirmed as updated" : "Restarted";
      openDialog(resultView(title, [
        [r.note, r.problem ? "warn" : ""],
        r.before_build ? [`Build before: ${r.before_build}`] : null,
        r.after_build ? [`Build after: ${r.after_build}`] : null,
        r.game_version ? [`Game version: ${r.game_version}`] : null,
        ...(r.warnings || []).map((w) => [w, "warn"]),
      ]));
    },
  }));
}

// -------------------------------------------------------------------- image update
async function openImageUpdate(id, message) {
  await refresh();
  const s = findServer(id);
  if (!s) return;
  let info, chk;
  try {
    info = await api(`/api/servers/${sid(id)}/image`);
    chk = await api(`/api/servers/${sid(id)}/image-update`);
  } catch (e) { return openDialog(resultView("Update", [[e.message, "warn"]])); }
  const running = s.status === "running";
  const err = el("div", { class: "form-error", hidden: true });
  const checkBtn = el("button", { type: "button", class: "secondary small", id: "image-check", textContent: "Check now", disabled: chk.supported === false });
  checkBtn.addEventListener("click", async () => {
    checkBtn.disabled = true; checkBtn.replaceChildren(spinner(), "Checking\u2026");
    try { await api(`/api/servers/${sid(id)}/image-update/check`, "POST"); await refresh(); openImageUpdate(id); }
    catch (e) { err.textContent = e.message; err.hidden = false; checkBtn.disabled = false; checkBtn.textContent = "Check now"; }
  });
  const verdict = chk.update_available === true ? ["Update available", "update"] :
    chk.update_available === false ? ["Up to date", "running"] : ["Not known", "stopped"];
  const status = el("div", { class: "update-status" },
    el("span", { class: `badge ${verdict[1]}`, textContent: verdict[0] }),
    el("span", { class: "hint", textContent: ` ${chk.message || ""}${chk.checked_at ? ` Checked ${fmtWhen(chk.checked_at)}.` : ""}` }),
    checkBtn);
  const explain = el("details", { class: "how-updates", open: chk.update_available !== true },
    el("summary", { textContent: "How updating works for this game" }),
    el("p", { class: "hint", textContent: s.update_note || "GameDock compares the image this server was created from with the newest one in the online registry (nothing is downloaded to check). Update now downloads the newest image and recreates the container from it." }),
    el("p", { class: "hint", textContent: "Many game images that use SteamCMD download the newest game files by themselves each time the container starts. For those, restarting the server is what updates the game, and \u201cUpdate available\u201d only refers to the image around it." }));
  openDialog(confirmView({
    title: `Update ${s.name}?`,
    paragraphs: [
      ...(message ? [message] : []),
      `This downloads the newest version of ${info.image} and recreates the container from it. Your data and settings are kept.`,
      `Current image id: ${shortId(info.image_id)}.`,
      running
        ? "The server is running: it will be stopped (with time to save), backed up, updated and started again."
        : "The server is stopped and will stay stopped.",
      "A backup of the data is taken automatically before the update. If the backup fails, the update is cancelled. If the image is already the newest, nothing is changed.",
    ],
    extra: [status, explain, err],
    confirmText: chk.update_available === true ? "Update now" : "Update anyway", danger: false,
    workingText: "Downloading the newest image, backing up and recreating the container. This can take several minutes\u2026",
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

// -------------------------------------------------------------------- log viewer
// Doubles as the server's detail view: its header shows the live state (Starting, Installing 52%, ...) next to the log.
async function openLogs(id) {
  await refresh();
  const s0 = findServer(id);
  if (!s0) return;
  const view = { lines: 200, follow: true, timer: null, busy: false };
  const pre = el("pre", { class: "logbox", id: "log-text", tabIndex: 0, "aria-label": `Log of ${s0.name}`, "aria-live": "off" });
  const stateLine = el("div", { class: "log-state" });
  const info = el("span", { class: "hint", id: "log-info" });
  const err = el("div", { class: "form-error", hidden: true });
  const follow = el("input", { type: "checkbox", id: "log-follow", checked: true });
  const lines = el("select", { id: "log-lines", "aria-label": "Number of lines" },
    ...[100, 200, 500, 1000].map((n) => el("option", { value: String(n), textContent: `${n} lines`, selected: n === view.lines })));
  const copy = el("button", { type: "button", class: "secondary", textContent: "Copy", id: "log-copy" });
  const reload = el("button", { type: "button", class: "secondary", textContent: "Refresh", id: "log-refresh" });

  function paintState() {
    const s = findServer(id) || s0;
    const st = serverState(s);
    stateLine.replaceChildren(badgeNode(st), el("span", { class: "log-state-text", textContent:
      st.sub || (st.kind === "running" && st.ready ? "Ready to join." : st.kind === "stopped" ? "Stopped. This is what it logged before it stopped." : "") }));
    if (st.working) stateLine.append(progressBar(st));
  }

  async function load() {
    if (!document.body.contains(pre)) { clearInterval(view.timer); return; }   // another dialog took over
    if (view.busy) return;
    view.busy = true;
    try {
      const r = await api(`/api/servers/${sid(id)}/logs?lines=${view.lines}`);
      const nearBottom = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 40;
      const first = !pre.dataset.loaded;
      pre.textContent = r.text || r.note || "(nothing logged yet)";
      pre.classList.toggle("empty", !r.text);
      pre.dataset.loaded = "1";
      if (first || (view.follow && nearBottom)) pre.scrollTop = pre.scrollHeight;
      info.textContent = `Updated ${new Date().toLocaleTimeString()} \u00b7 ${r.lines} line${r.lines === 1 ? "" : "s"}${r.truncated ? " (older lines are not shown)" : ""}`;
      err.hidden = true;
    } catch (e) { err.textContent = e.message; err.hidden = false; }
    finally { view.busy = false; paintState(); }
  }
  function schedule() {
    clearInterval(view.timer);
    if (view.follow) view.timer = setInterval(load, 3000);
  }
  follow.addEventListener("change", () => { view.follow = follow.checked; schedule(); if (view.follow) load(); });
  lines.addEventListener("change", () => { view.lines = Number(lines.value); load(); });
  reload.addEventListener("click", load);
  copy.addEventListener("click", () => copyText(pre.textContent, copy));

  const d = openWide(el("div", { class: "panel" },
    el("h3", { textContent: `Logs \u2013 ${s0.name}` }),
    stateLine,
    el("div", { class: "toolbar wrap" },
      el("label", { class: "check" }, follow, "Follow (refresh every 3 seconds)"), lines, reload, copy, el("span", { class: "spacer" }), info),
    err, pre,
    el("div", { class: "dialog-actions" },
      el("button", { type: "button", class: "secondary", textContent: "Close", onclick: closeDialog }))));
  d.addEventListener("close", () => clearInterval(view.timer), { once: true });
  paintState();
  await load();
  schedule();
}

// -------------------------------------------------------------------- config editor
const MASK = "********";
const isBool = (f) => f.type === "bool";
const boolOf = (v) => String(v).trim().toLowerCase() === "true";
const sameValue = (f, a, b) => (isBool(f) ? boolOf(a) === boolOf(b) : a === b);

async function openConfig(id) {
  await refresh();
  const s = findServer(id);
  if (!s) return;
  let files;
  try { files = await api(`/api/servers/${sid(id)}/config`); }
  catch (e) { return openDialog(resultView("Config", [[e.message, "warn"]])); }

  const select = el("select", { id: "config-file" }, ...files.map((f) => el("option", { value: f.path, textContent: f.label || f.path })));
  const pathLabel = el("div", { class: "meta" });
  const catNote = el("p", { class: "warn", hidden: true });
  const note = el("p", { class: "hint" });
  const err = el("div", { class: "form-error", hidden: true });
  const ok = el("div", { class: "okmsg", hidden: true });
  const body = el("div", { class: "cfg-body" });
  const save = el("button", { type: "button", class: "primary", textContent: "Save", id: "config-save" });

  // everything about the file currently open
  let cur = null;        // { r, orig: Map id->value, fields, formBase: {text, values: Map}, edits: Map, mode, rawSynced }
  let ui = {};           // live DOM pieces of the current view

  const editable = () => cur && cur.r.editable_now && cur.r.info.exists !== false;
  const fieldById = (fid) => cur.fields.find((f) => f.id === fid);
  const valueOf = (f) => (cur.edits.has(f.id) ? cur.edits.get(f.id) : cur.formBase.values.get(f.id));
  const isChanged = (f) => !cur.orig.has(f.id) || !sameValue(f, valueOf(f), cur.orig.get(f.id));
  const changedFields = () => cur.fields.filter(isChanged);
  const dirty = () => (cur.mode === "raw" ? ui.area.value !== cur.r.content
    : cur.edits.size > 0 || cur.formBase.text !== cur.r.content);

  function setFormBase(text, form) {
    cur.formBase = { text, values: new Map(form.fields.map((f) => [f.id, f.value])) };
    cur.fields = form.fields;
    cur.edits = new Map();
  }

  async function preview(text, edits) {
    return api(`/api/servers/${sid(id)}/config/form`, "POST",
      { path: select.value, content: text, changes: Object.fromEntries(edits) });
  }

  function refreshButtons() {
    save.disabled = !editable() || !dirty();
    if (cur.mode === "form") {
      const n = changedFields().length;
      ui.count.textContent = n ? `${n} setting${n === 1 ? "" : "s"} changed` : "No changes yet";
      ui.count.classList.toggle("has", n > 0);
      ui.fields.forEach(({ f, row, was, reset, input }) => {
        const changed = isChanged(f);
        row.classList.toggle("changed", changed);
        was.hidden = !changed;
        reset.hidden = !changed;
        if (changed) was.textContent = f.sensitive ? "password will change" : `was: ${cur.orig.has(f.id) ? (cur.orig.get(f.id) === "" ? "(empty)" : cur.orig.get(f.id)) : "(new)"}`;
      });
      applyFilter();
      paintReview();
    }
  }

  function paintReview() {
    const ch = changedFields();
    ui.review.hidden = !ch.length;
    if (!ch.length) return;
    ui.review.replaceChildren(el("strong", { textContent: "These changes will be saved:" }),
      el("ul", {}, ...ch.map((f) => {
        const from = cur.orig.has(f.id) ? cur.orig.get(f.id) : "(new)";
        const shown = (v) => (f.sensitive ? (v === "" ? "(empty)" : "\u2022\u2022\u2022\u2022\u2022\u2022") : v === "" ? "(empty)" : v);
        return el("li", {}, el("span", { class: "k", textContent: f.label }), ": ",
          el("span", { class: "from", textContent: shown(from) }), " \u2192 ", el("span", { class: "to", textContent: shown(valueOf(f)) }));
      })));
  }

  function applyFilter() {
    const q = ui.search.value.trim().toLowerCase();
    const onlyChanged = ui.onlyChanged.checked;
    let shown = 0;
    ui.groups.forEach(({ box, rows }) => {
      let any = false;
      rows.forEach(({ f, row }) => {
        const hit = (!q || `${f.label} ${f.key} ${f.group} ${f.sensitive ? "" : valueOf(f)}`.toLowerCase().includes(q)) && (!onlyChanged || isChanged(f));
        row.hidden = !hit;
        any = any || hit;
        if (hit) shown++;
      });
      box.hidden = !any;
    });
    ui.none.hidden = shown > 0;
    ui.none.textContent = onlyChanged && !q ? "Nothing has been changed yet." : "No setting matches your search.";
  }

  function inputFor(f) {
    const ro = !editable();
    const set = (v) => { if (sameValue(f, v, cur.formBase.values.get(f.id))) cur.edits.delete(f.id); else cur.edits.set(f.id, v); refreshButtons(); };
    let input;
    if (isBool(f)) {
      input = el("input", { type: "checkbox", role: "switch", checked: boolOf(valueOf(f)), disabled: ro, id: `cfg-${f.id}` });
      const text = el("span", { class: "switch-text", textContent: boolOf(valueOf(f)) ? "On" : "Off" });
      input.addEventListener("change", () => { text.textContent = input.checked ? "On" : "Off"; set(input.checked ? "true" : "false"); });
      return { input, widget: el("label", { class: "switch" }, input, el("span", { class: "slider" }), text) };
    }
    if (f.type === "json") {
      input = el("textarea", { class: "cfg-json", rows: Math.min(8, Math.max(2, valueOf(f).split("\n").length)), spellcheck: false, disabled: ro, id: `cfg-${f.id}` });
      input.value = valueOf(f);
      input.addEventListener("input", () => set(input.value));
      return { input, widget: input };
    }
    input = el("input", { type: f.sensitive ? "password" : "text", value: valueOf(f), disabled: ro, id: `cfg-${f.id}`,
      autocomplete: f.sensitive ? "new-password" : "off", spellcheck: false,
      inputMode: f.type === "number" ? "decimal" : "text" });
    if (f.sensitive && valueOf(f) === MASK) input.title = "Hidden. Leave as it is to keep the current value, or type a new one.";
    input.addEventListener("input", () => set(input.value));
    return { input, widget: input };
  }

  function buildForm() {
    ui.fields = [];
    ui.groups = [];
    ui.search = el("input", { type: "search", id: "cfg-search", placeholder: "Search settings\u2026", "aria-label": "Search settings",
      oninput: () => applyFilter() });
    ui.onlyChanged = el("input", { type: "checkbox", id: "cfg-only-changed", onchange: () => applyFilter() });
    ui.count = el("span", { class: "chg-count", id: "cfg-count" });
    ui.review = el("div", { class: "review", id: "cfg-review", hidden: true });
    ui.none = el("p", { class: "empty-small", hidden: true });
    const byGroup = new Map();
    cur.fields.forEach((f) => { if (!byGroup.has(f.group)) byGroup.set(f.group, []); byGroup.get(f.group).push(f); });
    const groupBoxes = [];
    for (const [group, fs] of byGroup) {
      const rows = [];
      const box = el("div", { class: "cfg-group" }, group ? el("h4", { textContent: group }) : null);
      fs.forEach((f) => {
        const { input, widget } = inputFor(f);
        const was = el("div", { class: "was", hidden: true });
        const reset = el("button", { type: "button", class: "small secondary", textContent: "Undo", hidden: true, "aria-label": `Undo change to ${f.label}`,
          onclick: () => {
            cur.edits.delete(f.id);
            const v = cur.orig.has(f.id) ? cur.orig.get(f.id) : cur.formBase.values.get(f.id);
            if (cur.orig.has(f.id) && !sameValue(f, v, cur.formBase.values.get(f.id))) cur.edits.set(f.id, v);
            if (isBool(f)) { input.checked = boolOf(v); input.dispatchEvent(new Event("change")); } else { input.value = v; input.dispatchEvent(new Event("input")); }
            refreshButtons();
          } });
        const row = el("div", { class: "cfg-row" },
          el("label", { class: "cfg-label", htmlFor: `cfg-${f.id}` }, el("span", { class: "nm", textContent: f.label }),
            el("span", { class: "key", textContent: f.key })),
          el("div", { class: "cfg-input" }, widget, el("div", { class: "cfg-sub" }, was, reset)));
        box.append(row);
        rows.push({ f, row });
        ui.fields.push({ f, row, was, reset, input });
      });
      ui.groups.push({ box, rows });
      groupBoxes.push(box);
    }
    return el("div", { class: "cfg-form" },
      el("div", { class: "cfg-tools" }, ui.search,
        el("label", { class: "check" }, ui.onlyChanged, "Only changed"), ui.count),
      ui.review,
      el("div", { class: "cfg-list" }, ...groupBoxes, ui.none));
  }

  function buildRaw(text) {
    ui.area = el("textarea", { id: "config-text", spellcheck: false, rows: 18, wrap: "soft", disabled: !editable() });
    ui.area.value = text;
    ui.area.addEventListener("input", () => refreshButtons());
    return ui.area;
  }

  async function show(mode) {
    err.hidden = true;
    const leaving = cur.mode;
    try {
      if (leaving === "form" && mode === "raw") {
        const text = cur.edits.size ? (await preview(cur.formBase.text, cur.edits)).content : cur.formBase.text;
        cur.rawSynced = text;
        cur.mode = "raw";
        body.replaceChildren(buildRaw(text), el("p", { class: "hint", textContent: "Raw text, exactly as stored in the file. Long lines wrap on screen only; nothing is added to the file." }));
      } else if (leaving === "raw" && mode === "form") {
        if (ui.area.value !== cur.rawSynced) {
          const res = await api(`/api/servers/${sid(id)}/config/form`, "POST", { path: select.value, content: ui.area.value });
          if (!res.form) throw new Error("This text can no longer be shown as a form (the structure is not recognised). Fix it in the raw view first.");
          setFormBase(ui.area.value, res.form);
        }
        cur.mode = "form";
        body.replaceChildren(buildForm());
      } else {
        cur.mode = mode;
        body.replaceChildren(mode === "form" ? buildForm() : buildRaw(cur.r.content));
        if (mode === "raw") { cur.rawSynced = cur.r.content; body.append(el("p", { class: "hint", textContent: "Raw text, exactly as stored in the file." })); }
      }
    } catch (e) {
      err.textContent = e.message; err.hidden = false;
      cur.mode = leaving;
    }
    paintToggle();
    refreshButtons();
  }

  function paintToggle() {
    if (!ui.toggle) return;
    ui.toggle.querySelectorAll("button").forEach((b) => {
      const on = b.dataset.mode === cur.mode;
      b.classList.toggle("on", on); b.setAttribute("aria-pressed", String(on));
    });
  }

  async function load() {
    err.hidden = true; ok.hidden = true; save.disabled = true;
    ui = {};
    try {
      const r = await api(`/api/servers/${sid(id)}/config/file?path=${encodeURIComponent(select.value)}`);
      cur = { r, mode: r.form ? "form" : "raw", orig: new Map((r.form ? r.form.fields : []).map((f) => [f.id, f.value])),
        fields: [], formBase: { text: r.content, values: new Map() }, edits: new Map(), rawSynced: r.content };
      if (r.form) setFormBase(r.content, r.form);
      pathLabel.replaceChildren("File: ", el("code", { textContent: r.info.path }),
        r.info.backups.length ? ` \u2013 ${r.info.backups.length} automatic .bak cop${r.info.backups.length === 1 ? "y" : "ies"} kept next to it` : "");
      catNote.textContent = r.info.note || ""; catNote.hidden = !r.info.note;
      note.textContent = r.note;
      if (!r.info.exists) return showMissing();
      ui.toggle = r.form ? el("div", { class: "seg", role: "group", "aria-label": "View" },
        el("button", { type: "button", "data-mode": "form", textContent: "Settings form", onclick: () => cur.mode !== "form" && show("form") }),
        el("button", { type: "button", "data-mode": "raw", textContent: "Raw text", onclick: () => cur.mode !== "raw" && show("raw") })) : null;
      wrap.replaceChildren(...[ui.toggle, body].filter(Boolean));
      body.replaceChildren(cur.mode === "form" ? buildForm() : buildRaw(r.content));
      if (!r.form) body.append(el("p", { class: "hint", textContent: "This kind of file has no settings form, so it is shown as raw text." }));
      paintToggle();
      refreshButtons();
    } catch (e) {
      cur = null;
      err.textContent = e.message; err.hidden = false;
      wrap.replaceChildren();
    }
  }

  function showMissing() {
    save.disabled = true;
    const manual = el("button", { type: "button", class: "secondary", textContent: "Write the file myself anyway",
      onclick: () => { cur.mode = "raw"; cur.r = { ...cur.r, info: { ...cur.r.info, exists: undefined } }; wrap.replaceChildren(body); body.replaceChildren(buildRaw("")); refreshButtons(); } });
    wrap.replaceChildren(el("div", { class: "empty-state inline", id: "config-missing" },
      el("h4", { textContent: "No settings file yet" }),
      el("p", { textContent: "This game has not created its settings file yet; it appears after the first full start." }),
      el("p", { class: "hint", textContent: s.status === "running" && !s.ready
        ? "The server is still starting. Check the Logs, and come back when it shows Running." : "Start the server and wait until it shows Running, then open this page again." }),
      manual));
  }

  const wrap = el("div", { class: "cfg-wrap" });
  select.addEventListener("change", load);
  save.addEventListener("click", async () => {
    err.hidden = true; ok.hidden = true; save.disabled = true;
    save.replaceChildren(spinner(), "Saving\u2026");
    try {
      let content;
      if (cur.mode === "raw") content = ui.area.value;
      else content = (await preview(cur.formBase.text, cur.edits)).content;
      const r = await api(`/api/servers/${sid(id)}/config/file`, "PUT", { path: select.value, content });
      await load();
      ok.textContent = r.note; ok.hidden = false;
    } catch (e) { err.textContent = e.message; err.hidden = false; if (cur) refreshButtons(); }
    finally { save.textContent = "Save"; if (cur) refreshButtons(); }
  });

  openWide(el("div", { class: "panel" },
    el("h3", { textContent: `Config \u2013 ${s.name}` }),
    files.length > 1 ? el("label", { class: "field" }, el("span", { textContent: "File" }), select) : select,
    pathLabel, catNote, note, wrap, ok, err,
    el("div", { class: "dialog-actions" },
      el("button", { type: "button", class: "secondary", textContent: "Close", onclick: closeDialog }), save)));
  if (files.length <= 1) select.hidden = true;
  load();
}

// -------------------------------------------------------------------- monitoring page
function bar(pct, cls = "") {
  return el("div", { class: `bar ${cls}` }, el("div", { class: "fill", style: `width:${Math.max(0, Math.min(100, pct || 0))}%` }));
}

function stateWord(r) {
  const sv = findServer(r.server_id);
  return sv ? serverState(sv).label : (r.running ? "running" : "stopped");
}

function renderMonitoring() {
  if (!stats) return;
  const h = stats.host;
  const memPct = h.mem_total ? 100 * h.mem_used / h.mem_total : 0;
  const diskPct = h.disk_total ? 100 * h.disk_used / h.disk_total : 0;
  const hist = history.host || [];
  const tile = (label, value, sub, extra) => el("div", { class: "tile" },
    el("div", { class: "live-label", textContent: label }),
    el("div", { class: "tile-value", textContent: value }),
    sub ? el("div", { class: "live-sub", textContent: sub }) : null, extra);
  $("host-strip").replaceChildren(
    tile("Host CPU", fmtPct(h.cpu_percent), `${h.cpu_count} cores` + (h.load1 !== null ? `, load ${h.load1.toFixed(2)}` : ""),
      sparkline(hist.map((p) => p.cpu), { max: 100, cls: "cpu" })),
    tile("Host memory", `${fmtBytes(h.mem_used)} / ${fmtBytes(h.mem_total)}`, `${fmtPct(memPct)} used`,
      el("div", {}, bar(memPct, memPct > 90 ? "hot" : ""))),
    tile("Disk (data folder)", `${fmtBytes(h.disk_free)} free`, `of ${fmtBytes(h.disk_total)}`,
      el("div", {}, bar(diskPct, diskPct > 90 ? "hot" : ""))),
    tile("Host uptime", fmtUptime(h.uptime_seconds), null, null));
  $("mon-error").hidden = !stats.error;
  $("mon-error").textContent = stats.error ? `Could not read container stats: ${stats.error}` : "";

  const keys = {
    cpu: (a, b) => b.cpu_percent - a.cpu_percent || a.name.localeCompare(b.name),
    memory: (a, b) => b.mem_used - a.mem_used || a.name.localeCompare(b.name),
    name: (a, b) => a.name.localeCompare(b.name),
  };
  const rows = [...stats.servers].sort((a, b) => (Number(b.running) - Number(a.running)) || keys[rankSort](a, b));
  const running = rows.filter((r) => r.running);
  const maxCpu = Math.max(100, ...running.map((r) => r.cpu_percent));
  const th = (label, key) => el("th", { class: key ? `sortable ${rankSort === key ? "sorted" : ""}` : "",
    textContent: label + (key && rankSort === key ? " \u25BE" : ""), onclick: key ? () => { rankSort = key; renderMonitoring(); } : null });
  const total = (f) => running.reduce((a, r) => a + f(r), 0);
  const body = rows.map((r) => el("tr", { class: r.running ? "" : "dim", id: `rank-${r.server_id}` },
    el("td", {}, el("strong", { textContent: r.name })),
    el("td", { textContent: stateWord(r) }),
    el("td", { class: "num" }, r.running ? el("div", { class: "cellbar" }, el("span", { textContent: fmtPct(r.cpu_percent) }), bar(100 * r.cpu_percent / maxCpu, "cpu")) : "\u2013"),
    el("td", { class: "num", textContent: r.running ? fmtPct(r.cpu_of_host_percent) : "\u2013" }),
    el("td", { class: "num", textContent: r.running ? fmtBytes(r.mem_used) : "\u2013" }),
    el("td", { class: "num", textContent: r.running ? fmtPct(r.mem_of_host_percent) : "\u2013" }),
    el("td", { class: "num", textContent: r.running ? `\u2193 ${fmtRate(r.net_rx_rate)}  \u2191 ${fmtRate(r.net_tx_rate)}` : "\u2013" }),
    el("td", { class: "num", textContent: r.running ? fmtUptime(r.uptime_seconds) : "\u2013" })));
  const foot = running.length ? el("tr", { class: "total" },
    el("td", { textContent: "All GameDock servers" }), el("td", { textContent: `${running.length} running` }),
    el("td", { class: "num", textContent: fmtPct(total((r) => r.cpu_percent)) }),
    el("td", { class: "num", textContent: fmtPct(total((r) => r.cpu_of_host_percent)) }),
    el("td", { class: "num", textContent: fmtBytes(total((r) => r.mem_used)) }),
    el("td", { class: "num", textContent: fmtPct(total((r) => r.mem_of_host_percent)) }),
    el("td", { class: "num", textContent: `\u2193 ${fmtRate(total((r) => r.net_rx_rate))}  \u2191 ${fmtRate(total((r) => r.net_tx_rate))}` }),
    el("td", {})) : null;
  $("rank-wrap").replaceChildren(rows.length
    ? el("table", { class: "list rank" },
        el("thead", {}, el("tr", {}, th("Server", "name"), th("State"), th("CPU", "cpu"), th("CPU of host"), th("Memory", "memory"), th("Memory of host"), th("Network"), th("Uptime"))),
        el("tbody", {}, ...body), foot ? el("tfoot", {}, foot) : null)
    : el("p", { class: "empty-small", textContent: "No servers deployed yet." }));
}

async function refreshStats() {
  try {
    stats = await api("/api/stats");
    if (pollCount++ % 3 === 0 || !history.host.length) {
      history = await api("/api/stats/history?window=600");
    }
  } catch (e) { /* the connection indicator already shows problems */ }
  renderServers();
  renderMonitoring();
}

// -------------------------------------------------------------------- MCP page
let mcpInfo = null;
let mcpAudit = [];
const DESTRUCTIVE_TOOLS = ["delete_server", "restore_backup", "delete_backup", "wipe_server_data"];

function copyText(text, button) {
  const done = () => { const old = button.textContent; button.textContent = "Copied"; setTimeout(() => { button.textContent = old; }, 1500); };
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(text).then(done, () => fallbackCopy(text, done));
  } else fallbackCopy(text, done);
}
function fallbackCopy(text, done) {
  const ta = el("textarea", { value: text });
  ta.style.position = "fixed"; ta.style.opacity = "0";
  document.body.append(ta); ta.select();
  try { document.execCommand("copy"); done(); } catch (_) { /* leave it to the user */ }
  ta.remove();
}

function snippetBlock(title, note, text) {
  const btn = el("button", { class: "small", textContent: "Copy" });
  btn.addEventListener("click", () => copyText(text, btn));
  return el("div", { class: "snippet" },
    el("div", { class: "snippet-head" }, el("strong", { textContent: title }), btn),
    note ? el("div", { class: "live-sub", textContent: note }) : null,
    el("pre", { textContent: text }));
}

function mcpSnippets(info) {
  const url = info.endpoint_url;
  const auth = info.token_set ? { headers: { Authorization: "Bearer <YOUR_GAMEDOCK_MCP_TOKEN>" } } : {};
  const generic = JSON.stringify({ mcpServers: { gamedock: { type: "streamable-http", url, ...auth } } }, null, 2);
  const bridgeArgs = ["-y", "mcp-remote", url];
  const bridge = { command: "npx", args: bridgeArgs };
  if (info.token_set) {
    bridgeArgs.push("--header", "Authorization:${GAMEDOCK_AUTH}");
    bridge.env = { GAMEDOCK_AUTH: "Bearer <YOUR_GAMEDOCK_MCP_TOKEN>" };
  }
  const py = [
    "import asyncio, httpx2",
    "from mcp import Client",
    "from mcp.client.streamable_http import streamable_http_client", "",
    "async def main():",
    info.token_set ? '    http = httpx2.AsyncClient(headers={"Authorization": "Bearer <YOUR_GAMEDOCK_MCP_TOKEN>"})'
      : "    http = httpx2.AsyncClient()",
    `    async with http, Client(streamable_http_client("${url}", http_client=http)) as c:`,
    "        print([t.name for t in (await c.list_tools()).tools])",
    '        print(await c.call_tool("list_servers", {}))', "",
    "asyncio.run(main())"].join("\n");
  const curl = ["curl -s " + url, '  -H "Content-Type: application/json"', '  -H "Accept: application/json, text/event-stream"']
    .concat(info.token_set ? ['  -H "Authorization: Bearer <YOUR_GAMEDOCK_MCP_TOKEN>"'] : [])
    .concat([`  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'`]).join(" \\\n");
  return [
    snippetBlock("Generic MCP client (streamable HTTP)", "Works for most MCP-capable agent harnesses: add this to their MCP server list.", generic),
    snippetBlock("Client that only speaks stdio", "Uses the mcp-remote bridge (needs Node.js).", JSON.stringify({ mcpServers: { gamedock: bridge } }, null, 2)),
    snippetBlock("Python (official MCP SDK)", null, py),
    snippetBlock("Quick check with curl", "Lists the tools this endpoint serves.", curl),
  ];
}

function renderMcp() {
  if (!mcpInfo) return;
  const i = mcpInfo;
  const modeText = { off: "Off", read_only: "Read only", standard: "Standard", full: "Full" }[i.mode] || i.mode;
  const modeHelp = {
    off: "Nothing is served.",
    read_only: "Agents can look but not change anything.",
    standard: "Agents can deploy, start/stop, change settings, back up and edit config files. Destructive tools are hidden.",
    full: "Standard plus delete / restore / wipe, each behind a two-step confirmation.",
  }[i.mode];
  const urlBtn = el("button", { class: "small", textContent: "Copy" });
  urlBtn.addEventListener("click", () => copyText(i.endpoint_url, urlBtn));
  const cell = (label, ...kids) => el("div", { class: "tile" }, el("div", { class: "live-label", textContent: label }), ...kids);
  const tools = (i.tools || []).map((t) => el("span", { class: `chip ${DESTRUCTIVE_TOOLS.includes(t) ? "danger" : ""}`, textContent: t }));
  $("mcp-status").replaceChildren(
    cell("Endpoint", el("div", { class: "tile-value mono", id: "mcp-url", textContent: i.endpoint_url }), urlBtn),
    cell("Mode", el("div", { class: "tile-value", id: "mcp-mode", textContent: modeText }),
      el("div", { class: "live-sub", textContent: i.reason || modeHelp || "" })),
    cell("Access token", el("div", { class: "tile-value", id: "mcp-token", textContent: i.token_set ? "Required" : "Not set" }),
      el("div", { class: "live-sub", textContent: i.token_set ? "Clients send it as a Bearer token. The value is never shown here." : "Only this machine can reach the endpoint. Set GAMEDOCK_MCP_TOKEN to require a token." })),
    cell(`Tools served (${tools.length})`, el("div", { class: "chips", id: "mcp-tools" }, ...(tools.length ? tools : [el("span", { class: "live-sub", textContent: "none" })])),
      i.enabled && !i.tools.some((t) => DESTRUCTIVE_TOOLS.includes(t)) ? el("div", { class: "live-sub", textContent: "Destructive tools are not available in this mode." }) : null));
  $("mcp-snippets").replaceChildren(...(i.enabled ? mcpSnippets(i)
    : [el("p", { class: "empty-small", textContent: "MCP is switched off, so there is nothing to connect to. Set GAMEDOCK_MCP_MODE (and GAMEDOCK_MCP_TOKEN if GameDock is not bound to localhost) and restart." })]));
  renderMcpAudit();
}

function renderMcpAudit() {
  const argText = (a) => {
    const parts = Object.entries(a || {}).map(([k, v]) => `${k}=${typeof v === "object" ? JSON.stringify(v) : v}`);
    const t = parts.join(" ");
    return t.length > 140 ? t.slice(0, 140) + "\u2026" : t;
  };
  const fmt = (iso) => { const d = new Date(iso); return isNaN(d) ? iso : d.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" }); };
  $("mcp-audit").replaceChildren(mcpAudit.length
    ? el("table", { class: "list audit" },
        el("thead", {}, el("tr", {}, el("th", { textContent: "Time" }), el("th", { textContent: "Tool" }), el("th", { textContent: "Arguments (secrets hidden)" }), el("th", { textContent: "Result" }))),
        el("tbody", {}, ...mcpAudit.map((e) => el("tr", {},
          el("td", { class: "nowrap", textContent: fmt(e.time) }),
          el("td", {}, el("code", { textContent: e.tool })),
          el("td", { class: "mono small", textContent: argText(e.args) }),
          el("td", {}, el("span", { class: `abadge st-${e.status}`, textContent: e.status.replaceAll("_", " ") }),
            e.detail ? el("div", { class: "live-sub", textContent: e.detail }) : null)))))
    : el("p", { class: "empty-small", textContent: "No MCP calls yet." }));
}

async function refreshMcp() {
  try {
    mcpInfo = await api("/api/mcp/info");
    mcpAudit = (await api("/api/mcp/audit?limit=15")).entries;
  } catch (e) { /* connection indicator shows problems */ }
  renderMcp();
}

// ------------------------------------------------------------ status banners
async function refreshStatus() {
  try { appStatus = await api("/api/status"); } catch (_) { /* the connection banner covers this */ }
  renderBanners();
}

function renderBanners() {
  const notes = [];
  if (connLost) {
    notes.push(banner("error", "Lost connection to GameDock.", "The page keeps trying and will recover by itself. If it does not, check that GameDock is still running on the machine."));
  }
  if (appStatus && !connLost) {
    if (!appStatus.docker.ok && !appStatus.docker.fake) {
      notes.push(banner("error", "Docker is not available, so servers cannot be started or changed.", appStatus.docker.message, "#/health", "See how to fix this"));
    }
    if (appStatus.catalog_problems.length) {
      const n = appStatus.catalog_problems.length;
      notes.push(banner("warn", `${n} game file${n > 1 ? "s" : ""} could not be read and ${n > 1 ? "are" : "is"} hidden from the catalog.`,
        appStatus.catalog_problems.map((p) => `${p.file}: ${p.problem}`).join(" "), "#/health", "Details"));
    }
  }
  $("banners").replaceChildren(...notes);
}

function banner(kind, title, text, href, linkText) {
  return el("div", { class: `banner ${kind}` },
    el("strong", { textContent: title }), text ? el("span", { textContent: ` ${text}` }) : null,
    href ? el("a", { href, textContent: ` ${linkText}` }) : null);
}

// ---------------------------------------------------------------- health page
let doctorShown = false;
async function runDoctor() {
  const btn = $("run-doctor");
  btn.disabled = true;
  btn.textContent = "Checking\u2026";
  $("doctor-result").replaceChildren(el("p", { class: "empty-small", role: "status", textContent: "Running checks\u2026" }));
  try {
    const r = await api("/api/doctor");
    doctorShown = true;
    const label = { ok: "OK", warn: "Warning", fail: "Problem", info: "Note" };
    $("doctor-result").replaceChildren(
      el("div", { class: `banner ${r.counts.fail ? "error" : r.counts.warn ? "warn" : "okbanner"}` }, el("strong", { textContent: r.summary })),
      el("ul", { class: "checks" }, ...r.checks.map((c) => el("li", { class: `chk-item ${c.status}` },
        el("span", { class: `badge chk-${c.status}`, textContent: label[c.status] || c.status }),
        el("div", {},
          el("div", { class: "check-title", textContent: c.title }),
          el("div", { class: "check-msg", textContent: c.message }),
          c.fix && (c.status === "warn" || c.status === "fail") ? el("div", { class: "check-fix" }, el("strong", { textContent: "How to fix: " }), c.fix) : null)))));
    $("doctor-when").textContent = `Last run ${new Date().toLocaleTimeString()}`;
  } catch (e) {
    $("doctor-result").replaceChildren(el("div", { class: "error", role: "alert", textContent: `Could not run the checks: ${e.message}` }));
  } finally {
    btn.disabled = false;
    btn.textContent = "Run checks again";
  }
}
$("run-doctor").addEventListener("click", runDoctor);

// ---------------------------------------------------------- times in words
let hostTz = null;                 // the machine's timezone name, from the schedules API
function fmtWhen(iso) {
  if (!iso) return "\u2013";
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  const opts = { weekday: "long", month: "long", day: "numeric", hour: "2-digit", minute: "2-digit" };
  try { return d.toLocaleString([], hostTz ? { ...opts, timeZone: hostTz } : opts); }
  catch (_) { return d.toLocaleString([], opts); }
}
function inWords(iso) {
  const ms = new Date(iso) - Date.now();
  if (isNaN(ms)) return "";
  if (ms <= 0) return "due now";
  const m = Math.round(ms / 60000);
  if (m < 60) return `in ${Math.max(1, m)} minute${m === 1 ? "" : "s"}`;
  const h = Math.round(m / 60);
  if (h < 48) return `in ${h} hour${h === 1 ? "" : "s"}`;
  return `in ${Math.round(h / 24)} days`;
}
const RESULT_LABEL = { ok: "Worked", failed: "Failed", skipped: "Skipped", waiting: "Waiting" };
const TRIGGER_LABEL = { schedule: "on schedule", "catch-up": "catch-up after GameDock was off", manual: "started by hand" };
const resultBadge = (r) => r ? el("span", { class: `badge result-${r}`, textContent: RESULT_LABEL[r] || r }) : null;

// ------------------------------------------------- schedule line on a server tile
function schedText(s) {
  const sc = s.schedule || {};
  if (!sc.backup && !sc.maintenance) return "Schedules: none set up";
  const parts = [`Backups: ${sc.backup || "off"}`, `Maintenance: ${sc.maintenance || "off"}`];
  if (sc.next_run) parts.push(`next ${inWords(sc.next_run)}`);
  return parts.join(" | ");
}
function schedLine(s) {
  const sc = s.schedule || {};
  const txt = schedText(s) + (sc.last_problem ? "|problem" : "");
  const node = el("button", { type: "button", class: "sched-line", title: "Open the schedules of this server",
    onclick: () => openSchedules(s.id), "aria-label": `Schedules of ${s.name}` },
    el("span", { textContent: schedText(s) }),
    sc.last_problem ? el("span", { class: "sched-problem", textContent: " \u2013 the last scheduled run failed" }) : null);
  node.dataset.sig = txt;
  return node;
}
function gameLine(s) {
  if (!s.steam_app_id) return null;
  const what = s.game_version ? `Game version ${s.game_version}` : s.installed_build ? `Game build ${s.installed_build}` : "Game version not known yet";
  const how = { up_to_date: ", up to date", update_available: ", newer version available", could_not_check: ", could not check for updates" }[s.game_update_status] || "";
  return el("div", { class: `meta game-line ${s.game_update_status}`, id: `game-line-${s.id}`,
    title: s.installed_build ? `Steam build ${s.installed_build}${s.latest_build ? `, newest ${s.latest_build}` : ""}` : "" }, what + how);
}

function updateFlag(s) {
  const image = s.update_available === true, game = s.game_update_status === "update_available";
  if (!image && !game) return null;
  const text = image && game ? "A newer game version and a newer game image are available. Open Updates for details."
    : game ? `A newer game version is available (build ${s.installed_build} \u2192 ${s.latest_build}).`
    : "A newer version of the game image is ready.";
  return el("div", { class: "update-flag" },
    el("span", { class: `badge update ${game && !image ? "game" : ""}`, textContent: "Update available" }),
    el("span", { class: "hint", textContent: text }),
    el("button", { type: "button", class: "primary small", textContent: "Update now", disabled: !!(busy.get(s.id) || s.activity),
      onclick: () => openUpdate(s.id), "aria-label": `Update ${s.name} now` }));
}

// ------------------------------------------------------------------ GameDock version and update
let versionInfo = null;
function renderVersion() {
  const v = versionInfo;
  const box = $("version-box");
  if (!v) { box.replaceChildren(); return; }
  const chip = el("button", { type: "button", class: "version-chip", id: "version-chip",
    title: "About this GameDock, and check for updates", onclick: openAbout },
    `Version ${v.version}`, v.commit_short ? el("span", { class: "commit", textContent: ` (${v.commit_short})` }) : null);
  const nodes = [chip];
  if (v.update_available === true) {
    nodes.push(el("button", { type: "button", class: "update-pill", id: "update-pill", onclick: openAbout,
      title: v.latest && v.latest.message ? `Newest change: ${v.latest.message}` : "A newer GameDock is available" },
      "Update available", v.latest && v.latest.version ? ` \u2013 ${v.latest.version}` : ""));
  }
  box.replaceChildren(...nodes);
}
async function refreshVersion() {
  try { versionInfo = await api("/api/version"); } catch (_) { /* the connection indicator covers this */ }
  renderVersion();
}

function aboutBody(v) {
  const rows = [
    ["Version", v.version],
    ["Commit", v.commit ? `${v.commit_short}${v.branch ? ` on ${v.branch}` : ""}${v.dirty ? " (has local changes)" : ""}` : "not a git checkout"],
    ["Repository", v.repo],
    ["Install folder", v.install_dir],
    ["Running since", fmtWhen(v.started_at)],
  ];
  return el("table", { class: "list about" }, el("tbody", {}, ...rows.map(([k, val]) => el("tr", {},
    el("th", { textContent: k }), el("td", { class: k === "Install folder" ? "mono small" : "", textContent: val })))));
}

function updateStatusBlock(v) {
  if (!v.check_enabled) return el("p", { class: "hint", textContent: "Update checks are switched off on this machine (GAMEDOCK_UPDATE_CHECK=false), so GameDock never contacts GitHub." });
  const when = v.checked_at ? `Last checked ${fmtWhen(v.checked_at)}.` : "Not checked yet.";
  if (v.update_available === true) {
    const l = v.latest || {};
    const n = l.ahead_by ? `${l.ahead_by} newer change${l.ahead_by === 1 ? "" : "s"}` : "A newer version";
    return el("div", { class: "update-box" },
      el("strong", { textContent: `Update available${l.version ? `: GameDock ${l.version}` : ""}` }),
      el("div", { textContent: `${n} on GitHub.${l.message ? ` Newest: \u201c${l.message}\u201d.` : ""}` }),
      el("div", { class: "hint", textContent: when }));
  }
  if (v.update_available === false) {
    return el("p", { class: "okmsg", textContent: `GameDock is up to date. ${when}` });
  }
  return el("p", { class: "hint", textContent: `${v.check_error || "Could not tell whether a newer GameDock exists."} ${when}` });
}

function openAbout(note) {
  const v = versionInfo;
  if (!v) return;
  const err = el("div", { class: "form-error", hidden: true });
  const check = el("button", { type: "button", class: "secondary", id: "about-check", textContent: "Check now", disabled: !v.check_enabled });
  check.addEventListener("click", async () => {
    check.disabled = true; check.replaceChildren(spinner(), "Checking\u2026");
    try { versionInfo = await api("/api/gamedock/update-check", "POST"); renderVersion(); openAbout(); }
    catch (e) { err.textContent = e.message; err.hidden = false; check.disabled = false; check.textContent = "Check now"; }
  });
  const su = v.self_update || {};
  const canUpdate = v.update_available === true && su.possible;
  const manual = v.update_available === true && !su.possible;
  const doUpdate = el("button", { type: "button", class: "primary", id: "about-update", textContent: "Update GameDock", disabled: !canUpdate,
    title: canUpdate ? "" : (su.reason || "No update to install") });
  doUpdate.addEventListener("click", confirmGameDockUpdate);
  const cmds = (su.manual_commands || []).join("\n");
  const copyBtn = el("button", { type: "button", class: "small", textContent: "Copy" });
  copyBtn.addEventListener("click", () => copyText(cmds, copyBtn));
  openDialog(el("div", { class: "panel" },
    el("h3", { textContent: `GameDock ${v.version}` }),
    typeof note === "string" ? el("div", { class: "okmsg", textContent: note }) : null,
    aboutBody(v),
    updateStatusBlock(v),
    manual ? el("p", { class: "warn", textContent: `The Update button is not available here: ${su.reason}` }) : null,
    err,
    el("details", { open: manual }, el("summary", { textContent: "Update by hand (on the machine that runs GameDock)" }),
      el("pre", { class: "logbox short", textContent: cmds }), copyBtn,
      el("p", { class: "hint", textContent: "The last command restarts GameDock only. Your game servers keep running. If the update changes required packages, also run: .venv/bin/pip install -e ." })),
    el("div", { class: "dialog-actions" },
      check, el("span", { class: "spacer" }), doUpdate,
      el("button", { type: "button", class: "secondary", textContent: "Close", onclick: closeDialog }))));
}

function confirmGameDockUpdate() {
  openDialog(confirmView({
    title: "Update GameDock?",
    paragraphs: [
      "GameDock will download the newest version from GitHub (git pull) and then restart itself. Only GameDock restarts: your game servers keep running and players stay connected.",
      "This page reconnects by itself after a few seconds and shows the new version.",
      "GameDock refuses to update while it is busy (a backup, update or scheduled job is running) or when its own files have local changes. Nothing is changed in that case.",
    ],
    confirmText: "Update GameDock", danger: false,
    workingText: "Downloading the update\u2026",
    onBack: openAbout,
    onConfirm: async () => {
      const before = versionInfo && versionInfo.started_at;
      const r = await api("/api/gamedock/update?confirm=true", "POST");
      if (!r.updated) { await refreshVersion(); return openAbout(r.message); }
      openDialog(el("div", { class: "panel", id: "restart-panel", role: "status" },
        el("h3", { textContent: "Restarting GameDock\u2026" }),
        el("p", { textContent: r.message }),
        el("div", { class: "statusbox busy" }, el("div", { class: "progress indeterminate" }, el("div", { class: "fill" })),
          el("div", { class: "status-text", id: "restart-text", textContent: "Waiting for GameDock to come back\u2026" }))));
      waitForRestart(before);
    },
  }));
}

async function waitForRestart(startedBefore) {
  const t0 = Date.now();
  while (Date.now() - t0 < 120000) {
    await sleep(1500);
    try {
      const res = await fetch("/api/version", { cache: "no-store" });
      if (res.ok) {
        const v = await res.json();
        if (v.started_at !== startedBefore) {
          const t = $("restart-text"); if (t) t.textContent = `Back up as version ${v.version}. Reloading\u2026`;
          await sleep(600);
          location.reload();
          return;
        }
      }
    } catch (_) { /* still restarting */ }
  }
  const t = $("restart-text");
  if (t) t.textContent = "GameDock has not come back after two minutes. Check the service on the machine (systemctl --user status gamedock), then reload this page.";
}

// ---------------------------------------------------------------------- schedules
let schedulesData = null;
async function refreshSchedules() {
  try { schedulesData = await api("/api/schedules"); hostTz = schedulesData.timezone && schedulesData.timezone.name; }
  catch (_) { /* connection indicator covers it */ }
  if (location.hash.startsWith("#/schedules")) renderSchedulesPage();
}

function runLine(label, st) {
  const row = el("div", { class: "run-row" }, el("span", { class: "run-label", textContent: label }));
  if (!st.enabled) { row.append(el("span", { class: "hint", textContent: "Off" })); }
  else row.append(el("span", { textContent: st.summary }),
    el("span", { class: "hint", textContent: st.next_run ? ` \u2013 next ${fmtWhen(st.next_run)} (${inWords(st.next_run)})` : "" }));
  const last = el("div", { class: "run-last" });
  if (st.last_result) {
    last.append(resultBadge(st.last_result), el("span", { class: "hint", textContent: ` ${fmtWhen(st.last_run)}` }));
    if (st.last_message) last.append(el("div", { class: st.last_result === "failed" ? "run-msg bad" : "run-msg", textContent: st.last_message }));
  } else last.append(el("span", { class: "hint", textContent: "Has not run yet." }));
  return el("div", { class: "run" }, row, last);
}

function renderSchedulesPage() {
  const d = schedulesData;
  if (!d) return;
  const tz = d.timezone;
  $("sched-tz").textContent = `Times of day are in this machine's timezone: ${tz.name} (${tz.utc_offset}).`;
  $("sched-problem").hidden = !d.problem;
  $("sched-problem").textContent = d.problem || "";
  $("sched-empty").hidden = d.servers.length > 0;
  $("sched-list").replaceChildren(...d.servers.map((s) => el("article", { class: "card", id: `sched-${s.server_id}` },
    el("div", { class: "card-head" }, el("h2", { textContent: s.server_name }),
      el("button", { type: "button", class: "primary", textContent: "Edit schedules", "aria-label": `Edit schedules of ${s.server_name}`,
        onclick: () => openSchedules(s.server_id) })),
    el("div", { class: "run-block" }, el("h4", { textContent: "Backups" }), runLine("", s.backup)),
    el("div", { class: "run-block" }, el("h4", { textContent: "Maintenance" }), runLine("", s.maintenance)))));
}

// A "how often" control: frequency select + hours / time / weekdays. kinds: which frequencies to offer.
const DAYS = [["mon", "Monday"], ["tue", "Tuesday"], ["wed", "Wednesday"], ["thu", "Thursday"], ["fri", "Friday"], ["sat", "Saturday"], ["sun", "Sunday"]];
function recurrenceControls(prefix, kinds, init) {
  const labels = { interval: "Every few hours", daily: "Every day at a set time", weekly: "On chosen days of the week" };
  const sel = el("select", { id: `${prefix}-freq`, "aria-label": "How often" },
    ...kinds.map((k) => el("option", { value: k, textContent: labels[k], selected: init.frequency === k })));
  const hours = el("input", { type: "number", min: 1, max: 168, value: init.every_hours || 6, id: `${prefix}-hours`, "aria-label": "Hours between backups" });
  const time = el("input", { type: "time", value: init.time || "03:00", id: `${prefix}-time`, "aria-label": "Time of day", required: true });
  const days = DAYS.map(([k, label]) => {
    const cb = el("input", { type: "checkbox", checked: (init.days || []).includes(k), id: `${prefix}-day-${k}` });
    return el("label", { class: "check day" }, cb, label);
  });
  const hoursRow = el("label", { class: "field" }, el("span", { textContent: "Hours between backups" }), hours);
  const timeRow = el("label", { class: "field" }, el("span", { textContent: "Time of day (this machine's timezone)" }), time);
  const daysRow = el("div", { class: "days", role: "group", "aria-label": "Days of the week" }, ...days);
  const show = () => {
    hoursRow.hidden = sel.value !== "interval";
    timeRow.hidden = sel.value === "interval";
    daysRow.hidden = sel.value !== "weekly";
  };
  sel.addEventListener("change", show);
  show();
  return {
    node: el("div", { class: "recur" }, el("label", { class: "field" }, el("span", { textContent: "How often" }), sel), hoursRow, timeRow, daysRow),
    read() {
      const f = sel.value;
      return { frequency: f, every_hours: f === "interval" ? Number(hours.value) : null, time: f === "interval" ? null : time.value,
        days: f === "weekly" ? DAYS.filter(([k]) => $(`${prefix}-day-${k}`).checked).map(([k]) => k) : null };
    },
  };
}

function historyTable(history) {
  if (!history.length) return el("p", { class: "empty-small", textContent: "No scheduled or manual runs yet." });
  return el("table", { class: "list history" },
    el("thead", {}, el("tr", {}, ...["When", "What", "Started", "Result"].map((h) => el("th", { textContent: h })))),
    el("tbody", {}, ...history.slice(0, 8).map((h) => el("tr", {},
      el("td", { class: "nowrap", textContent: fmtWhen(h.time) }),
      el("td", { textContent: h.kind === "backup" ? "Backup" : "Maintenance" }),
      el("td", { textContent: TRIGGER_LABEL[h.trigger] || h.trigger }),
      el("td", {}, resultBadge(h.result), el("div", { class: h.result === "failed" ? "run-msg bad" : "run-msg", textContent: h.message }))))));
}

async function openSchedules(id, message, kind = "ok") {
  await refresh();
  const s = findServer(id);
  if (!s) return closeDialog();
  let v;
  try { v = await api(`/api/servers/${sid(id)}/schedules`); hostTz = v.timezone.name; }
  catch (e) { return openDialog(resultView("Schedules", [[e.message, "warn"]])); }
  const b = v.backup, m = v.maintenance;
  const msg = el("div", { class: kind === "ok" ? "okmsg" : "form-error", hidden: !message, textContent: message || "" });

  // ---- backups section
  const bOn = el("input", { type: "checkbox", id: "sched-backup-on", checked: b.enabled });
  const bRec = recurrenceControls("sb", ["interval", "daily", "weekly"], b);
  const keep = el("input", { type: "number", min: 1, max: 100, id: "sched-keep", value: b.retention || "", placeholder: `${b.retention_in_use} (usual)` });
  const bErr = el("div", { class: "form-error", hidden: true });
  const bSave = el("button", { type: "button", class: "primary", id: "sched-backup-save", textContent: "Save backup schedule" });
  bSave.addEventListener("click", async () => {
    bSave.disabled = true; bErr.hidden = true;
    try {
      await api(`/api/servers/${sid(id)}/schedules/backup`, "PUT", { enabled: bOn.checked, ...bRec.read(),
        retention: keep.value === "" ? null : Number(keep.value) });
      await refresh(); refreshSchedules();
      openSchedules(id, "Backup schedule saved.");
    } catch (e) { bErr.textContent = e.message; bErr.hidden = false; bSave.disabled = false; }
  });

  // ---- maintenance section
  const mOn = el("input", { type: "checkbox", id: "sched-maint-on", checked: m.enabled });
  const mRec = recurrenceControls("sm", ["daily", "weekly"], m.frequency === "interval" ? { ...m, frequency: "weekly" } : m);
  const mBackup = el("input", { type: "checkbox", id: "sched-m-backup", checked: m.backup_first });
  const mImage = el("input", { type: "checkbox", id: "sched-m-image", checked: m.update_image && (m.image_update_supported || m.game_update_supported), disabled: !(m.image_update_supported || m.game_update_supported) });
  const mRestart = el("input", { type: "checkbox", id: "sched-m-restart", checked: m.restart });
  const mEmpty = el("input", { type: "checkbox", id: "sched-m-empty", checked: false, disabled: true });
  const mErr = el("div", { class: "form-error", hidden: true });
  const mSave = el("button", { type: "button", class: "primary", id: "sched-maint-save", textContent: "Save maintenance schedule" });
  mSave.addEventListener("click", async () => {
    mSave.disabled = true; mErr.hidden = true;
    try {
      await api(`/api/servers/${sid(id)}/schedules/maintenance`, "PUT", { enabled: mOn.checked, ...mRec.read(),
        backup_first: mBackup.checked, update_image: mImage.checked, restart: mRestart.checked });
      await refresh(); refreshSchedules();
      openSchedules(id, "Maintenance schedule saved.");
    } catch (e) { mErr.textContent = e.message; mErr.hidden = false; mSave.disabled = false; }
  });
  const runNow = el("button", { type: "button", class: "secondary", id: "sched-run-now", textContent: "Run maintenance now\u2026",
    disabled: !!s.activity, onclick: () => confirmMaintenanceNow(id, s, m) });
  const opt = (input, label, help) => el("div", { class: "opt" }, el("label", { class: "check" }, input, label),
    help ? el("div", { class: "hint opt-help", textContent: help }) : null);

  openWide(el("div", { class: "panel sched-panel", id: "sched-panel" },
    el("h3", { textContent: `Schedules \u2013 ${s.name}` }),
    el("p", { class: "hint", textContent: `Times of day are in this machine's timezone: ${v.timezone.name} (${v.timezone.utc_offset}). If GameDock is switched off at the time, a missed backup runs once when it starts again.` }),
    msg,
    el("section", { class: "sched-section", "aria-labelledby": "sb-title" },
      el("h4", { id: "sb-title", textContent: "Scheduled backups" }),
      el("label", { class: "check" }, bOn, "Back up this server automatically"),
      bRec.node,
      el("label", { class: "field" }, el("span", { textContent: "Backups to keep" }), keep,
        el("span", { class: "hint", textContent: "Older ones are removed automatically. Leave empty for GameDock's usual number." })),
      el("p", { class: "hint", textContent: b.how }),
      runLine("", b), bErr,
      el("div", { class: "dialog-actions" }, bSave)),
    el("section", { class: "sched-section", "aria-labelledby": "sm-title" },
      el("h4", { id: "sm-title", textContent: "Scheduled maintenance" }),
      el("p", { class: "hint", textContent: "Maintenance is the regular tidy-up: it can back up, bring the game up to date and restart the server, one step after another, never alongside anything else on this server." }),
      el("label", { class: "check" }, mOn, "Run maintenance on this server automatically"),
      mRec.node,
      el("div", { class: "opts" },
        opt(mBackup, "Take a backup first", "Saved as an automatic backup before anything changes."),
        opt(mImage, m.game_update_supported ? "Check for updates and apply them" : "Check for a newer game image and apply it",
          m.game_update_supported ? "Looks for a newer game version (Steam build) and, if there is one, installs it as part of the restart, after a safety backup. The result says which build the game moved from and to." + (m.image_update_supported ? " A newer game image is applied too." : "")
            : m.image_update_supported ? (s.update_note || "Downloads the newest image, then recreates the container with it. Your data and settings are kept.")
            : "Not available: this game updates its files inside the container, so there is no image to apply. Restarting is how it updates."),
        opt(mRestart, "Restart the server", "Players are disconnected for a short time."),
        opt(mEmpty, "Only when nobody is playing", `Not available: ${m.only_when_empty.reason}`)),
      el("p", { class: "hint", textContent: "A maintenance run that was missed because GameDock was off is skipped when it is more than 6 hours late, so the game is never restarted at a surprise time." }),
      runLine("", m), mErr,
      el("div", { class: "dialog-actions" }, runNow, el("span", { class: "spacer" }), mSave)),
    el("section", { class: "sched-section" }, el("h4", { textContent: "Recent runs" }), historyTable(v.history)),
    el("div", { class: "dialog-actions" }, el("span", { class: "spacer" }),
      el("button", { type: "button", class: "secondary", textContent: "Close", onclick: closeDialog }))));
}

function confirmMaintenanceNow(id, s, m) {
  const steps = [];
  if (m.backup_first) steps.push("take a backup");
  if (m.update_image && m.game_update_supported) steps.push("check for a newer game version and install it with the restart" + (m.image_update_supported ? ", and apply a newer game image" : ""));
  else if (m.update_image && m.image_update_supported) steps.push("download a newer game image if there is one and recreate the container with it");
  if (m.restart) steps.push("restart the server");
  const running = s.status === "running";
  openDialog(confirmView({
    title: `Run maintenance for ${s.name} now?`,
    paragraphs: [
      steps.length ? `GameDock will ${steps.join(", then ")}. It uses the options saved in the schedule (switched on or not).`
        : "The saved maintenance options do nothing yet. Close this, choose at least one option and save.",
      running && (m.restart || m.update_image)
        ? "The server is running: players are disconnected while it restarts, which can take a few minutes if a new image is downloaded."
        : running ? "The server keeps running." : "The server is stopped and stays stopped.",
      "Nothing else can be done to this server until maintenance has finished.",
    ],
    checkboxLabel: steps.length ? "Yes, run maintenance now." : undefined, confirmText: "Run maintenance now", danger: false,
    workingText: "Running maintenance. This can take several minutes\u2026",
    onBack: () => openSchedules(id),
    onConfirm: async () => {
      const r = await api(`/api/servers/${sid(id)}/schedules/maintenance/run?confirm=true`, "POST");
      await refresh();
      await openSchedules(id, `${r.result === "ok" ? "Maintenance finished." : "Maintenance finished with a problem."} ${r.message}`, r.result === "ok" ? "ok" : "warn");
    },
  }));
}

// -------------------------------------------------------------------- routing
function route() {
  const page = location.hash.startsWith("#/catalog") ? "catalog"
    : location.hash.startsWith("#/mcp") ? "mcp"
    : location.hash.startsWith("#/health") ? "health"
    : location.hash.startsWith("#/schedules") ? "schedules"
    : location.hash.startsWith("#/monitoring") ? "monitoring" : "dashboard";
  const titles = { dashboard: "Dashboard", catalog: "Catalog", monitoring: "Monitoring", schedules: "Schedules", mcp: "AI agents (MCP)", health: "Health" };
  for (const p of Object.keys(titles)) {
    $(`page-${p}`).hidden = page !== p;
    $(`nav-${p}`).classList.toggle("active", page === p);
    if (page === p) $(`nav-${p}`).setAttribute("aria-current", "page"); else $(`nav-${p}`).removeAttribute("aria-current");
  }
  document.title = `${titles[page]} - GameDock`;
  if (page === "health" && !doctorShown) runDoctor();
  if (page === "monitoring") renderMonitoring();
  if (page === "mcp") refreshMcp();
  if (page === "schedules") { renderSchedulesPage(); refreshSchedules(); }
  showError("");
}

async function refresh() {
  try {
    servers = await api("/api/servers");
    serversLoaded = true;
    if (connLost) { connLost = false; refreshStatus(); }
    $("conn").textContent = "connected";
    $("conn").className = "conn ok";
    renderServers();
  } catch (e) {
    connLost = true;
    $("conn").textContent = "disconnected";
    $("conn").className = "conn bad";
    renderBanners();
  }
}

async function loadCatalog() {
  try {
    catalog = await api("/api/catalog");
    renderCatalog();
  } catch (e) {
    showError(`Could not load the catalog: ${e.message}`);
    $("catalog-empty").textContent = "The catalog could not be loaded.";
  }
}

window.addEventListener("hashchange", route);
route();
loadCatalog();
refresh().then(refreshStats);
refreshStatus();
refreshVersion();
refreshSchedules();
setInterval(() => { if (!document.hidden) refreshStatus(); }, 15000);
setInterval(() => { if (!document.hidden) refreshVersion(); }, 60000);
setInterval(() => {
  if (!document.hidden) refresh().then(refreshStats);
  if (location.hash.startsWith("#/mcp") && !document.hidden) refreshMcp();
  if (location.hash.startsWith("#/schedules") && !document.hidden) refreshSchedules();
}, POLL_MS);
