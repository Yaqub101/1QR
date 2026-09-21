// Operator screen logic, run with `node --test tests/js/station.test.js` (also run by tests/test_station_engine.py).
// A fake DOM stands in for the browser: what matters is WHEN focus happens, how scanner suffixes are
// stripped, that double scans / double clicks make one request, and which colour + sound each result gets.
const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");

const logic = require(path.join(__dirname, "..", "..", "static", "station_logic.js"));
const { createStationScreen } = require(path.join(__dirname, "..", "..", "static", "station.js"));

const NUL = String.fromCharCode(0);

// ---------------------------------------------------------------- fake DOM
function fakeEl(props = {}) {
  return {
    value: "", textContent: "", className: "", hidden: false, disabled: false, focusCount: 0, children: [],
    attributes: {}, handlers: {}, dataset: {},
    focus() { this.focusCount += 1; },
    addEventListener(type, fn) { (this.handlers[type] ||= []).push(fn); },
    dispatch(type, event = {}) { (this.handlers[type] || []).forEach((fn) => fn({ preventDefault() {}, ...event })); },
    setAttribute(k, v) { this.attributes[k] = v; },
    appendChild(child) { this.children.push(child); return child; },
    replaceChildren(...kids) { this.children = kids; },
    ...props,
  };
}

function harness(overrides = {}) {
  const els = {
    scan: fakeEl(), banner: fakeEl(), message: fakeEl(), card: fakeEl({ hidden: true }), cardName: fakeEl(),
    cardPhoto: fakeEl(), cardFields: fakeEl(), confirmBtn: fakeEl({ hidden: true }), searchInput: fakeEl(),
    searchBtn: fakeEl(), stationSelect: null,
  };
  const doc = { createElement: () => fakeEl() };
  const calls = [];
  const sounds = [];
  const timers = [];
  let clock = 1000;
  let gate = null;
  const replies = [];
  const post = async (url, body) => {
    calls.push({ url, body });
    if (gate) await gate;
    const next = replies.shift();
    if (next instanceof Error) throw next;
    return next || { result: "INVALID", colour: "red", message: "QR NOT RECOGNISED — use PRN search or contact Admin", student: null };
  };
  const screen = createStationScreen({
    els, doc, post, sound: (name) => sounds.push(name), now: () => clock, resetMs: 2500, debounceMs: 1500,
    setTimeout: (fn, ms) => { timers.push({ fn, ms }); return timers.length; }, clearTimeout: () => {},
    stationId: "REG-01", ...overrides,
  });
  screen.init();
  return {
    els, doc, calls, sounds, timers, screen, replies,
    advance: (ms) => { clock += ms; },
    hold: () => { let release; gate = new Promise((r) => { release = r; }); return () => { gate = null; release(); }; },
    runTimers: () => { const due = timers.splice(0); due.forEach((t) => t.fn()); },
  };
}

const READY = { result: "READY", colour: "blue", message: "Check the photo, then confirm.", activity: "REGISTRATION",
  manual: false, student: { student_id: "s-1", name: "Asha Rao", photo_url: "/photo/s-1", fields: [{ key: "prn", label: "PRN", value: "E1" }] } };
const CONFIRMED = { result: "CONFIRMED", colour: "green", message: "REGISTERED", student: READY.student };
const DUPLICATE = { result: "DUPLICATE", colour: "amber", message: "ALREADY REGISTERED — 11:21 AM", student: READY.student };
const REJECTED = { result: "REJECTED", colour: "red", message: "STUDENT NOT ACTIVE — CONTACT ADMIN", student: READY.student };

const flush = () => new Promise((resolve) => setImmediate(resolve));
const scanValue = (h, value) => { h.els.scan.value = value; h.els.scan.dispatch("keydown", { key: "Enter" }); };

// ---------------------------------------------------------------- pure helpers
test("stripScannerSuffix removes the scanner's trailing Enter/newline in every form", () => {
  const cases = [
    ["abc123\n", "abc123"], ["abc123\r\n", "abc123"], ["abc123\r", "abc123"], ["abc123\t", "abc123"],
    ["  abc123  ", "abc123"], ["abc123" + NUL, "abc123"], ["\nabc123\n", "abc123"], ["", ""], ["\r\n", ""],
    [null, ""], [undefined, ""],
    ["abc123\ndef456\n", "abc123"], // two scans arrived together: take the first
  ];
  for (const [raw, expected] of cases) assert.equal(logic.stripScannerSuffix(raw), expected, JSON.stringify(raw));
});

test("classifyResult maps every outcome to a colour and a sound", () => {
  assert.deepEqual(logic.classifyResult("READY"), { colour: "blue", sound: null });
  assert.deepEqual(logic.classifyResult("CONFIRMED"), { colour: "green", sound: "success" });
  assert.deepEqual(logic.classifyResult("DUPLICATE"), { colour: "amber", sound: "duplicate" });
  assert.deepEqual(logic.classifyResult("REJECTED"), { colour: "red", sound: "rejected" });
  assert.deepEqual(logic.classifyResult("INVALID"), { colour: "red", sound: "rejected" });
  assert.deepEqual(logic.classifyResult("ERROR"), { colour: "red", sound: "rejected" });
  assert.deepEqual(logic.classifyResult("SOMETHING-NEW"), { colour: "red", sound: "rejected" }); // unknown is never green
});

test("the debouncer drops a repeat of the same code inside the window and anything while busy", () => {
  let t = 0;
  const d = logic.createDebouncer({ windowMs: 1500, now: () => t });
  assert.equal(d.accept("A"), true);
  assert.equal(d.accept("A"), false); // the scanner double-read
  t = 1499; assert.equal(d.accept("A"), false);
  t = 1500; assert.equal(d.accept("A"), true); // a genuine rescan later
  assert.equal(d.accept("B"), true); // a different student is never dropped by the window
  d.begin(); assert.equal(d.accept("C"), false); d.end(); assert.equal(d.accept("C"), true);
});

// ---------------------------------------------------------------- the screen
test("the scan box is focused when the screen loads", () => {
  const h = harness();
  assert.ok(h.els.scan.focusCount >= 1);
});

test("a scan with the scanner's trailing Enter sends ONE clean request and clears and refocuses the box", async () => {
  const h = harness();
  h.replies.push(READY);
  const before = h.els.scan.focusCount;
  scanValue(h, "token-abc\r\n");
  await flush();
  assert.deepEqual(h.calls, [{ url: "/scan", body: { token: "token-abc", station_id: "REG-01" } }]);
  assert.equal(h.els.scan.value, "");
  assert.ok(h.els.scan.focusCount > before);
});

test("a scanner that types the newline into the box (no Enter key event) is also handled", async () => {
  const h = harness();
  h.replies.push(READY);
  h.els.scan.value = "token-xyz\n";
  h.els.scan.dispatch("input");
  await flush();
  assert.equal(h.calls.length, 1);
  assert.equal(h.calls[0].body.token, "token-xyz");
});

test("typing without Enter does not submit anything", async () => {
  const h = harness();
  h.els.scan.value = "half-typed";
  h.els.scan.dispatch("input");
  await flush();
  assert.equal(h.calls.length, 0);
});

test("a rapid double scan of the same QR makes one request; a rescan after the window is allowed", async () => {
  const h = harness();
  h.replies.push(READY, READY);
  for (let i = 0; i < 3; i++) scanValue(h, "same-qr\n");
  await flush();
  assert.equal(h.calls.length, 1);
  h.advance(2000);
  scanValue(h, "same-qr\n");
  await flush();
  assert.equal(h.calls.length, 2);
});

test("a scan of a different QR while a request is in flight is ignored, not queued into a mess", async () => {
  const h = harness();
  const release = h.hold();
  h.replies.push(READY);
  scanValue(h, "first\n");
  scanValue(h, "second\n");
  await flush();
  assert.equal(h.calls.length, 1);
  assert.equal(h.els.scan.value, ""); // the box is still cleared and ready for the next real scan
  release();
  await flush();
  assert.equal(h.calls.length, 1);
});

test("READY shows the card and the confirm button; nothing is confirmed yet", async () => {
  const h = harness();
  h.replies.push(READY);
  scanValue(h, "t\n");
  await flush();
  assert.equal(h.els.card.hidden, false);
  assert.equal(h.els.cardName.textContent, "Asha Rao");
  assert.equal(h.els.confirmBtn.hidden, false);
  assert.equal(h.els.banner.className.includes("blue"), true);
  assert.deepEqual(h.sounds, []); // no chime until something is actually done
  assert.equal(h.calls.filter((c) => c.url === "/confirm").length, 0);
});

test("confirm sends the SAME identity that was scanned (the token), once, even on a double click", async () => {
  const h = harness();
  h.replies.push(READY, CONFIRMED);
  scanValue(h, "tok-1\n");
  await flush();
  h.els.confirmBtn.dispatch("click"); h.els.confirmBtn.dispatch("click"); h.els.confirmBtn.dispatch("click");
  await flush();
  const confirms = h.calls.filter((c) => c.url === "/confirm");
  assert.equal(confirms.length, 1);
  assert.deepEqual(confirms[0].body, { token: "tok-1", station_id: "REG-01" });
});

test("CONFIRMED is green with the success sound, then the screen resets for the next student and refocuses", async () => {
  const h = harness();
  h.replies.push(READY, CONFIRMED);
  scanValue(h, "tok\n");
  await flush();
  h.els.confirmBtn.dispatch("click");
  await flush();
  assert.ok(h.els.banner.className.includes("green"));
  assert.deepEqual(h.sounds, ["success"]);
  assert.equal(h.els.confirmBtn.hidden, true);
  const focusBefore = h.els.scan.focusCount;
  h.runTimers();
  assert.equal(h.els.card.hidden, true);
  assert.ok(h.els.scan.focusCount > focusBefore);
});

test("DUPLICATE is amber and REJECTED / INVALID are red, each with their own sound and the message shown as given", async () => {
  const invalid = { result: "INVALID", colour: "red", message: "QR NOT RECOGNISED — use PRN search or contact Admin", student: null };
  const cases = [[DUPLICATE, "amber", "duplicate"], [REJECTED, "red", "rejected"], [invalid, "red", "rejected"]];
  for (const [reply, colour, sound] of cases) {
    const h = harness();
    h.replies.push(reply);
    scanValue(h, "t\n");
    await flush();
    assert.ok(h.els.banner.className.includes(colour), reply.result);
    assert.equal(h.els.message.textContent, reply.message);
    assert.deepEqual(h.sounds, [sound], reply.result);
    assert.equal(h.els.confirmBtn.hidden, true, "nothing to confirm after a refusal");
  }
});

test("a network failure shows a calm red message and refocuses, never a raw error", async () => {
  const h = harness();
  h.replies.push(new Error("Failed to fetch: ECONNRESET at line 42"));
  scanValue(h, "t\n");
  await flush();
  assert.equal(h.els.message.textContent, "One moment, please try again.");
  assert.ok(h.els.banner.className.includes("red"));
  assert.ok(!h.els.message.textContent.includes("ECONNRESET"));
});

test("a failing sound never breaks the screen", async () => {
  const h = harness({ sound: () => { throw new Error("audio blocked"); } });
  h.replies.push(DUPLICATE);
  scanValue(h, "t\n");
  await flush();
  assert.ok(h.els.banner.className.includes("amber"));
});

test("pressing Enter on an empty scan box confirms the card that is showing (keyboard-only flow)", async () => {
  const h = harness();
  h.replies.push(READY, CONFIRMED);
  scanValue(h, "t\n");
  await flush();
  scanValue(h, ""); // a scanner's stray second Enter arrives instantly: it must NOT confirm the card
  await flush();
  assert.equal(h.calls.filter((c) => c.url === "/confirm").length, 0);
  h.advance(600); // the operator has now had time to look at the photo
  scanValue(h, "");
  await flush();
  assert.equal(h.calls.filter((c) => c.url === "/confirm").length, 1);
});

test("manual PRN search shows the photo card, and confirming it sends the student id, not a token", async () => {
  const h = harness();
  h.replies.push({ ...READY, manual: true }, { ...CONFIRMED, manual: true });
  h.els.searchInput.value = " e1 \n";
  h.els.searchBtn.dispatch("click");
  await flush();
  assert.deepEqual(h.calls[0], { url: "/search", body: { prn: "e1", station_id: "REG-01" } });
  assert.equal(h.els.cardPhoto.attributes.src, "/photo/s-1"); // the operator verifies the face
  h.els.confirmBtn.dispatch("click");
  await flush();
  assert.deepEqual(h.calls[1], { url: "/confirm", body: { student_id: "s-1", station_id: "REG-01" } });
  assert.equal(h.els.searchInput.value, "");
});

test("focus returns to the scan box after every action", async () => {
  const h = harness();
  h.replies.push(READY, CONFIRMED, DUPLICATE);
  let last = h.els.scan.focusCount;
  const step = async (fn) => { fn(); await flush(); assert.ok(h.els.scan.focusCount > last); last = h.els.scan.focusCount; };
  await step(() => scanValue(h, "a\n"));
  await step(() => h.els.confirmBtn.dispatch("click"));
  h.advance(3000);
  await step(() => scanValue(h, "b\n"));
});

test("an Admin's station choice is sent with every request", async () => {
  const h = harness();
  h.els.stationSelect = fakeEl({ value: "REG-02" });
  h.replies.push(READY);
  scanValue(h, "t\n");
  await flush();
  assert.equal(h.calls[0].body.station_id, "REG-02");
});
