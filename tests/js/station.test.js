// Operator screen logic, run with `node --test tests/js/station.test.js` (also run by tests/test_station_engine.py).
// A fake DOM stands in for the browser: what matters is WHEN focus happens, how scanner suffixes are
// stripped, that double scans / double clicks make one request, and which colour + sound each result gets.
const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");

const logic = require(path.join(__dirname, "..", "..", "static", "station_logic.js"));
const { createStationScreen, extractErrorMessage, post } = require(path.join(__dirname, "..", "..", "static", "station.js"));

const NUL = String.fromCharCode(0);

// ---------------------------------------------------------------- fake DOM
function fakeEl(props = {}) {
  const el = {
    value: "", textContent: "", className: "", hidden: false, disabled: false, focusCount: 0, blurCount: 0, children: [],
    attributes: {}, handlers: {}, dataset: {}, tagName: "DIV", open: false,
    focus() { this.focusCount += 1; if (this.ownerDoc) this.ownerDoc.activeElement = this; },
    blur() { this.blurCount += 1; if (this.ownerDoc && this.ownerDoc.activeElement === this) this.ownerDoc.activeElement = null; },
    addEventListener(type, fn) { (this.handlers[type] ||= []).push(fn); },
    dispatch(type, event = {}) { (this.handlers[type] || []).forEach((fn) => fn({ preventDefault() {}, target: this, ...event })); },
    setAttribute(k, v) { this.attributes[k] = v; },
    appendChild(child) { this.children.push(child); return child; },
    replaceChildren(...kids) { this.children = kids; },
    closest(selector) {
      if (typeof selector === "string" && selector.toLowerCase().includes(this.tagName.toLowerCase())) return this;
      return null;
    },
    ...props,
  };
  return el;
}

function harness(overrides = {}) {
  const doc = {
    activeElement: null,
    handlers: {},
    addEventListener(type, fn) { (this.handlers[type] ||= []).push(fn); },
    dispatch(type, event = {}) { (this.handlers[type] || []).forEach((fn) => fn({ preventDefault() {}, target: this, ...event })); },
    createElement: (tag = "div") => fakeEl({ tagName: tag.toUpperCase(), ownerDoc: doc }),
  };
  const els = {
    scan: fakeEl({ tagName: "INPUT", ownerDoc: doc }),
    banner: fakeEl(), message: fakeEl(), card: fakeEl({ hidden: true }), cardName: fakeEl(),
    cardPhoto: fakeEl(), cardFields: fakeEl(), confirmBtn: fakeEl({ hidden: true }),
    searchInput: fakeEl({ tagName: "INPUT", ownerDoc: doc }),
    searchBtn: fakeEl({ tagName: "BUTTON", ownerDoc: doc }),
    manualDetails: fakeEl({ tagName: "DETAILS", open: false, ownerDoc: doc }),
    operatorStationActions: fakeEl({ hidden: true }),
    downloadPassBtn: fakeEl({ tagName: "BUTTON", ownerDoc: doc }),
    reissuePassToggleBtn: fakeEl({ tagName: "BUTTON", ownerDoc: doc }),
    reissuePassPanel: fakeEl({ hidden: true }),
    reissueStudentPhoto: fakeEl({ tagName: "IMG" }),
    reissueStudentName: fakeEl(),
    reissueStudentPrn: fakeEl(),
    reissueReasonInput: fakeEl({ tagName: "INPUT", ownerDoc: doc }),
    reissueConfirmBtn: fakeEl({ tagName: "BUTTON", ownerDoc: doc }),
    reissueCancelBtn: fakeEl({ tagName: "BUTTON", ownerDoc: doc }),
    reissueSuccessBox: fakeEl({ hidden: true }),
    downloadNewPassBtn: fakeEl({ tagName: "A" }),
  };
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
  const downloads = [];
  const screen = createStationScreen({
    els, doc, post, sound: (name) => sounds.push(name), now: () => clock, resetMs: 2500, debounceMs: 1500,
    setTimeout: (fn, ms) => { timers.push({ fn, ms }); return timers.length; }, clearTimeout: () => {},
    triggerDownload: (url) => downloads.push(url),
    activity: "REGISTRATION", ...overrides,
  });
  screen.init();
  return {
    els, doc, calls, sounds, timers, screen, replies, downloads,
    advance: (ms) => { clock += ms; },
    hold: () => { let release; gate = new Promise((r) => { release = r; }); return () => { gate = null; release(); }; },
    runTimers: () => { const due = timers.splice(0); due.forEach((t) => t.fn()); },
  };
}

const READY = { result: "READY", colour: "blue", message: "Check the photo, then confirm.", activity: "REGISTRATION",
  manual: false, student: { student_id: "s-1", name: "Asha Rao", photo_url: "/photo/s-1", fields: [{ key: "prn", label: "PRN", value: "E1" }] } };
const CONFIRMED = { result: "CONFIRMED", colour: "green", message: "REGISTERED", student: READY.student };
const DUPLICATE = { result: "DUPLICATE", colour: "amber", message: "ALREADY REPORTED — 11:21 AM", student: READY.student };
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
test("no input is programmatically focused when the screen loads", () => {
  const h = harness();
  assert.equal(h.els.scan.focusCount, 0);
  assert.equal(h.els.searchInput.focusCount, 0);
  assert.equal(h.doc.activeElement, null);
});

test("a scan with the scanner's trailing Enter sends ONE clean request and clears the box with no refocus", async () => {
  const h = harness();
  h.replies.push(READY);
  scanValue(h, "token-abc\r\n");
  await flush();
  assert.deepEqual(h.calls, [{ url: "/scan", body: { token: "token-abc", activity: "REGISTRATION" } }]);
  assert.equal(h.els.scan.value, "");
  assert.equal(h.els.scan.focusCount, 0);
  assert.equal(h.els.searchInput.focusCount, 0);
  assert.equal(h.doc.activeElement, null);
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
  assert.deepEqual(confirms[0].body, { token: "tok-1", activity: "REGISTRATION" });
});

test("CONFIRMED is green with the success sound, then the screen resets for the next student", async () => {
  const h = harness();
  h.replies.push(READY, CONFIRMED);
  scanValue(h, "tok\n");
  await flush();
  h.els.confirmBtn.dispatch("click");
  await flush();
  assert.ok(h.els.banner.className.includes("green"));
  assert.deepEqual(h.sounds, ["success"]);
  assert.equal(h.els.confirmBtn.hidden, true);
  h.runTimers();
  assert.equal(h.els.card.hidden, true);
  assert.equal(h.els.scan.focusCount, 0);
  assert.equal(h.els.searchInput.focusCount, 0);
  assert.equal(h.doc.activeElement, null);
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
  assert.deepEqual(h.calls[0], { url: "/search", body: { prn: "e1", activity: "REGISTRATION" } });
  assert.equal(h.els.cardPhoto.attributes.src, "/photo/s-1"); // the operator verifies the face
  h.els.confirmBtn.dispatch("click");
  await flush();
  assert.deepEqual(h.calls[1], { url: "/confirm", body: { student_id: "s-1", activity: "REGISTRATION" } });
  assert.equal(h.els.searchInput.value, "");
});

test("no text input is ever focused automatically after load, scan, confirm, or reset", async () => {
  const h = harness();
  h.replies.push(READY, CONFIRMED, DUPLICATE);
  assert.equal(h.els.scan.focusCount, 0);
  assert.equal(h.els.searchInput.focusCount, 0);
  assert.equal(h.doc.activeElement, null);

  scanValue(h, "a\n");
  await flush();
  assert.equal(h.els.scan.focusCount, 0);
  assert.equal(h.els.searchInput.focusCount, 0);
  assert.equal(h.doc.activeElement, null);

  h.els.confirmBtn.dispatch("click");
  await flush();
  assert.equal(h.els.scan.focusCount, 0);
  assert.equal(h.els.searchInput.focusCount, 0);
  assert.equal(h.doc.activeElement, null);

  h.advance(3000);
  h.runTimers();
  assert.equal(h.els.scan.focusCount, 0);
  assert.equal(h.els.searchInput.focusCount, 0);
  assert.equal(h.doc.activeElement, null);
});

// ---------------------------------------------------------------- the Registry desk (tick boxes, Phase R4)
// The server sends the step, the student's state and the two boxes for this step; the screen shows them, the
// operator ticks, and confirm sends the ticked, not-yet-done boxes back with the step.
const ENTRY_MARKERS = [
  { key: "THOBE_ALLOCATION", label: "Robe allotted", done: true, time: "9:12 AM" },
  { key: "MONEY_RECEIVED", label: "Money received", done: false, time: null },
];
const REGISTRY_READY = { ...READY, activity: "REGISTRY", step: "ENTRY", confirm_label: "CONFIRM",
  state: "REPORTED / MONEY PENDING", markers: ENTRY_MARKERS };

function registryHarness() {
  const h = harness({ activity: "REGISTRY" });
  h.els.markers = fakeEl({ hidden: true });
  h.els.cardState = fakeEl();
  return h;
}
const boxes = (h) => h.els.markers.children.map((row) => row.children[0]);  // each row: [checkbox, label]

test("the Registry desk shows the student's state and one box per marker, done ones ticked and locked", async () => {
  const h = registryHarness();
  h.replies.push(REGISTRY_READY);
  scanValue(h, "tok-9\n");
  await flush();
  assert.equal(h.els.cardState.textContent, "REPORTED / MONEY PENDING");
  assert.equal(h.els.markers.hidden, false);
  assert.deepEqual(boxes(h).map((b) => [b.value, b.checked, b.disabled]),
    [["THOBE_ALLOCATION", true, true], ["MONEY_RECEIVED", false, false]]);
  assert.equal(h.els.markers.children[0].children[1].textContent, "Robe allotted — done 9:12 AM");
  assert.equal(h.els.markers.children[1].children[1].textContent, "Money received");
  assert.equal(h.els.confirmBtn.textContent, "CONFIRM");
});

test("confirm sends the step and only the boxes the operator ticked now", async () => {
  const h = registryHarness();
  h.replies.push(REGISTRY_READY);
  scanValue(h, "tok-9\n");
  await flush();
  boxes(h)[1].checked = true;
  h.replies.push(CONFIRMED);
  h.els.confirmBtn.dispatch("click");
  await flush();
  assert.deepEqual(h.calls[1], { url: "/confirm",
    body: { token: "tok-9", activity: "REGISTRY", step: "ENTRY", marks: ["MONEY_RECEIVED"] } });
});

test("confirming with nothing ticked still sends the confirm (it registers a new student)", async () => {
  const h = registryHarness();
  h.replies.push({ ...REGISTRY_READY, markers: ENTRY_MARKERS.map((m) => ({ ...m, done: false, time: null })) });
  scanValue(h, "tok-9\n");
  await flush();
  h.replies.push(CONFIRMED);
  h.els.confirmBtn.dispatch("click");
  await flush();
  assert.deepEqual(h.calls[1].body, { token: "tok-9", activity: "REGISTRY", step: "ENTRY", marks: [] });
});

test("a manual PRN search at the Registry desk confirms by student id with the step and the ticks", async () => {
  const h = registryHarness();
  h.replies.push({ ...REGISTRY_READY, manual: true });
  h.els.searchInput.value = "E1";
  h.els.searchBtn.dispatch("click");
  await flush();
  boxes(h)[1].checked = true;
  h.replies.push(CONFIRMED);
  h.els.confirmBtn.dispatch("click");
  await flush();
  assert.deepEqual(h.calls[1].body, { student_id: "s-1", activity: "REGISTRY", step: "ENTRY", marks: ["MONEY_RECEIVED"] });
});

test("an amber 'come back after the ceremony' still shows the state and the done boxes, with no confirm", async () => {
  const h = registryHarness();
  h.replies.push({ ...DUPLICATE, message: "ROBE AND MONEY RECEIVED — COME BACK AFTER THE CEREMONY", step: null,
    state: "DEGREE NOT RECEIVED", markers: ENTRY_MARKERS.map((m) => ({ ...m, done: true, time: "9:12 AM" })) });
  scanValue(h, "tok-9\n");
  await flush();
  assert.equal(h.els.confirmBtn.hidden, true);
  assert.equal(h.els.cardState.textContent, "DEGREE NOT RECEIVED");
  assert.deepEqual(boxes(h).map((b) => [b.checked, b.disabled]), [[true, true], [true, true]]);
});

test("the boxes are cleared when the screen resets for the next student", async () => {
  const h = registryHarness();
  h.replies.push(REGISTRY_READY);
  scanValue(h, "tok-9\n");
  await flush();
  h.replies.push(CONFIRMED);
  h.els.confirmBtn.dispatch("click");
  await flush();
  h.runTimers();
  assert.equal(h.els.markers.hidden, true);
  assert.equal(h.els.markers.children.length, 0);
  assert.equal(h.els.cardState.textContent, "");
});

test("an ordinary activity screen shows no boxes, sends no step or marks and keeps its own button label", async () => {
  const h = harness();
  h.els.confirmBtn.textContent = "CONFIRM LUNCH";
  h.replies.push(READY);
  scanValue(h, "tok-1\n");
  await flush();
  assert.equal(h.els.confirmBtn.textContent, "CONFIRM LUNCH");
  h.replies.push(CONFIRMED);
  h.els.confirmBtn.dispatch("click");
  await flush();
  assert.deepEqual(h.calls[1].body, { token: "tok-1", activity: "REGISTRATION" });
});

test("a registered student's confirm with no new box ticked is refused on the screen, with no request", async () => {
  const h = registryHarness();
  h.replies.push({ ...REGISTRY_READY, needs_tick: true });
  scanValue(h, "tok-9\n");
  await flush();
  h.els.confirmBtn.dispatch("click");
  await flush();
  assert.equal(h.calls.length, 1);  // only the scan
  assert.equal(h.els.message.textContent, "Tick at least one box, then confirm.");
  assert.equal(h.els.confirmBtn.hidden, false);  // the card stays up so the operator can tick
});

// ── STEP 4: Error messages in post() ──────────────────────────────────────────
test("post() error extraction: detail.message takes highest priority", async () => {
  const fakeFetch = async () => ({
    ok: false,
    status: 400,
    json: async () => ({ detail: { message: "Token has been revoked by admin." } }),
  });
  const res = await post("/scan", {}, fakeFetch);
  assert.equal(res.result, "ERROR");
  assert.equal(res.message, "Token has been revoked by admin.");
});

test("post() error extraction: detail as string is used when detail.message is absent", async () => {
  const fakeFetch = async () => ({
    ok: false,
    status: 404,
    json: async () => ({ detail: "Student not found in registry." }),
  });
  const res = await post("/scan", {}, fakeFetch);
  assert.equal(res.result, "ERROR");
  assert.equal(res.message, "Student not found in registry.");
});

test("post() error extraction: detail[0].msg is extracted for 422 validation error lists", async () => {
  const fakeFetch = async () => ({
    ok: false,
    status: 422,
    json: async () => ({
      detail: [
        { loc: ["body", "token"], msg: "QR token cannot be blank.", type: "value_error" },
        { loc: ["body", "activity"], msg: "Activity is required.", type: "missing" },
      ],
    }),
  });
  const res = await post("/confirm", {}, fakeFetch);
  assert.equal(res.result, "ERROR");
  assert.equal(res.message, "QR token cannot be blank.");
});

test("post() error extraction: generic fallback when no detail message exists", async () => {
  const fakeFetch = async () => ({
    ok: false,
    status: 500,
    json: async () => ({ detail: null }),
  });
  const res = await post("/scan", {}, fakeFetch);
  assert.equal(res.result, "ERROR");
  assert.equal(res.message, "One moment, please try again.");
});

// ── STEP 5: Document-level keydown buffer & zero programmatic focus ─────────
test("hardware barcode scanner: rapid keystroke burst (<60ms) followed by Enter submits the scan", async () => {
  const h = harness();
  h.replies.push(READY);

  // Simulate hardware scanner sending "TOK123" rapidly (10ms between keys) followed by Enter
  for (const ch of "TOK123") {
    h.doc.dispatch("keydown", { key: ch });
    h.advance(10);
  }
  h.doc.dispatch("keydown", { key: "Enter" });
  await flush();

  assert.equal(h.calls.length, 1);
  assert.deepEqual(h.calls[0], { url: "/scan", body: { token: "TOK123", activity: "REGISTRATION" } });
  assert.equal(h.screen.scannerBuffer.buffer, "");
});

test("hardware barcode scanner: slow human typing (>60ms) is discarded and does not submit", async () => {
  const h = harness();
  h.replies.push(READY);

  // Human typing letters with 150ms gap (> 60ms threshold)
  h.doc.dispatch("keydown", { key: "T" });
  h.advance(150);
  h.doc.dispatch("keydown", { key: "O" });
  h.advance(150);
  h.doc.dispatch("keydown", { key: "K" });
  h.advance(150);
  h.doc.dispatch("keydown", { key: "Enter" });
  await flush();

  // Burst was too slow: no scan submitted
  assert.equal(h.calls.length, 0);
  assert.equal(h.screen.scannerBuffer.buffer, "");
});

test("hardware barcode scanner: typing inside search-prn input is ignored by document scanner buffer", async () => {
  const h = harness();
  h.els.searchInput.focus();
  assert.equal(h.doc.activeElement, h.els.searchInput);

  // User typing fast or slow inside the PRN input
  h.doc.dispatch("keydown", { key: "2", target: h.els.searchInput });
  h.advance(10);
  h.doc.dispatch("keydown", { key: "0", target: h.els.searchInput });
  h.advance(10);
  h.doc.dispatch("keydown", { key: "2", target: h.els.searchInput });
  h.advance(10);
  h.doc.dispatch("keydown", { key: "Enter", target: h.els.searchInput });
  await flush();

  // The document-level scanner buffer did NOT intercept or record any of these keystrokes
  assert.equal(h.screen.scannerBuffer.buffer, "");
  assert.equal(h.calls.filter((c) => c.url === "/scan").length, 0);
});

test("hardware barcode scanner: Enter on empty buffer confirms candidate card after guard time", async () => {
  const h = harness();
  h.replies.push(READY, CONFIRMED);

  // Scanner brings up card
  for (const ch of "TOK456") {
    h.doc.dispatch("keydown", { key: ch });
    h.advance(10);
  }
  h.doc.dispatch("keydown", { key: "Enter" });
  await flush();
  assert.equal(h.els.confirmBtn.hidden, false);

  // Accidental immediate second Enter (< 500ms guard): ignored
  h.doc.dispatch("keydown", { key: "Enter" });
  await flush();
  assert.equal(h.calls.filter((c) => c.url === "/confirm").length, 0);

  // Enter after guard time: confirms candidate!
  h.advance(600);
  h.doc.dispatch("keydown", { key: "Enter" });
  await flush();
  assert.equal(h.calls.filter((c) => c.url === "/confirm").length, 1);
  assert.deepEqual(h.calls[1], { url: "/confirm", body: { token: "TOK456", activity: "REGISTRATION" } });
});

test("manual PRN search: <details> is closed by default, closes and blurs after scan, search, or reset", async () => {
  const h = harness();
  // 1. Initially closed
  assert.equal(h.els.manualDetails.open, false);

  // 2. Operator opens accordion and focuses PRN input
  h.els.manualDetails.open = true;
  h.els.searchInput.value = "PRN999";
  h.els.searchInput.focus();
  assert.equal(h.doc.activeElement, h.els.searchInput);

  // 3. Search closes accordion and blurs input
  h.replies.push(READY);
  await h.screen.search();
  assert.equal(h.els.manualDetails.open, false);
  assert.equal(h.doc.activeElement, null);

  // 4. Operator opens it again, but a scan arrives via camera or hardware scanner
  h.els.manualDetails.open = true;
  h.els.searchInput.focus();
  assert.equal(h.doc.activeElement, h.els.searchInput);
  h.replies.push(READY);
  await h.screen.submitScan("TOK789");
  assert.equal(h.els.manualDetails.open, false);
  assert.equal(h.doc.activeElement, null);
});

test("document.activeElement is never a text input after load, scan, confirm, or reset", async () => {
  const h = harness();
  // 1. After load
  assert.equal(h.doc.activeElement, null);
  assert.equal(h.els.scan.focusCount, 0);
  assert.equal(h.els.searchInput.focusCount, 0);

  // 2. After scan
  h.replies.push(READY);
  await h.screen.submitScan("TOK111");
  assert.equal(h.doc.activeElement, null);
  assert.equal(h.els.scan.focusCount, 0);
  assert.equal(h.els.searchInput.focusCount, 0);

  // 3. After confirm
  h.replies.push(CONFIRMED);
  await h.screen.confirm();
  assert.equal(h.doc.activeElement, null);
  assert.equal(h.els.scan.focusCount, 0);
  assert.equal(h.els.searchInput.focusCount, 0);

  // 4. After reset
  h.runTimers();
  assert.equal(h.doc.activeElement, null);
  assert.equal(h.els.scan.focusCount, 0);
  assert.equal(h.els.searchInput.focusCount, 0);
});

test("Registry pass download button triggers download for scanned or searched student", async () => {
  const h = harness();
  h.replies.push({
    result: "READY",
    message: "Check the photo, then confirm.",
    student: {
      student_id: "11111111-2222-3333-4444-555555555555",
      name: "Aaditya Patel",
      photo_url: "/photo/11111111-2222-3333-4444-555555555555",
      fields: [{ key: "prn", label: "PRN", value: "202401001" }],
    },
  });

  await h.screen.submitScan("TOK123");
  assert.equal(h.els.card.hidden, false);
  assert.equal(h.els.operatorStationActions.hidden, false);

  h.els.downloadPassBtn.dispatch("click");
  assert.deepEqual(h.downloads, ["/station/api/pass/11111111-2222-3333-4444-555555555555"]);
});

test("Reissue QR toggle opens panel and shows candidate identity preview", async () => {
  const h = harness();
  h.replies.push({
    result: "READY",
    message: "Check the photo, then confirm.",
    student: {
      student_id: "22222222-3333-4444-5555-666666666666",
      name: "Sneha Sharma",
      photo_url: "/photo/22222222-3333-4444-5555-666666666666",
      fields: [{ key: "prn", label: "PRN", value: "202401002" }],
    },
  });

  await h.screen.submitScan("TOK456");
  assert.equal(h.els.reissuePassPanel.hidden, true);

  h.els.reissuePassToggleBtn.dispatch("click");
  assert.equal(h.els.reissuePassPanel.hidden, false);
  assert.equal(h.els.reissueStudentName.textContent, "Sneha Sharma");
  assert.equal(h.els.reissueStudentPrn.textContent, "PRN: 202401002");
  assert.equal(h.els.reissueStudentPhoto.attributes.src, "/photo/22222222-3333-4444-5555-666666666666");

  // Cancel hides the panel
  h.els.reissueCancelBtn.dispatch("click");
  assert.equal(h.els.reissuePassPanel.hidden, true);
});

test("Reissue confirmation requires non-empty reason", async () => {
  const h = harness();
  h.replies.push({
    result: "READY",
    message: "Ready",
    student: {
      student_id: "33333333-4444-5555-6666-777777777777",
      name: "Rahul Verma",
      fields: [{ key: "prn", label: "PRN", value: "202401003" }],
    },
  });
  await h.screen.submitScan("TOK789");

  h.els.reissuePassToggleBtn.dispatch("click");
  h.els.reissueReasonInput.value = "   "; // blank/whitespace
  await h.els.reissueConfirmBtn.dispatch("click");

  assert.equal(h.calls.length, 1); // Only the initial /scan call, no /reissue-pass call
  assert.equal(h.els.message.textContent, "Please give a reason for reissuing this QR.");
  assert.equal(h.sounds[h.sounds.length - 1], "rejected");
});

test("Successful reissue updates UI, shows download button and does NOT auto-download", async () => {
  const h = harness();
  h.replies.push({
    result: "READY",
    message: "Ready",
    student: {
      student_id: "44444444-5555-6666-7777-888888888888",
      name: "Pooja Hegde",
      fields: [{ key: "prn", label: "PRN", value: "202401004" }],
    },
  });
  await h.screen.submitScan("TOK999");

  let autoDownloaded = false;
  h.screen.triggerDownload = () => { autoDownloaded = true; };

  h.els.reissuePassToggleBtn.dispatch("click");
  h.els.reissueReasonInput.value = "Card lost during bus transit";

  h.replies.push({
    ok: true,
    student_id: "44444444-5555-6666-7777-888888888888",
    message: "New QR pass issued. Previous QR invalidated.",
    pass_url: "/station/api/pass/44444444-5555-6666-7777-888888888888",
  });

  await h.els.reissueConfirmBtn.dispatch("click");

  // 1. Reissue endpoint was called with audited reason
  const reissueCall = h.calls.find((c) => c.url === "/station/api/reissue-pass");
  assert.ok(reissueCall);
  assert.deepEqual(reissueCall.body, {
    student_id: "44444444-5555-6666-7777-888888888888",
    reason: "Card lost during bus transit",
  });

  // 2. Banner and sound updated
  assert.equal(h.els.banner.className, "banner green");
  assert.equal(h.els.message.textContent, "New QR pass issued. Previous QR invalidated.");
  assert.equal(h.sounds[h.sounds.length - 1], "success");

  // 3. Panel closed, success box visible with link
  assert.equal(h.els.reissuePassPanel.hidden, true);
  assert.equal(h.els.reissueSuccessBox.hidden, false);
  assert.equal(h.els.downloadNewPassBtn.attributes.href, "/station/api/pass/44444444-5555-6666-7777-888888888888");

  // 4. Must NOT have auto-triggered download
  assert.equal(autoDownloaded, false);
});


