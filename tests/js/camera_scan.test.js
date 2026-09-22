// Camera-based QR scanning logic, run with `node --test tests/js/camera_scan.test.js` (also run by
// tests/test_station_engine.py, which globs tests/js/*.test.js). Fakes stand in for getUserMedia, the
// video/canvas elements and the two decoders (BarcodeDetector, jsQR) -- there is no real camera or
// browser in the test run. What matters: getUserMedia is asked for an IDEAL (not exact) environment
// camera so a laptop webcam still works, permission/no-camera failures map to one plain sentence each,
// a decode is only handed to onDecode once per cooldown window, a bad frame never stops scanning, and
// stop() releases every camera track.
const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");

const CameraScan = require(path.join(__dirname, "..", "..", "static", "camera_scan.js"));

const flush = () => new Promise((resolve) => setImmediate(resolve));

function harness(overrides = {}) {
  const timers = [];
  const cancelled = new Set();
  let nextId = 1;
  const schedule = (fn, ms) => { const id = nextId++; timers.push({ id, fn, ms }); return id; };
  const cancelSchedule = (id) => cancelled.add(id);
  const runDue = async () => {
    const due = timers.splice(0);
    due.forEach((t) => { if (!cancelled.has(t.id)) t.fn(); });
    await flush();
  };

  const video = { srcObject: null, videoWidth: 640, videoHeight: 480, play: async () => {} };
  const canvas = { width: 0, height: 0, getContext: () => ({ drawImage: () => {}, getImageData: () => ({ data: new Uint8ClampedArray(4) }) }) };

  let clock = 1000;
  const errors = [];
  const decodes = [];
  const deps = {
    video, canvas, now: () => clock, schedule, cancelSchedule,
    onDecode: (t) => decodes.push(t), onError: (m) => errors.push(m),
    intervalMs: 200, cooldownMs: 2000,
    ...overrides,
  };
  const scanner = CameraScan.createCameraScanner(deps);
  return { scanner, deps, timers, runDue, errors, decodes, advance: (ms) => { clock += ms; } };
}

test("isSupported requires BOTH getUserMedia and a decoder (BarcodeDetector or jsQR)", () => {
  assert.equal(harness({ mediaDevices: {} }).scanner.isSupported(), false);
  assert.equal(harness({ mediaDevices: { getUserMedia: async () => {} } }).scanner.isSupported(), false);
  assert.equal(harness({ mediaDevices: { getUserMedia: async () => {} }, BarcodeDetectorCtor: function () {} }).scanner.isSupported(), true);
  assert.equal(harness({ mediaDevices: { getUserMedia: async () => {} }, jsQR: () => null }).scanner.isSupported(), true);
});

test("classifyCameraError maps every DOMException name to one plain sentence, never the raw error", () => {
  const name = (n) => CameraScan.classifyCameraError({ name: n });
  assert.equal(name("NotAllowedError"), CameraScan.PERMISSION_DENIED);
  assert.equal(name("PermissionDeniedError"), CameraScan.PERMISSION_DENIED);
  assert.equal(name("SecurityError"), CameraScan.PERMISSION_DENIED);
  assert.equal(name("NotFoundError"), CameraScan.NO_CAMERA);
  assert.equal(name("DevicesNotFoundError"), CameraScan.NO_CAMERA);
  assert.equal(name("OverconstrainedError"), CameraScan.NO_CAMERA);
  assert.equal(name("NotReadableError"), CameraScan.START_FAILED);
  assert.equal(name(undefined), CameraScan.START_FAILED);
});

test("start() without getUserMedia reports 'no camera' and never touches mediaDevices", async () => {
  const h = harness({ mediaDevices: {} });
  const ok = await h.scanner.start();
  assert.equal(ok, false);
  assert.deepEqual(h.errors, [CameraScan.NO_CAMERA]);
});

test("start() asks for an IDEAL environment camera, never exact -- a laptop with only a front camera must still work", async () => {
  let constraints = null;
  const stream = { getTracks: () => [] };
  const h = harness({
    mediaDevices: { getUserMedia: async (c) => { constraints = c; return stream; } },
    BarcodeDetectorCtor: function () { this.detect = async () => []; },
  });
  const ok = await h.scanner.start();
  assert.equal(ok, true);
  assert.deepEqual(constraints, { video: { facingMode: { ideal: "environment" } }, audio: false });
  assert.equal(h.deps.video.srcObject, stream);
});

test("camera permission denied surfaces ONE plain sentence and the video never starts", async () => {
  const err = Object.assign(new Error("denied"), { name: "NotAllowedError" });
  const h = harness({ mediaDevices: { getUserMedia: async () => { throw err; } }, BarcodeDetectorCtor: function () {} });
  const ok = await h.scanner.start();
  assert.equal(ok, false);
  assert.deepEqual(h.errors, [CameraScan.PERMISSION_DENIED]);
  assert.equal(h.deps.video.srcObject, null);
});

test("no camera available (NotFoundError) surfaces the 'no camera' sentence", async () => {
  const err = Object.assign(new Error("none"), { name: "NotFoundError" });
  const h = harness({ mediaDevices: { getUserMedia: async () => { throw err; } }, BarcodeDetectorCtor: function () {} });
  await h.scanner.start();
  assert.deepEqual(h.errors, [CameraScan.NO_CAMERA]);
});

test("BarcodeDetector path: a decode is handed to onDecode once, then suppressed for the cooldown window", async () => {
  const stream = { getTracks: () => [] };
  let detects = 0;
  function FakeDetector() { this.detect = async () => { detects += 1; return [{ rawValue: "qr-token-1" }]; }; }
  const h = harness({ mediaDevices: { getUserMedia: async () => stream }, BarcodeDetectorCtor: FakeDetector });
  await h.scanner.start();

  await h.runDue(); // first tick decodes
  assert.deepEqual(h.decodes, ["qr-token-1"]);
  assert.equal(detects, 1);

  await h.runDue(); // still inside the cooldown: must not decode (or call onDecode) again
  assert.deepEqual(h.decodes, ["qr-token-1"]);
  assert.equal(detects, 1);

  h.advance(2000);
  await h.runDue();
  assert.deepEqual(h.decodes, ["qr-token-1", "qr-token-1"]);
});

test("jsQR fallback path draws the video frame onto the canvas and decodes it when BarcodeDetector is unavailable", async () => {
  const stream = { getTracks: () => [] };
  let drawn = 0;
  const canvasCtx = { drawImage: () => { drawn += 1; }, getImageData: () => ({ data: new Uint8ClampedArray(4) }) };
  const canvas = { width: 0, height: 0, getContext: () => canvasCtx };
  const h = harness({ mediaDevices: { getUserMedia: async () => stream }, jsQR: () => ({ data: "qr-token-2" }), canvas });
  await h.scanner.start();
  await h.runDue();
  assert.deepEqual(h.decodes, ["qr-token-2"]);
  assert.ok(drawn >= 1);
});

test("a bad frame (decoder throws) is swallowed, never surfaced, and scanning continues on the next tick", async () => {
  const stream = { getTracks: () => [] };
  let calls = 0;
  function FlakyDetector() {
    this.detect = async () => { calls += 1; if (calls === 1) throw new Error("bad frame"); return [{ rawValue: "recovered" }]; };
  }
  const h = harness({ mediaDevices: { getUserMedia: async () => stream }, BarcodeDetectorCtor: FlakyDetector });
  await h.scanner.start();
  await h.runDue();
  assert.deepEqual(h.decodes, []);
  assert.deepEqual(h.errors, []); // a single bad frame is not an operator-facing error
  await h.runDue();
  assert.deepEqual(h.decodes, ["recovered"]);
});

test("stop() releases every camera track, clears the video source, and no further ticks fire", async () => {
  const track1 = { stopped: false, stop() { this.stopped = true; } };
  const track2 = { stopped: false, stop() { this.stopped = true; } };
  const stream = { getTracks: () => [track1, track2] };
  function FakeDetector() { this.detect = async () => []; }
  const h = harness({ mediaDevices: { getUserMedia: async () => stream }, BarcodeDetectorCtor: FakeDetector });

  await h.scanner.start();
  assert.equal(h.scanner.running, true);
  h.scanner.stop();
  assert.equal(h.scanner.running, false);
  assert.equal(track1.stopped, true);
  assert.equal(track2.stopped, true);
  assert.equal(h.deps.video.srcObject, null);

  const before = h.decodes.length;
  await h.runDue(); // the pending tick was cancelled by stop()
  assert.equal(h.decodes.length, before);
});
