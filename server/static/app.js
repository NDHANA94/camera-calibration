// sintez-cam-server: vanilla JS frontend.
// REST for chessboards/sessions/cameras; WebSocket for the live frame stream.
//
// Camera modes:
//   mono             - 1 camera select, single-frame pipeline
//   stereo_lr        - 1 camera select, side-by-side L|R pipeline
//   stereo_separate  - 2 camera selects (left + right), dual pipeline
//
// Chessboards (formerly "profiles") are managed through an overlay window:
// the left panel only contains a select + summary, never the editing fields.

const $ = (id) => document.getElementById(id);

const ui = {
    tabs: document.querySelectorAll(".tab"),
    sessionCard: $("session-card"),
    remotePanel: $("remote-panel"),
    // Camera type + selectors (shared by Local + Remote tabs).
    // The visible sub-section (local vs remote) is swapped by JS based on
    // which tab is active; the Camera type dropdown drives both.
    cameraMode: $("camera-mode"),
    cameraSection: $("camera-section"),
    localCameras: $("local-cameras"),
    remoteCameras: $("remote-cameras"),
    // Local camera selectors
    cameraSelect: $("local-camera"),
    cameraLeft: $("local-camera-left"),
    cameraRight: $("local-camera-right"),
    refreshCameras: $("refresh-cameras"),
    refreshCameras2: $("refresh-cameras-2"),
    localCamSingle: $("local-cam-single"),
    localCamDual: $("local-cam-dual"),
    // Remote camera selectors
    remoteCameraSelect: $("remote-camera-select"),
    remoteCameraLeft: $("remote-camera-left"),
    remoteCameraRight: $("remote-camera-right"),
    remoteCamSingle: $("remote-cam-single"),
    remoteCamDual: $("remote-cam-dual"),
    refreshRemoteCameras: $("refresh-remote-cameras"),
    refreshRemoteCameras2: $("refresh-remote-cameras-2"),
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
    // Chessboard (replaces the old "profile" section in the left panel)
    chessboardSelect: $("chessboard-select"),
    chessboardCurrent: $("chessboard-current"),
    chessboardManage: $("chessboard-manage"),
    // Session-level controls
    sessionName: $("session-name"),
    requiredCaptures: $("required-captures"),
    // Capture / abort / finish
    startSession: $("start-session"),
    captureNow: $("capture-now"),
    abortSession: $("abort-session"),
    finishSession: $("finish-session"),
    // Live / status UI
    canvas: $("preview"),
    statePill: $("state-pill"),
    capturesPill: $("captures-pill"),
    boardPill: $("board-pill"),
    blurPill: $("blur-pill"),
    connPill: $("conn-pill"),
    bwPill: $("bw-pill"),
    statusCard: $("status-card"),
    progressBar: $("progress-bar"),
    capturedThumbs: $("captured-thumbs"),
    capturedCount: $("captured-count"),
    hint: $("hint"),
    // Chessboard overlay
    cbOverlay: $("chessboard-overlay"),
    cbOverlayTitle: $("cb-overlay-title"),
    cbList: $("chessboard-list"),
    cbNew: $("chessboard-new"),
    cbFormPanel: $("chessboard-form-panel"),
    cbFormHelp: $("chessboard-form-help"),
    cbName: $("cb-name"),
    cbMode: $("cb-mode"),
    cbW: $("cb-w"),
    cbH: $("cb-h"),
    cbSquare: $("cb-square"),
    cbRequired: $("cb-required"),
    cbSave: $("chessboard-save"),
    cbDelete: $("chessboard-delete"),
    cbCancel: $("chessboard-cancel"),
};

const state = {
    tab: "local",
    cameras: [],
    session: null,
    socket: null,
    canvasCtx: ui.canvas.getContext("2d"),
    // Remote SSH state
    sshConnected: false,
    // Name of the SSH profile currently connected (for highlighting the
    // matching row in the connection list). Empty when not connected.
    activeSshProfile: null,
    // Bandwidth tracking for the remote frame stream.  ``bytes`` accumulates
    // over the last ``bandwidth.windowMs``; the read-side updates every
    // ``bandwidth.updateMs`` to keep the indicator cheap.
    bandwidth: { bytes: 0, since: 0, lastSampleBytes: 0, lastSampleTs: 0, bps: 0, timer: null },
    remoteAgentId: null,
    remoteCameras: [],
    remotePollTimer: null,
    agentHealthTimer: null,
    agentLogSource: null,
    agentCheck: null,   // last {installed, version, latest, needs_update}
    _starting: false,
    // Chessboards (formerly "profiles")
    chessboards: [],
    selectedChessboard: null,   // {name, mode, ...} the one currently selected
    _editingChessboard: null,   // currently open in the overlay
    _overlayOriginalName: null, // original name when editing (in case the user renames)
};

// ---------- Tab switching ----------

ui.tabs.forEach((btn) => {
    btn.addEventListener("click", () => {
        ui.tabs.forEach((b) => b.classList.toggle("active", b === btn));
        state.tab = btn.dataset.tab;
        // Session card is always visible.  Inside it, swap which camera-pickers
        // sub-section is shown: Local tab → local cameras, Remote tab →
        // remote cameras (driven from the agent's enumerated list).
        ui.remotePanel.hidden = state.tab !== "remote";
        if (ui.localCameras) ui.localCameras.hidden = state.tab !== "local";
        if (ui.remoteCameras) ui.remoteCameras.hidden = state.tab !== "remote";
        // Re-apply the single/dual picker visibility for the new sub-section.
        _applyCameraModeToUI();
    });
});

// ---------- Middle panel views ----------
function setMiddleView(which) {
    $("live-view").hidden = which !== "live";
    $("detail-panel").hidden = which !== "detail";
    $("middle-placeholder").hidden = which !== "placeholder";
}

// ---------- Status card ----------

function setState(s) {
    ui.statePill.textContent = s;
    ui.statePill.className = "pill " + (
        s === "running" ? "warn" :
        s === "finished" ? "ok" :
        s === "failed" ? "bad" : ""
    );
    ui.statusCard.className = "status-card" + (
        s === "running" ? " running" :
        s === "finished" ? " finished" :
        s === "failed" ? " failed" : ""
    );
    updateProgress();
    updateButtons();
}

function setConn(s) {
    ui.connPill.textContent = s;
    ui.connPill.className = "pill " + (s === "connected" ? "ok" : s === "connecting" ? "warn" : "bad");
    updateButtons();
}

function updateProgress() {
    const required = state.session?.required_captures ?? 0;
    const captures = state.session?.captures ?? 0;
    ui.capturesPill.textContent = `captures: ${captures} / ${required || "—"}`;
    const pct = required > 0 ? Math.min(100, Math.round((captures / required) * 100)) : 0;
    ui.progressBar.style.width = pct + "%";
    ui.progressBar.classList.toggle("full", required > 0 && captures >= required);
    ui.capturesPill.className = "pill " + (required > 0 && captures >= required ? "ok" : "");
}

// ---------- Captured-frame strip ----------

function resetCapturedStrip() {
    ui.capturedThumbs.innerHTML = '<span class="empty">Captured frames will appear here.</span>';
    ui.capturedCount.textContent = "0";
}

function addCapturedThumb(n) {
    if (!state.session) return;
    const sid = state.session.id;
    const idx = String(n - 1).padStart(4, "0");
    const empty = ui.capturedThumbs.querySelector(".empty");
    if (empty) empty.remove();
    const img = document.createElement("img");
    const url = `/session-data/${sid}/frames/${idx}.png`;
    img.src = `${url}?t=${Date.now()}`;
    img.title = `Capture ${n}`;
    img.loading = "lazy";
    img.onerror = () => { setTimeout(() => { img.src = `${url}?t=${Date.now()}`; }, 500); };
    img.onclick = () => window.open(img.src, "_blank");
    ui.capturedThumbs.appendChild(img);
    ui.capturedThumbs.scrollLeft = ui.capturedThumbs.scrollWidth;
    ui.capturedCount.textContent = String(n);
}

// ---------- Button state management ----------

function updateButtons() {
    const running = state.session?.state === "running";
    const connected = state.socket !== null;
    const captures = state.session?.captures ?? 0;

    ui.startSession.disabled = !!state._starting;
    ui.startSession.textContent = running ? "▶ Restart Capture" : "▶ Start Capture";
    ui.captureNow.disabled = !running || !connected;
    ui.abortSession.disabled = !running;
    ui.finishSession.disabled = !running || captures < 3;
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
        _populateLocalCameraSelects(cams);
    } catch (err) {
        ui.hint.textContent = "Failed to list cameras: " + err.message;
    }
}

function _populateLocalCameraSelects(cams) {
    // Same camera list goes into mono + (left,right) for dual; the user picks.
    const fill = (sel, preferredId) => {
        sel.innerHTML = "";
        if (!cams.length) {
            const opt = document.createElement("option");
            opt.value = ""; opt.textContent = "No cameras found"; opt.disabled = true;
            sel.appendChild(opt);
            return;
        }
        cams.forEach((c) => {
            const opt = document.createElement("option");
            opt.value = c.id;
            opt.textContent = c.label;
            sel.appendChild(opt);
        });
        if (preferredId && cams.some((c) => c.id === preferredId)) sel.value = preferredId;
        else sel.selectedIndex = 0;
    };
    fill(ui.cameraSelect, ui.cameraSelect.value);
    fill(ui.cameraLeft, ui.cameraLeft.value);
    fill(ui.cameraRight, ui.cameraRight.value);
    // Make sure left and right default to DIFFERENT devices so the user
    // doesn't accidentally calibrate the same camera as both eyes.
    if (ui.cameraLeft.value === ui.cameraRight.value && cams.length >= 2) {
        ui.cameraRight.selectedIndex = 1;
    }
}

ui.refreshCameras.addEventListener("click", refreshCameras);
ui.refreshCameras2.addEventListener("click", refreshCameras);

// Camera-type select: show 1 or 2 selectors based on mode.
function _applyCameraModeToUI() {
    const isDual = ui.cameraMode.value === "stereo_separate";
    if (state.tab === "remote") {
        ui.remoteCamSingle.hidden = isDual;
        ui.remoteCamDual.hidden = !isDual;
    } else {
        ui.localCamSingle.hidden = isDual;
        ui.localCamDual.hidden = !isDual;
    }
}
ui.cameraMode.addEventListener("change", _applyCameraModeToUI);

refreshCameras();

// ---------- Calibration flags from checkboxes ----------

function getCalibFlags() {
    let flags = 0;
    document.querySelectorAll(".calib-flag").forEach((cb) => {
        if (cb.checked) flags |= parseInt(cb.value, 10);
    });
    return flags;
}

// ---------- Chessboards (the "profile" concept) ----------

const MODE_LABEL = {
    mono: "Mono",
    stereo_lr: "Stereo L|R",
    stereo_separate: "Stereo (2 cams)",
};

function _chessboardSummary(cb) {
    if (!cb) return '<span class="empty">No chessboard selected.</span>';
    const m = MODE_LABEL[cb.mode] || cb.mode;
    return `<strong>${escHtml(cb.name)}</strong>
            <span class="chess-summary-detail">
                ${m} &middot; ${cb.inner_corners_x}×${cb.inner_corners_y}
                &middot; ${cb.square_size_mm} mm
                &middot; N=${cb.required_captures}
            </span>`;
}

function _renderChessboardCurrent() {
    ui.chessboardCurrent.innerHTML = _chessboardSummary(state.selectedChessboard);
}

async function loadChessboards() {
    try {
        // Try the new endpoint first; fall back to /profiles for old servers.
        let list;
        try { list = await api("/chessboards"); }
        catch (_) { list = await api("/profiles"); }
        state.chessboards = list;
        const prevSelection = state.selectedChessboard?.name || ui.chessboardSelect.value;
        ui.chessboardSelect.innerHTML = "";
        const blank = document.createElement("option");
        blank.value = ""; blank.textContent = "(saved chessboards)";
        ui.chessboardSelect.appendChild(blank);
        list.forEach((cb) => {
            const opt = document.createElement("option");
            opt.value = cb.name;
            const m = MODE_LABEL[cb.mode] || cb.mode;
            opt.textContent = `${cb.name}  ·  ${m}  ·  ${cb.inner_corners_x}×${cb.inner_corners_y}  ${cb.square_size_mm}mm  N=${cb.required_captures}`;
            ui.chessboardSelect.appendChild(opt);
        });
        if (prevSelection && list.some((c) => c.name === prevSelection)) {
            ui.chessboardSelect.value = prevSelection;
            state.selectedChessboard = list.find((c) => c.name === prevSelection);
        } else if (list.length) {
            ui.chessboardSelect.selectedIndex = 1;
            state.selectedChessboard = list[0];
        } else {
            state.selectedChessboard = null;
        }
        _renderChessboardCurrent();
    } catch (err) {
        ui.hint.textContent = "Failed to load chessboards: " + err.message;
    }
}

ui.chessboardSelect.addEventListener("change", () => {
    const name = ui.chessboardSelect.value;
    state.selectedChessboard = state.chessboards.find((c) => c.name === name) || null;
    _renderChessboardCurrent();
});

// ---------- Chessboard overlay (open / close / list / edit) ----------

function openChessboardOverlay() {
    state._editingChessboard = null;
    state._overlayOriginalName = null;
    _renderOverlayList();
    _clearCbForm();
    _setCbFormHelp("Pick a chessboard from the list, or click + New chessboard.");
    ui.cbOverlay.hidden = false;
}
function closeChessboardOverlay() {
    ui.cbOverlay.hidden = true;
    state._editingChessboard = null;
    state._overlayOriginalName = null;
}
window.openChessboardOverlay = openChessboardOverlay;
window.closeChessboardOverlay = closeChessboardOverlay;

ui.chessboardManage.addEventListener("click", openChessboardOverlay);
// Click outside the dialog closes it
ui.cbOverlay.addEventListener("click", (e) => { if (e.target === ui.cbOverlay) closeChessboardOverlay(); });
// ESC closes the overlay
document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !ui.cbOverlay.hidden) closeChessboardOverlay();
});

function _renderOverlayList() {
    if (!state.chessboards.length) {
        ui.cbList.innerHTML = '<div class="cb-list-empty">No chessboards yet — click + New chessboard.</div>';
        return;
    }
    ui.cbList.innerHTML = state.chessboards.map((cb) => {
        const m = MODE_LABEL[cb.mode] || cb.mode;
        const active = state._editingChessboard && state._editingChessboard.name === cb.name
            ? " active" : "";
        return `
        <div class="cb-row${active}" data-cb-name="${escHtml(cb.name)}">
            <div class="cb-row-name">${escHtml(cb.name)}</div>
            <div class="cb-row-meta">${m} &middot; ${cb.inner_corners_x}×${cb.inner_corners_y} &middot; ${cb.square_size_mm} mm</div>
        </div>`;
    }).join("");
    ui.cbList.querySelectorAll(".cb-row").forEach((row) => {
        row.addEventListener("click", () => _loadCbIntoForm(row.dataset.cbName));
    });
}

function _loadCbIntoForm(name) {
    const cb = state.chessboards.find((c) => c.name === name);
    if (!cb) return;
    state._editingChessboard = cb;
    state._overlayOriginalName = cb.name;
    ui.cbName.value = cb.name;
    ui.cbMode.value = cb.mode || "mono";
    ui.cbW.value = cb.inner_corners_x;
    ui.cbH.value = cb.inner_corners_y;
    ui.cbSquare.value = cb.square_size_mm;
    ui.cbRequired.value = cb.required_captures;
    document.querySelectorAll(".cb-flag").forEach((flagCb) => {
        flagCb.checked = (cb.flags & parseInt(flagCb.value, 10)) !== 0;
    });
    _renderOverlayList();   // re-render so the active highlight updates
    _setCbFormHelp(`Editing <b>${escHtml(cb.name)}</b> — change values and click Save.`);
}

function _clearCbForm() {
    ui.cbName.value = "";
    ui.cbMode.value = "mono";
    ui.cbW.value = 9; ui.cbH.value = 6;
    ui.cbSquare.value = 25;
    ui.cbRequired.value = 20;
    document.querySelectorAll(".cb-flag").forEach((cb) => (cb.checked = false));
}

function _setCbFormHelp(html) {
    ui.cbFormHelp.innerHTML = html;
}

function _readCbForm() {
    return {
        name: sanitizeName(ui.cbName.value),
        mode: ui.cbMode.value,
        inner_corners_x: Math.max(2, Math.min(30, parseInt(ui.cbW.value, 10) || 9)),
        inner_corners_y: Math.max(2, Math.min(30, parseInt(ui.cbH.value, 10) || 6)),
        square_size_mm: Math.max(0.1, parseFloat(ui.cbSquare.value) || 25),
        required_captures: Math.max(3, Math.min(200, parseInt(ui.cbRequired.value, 10) || 20)),
        flags: (() => {
            let f = 0;
            document.querySelectorAll(".cb-flag").forEach((cb) => {
                if (cb.checked) f |= parseInt(cb.value, 10);
            });
            return f;
        })(),
    };
}

ui.cbNew.addEventListener("click", () => {
    state._editingChessboard = null;
    state._overlayOriginalName = null;
    _clearCbForm();
    _renderOverlayList();
    _setCbFormHelp("New chessboard — fill in the form and click Save.");
    ui.cbName.focus();
});

ui.cbSave.addEventListener("click", async () => {
    const payload = _readCbForm();
    if (!payload.name) {
        _setCbFormHelp("Please give the chessboard a name.");
        return;
    }
    try {
        // Save / overwrite via POST /chessboards/save (or /profiles/save as fallback).
        const url = state._overlayOriginalName && state._overlayOriginalName !== payload.name
            ? `/chessboards/${encodeURIComponent(state._overlayOriginalName)}?fallback=1`
            : "/chessboards/save";
        let result;
        try { result = await api(url, { method: "POST", body: JSON.stringify(payload) }); }
        catch (_) { result = await api("/profiles/save", { method: "POST", body: JSON.stringify(payload) }); }
        ui.hint.textContent = `Chessboard "${result.name}" saved.`;
        state._overlayOriginalName = result.name;
        await loadChessboards();
        // Re-open the same item so the rename takes effect in the overlay list.
        _loadCbIntoForm(result.name);
    } catch (err) {
        _setCbFormHelp("Save failed: " + err.message);
    }
});

ui.cbDelete.addEventListener("click", async () => {
    const name = state._overlayOriginalName;
    if (!name) {
        _setCbFormHelp("Pick a chessboard from the list to delete.");
        return;
    }
    if (!confirm(`Delete chessboard "${name}"?`)) return;
    try {
        try { await api(`/chessboards/${encodeURIComponent(name)}`, { method: "DELETE" }); }
        catch (_) { await api(`/profiles/${encodeURIComponent(name)}`, { method: "DELETE" }); }
        ui.hint.textContent = `Chessboard "${name}" deleted.`;
        state._editingChessboard = null;
        state._overlayOriginalName = null;
        _clearCbForm();
        await loadChessboards();
        _renderOverlayList();
        _setCbFormHelp("Pick a chessboard from the list, or click + New chessboard.");
    } catch (err) {
        _setCbFormHelp("Delete failed: " + err.message);
    }
});

ui.cbCancel.addEventListener("click", closeChessboardOverlay);

// Auto-load on page start
loadChessboards();

// ---------- Sessions ----------

function sanitizeName(raw) {
    return (raw || "").trim().replace(/[^A-Za-z0-9_.\-]+/g, "_").replace(/^_+|_+$/g, "") || "default";
}

// Auto-generate a session name from camera type + chessboard config so users
// who leave the field blank still get something meaningful and unique-ish.
// Format:  <mode>_<WxH>_<mm>mm_YYYY-MM-DD_HH-MM
function _autoSessionName(mode, cb) {
    if (!cb) return sanitizeName(`${mode}_session_${Date.now()}`);
    const ts = new Date();
    const yyyy = ts.getFullYear();
    const mm = String(ts.getMonth() + 1).padStart(2, "0");
    const dd = String(ts.getDate()).padStart(2, "0");
    const hh = String(ts.getHours()).padStart(2, "0");
    const mi = String(ts.getMinutes()).padStart(2, "0");
    return sanitizeName(
        `${mode}_${cb.inner_corners_x}x${cb.inner_corners_y}_${cb.square_size_mm}mm_${yyyy}-${mm}-${dd}_${hh}-${mi}`
    );
}

// What the user intends to do, normalised into the structure the API expects.
function _currentChessboard() {
    // Either a saved chessboard is selected, or we fall back to defaults so
    // the API still gets a sensible payload (this matches the old behaviour).
    const selected = state.selectedChessboard;
    const mode = _isDualMode() ? "stereo_separate" : (_isStereoLr() ? "stereo_lr" : "mono");
    if (selected) {
        return {
            name: selected.name,
            mode,
            // Pull the live advanced-option state so the user's current
            // checkbox + captures-count tweaks apply even when a saved
            // chessboard is selected.  This matches the old UX where the
            // form fields overrode the saved profile values.
            inner_corners_x: selected.inner_corners_x,
            inner_corners_y: selected.inner_corners_y,
            square_size_mm: selected.square_size_mm,
            flags: getCalibFlags(),
            required_captures: Math.max(3, Math.min(200, parseInt(ui.requiredCaptures.value, 10) || selected.required_captures || 20)),
        };
    }
    return {
        name: "default",
        mode,
        inner_corners_x: 9, inner_corners_y: 6,
        square_size_mm: 25.0,
        flags: getCalibFlags(),
        required_captures: Math.max(3, Math.min(200, parseInt(ui.requiredCaptures.value, 10) || 20)),
    };
}

function _isDualMode() {
    return ui.cameraMode && ui.cameraMode.value === "stereo_separate";
}
function _isStereoLr() {
    return ui.cameraMode && ui.cameraMode.value === "stereo_lr";
}
function _activeMode() {
    if (_isDualMode()) return "stereo_separate";
    if (_isStereoLr()) return "stereo_lr";
    return "mono";
}

// Combined create + start: one button does it all.
ui.startSession.addEventListener("click", async () => {
    if (state._starting) return;
    state._starting = true;
    updateButtons();
    try {
        if (state.session && state.session.state === "running") {
            disconnectStream();
            try { await api(`/sessions/${state.session.id}/abort`, { method: "POST" }); }
            catch (_) {}
            state.session = null;
        }

        if (state.tab === "remote") {
            await _remoteStartCapture();
            return;
        }

        // Local path
        const chessboard = _currentChessboard();
        if (!state.selectedChessboard) {
            ui.hint.textContent = "Select a chessboard first (or create one in Manage …).";
            return;
        }
        // Validate camera selection per mode.
        let cameraId = null, cameraId2 = null;
        if (_isDualMode()) {
            cameraId = ui.cameraLeft.value;
            cameraId2 = ui.cameraRight.value;
            if (!cameraId || !cameraId2) {
                ui.hint.textContent = "Select LEFT and RIGHT cameras.";
                return;
            }
            if (cameraId === cameraId2) {
                ui.hint.textContent = "LEFT and RIGHT cameras must be different devices.";
                return;
            }
        } else {
            cameraId = ui.cameraSelect.value;
            if (!cameraId) { ui.hint.textContent = "No camera selected."; return; }
        }

        const explicitName = ui.sessionName.value.trim();
        const finalName = explicitName || _autoSessionName(_activeMode(), chessboard);
        const body = {
            name: finalName,
            source: "local",
            camera_id: cameraId,
            camera_id_2: cameraId2,
            chessboard,
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
            ui.hint.style.borderColor = "";
            resetCapturedStrip();
            setMiddleView("live");
            setState(info.state);
            ui.hint.textContent = `Session started — need ${info.required_captures} captures. Hold the chessboard in view.`;
            connectStream();
            loadSessionList();
        } catch (err) {
            ui.hint.textContent = "Start failed: " + err.message;
        }
    } finally {
        state._starting = false;
        updateButtons();
    }
});

ui.captureNow.addEventListener("click", () => {
    if (state.tab === "remote") {
        if (!state.session) return;
        api(`/sessions/${state.session.id}/capture-now`, { method: "POST" }).catch(() => {});
    } else {
        if (!state.socket) return;
        try { state.socket.send(JSON.stringify({ type: "capture_now" })); } catch (_) {}
    }
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
        setMiddleView("placeholder");
        ui.hint.textContent = "Session aborted.";
        loadSessionList();
    } catch (err) {
        ui.hint.textContent = "Abort failed: " + err.message;
    }
});

ui.finishSession.addEventListener("click", () => finishAndCalibrate(false));

let _finishing = false;
async function finishAndCalibrate(auto) {
    if (!state.session || _finishing) return;
    _finishing = true;
    _stopCameraPoll();
    disconnectStream();
    ui.hint.textContent = auto
        ? "Required captures reached — calculating calibration…"
        : "Calculating calibration…";
    const sid = state.session.id;
    try {
        await api(`/calibrate/${sid}`, { method: "POST" });
        state.session = { ...state.session, state: "finished" };
        setState("finished");
        loadSessionList();
        await renderSessionDetail(sid);
        ui.hint.textContent = "Calibration complete.";
        ui.hint.style.borderColor = "";
    } catch (err) {
        ui.hint.textContent = "Calibration failed: " + err.message;
        ui.hint.style.borderColor = "var(--danger)";
        setState("failed");
        setMiddleView("placeholder");
    } finally {
        _finishing = false;
    }
}

// ---------- Stream ----------

function disconnectStream() {
    if (state.socket) {
        try { state.socket.send(JSON.stringify({ type: "stop" })); } catch (_) {}
        try { state.socket.close(); } catch (_) {}
        state.socket = null;
    }
    setConn("disconnected");
    _stopBandwidthMeter();
}

// ---------- Bandwidth meter (remote stream only) ----------
// Counts bytes received over the SSE frame stream and shows them in the
// "bw-pill" pill at the top of the right panel.  Updates at 1 Hz so the
// cost is negligible; the byte counter is incremented in the SSE onmessage
// handler above (one increment per frame, O(1)).
function _startBandwidthMeter() {
    const bw = state.bandwidth;
    bw.bytes = 0;
    bw.since = Date.now();
    bw.lastSampleBytes = 0;
    bw.lastSampleTs = bw.since;
    bw.bps = 0;
    const pill = $("bw-pill");
    if (pill) { pill.hidden = false; pill.textContent = "0 B/s"; }
    _stopBandwidthMeter();
    bw.timer = setInterval(() => {
        const now = Date.now();
        const dt = (now - bw.lastSampleTs) / 1000;
        const dBytes = bw.bytes - bw.lastSampleBytes;
        bw.bps = dt > 0 ? dBytes / dt : 0;
        bw.lastSampleBytes = bw.bytes;
        bw.lastSampleTs = now;
        const pill = $("bw-pill");
        if (pill) pill.textContent = _formatBandwidth(bw.bps);
    }, 1000);
}

function _stopBandwidthMeter() {
    const bw = state.bandwidth;
    if (bw.timer) { clearInterval(bw.timer); bw.timer = null; }
    bw.bytes = 0; bw.bps = 0; bw.lastSampleBytes = 0; bw.lastSampleTs = 0;
    const pill = $("bw-pill");
    if (pill) { pill.hidden = true; pill.textContent = "— B/s"; }
}

function _formatBandwidth(bps) {
    if (!bps || bps <= 0) return "0 B/s";
    if (bps < 1024) return `${bps.toFixed(0)} B/s`;
    if (bps < 1024 * 1024) return `${(bps / 1024).toFixed(1)} KB/s`;
    return `${(bps / 1024 / 1024).toFixed(2)} MB/s`;
}

function connectStream() {
    disconnectStream();
    if (!state.session) return;
    const sid = state.session.id;
    setConn("connecting");

    if (state.tab === "local") {
        const proto = location.protocol === "https:" ? "wss:" : "ws:";
        const url = `${proto}//${location.host}/ws/local/${sid}`;
        const ws = new WebSocket(url);
        ws.binaryType = "arraybuffer";
        state.socket = ws;
        ws.onopen = () => { setConn("connected"); updateButtons(); };
        ws.onclose = () => {
            setConn("disconnected");
            state.socket = null;
            if (!_finishing && state.session && state.session.state === "running") {
                state.session = { ...state.session, state: "idle" };
                setState("idle");
            }
            updateButtons();
        };
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
        const es = new EventSource(`/remote/stream/${sid}`);
        state.socket = es;
        es.onopen = () => {
            setConn("connected");
            updateButtons();
            _startBandwidthMeter();
        };
        es.onerror = () => { setConn("disconnected"); _stopBandwidthMeter(); };
        es.onmessage = (ev) => {
            try {
                const msg = JSON.parse(ev.data);
                if (msg.type === "frame") {
                    // Count the payload bytes for the bandwidth indicator.
                    // (Includes the base64-expanded size, which is close to
                    // the actual data rate and cheap to compute.)
                    state.bandwidth.bytes += (msg.data || "").length;
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
        if (state.session) {
            state.session.captures = msg.n;
            updateProgress();
            updateButtons();
            addCapturedThumb(msg.n);
            const required = state.session.required_captures ?? 0;
            if (required > 0 && msg.n >= required) {
                finishAndCalibrate(true);
            }
        }
    } else if (msg.type === "status") {
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

// Format an ISO timestamp into "YYYY-MM-DD HH:MM" in the user's local zone
// for the history list.  Sessions often span seconds; we keep seconds for
// the title tooltip and a coarser date in the visible row.
function _formatTimestamp(iso) {
    if (!iso) return "";
    let s = iso;
    if (s.endsWith("Z")) s = s.slice(0, -1) + "+00:00";
    const d = new Date(s);
    if (isNaN(d.getTime())) return iso;
    const pad = (n) => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function renderSessionList(sessions, health = {}) {
    const el = $("sessions-list");
    if (!sessions || !sessions.length) {
        el.innerHTML = '<span class="session-empty">No sessions yet.</span>';
        return;
    }
    // Sort latest first.  created_at is ISO-8601 UTC and sorts lexically,
    // but fall back to id for legacy sessions that lack it.
    sessions.sort((a, b) => {
        const ta = a.created_at || a.id;
        const tb = b.created_at || b.id;
        return tb.localeCompare(ta);
    });
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
        const when = _formatTimestamp(s.created_at);
        const whenHtml = when
            ? `<span class="session-when" title="${escHtml(s.created_at || s.id)}">${escHtml(when)}</span>`
            : "";
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
            <div class="session-row-bottom">
                ${whenHtml}
                ${paramsLine}
            </div>
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

function _modeLabel(mode) {
    if (!mode) return "Mono";
    if (mode === "stereo_separate") return "Stereo (2 cameras)";
    if (mode === "stereo_lr") return "Stereo L | R";
    return "Mono";
}

function viewSession(sessionId) {
    if (_activeIntrinsicsId === sessionId && !$("detail-panel").hidden) {
        closeDetail();
        return;
    }
    renderSessionDetail(sessionId);
}

async function renderSessionDetail(sessionId) {
    _activeIntrinsicsId = sessionId;

    document.querySelectorAll(".session-row").forEach((r) => r.classList.remove("active"));
    const row = $(`srow-${sessionId}`);
    if (row) row.classList.add("active");

    $("detail-title").textContent = "Loading…";
    $("detail-body").innerHTML = "";
    $("copy-intrinsics").hidden = true;
    setMiddleView("detail");

    try {
        const detail = await api(`/sessions/${sessionId}/detail`);
        $("detail-title").textContent = `Session — ${detail.name}`;

        const p = detail.profile ?? {};
        const created = _formatTimestamp(detail.created_at);
        const mode = _modeLabel(p.mode);

        let html = "";
        if (detail.state === "finished" && detail.rms != null) {
            html += `<div class="result-banner">✓ <strong>Calibration complete${p.is_stereo || (p.mode && p.mode !== 'mono') ? " (stereo)" : ""}.</strong>
                &nbsp; ${mode} RMS: <code>${Number(detail.rms).toFixed(4)}</code>
                &nbsp; <button class="secondary small" onclick="openSessionDir('${sessionId}')">Open result folder</button></div>`;
        } else if (detail.state === "failed") {
            html += `<div class="result-banner failed">⚠ Calibration failed for this session.</div>`;
        }

        html += `
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
                <span class="val">${detail.source}${detail.camera_id ? " · " + escHtml(detail.camera_id) : ""}${detail.camera_id_2 ? " + " + escHtml(detail.camera_id_2) : ""}</span>
                <span class="lbl">Mode</span>
                <span class="val">${mode}</span>
                <span class="lbl">Created</span>
                <span class="val">${created || "—"}</span>
            </div>
        </div>`;

        if (detail.frames && detail.frames.length > 0) {
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
    document.querySelectorAll(".session-row").forEach((r) => r.classList.remove("active"));
    _activeIntrinsicsId = null;
    setMiddleView(state.session?.state === "running" ? "live" : "placeholder");
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
    return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
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
            setMiddleView("placeholder");
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

loadSessionList();

// ==========================================================================
// SSH Profile management (kept identical to previous behavior)
// ==========================================================================

async function loadSshProfiles() {
    let profiles = [];
    try { profiles = await api("/remote/ssh-profiles"); } catch (_) {}
    ui.sshProfilesList.innerHTML = "";
    if (!profiles.length) {
        ui.sshProfilesList.innerHTML =
            '<div style="font-size:12px;color:var(--muted);padding:4px 0">No saved connections</div>';
        return;
    }
    profiles.forEach((p) => {
        const row = document.createElement("div");
        row.className = "ssh-profile-row row";
        row.dataset.profileRow = p.name;
        if (state.activeSshProfile === p.name) row.classList.add("ssh-profile-active");
        row.innerHTML = `
            <span class="ssh-profile-label">
                <strong>${escHtml(p.name)}</strong>
                <span class="ssh-profile-meta">${escHtml(p.username)}@${escHtml(p.host)}</span>
            </span>
            <button class="ssh-profile-btn ssh-profile-icon"
                    data-profile-toggle="${escHtml(p.name)}"
                    title="${state.activeSshProfile === p.name ? "Disconnect" : "Connect"}"
                    aria-label="${state.activeSshProfile === p.name ? "Disconnect" : "Connect"}">
                ${state.activeSshProfile === p.name ? "⏻" : "⏻"}
            </button>
            <button class="ssh-profile-btn ssh-profile-icon ssh-profile-edit"
                    data-profile-edit="${escHtml(p.name)}" title="Edit" aria-label="Edit">✎</button>
            <button class="ssh-profile-btn ssh-profile-icon ssh-profile-del"
                    data-profile-del="${escHtml(p.name)}" title="Delete" aria-label="Delete">🗑</button>
        `;
        ui.sshProfilesList.appendChild(row);
    });

    ui.sshProfilesList.querySelectorAll("[data-profile-toggle]").forEach((btn) => {
        btn.addEventListener("click", async () => {
            const name = btn.dataset.profileToggle;
            // If the user clicked the toggle for the currently-active profile
            // (or the global SSH is connected), disconnect.
            if (state.sshConnected) {
                _sshDisconnect();
                return;
            }
            // Otherwise fill the form from this profile and connect.
            let all; try { all = await api("/remote/ssh-profiles"); } catch (_) { return; }
            const p = all.find((x) => x.name === name); if (!p) return;
            _fillForm(p);
            state.activeSshProfile = p.name;
            await _sshConnect();
            // Re-render so the row gets the active highlight + Connect icon flips.
            await loadSshProfiles();
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
    // Apply the active highlight class to the matching row.  The class itself
    // is styled in styles.css; this function exists for callers that don't
    // want to re-render the whole list (e.g. focus changes).
    ui.sshProfilesList.querySelectorAll("[data-profile-row]").forEach((row) => {
        row.classList.toggle("ssh-profile-active", row.dataset.profileRow === name);
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
    _stopAgentHealthPoll();
    _stopAgentLog();
    if (ui.agentLog) { ui.agentLog.textContent = ""; ui.agentLog.hidden = true; }
}

// ---------- Agent heartbeat poll ----------

function _stopAgentHealthPoll() {
    if (state.agentHealthTimer) { clearInterval(state.agentHealthTimer); state.agentHealthTimer = null; }
}

function _startAgentHealthPoll(agentId) {
    _stopAgentHealthPoll();
    state.agentHealthTimer = setInterval(async () => {
        if (state.remoteAgentId !== agentId) { _stopAgentHealthPoll(); return; }
        let s;
        try {
            s = await api(`/remote/agent/${agentId}/status`);
        } catch (_) {
            return;
        }
        if (s.down) {
            _stopAgentHealthPoll();
            _onAgentDown(s.reason || "The remote agent went down.");
        }
    }, 5000);
}

function _onAgentDown(reason) {
    _stopCameraPoll();
    disconnectStream();
    if (state.session && state.session.state === "running" && state.tab === "remote") {
        setState("failed");
        setMiddleView("placeholder");
    }
    state.remoteAgentId = null;
    _showAgentPanel("agent-installed-panel");
    ui.hint.textContent = "⚠ Agent down: " + reason + " Re-enable to continue.";
    ui.hint.style.borderColor = "var(--danger)";
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
        state.activeSshProfile = null;
    } finally {
        ui.sshToggleConn.disabled = false;
        // Reflect the new connection state in the saved-connections list
        // (highlights the active row, swaps Connect/Disconnect icons).
        await loadSshProfiles();
    }
}

function _sshDisconnect() {
    _resetRemoteState();
    state.sshConnected = false;
    state.activeSshProfile = null;
    ui.sshConnectedPanel.hidden = true;
    _showAgentPanel(null);
    ui.sshToggleConn.textContent = "⇄ Connect";
    ui.hint.textContent = "Disconnected.";
    // Drop the active highlight + bandwidth display.
    _highlightProfile(null);
    _stopBandwidthMeter();
    loadSshProfiles();
}

// ---------- Agent install-state rendering ----------

function _renderAgentInstallState(check) {
    state.agentCheck = check;
    if (!check.installed) {
        _showAgentPanel("agent-not-installed-panel");
        ui.hint.textContent = "Agent not installed. Install it to continue.";
        return;
    }
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

ui.installAgentBtn.addEventListener("click", async () => {
    ui.installAgentBtn.disabled = true;
    ui.hint.textContent = "Installing agent …";
    const ok = await _runSseJob("/remote/ssh-install", ui.installLog);
    ui.installAgentBtn.disabled = false;
    if (ok) { await _recheckAgent(); }
    else { ui.hint.textContent = "Installation failed — see log above."; }
});

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
        ui.agentLog.textContent += "── live tail ──\n";
    } else {
        ui.agentLog.textContent += line + "\n";
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
    if (state.remoteAgentId) {
        try {
            await api("/remote/ssh-disable", {
                method: "POST", body: JSON.stringify({ agent_id: state.remoteAgentId }),
            });
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
    await _recheckAgent();
    ui.hint.textContent = "Agent disabled.";
}

ui.disableAgentBtn.addEventListener("click", _doDisableAgent);

// ---------- Remote camera list ----------

function _populateCameraSelect(cameras) {
    state.remoteCameras = cameras || [];
    // Same list into single + left + right.
    const fill = (sel, preferredId) => {
        sel.innerHTML = "";
        if (!cameras || cameras.length === 0) {
            const opt = document.createElement("option");
            opt.value = ""; opt.textContent = "No cameras found"; opt.disabled = true;
            sel.appendChild(opt);
            return false;
        }
        cameras.forEach((c) => {
            const opt = document.createElement("option");
            opt.value = c.id;
            opt.textContent = c.label || c.id;
            sel.appendChild(opt);
        });
        if (preferredId && cameras.some((c) => c.id === preferredId)) sel.value = preferredId;
        else sel.selectedIndex = 0;
        return true;
    };
    fill(ui.remoteCameraSelect, ui.remoteCameraSelect.value);
    fill(ui.remoteCameraLeft, ui.remoteCameraLeft.value);
    fill(ui.remoteCameraRight, ui.remoteCameraRight.value);
    // Make LEFT != RIGHT by default so the user doesn't accidentally bind the
    // same camera to both eyes.
    if (ui.remoteCameraLeft.value === ui.remoteCameraRight.value && state.remoteCameras.length >= 2) {
        ui.remoteCameraRight.selectedIndex = 1;
    }
    return cameras && cameras.length > 0;
}

async function _refreshRemoteCameras() {
    if (!state.remoteAgentId) return;
    ui.refreshRemoteCameras.disabled = true;
    ui.refreshRemoteCameras2.disabled = true;
    ui.hint.textContent = "Re-scanning remote cameras …";
    try {
        const r = await api(`/remote/agent/${state.remoteAgentId}/cameras?refresh=1`);
        const any = _populateCameraSelect(r.cameras);
        _applyCameraModeToUI();
        ui.hint.textContent = any
            ? "Camera list refreshed — select a camera, then press ▶ Start Capture."
            : "No cameras found on remote.";
    } catch (err) {
        ui.hint.textContent = "Refresh failed: " + err.message;
    } finally {
        ui.refreshRemoteCameras.disabled = false;
        ui.refreshRemoteCameras2.disabled = false;
    }
}

ui.refreshRemoteCameras.addEventListener("click", _refreshRemoteCameras);
ui.refreshRemoteCameras2.addEventListener("click", _refreshRemoteCameras);

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
                _applyCameraModeToUI();
                ui.hint.textContent = any
                    ? "Agent running — select a camera, then press ▶ Start Capture."
                    : "Agent connected — no cameras found on remote.";
                ui.hint.style.borderColor = "";
                _showAgentPanel("agent-running-panel");
                _startAgentHealthPoll(agentId);
                return;
            }
            if (r.pending && attempts % 5 === 1) {
                ui.hint.textContent =
                    `Agent process started (id ${agentId.slice(0, 8)}…) — ` +
                    "probing agent via SSH tunnel. " +
                    "Check the agent log below if this takes more than ~10 s.";
            }
        } catch (err) {
            if (attempts === 1 || attempts % 10 === 0) {
                ui.hint.textContent =
                    `Polling agent failed (${err.message}). ` +
                    "Check the agent log below.";
            }
        }
        if (attempts >= 60) {
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
    const chessboard = _currentChessboard();
    if (!state.selectedChessboard) {
        ui.hint.textContent = "Select a chessboard first (or create one in Manage …).";
        return;
    }
    let cameraId = null, cameraId2 = null;
    if (_isDualMode()) {
        cameraId = ui.remoteCameraLeft.value;
        cameraId2 = ui.remoteCameraRight.value;
        if (!cameraId || !cameraId2) {
            ui.hint.textContent = "Select LEFT and RIGHT cameras.";
            return;
        }
        if (cameraId === cameraId2) {
            ui.hint.textContent = "LEFT and RIGHT cameras must be different devices.";
            return;
        }
    } else {
        cameraId = ui.remoteCameraSelect.value;
        if (!cameraId) { ui.hint.textContent = "Select a remote camera first."; return; }
    }

    const explicitName = ui.sessionName.value.trim();
    const finalName = explicitName || _autoSessionName(_activeMode(), chessboard);

    let info;
    try {
        info = await api("/sessions", {
            method: "POST",
            body: JSON.stringify({
                name: finalName, source: "remote",
                camera_id: null, camera_id_2: null, chessboard,
            }),
        });
        state.session = info;
        info = await api(`/sessions/${state.session.id}/start`, { method: "POST" });
        state.session = info;
    } catch (err) {
        ui.hint.textContent = "Failed to create/start session: " + err.message;
        return;
    }

    try {
        await api(`/remote/agent/${state.remoteAgentId}/bind`, {
            method: "POST",
            body: JSON.stringify({
                session_id: state.session.id,
                camera_id: cameraId,
                camera_id_2: cameraId2,
            }),
        });
    } catch (err) {
        ui.hint.textContent = "Failed to bind camera: " + err.message;
        return;
    }

    resetCapturedStrip();
    setMiddleView("live");
    setState(info.state);
    ui.hint.textContent = `Remote calibration started — need ${info.required_captures} captures.`;
    connectStream();
    loadSessionList();
}
