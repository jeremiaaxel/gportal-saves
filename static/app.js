const $ = (sel) => document.querySelector(sel);
let selected = null;
let pollTimer = null;
let configState = { configured: false };

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
  $("#status-card").classList.toggle("hidden", !configured);
  $("#snapshots-card").classList.toggle("hidden", !configured);
  $("#restore-card").classList.toggle("hidden", !configured);
  $("#settings-card").classList.add("hidden"); // start collapsed

  if (configured) {
    if (!$("#set-host").value) {
      $("#set-host").value = st.host || "";
      $("#set-port").value = st.port || 21;
      $("#set-user").value = st.user || "";
      $("#set-client-dir").value = st.client_dir || "";
      $("#set-pass").placeholder = st.has_password ? "password stored — leave blank to keep" : "password";
    }
    renderSnapshots(st);
    renderJob(st);
  }
}

function renderSnapshots(st) {
  const tbody = $("#snap-table tbody");
  tbody.innerHTML = "";
  const all = Object.values(st.snapshots).flat();
  if (!all.length) {
    tbody.innerHTML = `<tr><td colspan="4" class="muted">No snapshots yet — hit "⬇ Backup now".</td></tr>`;
  }
  for (const gid of st.games) {
    for (const s of st.snapshots[gid] || []) {
      const tr = document.createElement("tr");
      tr.className = selected === s.name ? "selected" : "";
      tr.innerHTML = `<td>${s.name}</td><td>—</td><td>—</td><td>
        <button class="btn tiny" data-sel="${s.name}">${selected === s.name ? "✓ selected" : "select"}</button></td>`;
      tbody.appendChild(tr);
    }
  }
  $("#store-path").textContent = "Local store: " + st.store_root;
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

function selectSnapshot(name) {
  selected = name;
  document.querySelectorAll("#snap-table tbody tr").forEach((tr) => {
    const btn = tr.querySelector("[data-sel]");
    if (!btn) return;
    const isSel = btn.getAttribute("data-sel") === name;
    tr.classList.toggle("selected", isSel);
    btn.textContent = isSel ? "✓ selected" : "select";
  });
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

async function testConfig(payload, resultEl) {
  resultEl.textContent = "Testing…";
  resultEl.className = "muted";
  try {
    const r = await api("/api/config/test", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!r.connected) {
      resultEl.textContent = "✘ " + (r.error || "Connection failed");
      resultEl.className = "err";
      return null;
    }
    if (!r.game) {
      resultEl.textContent = "⚠ Connected, but no known game detected at the FTP root. Check the path with your host.";
      resultEl.className = "warn";
      return r;
    }
    const crit = (r.probes || []).filter((p) => p.critical);
    const ok = crit.every((p) => p.found);
    resultEl.innerHTML = `✔ Connected — ${r.title} detected. ` +
      (r.probes || []).map((p) =>
        `${p.found ? "✔" : (p.critical ? "✘" : "⚠")} ${p.role}: ${p.found ? p.files + " files, " + fmtBytes(p.bytes) : "not found"}`
      ).join(" · ") +
      (ok ? "" : " — critical path missing, check with host support.");
    resultEl.className = ok ? "ok" : "warn";
    return r;
  } catch (e) {
    resultEl.textContent = "✘ " + e.message;
    resultEl.className = "err";
    return null;
  }
}

$("#btn-cfg-test").onclick = async () => {
  const payload = readWizard();
  if (!payload.host || !payload.user) {
    $("#cfg-test-result").textContent = "Fill in host, user and password first.";
    return;
  }
  const r = await testConfig(payload, $("#cfg-test-result"));
  if (r && r.game) $("#ws1-check").textContent = "✔";
};

$("#btn-cfg-save").onclick = async () => {
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
    selected = null;
    refresh();
  } catch (e) {
    $("#cfg-save-result").textContent = "✘ " + e.message;
  }
};

$("#btn-settings").onclick = () => {
  $("#settings-card").classList.toggle("hidden");
};

$("#btn-set-test").onclick = () => {
  const payload = {
    host: $("#set-host").value.trim(),
    port: parseInt($("#set-port").value || "21", 10),
    user: $("#set-user").value.trim(),
    password: $("#set-pass").value,
  };
  if (!payload.password && configState.has_password) payload.password = "•••stored•••";
  testConfig(payload, $("#set-result"));
};

$("#btn-set-save").onclick = async () => {
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
};

/* ── main actions ────────────────────────────────────────── */
$("#btn-status").onclick = () => doAction("/api/status");
$("#btn-pull").onclick = () => doAction("/api/pull");
$("#btn-verify").onclick = () => doAction("/api/verify");
$("#btn-push-server").onclick = () => {
  if (!selected) return alert("Select a snapshot first.");
  if (!confirm(`Restore "${selected}" to the GPORTAL server?\nMake sure the server is STOPPED in the GPORTAL web UI first.`)) return;
  doAction("/api/push", { snapshot: selected, target: "server" });
};
$("#btn-push-client").onclick = () => {
  if (!selected) return alert("Select a snapshot first.");
  if (!configState.client_dir) {
    return alert("Set the client Saved folder in ⚙ Settings first (Step 2 of setup).");
  }
  doAction("/api/push", { snapshot: selected, target: "client" });
};

document.addEventListener("click", (e) => {
  const sel = e.target.getAttribute && e.target.getAttribute("data-sel");
  if (sel) selectSnapshot(sel);
});

refresh();
