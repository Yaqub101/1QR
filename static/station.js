/* Operator screen: SCAN -> VERIFY -> CONFIRM (SYSTEM_SPEC section 13).
 *
 * createStationScreen(deps) holds all the behaviour and touches the page only through `deps`, so it is
 * unit-tested under node with a fake DOM (tests/js/station.test.js). The bottom of the file wires it to
 * the real page. Rules it keeps:
 *   - the scan box is focused on load and again after EVERY action;
 *   - the scanner's trailing Enter/newline is stripped; double scans and double clicks make ONE request;
 *   - green = done, amber = already done, red = cannot proceed, each with a short sound;
 *   - the operator only ever reads the server's plain sentence, or "One moment, please try again.".
 */
(function (root, factory) {
  const exportsObj = factory(typeof require === "function" ? require("./station_logic.js") : root.StationLogic);
  if (typeof module === "object" && module.exports) module.exports = exportsObj;
  else {
    root.createStationScreen = exportsObj.createStationScreen;
    root.extractErrorMessage = exportsObj.extractErrorMessage;
    root.stationPost = exportsObj.post;
  }
})(typeof self !== "undefined" ? self : this, function (Logic) {
  "use strict";

  const TEMPORARY = "One moment, please try again.";
  const IDLE_MESSAGE = "Scan a QR to begin.";
  const ENTER_CONFIRM_GUARD_MS = 500; // a scanner's second Enter must not confirm the card it just brought up

  function extractErrorMessage(data) {
    const detail = data && data.detail;
    if (detail && typeof detail.message === "string" && detail.message.trim()) {
      return detail.message;
    }
    if (typeof detail === "string" && detail.trim()) {
      return detail;
    }
    if (Array.isArray(detail) && detail.length > 0 && detail[0] && typeof detail[0].msg === "string" && detail[0].msg.trim()) {
      return detail[0].msg;
    }
    return TEMPORARY;
  }

  async function post(url, body, fetchImpl) {
    const _fetch = fetchImpl || (typeof fetch !== "undefined" ? fetch : null);
    const response = await _fetch(url, {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    let data = null;
    try { data = await response.json(); } catch (_) { /* not JSON */ }
    if (response.status === 401) {
      if (typeof window !== "undefined" && window.location) {
        window.location.href = "/login";
      }
    }
    if (response.ok && data) return data;
    return { result: "ERROR", message: extractErrorMessage(data), student: null };
  }

  function createStationScreen(deps) {
    const { els, doc, post, sound, now } = deps;
    const later = deps.setTimeout;
    const cancel = deps.clearTimeout || function () {};
    const resetMs = deps.resetMs != null ? deps.resetMs : 2500;
    const debouncer = Logic.createDebouncer({ windowMs: deps.debounceMs != null ? deps.debounceMs : 1500, now });

    let pending = null; // what /confirm will send: { kind: "token" | "student", value, step, needsTick }
    const TICK_ONE = "Tick at least one box, then confirm.";
    let resetTimer = null;
    let readyAt = -Infinity;
    let currentStudent = null;

    const scannerBuffer = Logic.createScannerBuffer({
      maxBurstGapMs: deps.maxBurstGapMs != null ? deps.maxBurstGapMs : 60,
      now,
      onScan: (scanned) => submitScan(scanned),
      onConfirm: () => {
        if (now() - readyAt > ENTER_CONFIRM_GUARD_MS) confirm();
      },
    });

    function playSound(name) {
      if (!name) return;
      try { sound(name); } catch (_) { /* a blocked speaker must never break scanning */ }
    }

    function showBanner(colour, message) {
      els.banner.className = "banner " + colour;
      els.message.textContent = message;
    }

    // The Registry desk's tick boxes: one per marker the server sends; done ones are ticked and locked.
    function renderMarkers(markers) {
      if (!els.markers) return;
      const list = Array.isArray(markers) ? markers : [];
      els.markers.replaceChildren(...list.map((m) => {
        const row = doc.createElement("label");
        row.className = "marker" + (m.done ? " done" : "");
        const box = doc.createElement("input");
        box.type = "checkbox";
        box.value = m.key;
        box.checked = !!m.done;
        box.disabled = !!m.done;
        const text = doc.createElement("span");
        text.textContent = m.done && m.time ? `${m.label} — done ${m.time}` : m.label;
        row.appendChild(box);
        row.appendChild(text);
        return row;
      }));
      els.markers.hidden = list.length === 0;
    }

    function tickedMarks() {
      if (!els.markers) return [];
      return Array.from(els.markers.children)  // a real page gives an HTMLCollection, not an array
        .map((row) => row.children[0])
        .filter((box) => box && box.checked && !box.disabled)
        .map((box) => box.value);
    }

    function clearCard() {
      currentStudent = null;
      if (els.manualDetails) els.manualDetails.open = false;
      if (els.searchInput) {
        els.searchInput.value = "";
        if (doc && doc.activeElement === els.searchInput && typeof els.searchInput.blur === "function") {
          els.searchInput.blur();
        }
      }
      els.card.hidden = true;
      els.confirmBtn.hidden = true;
      if (els.cardState) els.cardState.textContent = "";
      renderMarkers([]);
      els.cardName.textContent = "";
      els.cardFields.replaceChildren();
      if (els.cardPhoto && els.cardPhoto.setAttribute) els.cardPhoto.setAttribute("src", "/static/placeholder.svg");
      if (els.operatorStationActions) els.operatorStationActions.hidden = true;
      if (els.reissuePassPanel) els.reissuePassPanel.hidden = true;
      if (els.reissueSuccessBox) els.reissueSuccessBox.hidden = true;
      if (els.reissueReasonInput) els.reissueReasonInput.value = "";
      pending = null;
    }

    function renderCard(student) {
      currentStudent = student;
      els.cardName.textContent = student.name;
      els.cardPhoto.setAttribute("src", student.photo_url || "/static/placeholder.svg");
      els.cardFields.replaceChildren(
        ...student.fields.map((f) => {
          const row = doc.createElement("div");
          row.className = "field";
          const label = doc.createElement("dt");
          label.textContent = f.label;
          const value = doc.createElement("dd");
          value.textContent = f.value == null ? "" : String(f.value);
          row.appendChild(label);
          row.appendChild(value);
          return row;
        })
      );
      if (els.operatorStationActions) els.operatorStationActions.hidden = false;
      if (els.reissuePassPanel) els.reissuePassPanel.hidden = true;
      if (els.reissueSuccessBox) els.reissueSuccessBox.hidden = true;
      if (els.reissueReasonInput) els.reissueReasonInput.value = "";
      els.card.hidden = false;
    }

    function scheduleReset() {
      cancel(resetTimer);
      resetTimer = later(() => {
        clearCard();
        showBanner("blue", IDLE_MESSAGE);
      }, resetMs);
    }

    function render(reply, tokenForPending) {
      const view = Logic.classifyResult(reply.result);
      showBanner(view.colour, reply.message || TEMPORARY);
      playSound(view.sound);
      if (reply.student) renderCard(reply.student);
      else clearCard();
      if (reply.student && els.cardState) els.cardState.textContent = reply.state || "";
      if (reply.student && reply.markers) renderMarkers(reply.markers);

      if (reply.result === "READY" && reply.student) {
        pending = reply.manual
          ? { kind: "student", value: reply.student.student_id }
          : { kind: "token", value: tokenForPending };
        // The Registry desk works out the step (entry / robe return) and names it; other screens send none.
        if (reply.step) { pending.step = reply.step; pending.needsTick = !!reply.needs_tick; }
        if (reply.confirm_label) els.confirmBtn.textContent = reply.confirm_label;
        els.confirmBtn.hidden = false;
        readyAt = now();
      } else {
        if (reply.result === "CONFIRMED" && reply.student && els.lastScanSection) {
          if (els.lastScanName) els.lastScanName.textContent = reply.student.name;
          if (els.lastScanPhoto) els.lastScanPhoto.setAttribute("src", reply.student.photo_url || "/static/placeholder.svg");
          if (els.lastScanDetails) {
            const prnField = (reply.student.fields || []).find((f) => f.key === "prn");
            const progField = (reply.student.fields || []).find((f) => f.key === "programme");
            const prnVal = prnField ? `PRN: ${prnField.value}` : "";
            const progVal = progField ? progField.value : "";
            els.lastScanDetails.textContent = [prnVal, progVal].filter(Boolean).join(" · ");
          }
          if (els.lastScanBadge) els.lastScanBadge.textContent = "✓ " + (reply.message || "Confirmed");
          if (els.lastScanTime) {
            const d = new Date();
            els.lastScanTime.textContent = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
          }
          els.lastScanSection.hidden = false;
        }
        pending = null;
        els.confirmBtn.hidden = true;
        scheduleReset(); // refusals and completions clear themselves so the next student starts clean
      }
    }

    async function run(request, tokenForPending) {
      cancel(resetTimer);
      pending = null;
      debouncer.begin();
      let reply;
      try {
        reply = await request();
      } catch (_) {
        reply = { result: "ERROR", message: TEMPORARY, student: null };
      } finally {
        debouncer.end();
      }
      render(reply, tokenForPending);
    }

    async function submitScan(raw) {
      if (els.manualDetails) els.manualDetails.open = false;
      if (els.searchInput && doc && doc.activeElement === els.searchInput && typeof els.searchInput.blur === "function") {
        els.searchInput.blur();
      }
      const token = Logic.stripScannerSuffix(raw);
      if (els.scan) els.scan.value = ""; // clear legacy box if present
      if (!token) return;
      if (!debouncer.accept(token)) return;
      await run(() => post("/scan", { token, activity: deps.activity }), token);
    }

    async function confirm() {
      if (!pending || debouncer.busy) return;
      const body = pending.kind === "token"
        ? { token: pending.value, activity: deps.activity }
        : { student_id: pending.value, activity: deps.activity };
      if (pending.step) {
        body.step = pending.step;
        body.marks = tickedMarks();
        if (pending.needsTick && body.marks.length === 0) {  // say it here; no request, the card stays up
          showBanner("red", TICK_ONE);
          playSound("rejected");
          return;
        }
      }
      els.confirmBtn.disabled = true;
      try {
        await run(() => post("/confirm", body), null);
      } finally {
        els.confirmBtn.disabled = false;
      }
    }

    async function search() {
      const prn = Logic.stripScannerSuffix(els.searchInput ? els.searchInput.value : "");
      if (els.manualDetails) els.manualDetails.open = false;
      if (els.searchInput) {
        if (typeof els.searchInput.blur === "function") els.searchInput.blur();
        els.searchInput.value = "";
      }
      if (!prn || debouncer.busy) return;
      await run(() => post("/search", { prn, activity: deps.activity }), null);
    }

    const triggerDownload = deps.triggerDownload || function (url) {
      if (typeof window !== "undefined") {
        const a = doc.createElement("a");
        a.href = url;
        a.setAttribute("download", "");
        if (doc.body && typeof doc.body.appendChild === "function") {
          doc.body.appendChild(a);
          a.click();
          a.remove();
        } else if (typeof window.location !== "undefined") {
          window.location.href = url;
        }
      }
    };

    function handleDownloadPass() {
      if (!currentStudent || !currentStudent.student_id) return;
      triggerDownload(`/station/api/pass/${currentStudent.student_id}`);
    }

    function handleToggleReissue() {
      if (!els.reissuePassPanel) return;
      const willShow = els.reissuePassPanel.hidden;
      els.reissuePassPanel.hidden = !willShow;
      if (willShow && currentStudent) {
        if (els.reissueStudentPhoto && els.reissueStudentPhoto.setAttribute) {
          els.reissueStudentPhoto.setAttribute("src", currentStudent.photo_url || "/static/placeholder.svg");
        }
        if (els.reissueStudentName) {
          els.reissueStudentName.textContent = currentStudent.name || "";
        }
        if (els.reissueStudentPrn) {
          const prnField = (currentStudent.fields || []).find((f) => f.key === "prn");
          els.reissueStudentPrn.textContent = prnField ? `PRN: ${prnField.value}` : "";
        }
        if (els.reissueReasonInput) {
          els.reissueReasonInput.value = "";
        }
      }
    }

    function handleCancelReissue() {
      if (els.reissuePassPanel) els.reissuePassPanel.hidden = true;
      if (els.reissueReasonInput) els.reissueReasonInput.value = "";
    }

    async function handleConfirmReissue() {
      if (!currentStudent || !currentStudent.student_id || debouncer.busy) return;
      const reason = (els.reissueReasonInput ? els.reissueReasonInput.value : "").trim();
      if (!reason) {
        showBanner("red", "Please give a reason for reissuing this QR.");
        playSound("rejected");
        return;
      }
      if (els.reissueConfirmBtn) els.reissueConfirmBtn.disabled = true;
      try {
        const reply = await post("/station/api/reissue-pass", {
          student_id: currentStudent.student_id,
          reason,
        });
        if (reply && reply.ok) {
          if (els.reissuePassPanel) els.reissuePassPanel.hidden = true;
          if (els.reissueReasonInput) els.reissueReasonInput.value = "";
          showBanner("green", "New QR pass issued. Previous QR invalidated.");
          playSound("success");

          if (els.reissueSuccessBox) {
            const passUrl = reply.pass_url || `/station/api/pass/${currentStudent.student_id}`;
            if (els.downloadNewPassBtn && els.downloadNewPassBtn.setAttribute) {
              els.downloadNewPassBtn.setAttribute("href", passUrl);
            }
            els.reissueSuccessBox.hidden = false;
          }
        } else {
          const errMsg = (reply && reply.message) || "Could not reissue QR pass.";
          showBanner("red", errMsg);
          playSound("rejected");
        }
      } catch (_) {
        showBanner("red", TEMPORARY);
        playSound("rejected");
      } finally {
        if (els.reissueConfirmBtn) els.reissueConfirmBtn.disabled = false;
      }
    }

    function init() {
      showBanner("blue", IDLE_MESSAGE);
      if (doc && typeof doc.addEventListener === "function") {
        doc.addEventListener("keydown", scannerBuffer.handleKeydown);
      }
      if (els.confirmBtn) els.confirmBtn.addEventListener("click", confirm);
      if (els.searchBtn) els.searchBtn.addEventListener("click", search);
      if (els.downloadPassBtn) els.downloadPassBtn.addEventListener("click", handleDownloadPass);
      if (els.reissuePassToggleBtn) els.reissuePassToggleBtn.addEventListener("click", handleToggleReissue);
      if (els.reissueCancelBtn) els.reissueCancelBtn.addEventListener("click", handleCancelReissue);
      if (els.reissueConfirmBtn) els.reissueConfirmBtn.addEventListener("click", handleConfirmReissue);
      if (els.downloadNewPassBtn) {
        els.downloadNewPassBtn.addEventListener("click", (evt) => {
          if (currentStudent && currentStudent.student_id) {
            triggerDownload(`/station/api/pass/${currentStudent.student_id}`);
          }
        });
      }
      if (els.reissueReasonInput) {
        els.reissueReasonInput.addEventListener("keydown", (event) => {
          if (event.key === "Enter") {
            event.preventDefault();
            handleConfirmReissue();
          }
        });
      }
      if (els.searchInput) {
        els.searchInput.addEventListener("keydown", (event) => {
          if (event.key === "Enter") { event.preventDefault(); search(); }
        });
      }
      if (els.scan) {
        els.scan.addEventListener("keydown", (event) => {
          if (event.key !== "Enter") return;
          event.preventDefault();
          if (Logic.stripScannerSuffix(els.scan.value)) submitScan(els.scan.value);
          else if (now() - readyAt > ENTER_CONFIRM_GUARD_MS) confirm();
        });
        els.scan.addEventListener("input", () => {
          if (/[\r\n]/.test(els.scan.value)) submitScan(els.scan.value);
        });
      }
    }

    return {
      init, submitScan, confirm, search, scannerBuffer,
      triggerDownload, handleDownloadPass, handleToggleReissue, handleCancelReissue, handleConfirmReissue,
      get currentStudent() { return currentStudent; },
    };
  }

  return { createStationScreen, extractErrorMessage, post };
});

/* ---- browser wiring (skipped under node) ------------------------------------------------------- */
(function () {
  if (typeof document === "undefined" || typeof window === "undefined") return;
  const rootEl = document.getElementById("station-root");
  if (!rootEl) return;

  const byId = (id) => document.getElementById(id);
  const els = {
    scan: byId("scan"), banner: byId("banner"), message: byId("message"), card: byId("card"),
    cardName: byId("card-name"), cardPhoto: byId("card-photo"), cardFields: byId("card-fields"),
    confirmBtn: byId("confirm"), searchInput: byId("search-prn"), searchBtn: byId("search-btn"),
    markers: byId("markers"), cardState: byId("card-state"),
    manualDetails: byId("manual-details"),
    operatorStationActions: byId("operator-station-actions"),
    downloadPassBtn: byId("download-pass-btn"),
    reissuePassToggleBtn: byId("reissue-pass-toggle-btn"),
    reissuePassPanel: byId("reissue-pass-panel"),
    reissueStudentPhoto: byId("reissue-student-photo"),
    reissueStudentName: byId("reissue-student-name"),
    reissueStudentPrn: byId("reissue-student-prn"),
    reissueReasonInput: byId("reissue-reason-input"),
    reissueConfirmBtn: byId("reissue-confirm-btn"),
    reissueCancelBtn: byId("reissue-cancel-btn"),
    reissueSuccessBox: byId("reissue-success-box"),
    downloadNewPassBtn: byId("download-new-pass-btn"),
    lastScanSection: byId("last-scan-section"), lastScanName: byId("last-scan-name"),
    lastScanPhoto: byId("last-scan-photo"), lastScanDetails: byId("last-scan-details"),
    lastScanBadge: byId("last-scan-badge"), lastScanTime: byId("last-scan-time"),
  };

  const post = window.stationPost || (async function (url, body) {
    const response = await fetch(url, {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    let data = null;
    try { data = await response.json(); } catch (_) { /* not JSON */ }
    if (response.status === 401) { window.location.href = "/login"; }
    if (response.ok && data) return data;
    const detail = data && data.detail;
    const message = (window.extractErrorMessage ? window.extractErrorMessage(data) : ((detail && detail.message) || "One moment, please try again."));
    return { result: "ERROR", message, student: null };
  });

  // Short cues made with the browser's own synthesiser: nothing to download, works offline.
  let audio = null;
  function tone(freq, start, length, type) {
    const osc = audio.createOscillator();
    const gain = audio.createGain();
    osc.type = type; osc.frequency.value = freq;
    gain.gain.setValueAtTime(0.18, audio.currentTime + start);
    gain.gain.exponentialRampToValueAtTime(0.001, audio.currentTime + start + length);
    osc.connect(gain); gain.connect(audio.destination);
    osc.start(audio.currentTime + start); osc.stop(audio.currentTime + start + length + 0.02);
  }
  function sound(name) {
    const Ctx = window.AudioContext || window.webkitAudioContext;
    if (!Ctx) return;
    audio = audio || new Ctx();
    if (audio.state === "suspended") audio.resume();
    if (name === "success") { tone(880, 0, 0.12, "sine"); tone(1175, 0.13, 0.16, "sine"); }
    else if (name === "duplicate") { tone(520, 0, 0.14, "triangle"); tone(520, 0.22, 0.14, "triangle"); }
    else { tone(170, 0, 0.42, "sawtooth"); }
  }

  const screen = (typeof createStationScreen === "function" ? createStationScreen : window.createStationScreen)({
    els, doc: document, post, sound, now: Date.now,
    setTimeout: window.setTimeout.bind(window), clearTimeout: window.clearTimeout.bind(window),
    activity: rootEl.dataset.activity,
  });
  screen.init();

  // Camera-based scanning: battery-conscious lifecycle with inactivity timeout & tap-to-resume
  const cameraVideo = byId("camera-video");
  const cameraCanvas = byId("camera-canvas");
  const cameraStatus = byId("camera-status");
  const modeBadge = byId("scanner-mode-badge");
  const pauseOverlay = byId("camera-pause-overlay");
  const resumeBtn = byId("resume-scan-btn");
  const permOverlay = byId("camera-permission-overlay");
  const enableBtn = byId("enable-camera-btn");
  const permMsg = byId("camera-perm-msg");
  const cameraDetails = byId("camera-details");

  if (cameraVideo && window.CameraScan) {
    const SCANNER_IDLE_TIMEOUT = 60000; // 60 seconds inactivity timeout to save battery
    let lastActiveAt = Date.now();
    let isPaused = false;

    function setScannerStatus(mode, message) {
      if (modeBadge) {
        modeBadge.className = "scanner-mode-badge " + mode.toLowerCase();
        modeBadge.textContent = mode.toUpperCase();
      }
      if (cameraStatus && message) {
        cameraStatus.textContent = message;
      }
    }

    function recordActivity() {
      lastActiveAt = Date.now();
    }

    ["touchstart", "mousedown", "mousemove", "keydown", "click", "scroll"].forEach((evt) => {
      document.addEventListener(evt, recordActivity, { passive: true });
    });

    let BarcodeDetectorCtor = null;
    if (window.BarcodeDetector) {
      try { BarcodeDetectorCtor = window.BarcodeDetector; } catch (_) { BarcodeDetectorCtor = null; }
    }

    const scanner = window.CameraScan.createCameraScanner({
      video: cameraVideo, canvas: cameraCanvas,
      mediaDevices: navigator.mediaDevices,
      BarcodeDetectorCtor, jsQR: BarcodeDetectorCtor ? null : window.jsQR || null,
      now: Date.now, schedule: window.setTimeout.bind(window), cancelSchedule: window.clearTimeout.bind(window),
      onDecode: (text) => {
        recordActivity();
        setScannerStatus("PROCESSING", "Verifying student pass…");
        screen.submitScan(text);
        setTimeout(() => {
          if (!isPaused && scanner.running) {
            setScannerStatus("READY", "Point camera at the student's QR code.");
          }
        }, 1200);
      },
      onError: (message) => {
        setScannerStatus("ERROR", message);
        if (permOverlay && permMsg) {
          permMsg.textContent = message;
          permOverlay.hidden = false;
        }
      },
    });

    async function startScanner() {
      if (!scanner.isSupported()) {
        if (cameraStatus) cameraStatus.textContent = "Camera scanning is not supported on this browser — use PRN search instead.";
        return;
      }
      isPaused = false;
      if (pauseOverlay) pauseOverlay.hidden = true;
      if (permOverlay) permOverlay.hidden = true;
      const ok = await scanner.start();
      if (ok) {
        setScannerStatus("READY", "Point camera at the student's QR code.");
      } else {
        if (permOverlay) permOverlay.hidden = false;
      }
    }

    function pauseScanner() {
      if (scanner.running) {
        scanner.stop();
        isPaused = true;
        if (pauseOverlay) pauseOverlay.hidden = false;
        setScannerStatus("PAUSED", "Scanner paused to save battery.");
      }
    }

    // Inactivity watchdog: pause camera after 60 seconds without interaction
    setInterval(() => {
      if (!isPaused && scanner.running && Date.now() - lastActiveAt >= SCANNER_IDLE_TIMEOUT) {
        pauseScanner();
      }
    }, 2500);

    if (resumeBtn) resumeBtn.addEventListener("click", () => startScanner());
    if (pauseOverlay) pauseOverlay.addEventListener("click", () => startScanner());
    if (enableBtn) enableBtn.addEventListener("click", () => startScanner());

    window.addEventListener("pagehide", () => scanner.stop());
    document.addEventListener("visibilitychange", () => {
      if (document.hidden) {
        pauseScanner();
      }
    });

    if (cameraDetails) {
      cameraDetails.addEventListener("toggle", () => {
        if (cameraDetails.open) startScanner();
        else pauseScanner();
      });
    }

    // Auto-activate scanner when station loads
    startScanner();
  }
})();
