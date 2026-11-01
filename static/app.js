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
  $("#setup-card").classList.toggle("hidden", configured);
  $("#workflows-wrap").classList.toggle("hidden", !configured);
  $("#settings-card").classList.add("hidden"); // start collapsed

  if (configured) {
    if (!$("#set-host").value) {
      $("#set-host").value = st.host || "";
      $("#set-port").value = st.port || 21;
      $("#set-user").value = st.user || "";
      $("#set-client-dir").value = st.client_dir || "";
      $("#set-pass").placeholder = st.has_password ? "password stored — leave blank to keep" : "password";
    }
    $("#storage-note").textContent = storageNote(st.storage);
    renderSnapshots(st);
    renderJob(st);
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

/* ── setup wizard + settings ─────────────────────────────── */
function readWizard() {
  return {
    host: $("#cfg-host").value.trim(),
    port: parseInt($("#cfg-port").value || "21", 10),
    user: $("#cfg-user").value.trim(),
    password: $("#cfg-pass").value,
    client_dir: $("#cfg-client-dir").value.trim(),
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

$("#btn-cfg-suggest").onclick = async () => {
  try {
    const r = await api("/api/config/suggest-client-dir");
    if (r.found) {
      $("#cfg-client-dir").value = r.path;
      $("#cfg-parse-result").textContent = r.why;
      $("#cfg-parse-result").className = "ok";
    } else {
      $("#cfg-parse-result").textContent = "No Icarus client folder found — enter the path manually.";
      $("#cfg-parse-result").className = "warn";
    }
  } catch (e) {
    $("#cfg-parse-result").textContent = "✘ " + e.message;
  }
};

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
  if (!payload.host || !payload.user || !payload.password) {
    $("#cfg-test-result").textContent = "Fill in host, user and password first (or paste the FTP line).";
    $("#cfg-test-result").className = "test-result warn";
    return;
  }
  await testConfig(payload, $("#cfg-test-result"));
}, "⏳ Testing connection…", 50000);

$("#btn-cfg-save").onclick = withBusy($("#btn-cfg-save"), async () => {
  const payload = readWizard();
  if (!payload.host || !payload.user || !payload.password) {
    $("#cfg-save-result").textContent = "Host, user and password are required.";
    return;
  }
  try {
    await api("/api/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    $("#cfg-save-result").textContent = "Saved.";
    refresh();
  } catch (e) {
    $("#cfg-save-result").textContent = "✘ " + e.message;
  }
}, "⏳ Saving…");

$("#btn-settings").onclick = () => {
  $("#settings-card").classList.toggle("hidden");
};

$("#btn-set-test").onclick = withBusy($("#btn-set-test"), async () => {
  const payload = {
    host: $("#set-host").value.trim(),
    port: parseInt($("#set-port").value || "21", 10),
    user: $("#set-user").value.trim(),
    password: $("#set-pass").value,
  };
  if (!payload.password && configState.has_password) payload.password = "•••stored•••";
  await testConfig(payload, $("#set-result"));
}, "⏳ Testing connection…", 50000);

$("#btn-set-save").onclick = withBusy($("#btn-set-save"), async () => {
  try {
    await api("/api/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        host: $("#set-host").value.trim(),
        port: parseInt($("#set-port").value || "21", 10),
        user: $("#set-user").value.trim(),
        password: $("#set-pass").value,
        client_dir: $("#set-client-dir").value.trim(),
      }),
    });
    $("#set-result").textContent = "Saved.";
    $("#set-pass").value = "";
    refresh();
  } catch (e) {
    $("#set-result").textContent = "✘ " + e.message;
  }
}, "⏳ Saving…");
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
    alert("Set the client save folder in ⚙ Settings first.");
    return;
  }

  // confirmation dialogue — mandatory for both restore targets
  const ok = confirm(
    `Restore ${snapLabel} → ${isServer ? "the GPORTAL server" : "your local client folder"}?\n\n` +
    (isServer
      ? "⚠ Make sure the server is STOPPED in the GPORTAL web UI first.\n" +
        "Existing save files on the server will be overwritten."
      : "⚠ Files in your client Saved folder will be overwritten.\n" +
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
