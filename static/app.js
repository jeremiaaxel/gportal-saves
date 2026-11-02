const $ = (sel) => document.querySelector(sel);

let pollTimer = null;
let configState = { configured: false };

/* ── button busy states, always time-bounded so nothing sticks ── */
const busyBtns = new Map(); // btn -> original label

function setBusy(btn, label, timeoutMs = 45000) {
  if (busyBtns.has(btn)) return; // already busy
  busyBtns.set(btn, btn.textContent);
  btn.disabled = true;
  btn.textContent = label;
  btn.classList.add("busy");
  btn._busyTimer = setTimeout(() => clearBusy(btn, true), timeoutMs);
}

function clearBusy(btn, timedOut = false) {
  if (!busyBtns.has(btn)) return;
  clearTimeout(btn._busyTimer);
  btn.textContent = busyBtns.get(btn);
  btn.classList.remove("busy");
  btn.disabled = false;
  busyBtns.delete(btn);
  if (timedOut) {
    const el = $("#cfg-test-result");
    if (el && document.contains(el)) {
      el.textContent = "⚠ This took unusually long — server may be slow or unreachable. Check the host/port and try again.";
      el.className = "test-result warn";
    }
  }
}

function withBusy(btn, fn, label = "⏳ Working…", timeoutMs = 45000) {
  return async (...args) => {
    if (busyBtns.has(btn)) return;
    setBusy(btn, label, timeoutMs);
    try {
      await fn(...args);
    } finally {
      clearBusy(btn);
    }
  };
}

async function api(path, opts) {
  const r = await fetch(path, opts);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || `${r.status} ${r.statusText}`);
  return data;
}

function fmtBytes(n) {
  if (n == null || n === undefined) return "—";
  if (n > 1e9) return (n / 1e9).toFixed(2) + " GB";
  if (n > 1e6) return (n / 1e6).toFixed(1) + " MB";
  return (n / 1e3).toFixed(0) + " KB";
}

async function refresh() {
  try {
    const st = await api("/api/state");
    configState = st;
    renderState(st);
  } catch (e) {
    $("#conn-banner").textContent = "UI backend not reachable: " + e.message;
    $("#conn-banner").classList.remove("hidden");
  }
}

function renderState(st) {
  const configured = !!st.configured;
  configState = st;
  // setup card visible when: not configured (first-run), or user opened
  // settings and hasn't closed/saved it
  const setupOpen = !configured || setupMode === "settings-open";
  $("#setup-backdrop").classList.toggle("hidden", !setupOpen);
  $("#workflows-wrap").classList.toggle("hidden", !configured);

  if (configured) {
    $("#storage-note").textContent = storageNote(st.storage);
    renderSnapshots(st);
    renderJob(st);
    // NOTE: never auto-refresh settings fields here — setSetupMode() runs on
    // button click; re-running it on every poll would clobber in-progress edits
  }
  $("#version-footer").textContent = `gportal-saves v${st.version || "?"} · store: ${st.store_root}`;
}

function storageNote(kind) {
  switch (kind) {
    case "keyring": return "Credentials will be stored in your system keyring (encrypted).";
    case "file": return "No keyring found — credentials will be stored in ~/.local/state/gportal-saves/ (chmod 600).";
    case "env": return "Config comes from environment variables this session.";
    default: return "";
  }
}

function renderSnapshots(st) {
  const tbody = $("#snap-table tbody");
  tbody.innerHTML = "";
  const list = (st.snapshots[st.games[0]] || []);
  const lv = (st.last_verify || {})[st.games[0]];
  if (!list.length) {
    tbody.innerHTML = `<tr><td colspan="4" class="muted">No snapshots yet — hit "⬇ Backup now".</td></tr>`;
  }
  for (const s of list) {
    const tr = document.createElement("tr");
    const v = s.verify;
    const vBadge = v ? (v.ok ? `<span class="badge ok">✔ verified</span>` : `<span class="badge err">✘ failed</span>`)
                      : (lv && lv.snapshot === s.name ? `<span class="badge ok">✔ verified</span>` : `<span class="badge muted-badge">not verified</span>`);
    tr.innerHTML = `<td>${s.name}${s.name === list[0].name ? ' <span class="badge accent">latest</span>' : ""}</td>
      <td>${s.files ?? "—"}</td>
      <td>${fmtBytes(s.bytes)}</td>
      <td>${vBadge}</td>`;
    tbody.appendChild(tr);
  }
  // restore "from" options
  const sel = $("#restore-snapshot");
  sel.innerHTML = "";
  for (const s of list) {
    const opt = document.createElement("option");
    opt.value = s.name;
    opt.textContent = s.name + (s.name === list[0].name ? "  (latest)" : "");
    sel.appendChild(opt);
  }
  if (list.length) $("#latest-name").textContent = `(${list[0].name})`;
  $("#store-path").textContent = "Local store: " + st.store_root;
}

function restoreSource() {
  return document.querySelector('input[name="restore-src"]:checked').value === "pick"
    ? $("#restore-snapshot").value : null; // null = latest
}

function restoreTarget() {
  return document.querySelector('input[name="restore-dst"]:checked').value;
}

function renderJob(st) {
  const jobIds = Object.keys(st.jobs).sort((a, b) => (st.jobs[b].started || 0) - (st.jobs[a].started || 0));
  if (!jobIds.length) return;
  const j = st.jobs[jobIds[0]];
  const logEl = $("#log");
  logEl.textContent = j.log.join("\n") || "(starting…)";
  if (j.error) logEl.textContent += "\n✘ ERROR: " + j.error;
  if (j.status === "done") logEl.textContent += "\n✔ done";
  if (j.status === "running") logEl.textContent += "\n… running";
  logEl.classList.toggle("error", j.status === "error");
  // snapshot sizes once a pull finishes
  if (j.status === "done" && j.kind === "pull" && j.result && j.result.checklist) {
    const rows = [...$("#snap-table tbody").rows];
    const row = rows.find((r) => r.textContent.includes(j.result.snapshot));
    if (row) {
      row.children[1].textContent = j.result.files;
      row.children[2].textContent = fmtBytes(j.result.bytes);
    }
  }
  if (j.status === "running") {
    if (!pollTimer) pollTimer = setTimeout(() => { pollTimer = null; refresh(); }, 1200);
  }
}

async function doAction(path, body) {
  try {
    const res = await api(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    });
    $("#log").textContent = "Started " + res.job + "…";
    setTimeout(refresh, 300);
  } catch (e) {
    $("#log").textContent = "✘ " + e.message;
    $("#log").classList.add("error");
  }
}

/* ── setup wizard + settings (one surface, two modes) ────── */
let setupMode = "first-run"; // "first-run" (wizard, unconfigured) |
                             // "settings-open" (user clicked ⚙) |
                             // "closed" (settings saved/closed)

function readWizard() {
  return {
    host: $("#cfg-host").value.trim(),
    port: parseInt($("#cfg-port").value || "21", 10),
    user: $("#cfg-user").value.trim(),
    password: $("#cfg-pass").value,
    client_dir: $("#cfg-client-dir").value.trim(),
    ftp_root: $("#cfg-ftp-root").value.trim(),
  };
}

function applyParsed(p) {
  const el = $("#cfg-parse-result");
  if (!p.ok) {
    el.textContent = "✘ " + (p.error || "Could not parse that.");
    el.className = "err";
    return;
  }
  if (p.host) $("#cfg-host").value = p.host;
  if (p.user) $("#cfg-user").value = p.user;
  if (p.password) $("#cfg-pass").value = p.password;
  if (p.port) $("#cfg-port").value = p.port;
  el.textContent = (p.warning ? "⚠ " + p.warning : "✔ Filled the fields below from your paste.") +
    (p.password ? "" : " (No password in the URL — enter it manually.)");
  el.className = p.warning ? "warn" : "ok";
}

$("#btn-cfg-parse").onclick = () => parsePaste();

// auto-parse as soon as a complete URL is pasted (no button click needed)
$("#cfg-url").addEventListener("input", () => {
  const v = $("#cfg-url").value.trim();
  // fire once the paste looks complete: scheme + host + at least a user
  if (/^[a-z]+:\/\/[^/]+@/i.test(v) || /^[a-z0-9.-]+:\d+$/i.test(v)) {
    parsePaste();
  }
});

async function parsePaste() {
  const url = $("#cfg-url").value;
  if (!url.trim()) { $("#cfg-parse-result").textContent = "Paste an FTP line first."; return; }
  try {
    const p = await api("/api/config/parse-url", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url }),
    });
    applyParsed(p);
  } catch (e) {
    $("#cfg-parse-result").textContent = "✘ " + e.message;
    $("#cfg-parse-result").className = "err";
  }
}

/* Step 2: suggestion-first flow — probe on wizard open, offer "Use it" */
async function probeClientDir() {
  try {
    const r = await api("/api/config/suggest-client-dir");
    const sug = $("#client-dir-suggestion");
    if (r.found) {
      $("#suggest-path").textContent = r.path;
      sug.dataset.path = r.path;
      sug.classList.remove("hidden");
      $("#client-dir-custom").classList.add("hidden");
      return true;
    }
    sug.classList.add("hidden");
    $("#client-dir-custom").classList.remove("hidden"); // no suggestion -> manual entry
    return false;
  } catch (e) {
    $("#client-dir-suggestion").classList.add("hidden");
    $("#client-dir-custom").classList.remove("hidden");
    return false;
  }
}

$("#btn-use-suggestion").onclick = () => {
  const sug = $("#client-dir-suggestion");
  $("#cfg-client-dir").value = sug.dataset.path || "";
  sug.classList.add("hidden");
  $("#client-dir-custom").classList.remove("hidden");
  $("#cfg-parse-result").textContent = "✔ Using the detected save folder on this PC.";
  $("#cfg-parse-result").className = "ok";
};

$("#btn-open-suggestion").onclick = async () => {
  const path = $("#client-dir-suggestion").dataset.path;
  if (!path) return;
  try {
    await api("/api/fs/open", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path }) });
  } catch (e) {
    alert("Could not open the folder: " + e.message);
  }
};

/* snapshots footer: reveal the local snapshot store */
$("#btn-open-store").onclick = async () => {
  try {
    const st = await api("/api/state");
    await api("/api/fs/open", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: st.store_root }),
    });
  } catch (e) {
    alert("Could not open the folder: " + e.message);
  }
};

/* ── snapshot deletion (danger zone) ─────────────────────── */
$("#btn-delete-last").onclick = async () => {
  try {
    const st = await api("/api/state");
    const list = st.snapshots[st.games[0]] || [];
    if (!list.length) return alert("No snapshots to delete.");
    if (list.length === 1) {
      if (!confirm("Delete the only snapshot (" + list[0].name + ")?\n\nThis is your entire local backup — no undo.")) return;
      if (!confirm("Really delete it? Make sure you have your saves somewhere else first.")) return;
    } else {
      const names = list.map((s) => s.name).join("\n");
      const pick = prompt(
        "Delete which snapshot? Type the exact name (no undo):\n\n" + names + "\n\n" +
        "Type ALL to delete every snapshot:"
      );
      if (!pick) return;
      if (pick.trim().toUpperCase() === "ALL") {
        if (!confirm("Delete ALL " + list.length + " snapshots? No undo.")) return;
        await api("/api/snapshots/delete-all", { method: "POST" });
        refresh();
        return;
      }
      if (!list.some((s) => s.name === pick.trim())) return alert("No snapshot with that name.");
      if (!confirm('Delete snapshot "' + pick.trim() + '"? No undo.')) return;
      await api("/api/snapshots/delete", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: pick.trim() }),
      });
    }
    refresh();
  } catch (e) {
    alert("Delete failed: " + e.message);
  }
};

$("#btn-cleanup-all").onclick = async () => {
  try {
    const st = await api("/api/state");
    const list = st.snapshots[st.games[0]] || [];
    const n = list.length;
    const totalBytes = list.reduce((a, s) => a + (s.bytes || 0), 0);
    if (!confirm(
      "Delete ALL " + n + " local snapshot(s) (" + fmtBytes(totalBytes) + ")?\n\n" +
      "This wipes your entire local backup of the GPORTAL server saves.\n" +
      "Nothing on the GPORTAL server or in your game is touched — but if your saves exist " +
      "NOWHERE else, they will be unrecoverable."
    )) return;
    if (!confirm("Last check: are you sure you have your saves backed up somewhere else?")) return;
    const r = await api("/api/snapshots/delete-all", { method: "POST" });
    $("#cleanup-result").textContent = `✔ Deleted ${r.deleted_count} snapshot(s). The store will be rebuilt on the next Backup.`;
    refresh();
  } catch (e) {
    $("#cleanup-result").textContent = "✘ " + e.message;
  }
};

$("#btn-use-custom").onclick = () => {
  $("#client-dir-suggestion").classList.add("hidden");
  $("#client-dir-custom").classList.remove("hidden");
};

$("#btn-cfg-suggest").onclick = () => probeClientDir();

/* ── native folder picker (real OS dialog, e.g. Dolphin) ─── */
async function nativePick(startPath) {
  // blocks until the user closes the OS dialog — busy state so it's obvious
  const btn = $("#btn-browse-client-dir");
  setBusy(btn, "⏳ Pick a folder in the dialog…", 3600000); // user-paced
  try {
    const r = await api("/api/fs/pick", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ start: startPath }),
    });
    if (r.ok && r.path) {
      $("#cfg-client-dir").value = r.path;
      $("#cfg-parse-result").textContent = "✔ Save folder selected via system dialog.";
      $("#cfg-parse-result").className = "ok";
    } else if (r.cancelled) {
      $("#cfg-parse-result").textContent = "Folder selection cancelled.";
      $("#cfg-parse-result").className = "muted";
    } else {
      $("#cfg-parse-result").textContent = "✘ " + (r.error || "Picker failed.");
      $("#cfg-parse-result").className = "err";
    }
  } catch (e) {
    $("#cfg-parse-result").textContent = "✘ " + e.message;
    $("#cfg-parse-result").className = "err";
  } finally {
    clearBusy(btn);
  }
}

$("#btn-browse-client-dir").onclick = () =>
  nativePick($("#cfg-client-dir").value.trim() || (browserState.suggestion ?? ""));

const browserState = { cwd: "", suggestion: null };

/* ── FTP folder override (server save folder) ───────────── */
function updateFtpRootNote() {
  const v = $("#cfg-ftp-root").value.trim();
  $("#ftp-root-note").textContent = v
    ? `Override active: save paths resolve under "${v}" on the server. Test connection to verify.`
    : "The game's save folder on the server is found automatically. Only set this if auto-detection fails (unusual hosting layout).";
  $("#btn-ftp-root-clear").classList.toggle("hidden", !v);
}

$("#cfg-ftp-root").addEventListener("input", updateFtpRootNote);
$("#btn-ftp-root-clear").onclick = () => {
  $("#cfg-ftp-root").value = "";
  updateFtpRootNote();
};

const ftpBrowser = { cwd: "/" };

async function ftpList(path) {
  const r = await api("/api/ftp/list", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path }),
  });
  if (!r.ok) throw new Error(r.error || "FTP listing failed");
  return r;
}

function renderFtpBrowser(r) {
  ftpBrowser.cwd = r.path || "/";
  $("#ftp-browser-path").textContent = ftpBrowser.cwd;
  const list = $("#ftp-browser-list");
  list.innerHTML = "";
  for (const name of r.entries) {
    const el = document.createElement("button");
    el.className = "browser-row";
    el.innerHTML = `<span class="bicon">📁</span><span class="bname"></span>`;
    el.querySelector(".bname").textContent = name;
    const child = ftpBrowser.cwd === "/" ? "/" + name : ftpBrowser.cwd + "/" + name;
    el.onclick = () => openFtpBrowserAt(child);
    list.appendChild(el);
  }
  if (!r.entries.length) {
    const d = document.createElement("div");
    d.className = "muted";
    d.style.padding = "8px";
    d.textContent = `No subfolders${r.files ? ` — ${r.files} file(s) here` : ""}`;
    list.appendChild(d);
  }
  $("#btn-ftp-browser-up").disabled = !r.parent;
  $("#ftp-browser-hint").textContent = r.game_root_here
    ? `✔ This folder contains the game: ${r.game_root_here}`
    : "";
}

async function openFtpBrowserAt(path) {
  const list = $("#ftp-browser-list");
  list.innerHTML = `<div class="muted" style="padding:8px">Loading…</div>`;
  try {
    renderFtpBrowser(await ftpList(path));
  } catch (e) {
    list.innerHTML = "";
    const d = document.createElement("div");
    d.className = "err";
    d.style.padding = "8px";
    d.textContent = "✘ " + e.message;
    list.appendChild(d);
  }
}

function openFtpBrowser() {
  $("#ftp-browser-backdrop").classList.remove("hidden");
  openFtpBrowserAt($("#cfg-ftp-root").value.trim() || "/");
}

function closeFtpBrowser() {
  $("#ftp-browser-backdrop").classList.add("hidden");
}

$("#btn-browse-ftp").onclick = openFtpBrowser;
$("#btn-ftp-browser-close").onclick = closeFtpBrowser;
$("#btn-ftp-browser-cancel").onclick = closeFtpBrowser;
$("#btn-ftp-browser-up").onclick = () => {
  if (ftpBrowser.cwd && ftpBrowser.cwd !== "/") {
    openFtpBrowserAt(ftpBrowser.cwd.replace(/\/[^/]+\/?$/, "") || "/");
  }
};
$("#btn-ftp-browser-pick").onclick = () => {
  $("#cfg-ftp-root").value = ftpBrowser.cwd === "/" ? "" : ftpBrowser.cwd;
  closeFtpBrowser();
  updateFtpRootNote();
};
$("#ftp-browser-backdrop").addEventListener("click", (e) => {
  if (e.target === $("#ftp-browser-backdrop")) closeFtpBrowser();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !$("#ftp-browser-backdrop").classList.contains("hidden")) closeFtpBrowser();
});

async function testConfig(payload, resultEl) {
  resultEl.textContent = "Testing…";
  resultEl.className = "test-result";
  try {
    const r = await api("/api/config/test", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!r.connected) {
      resultEl.textContent = "✘ " + (r.error || "Connection failed");
      resultEl.className = "test-result err";
      return null;
    }
    if (!r.game) {
      resultEl.textContent = "⚠ Connected, but no known game detected at the FTP root. Check the path with your host.";
      resultEl.className = "test-result warn";
      return r;
    }
    const crit = (r.probes || []).filter((p) => p.critical);
    const ok = crit.every((p) => p.found);
    resultEl.innerHTML = `✔ Connected — ${r.title} detected.<br>` +
      (r.probes || []).map((p) =>
        `${p.found ? "✔" : (p.critical ? "✘" : "⚠")} ${p.role}: ${p.found ? p.files + " files, " + fmtBytes(p.bytes) : "not found"}`
      ).join("<br>") +
      (ok ? "" : "<br>— critical path missing, check with host support.");
    resultEl.className = ok ? "test-result ok" : "test-result warn";
    return r;
  } catch (e) {
    resultEl.textContent = "✘ " + e.message;
    resultEl.className = "test-result err";
    return null;
  }
}

$("#btn-cfg-test").onclick = withBusy($("#btn-cfg-test"), async () => {
  const payload = readWizard();
  // settings mode with stored password: blank fields are fine — backend
  // merges host/user/password with the stored config when blanks are sent
  const editingStored = setupMode === "settings-open" && configState.has_password;
  if (editingStored) {
    if (!payload.host) payload.host = configState.host || "";
    if (!payload.user) payload.user = configState.user || "";
    // blank password => backend substitutes the stored one
  } else if (!payload.host || !payload.user || !payload.password) {
    $("#cfg-test-result").textContent = "Fill in host, user and password first (or paste the FTP line).";
    $("#cfg-test-result").className = "test-result warn";
    return;
  }
  await testConfig(payload, $("#cfg-test-result"));
}, "⏳ Testing connection…", 50000);

$("#btn-cfg-save").onclick = withBusy($("#btn-cfg-save"), async () => {
  const payload = readWizard();
  if (!payload.host || !payload.user) {
    $("#cfg-save-result").textContent = "Host and user are required.";
    return;
  }
  const editingStored = setupMode === "settings-open" && configState.has_password;
  if (!payload.password && !editingStored) {
    $("#cfg-save-result").textContent = "Password is required (or leave blank only when a password is already stored).";
    return;
  }
  try {
    await api("/api/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    $("#cfg-save-result").textContent = "Saved.";
    $("#cfg-pass").value = "";
    $("#cfg-pass").placeholder = "stored — leave blank to keep";
    if (setupMode === "settings-open") {
      setupMode = "closed";
      $("#setup-backdrop").classList.add("hidden");
    }
    refresh();
  } catch (e) {
    $("#cfg-save-result").textContent = "✘ " + e.message;
  }
}, "⏳ Saving…");

function setSetupMode(mode) {
  // mode: "first-run" | "settings"
  setupMode = mode === "settings" ? "settings-open" : "first-run";
  const isSettings = mode === "settings";
  // NOTE: does not touch #setup-card visibility — renderState enforces it
  // from setupMode on its next poll (~1.2s), and the button handlers below
  // flip it immediately for responsiveness
  $("#setup-title").textContent = isSettings ? "⚙ Settings" : "One-time setup";
  $("#setup-subtitle").textContent = isSettings
    ? "Same wizard as first run — edit anything and Save. Blank password keeps the stored one."
    : "Takes ~2 minutes. Credentials go into your system keyring — never into a file in this project.";
  $("#setup-stale-note").classList.toggle("hidden", isSettings);
  $("#danger-zone").classList.toggle("hidden", !isSettings); // cleanup only makes sense once configured
  $("#btn-setup-close").classList.toggle("hidden", !isSettings);
  $("#btn-cfg-save").textContent = isSettings ? "Save settings" : "Save configuration";
  if (isSettings && configState) {
    // settings mode pre-fills from stored config
    $("#cfg-host").value = configState.host || "";
    $("#cfg-port").value = configState.port || 21;
    $("#cfg-user").value = configState.user || "";
    $("#cfg-client-dir").value = configState.client_dir || "";
    $("#cfg-ftp-root").value = configState.ftp_root || "";
    updateFtpRootNote();
    $("#cfg-pass").value = "";
    $("#cfg-pass").placeholder = configState.has_password
      ? "stored — leave blank to keep" : "FTP password";
    // if a PC save folder is already stored, show manual view; else probe
    if (configState.client_dir) {
      $("#client-dir-suggestion").classList.add("hidden");
      $("#client-dir-custom").classList.remove("hidden");
    } else {
      probeClientDir();
    }
  } else {
    probeClientDir(); // first-run: suggestion-first
  }
}

$("#btn-settings").onclick = () => {
  setSetupMode("settings");
  $("#setup-backdrop").classList.remove("hidden");
};
$("#btn-setup-close").onclick = () => {
  setupMode = "closed";
  $("#setup-backdrop").classList.add("hidden");
};
// first-run modal is persistent: no backdrop-click dismissal (nothing
// configured yet — the app behind it is unusable anyway). Settings mode
// dismisses on backdrop click.
$("#setup-backdrop").addEventListener("click", (e) => {
  if (e.target === $("#setup-backdrop") && setupMode === "settings-open") {
    setupMode = "closed";
    $("#setup-backdrop").classList.add("hidden");
  }
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !$("#setup-backdrop").classList.contains("hidden")
      && setupMode === "settings-open") {
    setupMode = "closed";
    $("#setup-backdrop").classList.add("hidden");
  }
});

/* ── main actions ────────────────────────────────────────── */
$("#btn-status").onclick = withBusy($("#btn-status"), () => doAction("/api/status"), "⏳ Checking…", 60000);
$("#btn-pull").onclick = withBusy($("#btn-pull"), () => doAction("/api/pull"), "⏳ Job started — see log…");
$("#btn-verify").onclick = withBusy($("#btn-verify"), () => doAction("/api/verify"), "⏳ Verifying…");

/* ── restore flow ────────────────────────────────────────── */
document.querySelectorAll('input[name="restore-src"]').forEach((r) =>
  r.addEventListener("change", () => {
    $("#restore-snapshot").classList.toggle("hidden",
      document.querySelector('input[name="restore-src"]:checked').value !== "pick");
  }));

$("#btn-restore").onclick = withBusy($("#btn-restore"), async () => {
  const snap = restoreSource();
  const target = restoreTarget();
  const snapLabel = snap ? `"${snap}"` : "the latest snapshot";
  const isServer = target === "server";

  if (!isServer && !configState.client_dir) {
    alert("Set your PC's Icarus save folder in ⚙ Settings first (button top-right).");
    return;
  }

  // confirmation dialogue — mandatory for both restore targets
  const ok = confirm(
    `Restore ${snapLabel} → ${isServer ? "the GPORTAL server" : "your PC's Icarus save folder"}?\n\n` +
    (isServer
      ? "⚠ Make sure the server is STOPPED in the GPORTAL web UI first.\n" +
        "Existing save files on the server will be overwritten."
      : "⚠ Files in your PC's Icarus save folder will be overwritten.\n" +
        `Target: ${configState.client_dir}`)
  );
  if (!ok) return;

  setBusy($("#btn-restore"), "⏳ Job started — see log…");
  try {
    await doAction("/api/push", { snapshot: snap, target });
  } finally {
    setTimeout(() => clearBusy($("#btn-restore")), 3000); // job hands off to the log
  }
}, "⬆ Restore");

refresh();
