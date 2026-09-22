/* Camera-based QR scanning for the operator screen (station.js).
 *
 * createCameraScanner(deps) holds all the behaviour and touches the camera/DOM only through `deps`, so
 * it is unit-tested under node with fakes (tests/js/camera_scan.test.js), the same way station.js is.
 * It does NOT talk to the server: a successful decode is handed to `deps.onDecode(text)`, which the
 * caller (station.js) wires to the exact same `submitScan()` the manual scan box uses -- there is no
 * second path to /scan.
 *
 * Decoding prefers the browser's native BarcodeDetector; where that is unavailable it falls back to the
 * vendored jsQR library (static/jsqr.min.js), fed frames drawn onto a canvas.
 */
(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.CameraScan = factory();
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  const PERMISSION_DENIED = "Camera permission was denied — use PRN search instead.";
  const NO_CAMERA = "No camera was found on this device — use PRN search instead.";
  const START_FAILED = "Camera could not start — use PRN search instead.";

  // getUserMedia rejects with a DOMException whose `name` says why; browsers disagree on the exact
  // name for older cases, so a few aliases are mapped to the same one plain sentence.
  function classifyCameraError(err) {
    const name = (err && err.name) || "";
    if (name === "NotAllowedError" || name === "PermissionDeniedError" || name === "SecurityError") return PERMISSION_DENIED;
    if (name === "NotFoundError" || name === "DevicesNotFoundError" || name === "OverconstrainedError") return NO_CAMERA;
    return START_FAILED;
  }

  function createCameraScanner(deps) {
    const intervalMs = deps.intervalMs != null ? deps.intervalMs : 200; // ~5 decode attempts/second: plenty for a still QR, easy on battery
    const cooldownMs = deps.cooldownMs != null ? deps.cooldownMs : 2000; // give the operator a moment before the same frame decodes again
    const now = deps.now || Date.now;
    const schedule = deps.schedule; // (fn, ms) -> handle
    const cancelSchedule = deps.cancelSchedule || function () {};

    let stream = null;
    let detector = null; // BarcodeDetector instance, if used
    let running = false;
    let timer = null;
    let lastDecodedAt = -Infinity;

    function hasGetUserMedia() {
      return !!(deps.mediaDevices && typeof deps.mediaDevices.getUserMedia === "function");
    }

    function canDecode() {
      return !!(deps.BarcodeDetectorCtor || deps.jsQR);
    }

    function isSupported() {
      return hasGetUserMedia() && canDecode();
    }

    async function decodeOnce() {
      if (deps.BarcodeDetectorCtor) {
        if (!detector) detector = new deps.BarcodeDetectorCtor({ formats: ["qr_code"] });
        const found = await detector.detect(deps.video);
        return (found && found[0] && found[0].rawValue) || null;
      }
      if (deps.jsQR) {
        const w = deps.video.videoWidth, h = deps.video.videoHeight;
        if (!w || !h) return null; // the stream hasn't produced a frame yet
        const ctx = deps.canvas.getContext("2d");
        deps.canvas.width = w;
        deps.canvas.height = h;
        ctx.drawImage(deps.video, 0, 0, w, h);
        const frame = ctx.getImageData(0, 0, w, h);
        const code = deps.jsQR(frame.data, w, h);
        return (code && code.data) || null;
      }
      return null;
    }

    function tick() {
      if (!running) return;
      const proceed = now() - lastDecodedAt >= cooldownMs ? decodeOnce() : Promise.resolve(null);
      Promise.resolve(proceed)
        .then((text) => { if (text) { lastDecodedAt = now(); deps.onDecode(text); } })
        .catch(() => {}) // a single bad frame must never stop scanning or surface a raw error
        .finally(() => { if (running) timer = schedule(tick, intervalMs); });
    }

    async function start() {
      if (!hasGetUserMedia()) { deps.onError(NO_CAMERA); return false; }
      if (!canDecode()) { deps.onError(START_FAILED); return false; }
      try {
        // `ideal`, never `exact`: a laptop webcam has no "environment" camera at all, and an exact
        // constraint would simply fail to start rather than falling back to whatever camera exists.
        stream = await deps.mediaDevices.getUserMedia({ video: { facingMode: { ideal: "environment" } }, audio: false });
      } catch (err) {
        deps.onError(classifyCameraError(err));
        return false;
      }
      deps.video.srcObject = stream;
      try { await deps.video.play(); } catch (_) { /* playsinline+muted already covers autoplay in real browsers */ }
      detector = null;
      running = true;
      lastDecodedAt = -Infinity;
      timer = schedule(tick, intervalMs);
      return true;
    }

    function stop() {
      running = false;
      if (timer != null) { cancelSchedule(timer); timer = null; }
      if (stream) { stream.getTracks().forEach((t) => t.stop()); stream = null; }
      if (deps.video) deps.video.srcObject = null;
      detector = null;
    }

    return { start, stop, isSupported, get running() { return running; } };
  }

  return { createCameraScanner, classifyCameraError, PERMISSION_DENIED, NO_CAMERA, START_FAILED };
});
