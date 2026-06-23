// sintez-cam-server: vanilla JS frontend.
// REST for profiles/sessions/cameras; WebSocket for the live frame stream.

const $ = (id) => document.getElementById(id);

const ui = {
    tabs: document.querySelectorAll(".tab"),
    localPanel: $("local-panel"),
    remotePanel: $("remote-panel"),
    cameraSelect: $("local-camera"),
    refreshCameras: $("refresh-cameras"),
    // SSH profiles list + form
    sshProfilesList: $("ssh-profiles-list"),
    sshAddConnection: $("ssh-add-connection"),
    sshFormPanel: $("ssh-form-panel"),
    sshProfileName: $("ssh-profile-name"),
    sshSaveProfile: $("ssh-save-profile"),
    sshCancelForm: $("ssh-cancel-form"),
    // SSH credentials (inside form)
    sshHost: $("ssh-host"),
    sshPort: $("ssh-port"),
    sshUser: $("ssh-user"),
    sshPass: $("ssh-pass"),
    agentPort: $("agent-port"),
    sshServerUrl: $("ssh-server-url"),
    // Single connect/disconnect toggle
    sshToggleConn: $("ssh-toggle-conn"),
    // Post-connect panels
    sshConnectedPanel: $("ssh-connected-panel"),
    sshConnLabel: $("ssh-conn-label"),
    agentNotInstalledPanel: $("agent-not-installed-panel"),
    agentInstalledPanel: $("agent-installed-panel"),
    agentEnablingPanel: $("agent-enabling-panel"),
    agentRunningPanel: $("agent-running-panel"),
    installAgentBtn: $("install-agent-btn"),
    installLog: $("install-log"),
    agentInstalledPill: $("agent-installed-pill"),
    agentVersionPill: $("agent-version-pill"),
    agentUpdateBanner: $("agent-update-banner"),
    agentLatestVersion: $("agent-latest-version"),
    reinstallAgentBtn: $("reinstall-agent-btn"),
    removeAgentBtn: $("remove-agent-btn"),
    manageLog: $("manage-log"),
    agentLog: $("agent-log"),
    enableAgentBtn: $("enable-agent-btn"),
    reEnableAgentBtn: $("re-enable-agent-btn"),
    disableAgentBtn: $("disable-agent-btn"),
    refreshRemoteCameras: $("refresh-remote-cameras"),
    remoteCameraSelect: $("remote-camera-select"),
    // Manual token fallback
    remoteToken: $("remote-token"),
    issueToken: $("issue-token"),
    agentCmd: $("agent-cmd"),
    boardW: $("board-w"),
    boardH: $("board-h"),
    squareSize: $("square-size"),
    requiredCaptures: $("required-captures"),
    sessionName: $("session-name"),
    saveProfile: $("save-profile"),
    deleteProfile: $("delete-profile"),
    profileSelect: $("profile-select"),
    startSession: $("start-session"),
    captureNow: $("capture-now"),
    abortSession: $("abort-session"),
    finishSession: $("finish-session"),
    canvas: $("preview"),
    statePill: $("state-pill"),
    capturesPill: $("captures-pill"),
    boardPill: $("board-pill"),
    blurPill: $("blur-pill"),
    connPill: $("conn-pill"),
    hint: $("hint"),
    result: $("result"),
};

const state = {
    tab: "local",
    cameras: [],
    session: null,
    socket: null,
    canvasCtx: ui.canvas.getContext("2d"),
    // Remote SSH state
    sshConnected: false,
    remoteAgentId: null,
    remoteCameras: [],
    remotePollTimer: null,
    agentLogSource: null,
    agentCheck: null,   // last {installed, version, latest, needs_update}
    _editingProfileName: null,
};

// ---------- Tab switching ----------

ui.tabs.forEach((btn) => {
    btn.addEventListener("click", () => {
        ui.tabs.forEach((b) => b.classList.toggle("active", b === btn));
        state.tab = btn.dataset.tab;
        ui.localPanel.hidden = state.tab !== "local";
        ui.remotePanel.hidden = state.tab !== "remote";
    });
});

// ---------- Status pills ----------

function setState(s) {
    ui.statePill.textContent = s;
    ui.statePill.className = "pill " + (
        s === "running" ? "ok" :
        s === "finished" ? "ok" :
        s === "failed" ? "bad" : "warn"
    );
    updateButtons();
}

function setConn(s) {
    ui.connPill.textContent = s;
    ui.connPill.className = "pill " + (s === "connected" ? "ok" : s === "connecting" ? "warn" : "bad");
    updateButtons();
}

// ---------- Button state management ----------

function updateButtons() {
    const running = state.session?.state === "running";
    const connected = state.socket !== null;
    const captures = state.session?.captures ?? 0;

    ui.startSession.disabled = running;
    ui.captureNow.disabled = !running || !connected;
    ui.abortSession.disabled = !state.session;
    ui.finishSession.disabled = !state.session || captures < 3;
}

updateButtons();

// ---------- REST helper ----------

async function api(path, options = {}) {
    const res = await fetch(path, {
        headers: { "Content-Type": "application/json" },
        ...options,
    });
    if (!res.ok) {
        const txt = await res.text();
        throw new Error(`${res.status}: ${txt}`);
    }
    if (res.status === 204) return null;
    return res.json();
}

// ---------- Cameras ----------

async function refreshCameras() {
    try {
        const cams = await api("/cameras");
        state.cameras = cams;
        ui.cameraSelect.innerHTML = "";
        cams.forEach((c) => {
            const opt = document.createElement("option");
            opt.value = c.id;
            opt.textContent = c.label;
            ui.cameraSelect.appendChild(opt);
        });
    } catch (err) {
        ui.hint.textContent = "Failed to list cameras: " + err.message;
    }
}

ui.refreshCameras.addEventListener("click", refreshCameras);
refreshCameras();

// ---------- Calibration flags from checkboxes ----------

function getCalibFlags() {
    let flags = 0;
    document.querySelectorAll(".calib-flag").forEach((cb) => {
        if (cb.checked) flags |= parseInt(cb.value, 10);
    });
    return flags;
}

// ---------- Profiles ----------

async function loadProfiles() {
    try {
        const profiles = await api("/profiles");
        ui.profileSelect.innerHTML = "";
        const blank = document.createElement("option");
        blank.value = ""; blank.textContent = "(saved profiles)";
        ui.profileSelect.appendChild(blank);
        profiles.forEach((p) => {
            const opt = document.createElement("option");
            opt.value = p.name;
            opt.textContent = `${p.name}  ${p.inner_corners_x}×${p.inner_corners_y}  ${p.square_size_mm}mm  N=${p.required_captures}`;
            ui.profileSelect.appendChild(opt);
        });
    } catch (_) {}
}

// Populate profile fields when the user selects one
ui.profileSelect.addEventListener("change", async () => {
    if (!ui.profileSelect.value) return;
    try {
        const p = await api(`/profiles/${encodeURIComponent(ui.profileSelect.value)}`);
        ui.boardW.value = p.inner_corners_x;
        ui.boardH.value = p.inner_corners_y;
        ui.squareSize.value = p.square_size_mm;
        ui.requiredCaptures.value = p.required_captures;
        ui.sessionName.value = p.name;
        // Restore calibration flag checkboxes
        document.querySelectorAll(".calib-flag").forEach((cb) => {
            cb.checked = (p.flags & parseInt(cb.value, 10)) !== 0;
        });
    } catch (err) {
        ui.hint.textContent = "Load profile failed: " + err.message;
    }
});

ui.saveProfile.addEventListener("click", async () => {
    if (!ui.sessionName.value.trim()) { ui.hint.textContent = "Enter a profile name first."; return; }
    const profile = currentProfile();
    try {
        await api("/profiles/save", { method: "POST", body: JSON.stringify(profile) });
        ui.sessionName.value = profile.name;   // reflect sanitized name back
        ui.hint.textContent = `Profile "${profile.name}" saved.`;
        loadProfiles();
    } catch (err) {
        ui.hint.textContent = "Save failed: " + err.message;
    }
});

ui.deleteProfile.addEventListener("click", async () => {
    const name = ui.profileSelect.value || ui.sessionName.value.trim();
    if (!name) { ui.hint.textContent = "Select a profile to delete."; return; }
    if (!confirm(`Delete profile "${name}"?`)) return;
    try {
        await api(`/profiles/${encodeURIComponent(name)}`, { method: "DELETE" });
        ui.hint.textContent = `Profile "${name}" deleted.`;
        loadProfiles();
    } catch (err) {
        ui.hint.textContent = "Delete failed: " + err.message;
    }
});

// Auto-load on page start
loadProfiles();

// ---------- Sessions ----------

function sanitizeName(raw) {
    return (raw || "").trim().replace(/[^A-Za-z0-9_.\-]+/g, "_").replace(/^_+|_+$/g, "") || "default";
}

function currentProfile() {
    return {
        name: sanitizeName(ui.sessionName.value),
        inner_corners_x: parseInt(ui.boardW.value, 10),
        inner_corners_y: parseInt(ui.boardH.value, 10),
        square_size_mm: parseFloat(ui.squareSize.value),
        flags: getCalibFlags(),
        required_captures: Math.max(3, Math.min(200, parseInt(ui.requiredCaptures.value, 10) || 20)),
    };
}

// Combined create + start: one button does it all.
ui.startSession.addEventListener("click", async () => {
    if (state.session?.state === "running") return;

    if (state.tab === "remote") {
        await _remoteStartCapture();
        return;
    }

    const profile = currentProfile();
    const body = {
        name: profile.name,
        source: "local",
        camera_id: ui.cameraSelect.value,
        profile,
    };
    let info;
    try {
        info = await api("/sessions", { method: "POST", body: JSON.stringify(body) });
    } catch (err) {
        ui.hint.textContent = "Failed to create session: " + err.message;
        return;
    }
    state.session = info;
    try {
        info = await api(`/sessions/${state.session.id}/start`, { method: "POST" });
        state.session = info;
        setState(info.state);
        ui.capturesPill.textContent = `captures: 0 / ${info.required_captures}`;
        ui.hint.textContent = `Session started — need ${info.required_captures} captures. Hold the chessboard in view.`;
        connectStream();
        loadSessionList();
    } catch (err) {
        ui.hint.textContent = "Start failed: " + err.message;
    }
});

// Manual force-capture
ui.captureNow.addEventListener("click", () => {
    if (!state.socket) return;
    try { state.socket.send(JSON.stringify({ type: "capture_now" })); } catch (_) {}
    ui.hint.textContent = "Force capture requested…";
});

ui.abortSession.addEventListener("click", async () => {
    if (!state.session) return;
    if (state.socket) {
        try { state.socket.send(JSON.stringify({ type: "abort" })); } catch (_) {}
    }
    _stopCameraPoll();
    try {
        const info = await api(`/sessions/${state.session.id}/abort`, { method: "POST" });
        state.session = info;
        setState(info.state);
        disconnectStream();
        ui.hint.textContent = "Session aborted.";
        loadSessionList();
    } catch (err) {
        ui.hint.textContent = "Abort failed: " + err.message;
    }
});

ui.finishSession.addEventListener("click", async () => {
    if (!state.session) return;
    _stopCameraPoll();
    disconnectStream();
    try {
        const result = await api(`/calibrate/${state.session.id}`, { method: "POST" });
        showResult(result);
        state.session = { ...state.session, state: "finished" };
        setState("finished");
        loadSessionList();
    } catch (err) {
        ui.hint.textContent = "Calibration failed: " + err.message;
        setState("failed");
    }
});

function showResult(r) {
    const sid = state.session?.id ?? "";
    ui.result.hidden = false;
    ui.result.innerHTML = `
        <strong>Calibration complete.</strong><br/>
        RMS: <code>${r.rms.toFixed(4)}</code> &nbsp;
        Mean reprojection error: <code>${r.reprojection_error.toFixed(4)} px</code><br/>
        Image size: <code>${r.image_size.join(" × ")} px</code><br/>
        <span style="color:var(--muted);font-size:11px">
            Files saved: result.npz, result.yaml, meta.json
        </span>
        ${sid ? `<br/><button class="secondary small" style="margin-top:8px" onclick="openSessionDir('${sid}')">Open result directory</button>` : ""}
    `;
}

// ---------- Stream ----------

function disconnectStream() {
    if (state.socket) {
        // WebSocket vs EventSource -- both have .close()
        try { state.socket.send(JSON.stringify({ type: "stop" })); } catch (_) {}
        try { state.socket.close(); } catch (_) {}
        state.socket = null;
    }
    setConn("disconnected");
}

function connectStream() {
    disconnectStream();
    if (!state.session) return;
    const sid = state.session.id;
    setConn("connecting");

    if (state.tab === "local") {
        // Local camera: still a raw WebSocket, frames come as binary JPEGs.
        const proto = location.protocol === "https:" ? "wss:" : "ws:";
        const url = `${proto}//${location.host}/ws/local/${sid}`;
        const ws = new WebSocket(url);
        ws.binaryType = "arraybuffer";
        state.socket = ws;
        ws.onopen = () => { setConn("connected"); updateButtons(); };
        ws.onclose = () => { setConn("disconnected"); state.socket = null; updateButtons(); };
        ws.onerror = () => { setConn("disconnected"); };
        ws.onmessage = (ev) => {
            if (typeof ev.data === "string") {
                try { handleEvent(JSON.parse(ev.data)); } catch (_) {}
            } else {
                drawFrame(ev.data);
            }
        };
    } else {
        // Remote camera: SSE proxy of the agent's frame stream.
        // Frames are base64-encoded JPEGs sent as JSON messages.
        const es = new EventSource(`/remote/stream/${sid}`);
        state.socket = es;
        es.onopen = () => { setConn("connected"); updateButtons(); };
        es.onerror = () => { setConn("disconnected"); };
        es.onmessage = (ev) => {
            try {
                const msg = JSON.parse(ev.data);
                if (msg.type === "frame") {
                    const bin = atob(msg.data);
                    const buf = new Uint8Array(bin.length);
                    for (let i = 0; i < bin.length; i++) buf[i] = bin.charCodeAt(i);
                    drawFrame(buf.buffer);
                } else {
                    handleEvent(msg);
                }
            } catch (_) {}
        };
    }
}

function handleEvent(msg) {
    if (msg.type === "capture") {
        const total = state.session?.required_captures ?? "—";
        ui.capturesPill.textContent = `captures: ${msg.n} / ${total}`;
        ui.capturesPill.className = "pill ok";
        if (state.session) {
            state.session.captures = msg.n;
            updateButtons();
        }
    } else if (msg.type === "status") {
        // Board detection + blur from pipeline (sent every ~10 frames)
        ui.boardPill.textContent = `board: ${msg.board ? "OK" : "NO"}`;
        ui.boardPill.className = "pill " + (msg.board ? "ok" : "bad");
        ui.blurPill.textContent = `blur: ${msg.blur}`;
        ui.blurPill.className = "pill " + (msg.blur >= 35 ? "ok" : "warn");
    } else if (msg.type === "hint") {
        ui.hint.textContent = msg.message;
    } else if (msg.type === "error") {
        ui.hint.textContent = "Error: " + msg.message;
        ui.hint.style.borderColor = "var(--danger)";
    }
}

function drawFrame(buf) {
    const blob = new Blob([buf], { type: "image/jpeg" });
    const url = URL.createObjectURL(blob);
    const img = new Image();
    img.onload = () => {
        if (ui.canvas.width !== img.width) ui.canvas.width = img.width;
        if (ui.canvas.height !== img.height) ui.canvas.height = img.height;
        state.canvasCtx.drawImage(img, 0, 0);
        URL.revokeObjectURL(url);
    };
    img.src = url;
}

// ---------- Sessions list ----------

async function loadSessionList() {
    try {
        const [sessions, health] = await Promise.all([
            api("/sessions"),
            api("/sessions/health").catch(() => ({})),
        ]);
        renderSessionList(sessions, health);
    } catch (_) {}
}

function renderSessionList(sessions, health = {}) {
    const el = $("sessions-list");
    if (!sessions || !sessions.length) {
        el.innerHTML = '<span class="session-empty">No sessions yet.</span>';
        return;
    }
    sessions.sort((a, b) => (b.created_at ?? b.id).localeCompare(a.created_at ?? a.id));
    el.innerHTML = sessions.map((s) => {
        const h = health[s.id] ?? {};
        const healthBadge = h.label
            ? `<span class="health-badge ${h.color ?? "muted"}" title="${escHtml(h.tip ?? "")}">${escHtml(h.label)}</span>`
            : "";
        const viewBtn = s.state === "finished"
            ? `<button class="secondary small" onclick="viewSession('${s.id}')">View</button>`
            : "";
        const p = s.profile ?? {};
        const paramsLine = p.inner_corners_x != null
            ? `<span class="session-params">${p.inner_corners_x} × ${p.inner_corners_y} corners &nbsp;·&nbsp; ${p.square_size_mm} mm &nbsp;·&nbsp; ${s.captures} / ${s.required_captures} captures</span>`
            : `<span class="session-params">${s.captures} / ${s.required_captures} captures</span>`;
        return `
        <div class="session-row" id="srow-${s.id}">
            <div class="session-row-top">
                <div class="session-info">
                    <span class="session-name" title="${s.id}">${escHtml(s.name)}</span>
                    <span class="session-badge ${s.state}">${s.state}</span>
                    ${healthBadge}
                </div>
                <div class="session-actions">
                    ${viewBtn}
                    <button class="secondary small" onclick="openSessionDir('${s.id}')">Open Dir</button>
                    <button class="danger small" onclick="confirmDeleteSession('${s.id}', '${escHtml(s.name)}')">Delete</button>
                </div>
            </div>
            ${paramsLine}
        </div>`;
    }).join("");
}

// ---------- Frame lightbox ----------

let _lbFrames = [];
let _lbSessionId = null;
let _lbIndex = 0;

function openLightbox(index) {
    _lbIndex = index;
    _lbRefresh();
    $("lightbox").hidden = false;
    document.addEventListener("keydown", _lbKey);
}

function closeLightbox() {
    $("lightbox").hidden = true;
    document.removeEventListener("keydown", _lbKey);
}

function lightboxNav(dir) {
    _lbIndex = (_lbIndex + dir + _lbFrames.length) % _lbFrames.length;
    _lbRefresh();
}

function _lbRefresh() {
    $("lb-img").src = `/session-data/${_lbSessionId}/frames/${_lbFrames[_lbIndex]}`;
    $("lb-counter").textContent = `${_lbIndex + 1} / ${_lbFrames.length}  ·  ${_lbFrames[_lbIndex]}`;
}

function _lbKey(e) {
    if (e.key === "Escape")      closeLightbox();
    else if (e.key === "ArrowLeft")  lightboxNav(-1);
    else if (e.key === "ArrowRight") lightboxNav(1);
}

// Close on backdrop click
$("lightbox").addEventListener("click", (e) => { if (e.target === $("lightbox")) closeLightbox(); });

// ---------- Session detail viewer ----------

let _activeIntrinsicsId = null;

const FLAG_NAMES = { 4096: "Zero tangential dist", 1024: "Fix aspect ratio", 16384: "Rational model" };

function decodeFlags(flags) {
    const n = parseInt(flags ?? 0, 10);
    if (n === 0) return "Default (none)";
    const active = Object.entries(FLAG_NAMES).filter(([v]) => n & parseInt(v)).map(([, name]) => name);
    return active.length ? active.join(", ") : `0x${n.toString(16)}`;
}

async function viewSession(sessionId) {
    if (_activeIntrinsicsId === sessionId) { closeDetail(); return; }
    _activeIntrinsicsId = sessionId;

    document.querySelectorAll(".session-row").forEach((r) => r.classList.remove("active"));
    const row = $(`srow-${sessionId}`);
    if (row) row.classList.add("active");

    const panel = $("detail-panel");
    $("detail-title").textContent = "Loading…";
    $("detail-body").innerHTML = "";
    $("copy-intrinsics").hidden = true;
    panel.hidden = false;
    panel.scrollIntoView({ behavior: "smooth", block: "nearest" });

    try {
        const detail = await api(`/sessions/${sessionId}/detail`);
        $("detail-title").textContent = `Session — ${detail.name}`;

        const p = detail.profile ?? {};
        const created = (detail.created_at ?? "").replace("T", "  ").slice(0, 19);

        // --- Board params section ---
        let html = `
        <div class="detail-section">
            <div class="detail-section-title">Chessboard &amp; capture settings</div>
            <div class="detail-kv">
                <span class="lbl">Inner corners</span>
                <span class="val">${p.inner_corners_x ?? "—"} × ${p.inner_corners_y ?? "—"}</span>
                <span class="lbl">Square size</span>
                <span class="val">${p.square_size_mm ?? "—"} mm</span>
                <span class="lbl">Captures</span>
                <span class="val">${detail.captures} / ${detail.required_captures}</span>
                <span class="lbl">Calib flags</span>
                <span class="val">${escHtml(decodeFlags(p.flags))}</span>
                <span class="lbl">Source</span>
                <span class="val">${detail.source}${detail.camera_id ? " · " + escHtml(detail.camera_id) : ""}</span>
                <span class="lbl">Created</span>
                <span class="val">${created || "—"}</span>
            </div>
        </div>`;

        // --- Captured frames grid ---
        if (detail.frames && detail.frames.length > 0) {
            // Store for lightbox access
            _lbFrames = detail.frames;
            _lbSessionId = sessionId;
            const thumbs = detail.frames.map((f, i) =>
                `<img class="frame-thumb" src="/session-data/${sessionId}/frames/${f}"
                      title="Frame ${i + 1} of ${detail.frames.length} — ${f}" loading="lazy"
                      onclick="openLightbox(${i})" />`
            ).join("");
            html += `
        <div class="detail-section">
            <div class="detail-section-title">Captured frames (${detail.frames.length})</div>
            <div class="frame-grid">${thumbs}</div>
        </div>`;
        }

        $("detail-body").innerHTML = html;

        // --- Intrinsics YAML (finished sessions only) ---
        if (detail.state === "finished") {
            try {
                const intr = await api(`/calibrate/${sessionId}/intrinsics`);
                $("detail-body").insertAdjacentHTML("beforeend", `
                <div class="detail-section">
                    <div class="detail-section-title">Intrinsics</div>
                    <pre class="yaml-block" id="intrinsics-yaml">${escHtml(intr.yaml)}</pre>
                </div>`);
                $("copy-intrinsics").hidden = false;
            } catch (_) {}
        }
    } catch (err) {
        $("detail-title").textContent = "Error";
        $("detail-body").innerHTML =
            `<div style="padding:12px;color:var(--danger)">Failed to load: ${escHtml(err.message)}</div>`;
    }
}

function closeDetail() {
    $("detail-panel").hidden = true;
    document.querySelectorAll(".session-row").forEach((r) => r.classList.remove("active"));
    _activeIntrinsicsId = null;
}

$("copy-intrinsics").addEventListener("click", () => {
    const yamlEl = $("intrinsics-yaml");
    if (!yamlEl) return;
    const text = yamlEl.textContent;
    const btn = $("copy-intrinsics");
    const flash = () => { btn.textContent = "Copied!"; setTimeout(() => { btn.textContent = "Copy YAML"; }, 1800); };
    navigator.clipboard.writeText(text).then(flash).catch(() => {
        const ta = document.createElement("textarea");
        ta.value = text; ta.style.cssText = "position:fixed;opacity:0";
        document.body.appendChild(ta); ta.select(); document.execCommand("copy");
        document.body.removeChild(ta); flash();
    });
});

function escHtml(s) {
    return s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

async function openSessionDir(id) {
    try {
        await api(`/sessions/${id}/open-dir`, { method: "POST" });
    } catch (err) {
        ui.hint.textContent = "Could not open directory: " + err.message;
    }
}

async function confirmDeleteSession(id, name) {
    if (!confirm(`Delete session "${name}" and all its data?`)) return;
    try {
        await api(`/sessions/${id}`, { method: "DELETE" });
        ui.hint.textContent = `Session "${name}" deleted.`;
        if (_activeIntrinsicsId === id) closeDetail();
        if (state.session?.id === id) {
            disconnectStream();
            state.session = null;
            ui.result.hidden = true;
            setState("idle");
            ui.capturesPill.textContent = "captures: 0 / —";
            ui.boardPill.textContent = "board: —";
            ui.boardPill.className = "pill";
            ui.blurPill.textContent = "blur: —";
            ui.blurPill.className = "pill";
        }
        loadSessionList();
    } catch (err) {
        ui.hint.textContent = "Delete failed: " + err.message;
    }
}

// Load session list on page start and keep it up to date
loadSessionList();

// ==========================================================================
// SSH Profile management
// ==========================================================================

async function loadSshProfiles() {
    let profiles = [];
    try { profiles = await api("/remote/ssh-profiles"); } catch (_) {}
    ui.sshProfilesList.innerHTML = "";
    if (!profiles.length) {
        ui.sshProfilesList.innerHTML =
            '<div style="font-size:12px;color:var(--muted);padding:4px 0">No saved connections</div>';
    }
    profiles.forEach((p) => {
        const row = document.createElement("div");
        row.className = "row";
        row.dataset.profileRow = p.name;
        row.style.cssText =
            "align-items:center;gap:4px;margin-bottom:4px;border:1px solid var(--border);border-radius:3px;padding:4px 6px";
        row.innerHTML = `
            <span style="flex:1;font-size:13px">
                <strong>${escHtml(p.name)}</strong>
                <span style="color:var(--muted);font-size:11px"> ${escHtml(p.username)}@${escHtml(p.host)}</span>
            </span>
            <button class="secondary small" data-profile-use="${escHtml(p.name)}" title="Select this connection">Use</button>
            <button class="secondary small" data-profile-edit="${escHtml(p.name)}">Edit</button>
            <button class="danger small" data-profile-del="${escHtml(p.name)}">Del</button>
        `;
        ui.sshProfilesList.appendChild(row);
    });

    ui.sshProfilesList.querySelectorAll("[data-profile-use]").forEach((btn) => {
        btn.addEventListener("click", async () => {
            const name = btn.dataset.profileUse;
            let all; try { all = await api("/remote/ssh-profiles"); } catch (_) { return; }
            const p = all.find((x) => x.name === name); if (!p) return;
            _fillForm(p);
            _highlightProfile(name);
            ui.sshToggleConn.disabled = false;
            ui.hint.textContent = `"${name}" selected — press Connect.`;
        });
    });

    ui.sshProfilesList.querySelectorAll("[data-profile-edit]").forEach((btn) => {
        btn.addEventListener("click", async () => {
            const name = btn.dataset.profileEdit;
            let all; try { all = await api("/remote/ssh-profiles"); } catch (_) { return; }
            const p = all.find((x) => x.name === name); if (!p) return;
            state._editingProfileName = name;
            _fillForm(p);
            _openForm();
        });
    });

    ui.sshProfilesList.querySelectorAll("[data-profile-del]").forEach((btn) => {
        btn.addEventListener("click", async () => {
            const name = btn.dataset.profileDel;
            if (!confirm(`Delete profile "${name}"?`)) return;
            try {
                await api(`/remote/ssh-profiles/${encodeURIComponent(name)}`, { method: "DELETE" });
                await loadSshProfiles();
                ui.hint.textContent = `Profile "${name}" deleted.`;
            } catch (err) {
                ui.hint.textContent = "Delete failed: " + err.message;
            }
        });
    });
}

function _fillForm(p) {
    ui.sshProfileName.value = p.name || "";
    ui.sshHost.value = p.host || "";
    ui.sshPort.value = p.port || 22;
    ui.sshUser.value = p.username || "";
    ui.sshPass.value = p.password || "";
    ui.agentPort.value = p.agent_port || 8765;
}

function _openForm() { ui.sshFormPanel.hidden = false; ui.sshProfileName.focus(); }
function _closeForm() { ui.sshFormPanel.hidden = true; state._editingProfileName = null; }

function _highlightProfile(name) {
    ui.sshProfilesList.querySelectorAll("[data-profile-row]").forEach((row) => {
        row.style.borderColor = row.dataset.profileRow === name ? "var(--accent, #4a90e2)" : "var(--border)";
    });
}

ui.sshAddConnection.addEventListener("click", () => {
    state._editingProfileName = null;
    _fillForm({ name: "", host: "", port: 22, username: "", password: "", agent_port: 8765 });
    _openForm();
});

ui.sshSaveProfile.addEventListener("click", async () => {
    const name = ui.sshProfileName.value.trim() || ui.sshHost.value.trim();
    if (!name) { ui.hint.textContent = "Enter a connection name to save."; return; }
    try {
        if (state._editingProfileName && state._editingProfileName !== name) {
            await api(`/remote/ssh-profiles/${encodeURIComponent(state._editingProfileName)}`, { method: "DELETE" });
        }
        await api("/remote/ssh-profiles", {
            method: "POST",
            body: JSON.stringify({
                name,
                host: ui.sshHost.value.trim(),
                port: parseInt(ui.sshPort.value, 10) || 22,
                username: ui.sshUser.value.trim(),
                password: ui.sshPass.value,
                agent_port: parseInt(ui.agentPort.value, 10) || 8765,
            }),
        });
        ui.hint.textContent = `Profile "${name}" saved.`;
        _closeForm();
        await loadSshProfiles();
        _highlightProfile(name);
        ui.sshToggleConn.disabled = false;
    } catch (err) {
        ui.hint.textContent = "Save failed: " + err.message;
    }
});

ui.sshCancelForm.addEventListener("click", _closeForm);

loadSshProfiles();

// ==========================================================================
// Remote panel state machine
// ==========================================================================

function _sshCreds() {
    return {
        host: ui.sshHost.value.trim(),
        port: parseInt(ui.sshPort.value, 10) || 22,
        username: ui.sshUser.value.trim(),
        password: ui.sshPass.value,
        agent_port: parseInt(ui.agentPort.value, 10) || 8765,
    };
}

function _showAgentPanel(which) {
    ["agent-not-installed-panel", "agent-installed-panel",
     "agent-enabling-panel", "agent-running-panel"].forEach((id) => {
        $(id).hidden = id !== which;
    });
}

function _resetRemoteState() {
    state.remoteAgentId = null;
    state.remoteCameras = [];
    _stopCameraPoll();
    _stopAgentLog();
    if (ui.agentLog) { ui.agentLog.textContent = ""; ui.agentLog.hidden = true; }
}

// ---------- SSH Connect / Disconnect toggle ----------

ui.sshToggleConn.addEventListener("click", async () => {
    if (state.sshConnected) { _sshDisconnect(); } else { await _sshConnect(); }
});

async function _sshConnect() {
    const { host, username } = _sshCreds();
    if (!host || !username) { ui.hint.textContent = "Enter host and username first."; return; }
    ui.sshToggleConn.disabled = true;
    ui.sshToggleConn.textContent = "Connecting …";
    ui.hint.textContent = "Connecting …";
    try {
        const result = await api("/remote/ssh-check", {
            method: "POST", body: JSON.stringify(_sshCreds()),
        });
        state.sshConnected = true;
        ui.sshToggleConn.textContent = "✕ Disconnect";
        ui.sshConnectedPanel.hidden = false;
        ui.sshConnLabel.textContent = `Connected to ${host}`;
        _renderAgentInstallState(result);
    } catch (err) {
        ui.hint.textContent = "Connection failed: " + err.message;
        ui.sshConnectedPanel.hidden = true;
        state.sshConnected = false;
        ui.sshToggleConn.textContent = "⇄ Connect";
    } finally {
        ui.sshToggleConn.disabled = false;
    }
}

function _sshDisconnect() {
    _resetRemoteState();
    state.sshConnected = false;
    ui.sshConnectedPanel.hidden = true;
    _showAgentPanel(null);
    ui.sshToggleConn.textContent = "⇄ Connect";
    ui.hint.textContent = "Disconnected.";
}

// ---------- Agent install-state rendering ----------

function _renderAgentInstallState(check) {
    state.agentCheck = check;
    if (!check.installed) {
        _showAgentPanel("agent-not-installed-panel");
        ui.hint.textContent = "Agent not installed. Install it to continue.";
        return;
    }
    // Installed: show version + optional update banner.
    const ver = check.version || "unknown";
    ui.agentVersionPill.hidden = false;
    ui.agentVersionPill.textContent = `v${ver}`;
    ui.agentVersionPill.className = "pill " + (check.needs_update ? "warn" : "ok");
    ui.agentUpdateBanner.hidden = !check.needs_update;
    if (check.needs_update) ui.agentLatestVersion.textContent = check.latest || "?";
    ui.reinstallAgentBtn.textContent = check.needs_update
        ? "↻ Update Agent" : "↻ Reinstall";
    _showAgentPanel("agent-installed-panel");
    ui.hint.textContent = check.needs_update
        ? `Agent v${ver} installed — a newer version (${check.latest}) is available. Update or Enable.`
        : "Agent is installed. Enable it to see available cameras.";
}

async function _recheckAgent() {
    try {
        const r = await api("/remote/ssh-check", {
            method: "POST", body: JSON.stringify(_sshCreds()),
        });
        _renderAgentInstallState(r);
    } catch (err) {
        ui.hint.textContent = "Re-check failed: " + err.message;
    }
}

// Run an SSE endpoint that streams {message} lines; resolves true if a
// "DONE:" line was seen.  Used by install / reinstall / remove.
async function _runSseJob(url, logEl) {
    logEl.hidden = false;
    logEl.textContent = "";
    let success = false;
    const append = (line) => {
        logEl.hidden = false;
        logEl.textContent += line + "\n";
        logEl.scrollTop = logEl.scrollHeight;
    };
    try {
        const resp = await fetch(url, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(_sshCreds()),
        });
        if (!resp.ok) { append("ERROR: " + (await resp.text())); return false; }
        const reader = resp.body.getReader();
        const dec = new TextDecoder();
        let buf = "";
        while (true) {
            const { done, value } = await reader.read();
            if (done) break;
            buf += dec.decode(value, { stream: true });
            const lines = buf.split("\n"); buf = lines.pop();
            for (const line of lines) {
                if (!line.startsWith("data: ")) continue;
                try {
                    const evt = JSON.parse(line.slice(6));
                    append(evt.message);
                    if (evt.message.startsWith("DONE:")) success = true;
                } catch (_) {}
            }
        }
    } catch (err) {
        append("ERROR: " + err.message);
    }
    return success;
}

// ---------- Install Agent (SSE stream) ----------

ui.installAgentBtn.addEventListener("click", async () => {
    ui.installAgentBtn.disabled = true;
    ui.hint.textContent = "Installing agent …";
    const ok = await _runSseJob("/remote/ssh-install", ui.installLog);
    ui.installAgentBtn.disabled = false;
    if (ok) { await _recheckAgent(); }
    else { ui.hint.textContent = "Installation failed — see log above."; }
});

// ---------- Reinstall / Update Agent ----------

ui.reinstallAgentBtn.addEventListener("click", async () => {
    ui.reinstallAgentBtn.disabled = true;
    ui.enableAgentBtn.disabled = true;
    ui.hint.textContent = "Reinstalling agent …";
    const ok = await _runSseJob("/remote/ssh-install", ui.manageLog);
    ui.reinstallAgentBtn.disabled = false;
    ui.enableAgentBtn.disabled = false;
    if (ok) { await _recheckAgent(); ui.hint.textContent = "Agent reinstalled."; }
    else { ui.hint.textContent = "Reinstall failed — see log above."; }
});

// ---------- Remove Agent ----------

ui.removeAgentBtn.addEventListener("click", async () => {
    if (!confirm("Remove the agent from the remote device?")) return;
    ui.removeAgentBtn.disabled = true;
    ui.enableAgentBtn.disabled = true;
    ui.hint.textContent = "Removing agent …";
    const ok = await _runSseJob("/remote/ssh-uninstall", ui.manageLog);
    ui.removeAgentBtn.disabled = false;
    ui.enableAgentBtn.disabled = false;
    if (ok) {
        ui.agentVersionPill.hidden = true;
        ui.agentUpdateBanner.hidden = true;
        _showAgentPanel("agent-not-installed-panel");
        ui.hint.textContent = "Agent removed from remote.";
    } else {
        ui.hint.textContent = "Removal failed — see log above.";
    }
});

// ---------- Enable Agent ----------

function _appendAgentLog(line) {
    ui.agentLog.hidden = false;
    if (line === "__READY__") {
        // Marker from the server: the existing log has been replayed and
        // we're now following new lines.  Insert a visible separator so
        // the user can tell historical from live output.
        ui.agentLog.textContent += "── live tail ──\n";
    } else {
        ui.agentLog.textContent += line + "\n";
        // Keep the log bounded to the last ~400 lines so it doesn't grow forever.
        const lines = ui.agentLog.textContent.split("\n");
        if (lines.length > 450) {
            ui.agentLog.textContent = lines.slice(lines.length - 400).join("\n");
        }
    }
    ui.agentLog.scrollTop = ui.agentLog.scrollHeight;
}

function _stopAgentLog() {
    if (state.agentLogSource) {
        try { state.agentLogSource.close(); } catch (_) {}
        state.agentLogSource = null;
    }
}

function _startAgentLog(agentId) {
    _stopAgentLog();
    ui.agentLog.hidden = false;
    ui.agentLog.textContent = "Connecting to remote log stream …\n";
    const es = new EventSource(`/remote/agent/${agentId}/log`);
    state.agentLogSource = es;
    es.onmessage = (e) => {
        try {
            const evt = JSON.parse(e.data);
            if (evt.line) _appendAgentLog(evt.line);
        } catch (_) {}
    };
    es.onerror = () => {
        _appendAgentLog("(log stream disconnected)");
    };
}

async function _doEnableAgent() {
    // If an agent is already running (e.g. "Restart Agent"), disable it first
    // so it releases the port before we launch a fresh one.
    if (state.remoteAgentId) {
        try {
            await api("/remote/ssh-disable", {
                method: "POST", body: JSON.stringify({ agent_id: state.remoteAgentId }),
            });
            // Give the old process a moment to exit and release the port.
            await new Promise((r) => setTimeout(r, 1000));
        } catch (_) { /* best-effort */ }
    }
    _showAgentPanel("agent-enabling-panel");
    _resetRemoteState();
    ui.hint.textContent = "Starting agent on remote …";
    const creds = _sshCreds();
    let agentId, serverUrl;
    try {
        const r = await api("/remote/ssh-enable", {
            method: "POST",
            body: JSON.stringify({ ...creds, server_url: ui.sshServerUrl.value.trim() }),
        });
        agentId = r.agent_id;
        serverUrl = r.server_url;
    } catch (err) {
        _showAgentPanel("agent-installed-panel");
        ui.hint.textContent = "Failed to start agent: " + err.message;
        return;
    }
    state.remoteAgentId = agentId;
    ui.hint.textContent =
        `Agent started on ${creds.host} → ${serverUrl} — waiting for camera list …`;
    _startAgentLog(agentId);
    _startCameraPoll(agentId);
}

ui.enableAgentBtn.addEventListener("click", _doEnableAgent);
ui.reEnableAgentBtn.addEventListener("click", _doEnableAgent);

// ---------- Disable Agent ----------

async function _doDisableAgent() {
    if (!state.remoteAgentId) { _showAgentPanel("agent-installed-panel"); return; }
    ui.disableAgentBtn.disabled = true;
    ui.hint.textContent = "Disabling agent …";
    try {
        await api("/remote/ssh-disable", {
            method: "POST", body: JSON.stringify({ agent_id: state.remoteAgentId }),
        });
    } catch (err) {
        ui.hint.textContent = "Disable failed: " + err.message;
    }
    ui.disableAgentBtn.disabled = false;
    _resetRemoteState();
    // Re-check so the installed panel reflects current version/state.
    await _recheckAgent();
    ui.hint.textContent = "Agent disabled.";
}

ui.disableAgentBtn.addEventListener("click", _doDisableAgent);

// ---------- Remote camera list ----------

function _populateCameraSelect(cameras) {
    state.remoteCameras = cameras || [];
    ui.remoteCameraSelect.innerHTML = "";
    if (!cameras || cameras.length === 0) {
        const opt = document.createElement("option");
        opt.value = ""; opt.textContent = "No cameras found"; opt.disabled = true;
        ui.remoteCameraSelect.appendChild(opt);
        return false;
    }
    cameras.forEach((c) => {
        const opt = document.createElement("option");
        opt.value = c.id;
        opt.textContent = c.label || c.id;
        ui.remoteCameraSelect.appendChild(opt);
    });
    return true;
}

async function _refreshRemoteCameras() {
    if (!state.remoteAgentId) return;
    ui.refreshRemoteCameras.disabled = true;
    ui.hint.textContent = "Re-scanning remote cameras …";
    try {
        const r = await api(`/remote/agent/${state.remoteAgentId}/cameras?refresh=1`);
        const any = _populateCameraSelect(r.cameras);
        ui.hint.textContent = any
            ? "Camera list refreshed — select a camera, then press ▶ Start Capture."
            : "No cameras found on remote.";
    } catch (err) {
        ui.hint.textContent = "Refresh failed: " + err.message;
    } finally {
        ui.refreshRemoteCameras.disabled = false;
    }
}

ui.refreshRemoteCameras.addEventListener("click", _refreshRemoteCameras);

// ---------- Camera list polling ----------

function _startCameraPoll(agentId) {
    _stopCameraPoll();
    let attempts = 0;
    state.remotePollTimer = setInterval(async () => {
        attempts++;
        try {
            const r = await api(`/remote/agent/${agentId}/cameras`);
            if (r.connected) {
                _stopCameraPoll();
                const any = _populateCameraSelect(r.cameras);
                ui.hint.textContent = any
                    ? "Agent running — select a camera, then press ▶ Start Capture."
                    : "Agent connected — no cameras found on remote.";
                _showAgentPanel("agent-running-panel");
                return;
            }
            // r.connected is false: could be the explicit "pending" state
            // (server issued token but agent hasn't WS-connected yet) — show
            // an informative hint instead of staying silent.
            if (r.pending && attempts % 5 === 1) {
                ui.hint.textContent =
                    `Agent process started (id ${agentId.slice(0, 8)}…) — ` +
                    "probing agent via SSH tunnel. " +
                    "Check the agent log below if this takes more than ~10 s.";
            }
        } catch (err) {
            // Only surface a hint on the first failure and then every 10 polls
            // to avoid spamming the user while we keep retrying.
            if (attempts === 1 || attempts % 10 === 0) {
                ui.hint.textContent =
                    `Polling agent failed (${err.message}). ` +
                    "Check the agent log below.";
            }
        }
        if (attempts >= 60) { // 60 × 2 s = 2 min
            _stopCameraPoll();
            _showAgentPanel("agent-installed-panel");
            ui.hint.textContent =
                "Agent did not connect in 2 min. Check the agent log below, " +
                "verify the SSH server URL is reachable from the remote box, then retry.";
        }
    }, 2000);
}

function _stopCameraPoll() {
    if (state.remotePollTimer) { clearInterval(state.remotePollTimer); state.remotePollTimer = null; }
}

// ---------- Start Capture (remote) ----------

async function _remoteStartCapture() {
    if (!state.remoteAgentId) {
        ui.hint.textContent = "Enable the remote agent first.";
        return;
    }
    const cameraId = ui.remoteCameraSelect.value;
    if (!cameraId) { ui.hint.textContent = "Select a remote camera first."; return; }

    // Create + start session
    const profile = currentProfile();
    let info;
    try {
        info = await api("/sessions", {
            method: "POST",
            body: JSON.stringify({ name: profile.name, source: "remote", camera_id: null, profile }),
        });
        state.session = info;
        info = await api(`/sessions/${state.session.id}/start`, { method: "POST" });
        state.session = info;
    } catch (err) {
        ui.hint.textContent = "Failed to create/start session: " + err.message;
        return;
    }

    // Bind agent → session + camera (triggers streaming)
    try {
        await api(`/remote/agent/${state.remoteAgentId}/bind`, {
            method: "POST",
            body: JSON.stringify({ session_id: state.session.id, camera_id: cameraId }),
        });
    } catch (err) {
        ui.hint.textContent = "Failed to bind camera: " + err.message;
        return;
    }

    setState(info.state);
    ui.capturesPill.textContent = `captures: 0 / ${info.required_captures}`;
    ui.hint.textContent = `Remote calibration started — need ${info.required_captures} captures.`;
    connectStream();
    loadSessionList();
}

// ---------- Manual token fallback ----------

ui.issueToken.addEventListener("click", async () => {
    if (!state.session) {
        const profile = currentProfile();
        const body = { name: profile.name, source: "remote", camera_id: null, profile };
        try {
            state.session = await api("/sessions", { method: "POST", body: JSON.stringify(body) });
        } catch (err) { ui.hint.textContent = err.message; return; }
    }
    try {
        const t = await api(`/remote/${state.session.id}/token`, { method: "POST" });
        ui.remoteToken.value = t.token;
        ui.agentCmd.textContent = t.agent_command;
        ui.hint.textContent = "Run the agent command on the remote machine, then press Start Capture.";
        updateButtons();
    } catch (err) {
        ui.hint.textContent = "Token issue failed: " + err.message;
    }
});
