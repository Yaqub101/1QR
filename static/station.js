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
  if (typeof module === "object" && module.exports) module.exports = { createStationScreen: factory(require("./station_logic.js")) };
  else root.createStationScreen = factory(root.StationLogic);
})(typeof self !== "undefined" ? self : this, function (Logic) {
  "use strict";

  const TEMPORARY = "One moment, please try again.";
  const IDLE_MESSAGE = "Scan a QR to begin.";
  const ENTER_CONFIRM_GUARD_MS = 500; // a scanner's second Enter must not confirm the card it just brought up

  function createStationScreen(deps) {
    const { els, doc, post, sound, now } = deps;
    const later = deps.setTimeout;
    const cancel = deps.clearTimeout || function () {};
    const resetMs = deps.resetMs != null ? deps.resetMs : 2500;
    const debouncer = Logic.createDebouncer({ windowMs: deps.debounceMs != null ? deps.debounceMs : 1500, now });

    let pending = null; // what /confirm will send: { kind: "token" | "student", value, step }
    let resetTimer = null;
    let readyAt = -Infinity;

    function focusScan() { els.scan.focus(); }

    function playSound(name) {
      if (!name) return;
      try { sound(name); } catch (_) { /* a blocked speaker must never break scanning */ }
    }

    function showBanner(colour, message) {
      els.banner.className = "banner " + colour;
      els.message.textContent = message;
    }

    function clearCard() {
      els.card.hidden = true;
      els.confirmBtn.hidden = true;
      els.cardName.textContent = "";
      els.cardFields.replaceChildren();
      pending = null;
    }

    function renderCard(student) {
      els.cardName.textContent = student.name;
      els.cardPhoto.setAttribute("src", student.photo_url);
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
      els.card.hidden = false;
    }

    function scheduleReset() {
      cancel(resetTimer);
      resetTimer = later(() => {
        clearCard();
        showBanner("blue", IDLE_MESSAGE);
        focusScan();
      }, resetMs);
    }

    function render(reply, tokenForPending) {
      const view = Logic.classifyResult(reply.result);
      showBanner(view.colour, reply.message || TEMPORARY);
      playSound(view.sound);
      if (reply.student) renderCard(reply.student);
      else clearCard();

      if (reply.result === "READY" && reply.student) {
        pending = reply.manual
          ? { kind: "student", value: reply.student.student_id }
          : { kind: "token", value: tokenForPending };
        // The Registry desk works out the step (entry / robe return) and names it; other screens send none.
        if (reply.step) pending.step = reply.step;
        if (reply.confirm_label) els.confirmBtn.textContent = reply.confirm_label;
        els.confirmBtn.hidden = false;
        readyAt = now();
      } else {
        pending = null;
        els.confirmBtn.hidden = true;
        scheduleReset(); // refusals and completions clear themselves so the next student starts clean
      }
      focusScan();
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
      const token = Logic.stripScannerSuffix(raw);
      els.scan.value = ""; // always leave the box empty for the next scan
      if (!token) { focusScan(); return; }
      if (!debouncer.accept(token)) { focusScan(); return; }
      await run(() => post("/scan", { token, activity: deps.activity }), token);
    }

    async function confirm() {
      if (!pending || debouncer.busy) return;
      const body = pending.kind === "token"
        ? { token: pending.value, activity: deps.activity }
        : { student_id: pending.value, activity: deps.activity };
      if (pending.step) body.step = pending.step;
      els.confirmBtn.disabled = true;
      try {
        await run(() => post("/confirm", body), null);
      } finally {
        els.confirmBtn.disabled = false;
      }
    }

    async function search() {
      const prn = Logic.stripScannerSuffix(els.searchInput.value);
      els.searchInput.value = "";
      if (!prn || debouncer.busy) { focusScan(); return; }
      await run(() => post("/search", { prn, activity: deps.activity }), null);
    }

    function init() {
      focusScan();
      showBanner("blue", IDLE_MESSAGE);
      els.scan.addEventListener("keydown", (event) => {
        if (event.key !== "Enter") return;
        event.preventDefault();
        if (Logic.stripScannerSuffix(els.scan.value)) submitScan(els.scan.value);
        else if (now() - readyAt > ENTER_CONFIRM_GUARD_MS) confirm(); // keyboard-only: Enter confirms the card
      });
      // Some scanners put the newline INTO the box instead of sending an Enter key event.
      els.scan.addEventListener("input", () => {
        if (/[\r\n]/.test(els.scan.value)) submitScan(els.scan.value);
      });
      els.confirmBtn.addEventListener("click", confirm);
      els.searchBtn.addEventListener("click", search);
      els.searchInput.addEventListener("keydown", (event) => {
        if (event.key === "Enter") { event.preventDefault(); search(); }
      });
    }

    return { init, submitScan, confirm, search, focusScan };
  }

  return createStationScreen;
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
  };

  async function post(url, body) {
    const response = await fetch(url, {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    let data = null;
    try { data = await response.json(); } catch (_) { /* not JSON */ }
    if (response.status === 401) { window.location.href = "/login"; }
    if (response.ok && data) return data;
    const detail = data && data.detail;
    return { result: "ERROR", message: (detail && detail.message) || "One moment, please try again.", student: null };
  }

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

  const screen = window.createStationScreen({
    els, doc: document, post, sound, now: Date.now,
    setTimeout: window.setTimeout.bind(window), clearTimeout: window.clearTimeout.bind(window),
    activity: rootEl.dataset.activity,
  });
  screen.init();

  // Keep the scan box focused whatever the operator touches.
  document.addEventListener("click", (event) => {
    if (!event.target.closest("input, select, button, summary, a")) screen.focusScan();
  });
  window.addEventListener("focus", () => screen.focusScan());
  document.addEventListener("visibilitychange", () => { if (!document.hidden) screen.focusScan(); });

  // Camera-based scanning: a decoded QR is handed to the SAME submitScan() the manual scan box uses,
  // so it goes through the identical /scan -> render -> /confirm path (no second code path to the server).
  const cameraDetails = byId("camera-details");
  if (cameraDetails && window.CameraScan) {
    const cameraStatus = byId("camera-status");
    const cameraVideo = byId("camera-video");
    const cameraCanvas = byId("camera-canvas");
    const IDLE_STATUS = "Point the camera at the student's QR code.";

    let BarcodeDetectorCtor = null;
    if (window.BarcodeDetector) {
      try { BarcodeDetectorCtor = window.BarcodeDetector; } catch (_) { BarcodeDetectorCtor = null; }
    }

    const scanner = window.CameraScan.createCameraScanner({
      video: cameraVideo, canvas: cameraCanvas,
      mediaDevices: navigator.mediaDevices,
      BarcodeDetectorCtor, jsQR: BarcodeDetectorCtor ? null : window.jsQR || null,
      now: Date.now, schedule: window.setTimeout.bind(window), cancelSchedule: window.clearTimeout.bind(window),
      onDecode: (text) => { screen.submitScan(text); },
      onError: (message) => { cameraStatus.textContent = message; }, // stays open: the message is inside it
    });

    if (!scanner.isSupported()) {
      cameraStatus.textContent = "Camera scanning is not supported on this browser — use PRN search instead.";
    } else {
      cameraDetails.addEventListener("toggle", () => {
        if (cameraDetails.open) { cameraStatus.textContent = IDLE_STATUS; scanner.start(); }
        else scanner.stop();
      });
      window.addEventListener("pagehide", () => scanner.stop());
      document.addEventListener("visibilitychange", () => { if (document.hidden) scanner.stop(); else if (cameraDetails.open) scanner.start(); });
    }
  }
})();
