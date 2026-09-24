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
    searchBtn: fakeEl(),
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
    activity: "REGISTRATION", isTouch: overrides.isTouch != null ? overrides.isTouch : false, ...overrides,
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
  assert.deepEqual(h.calls, [{ url: "/scan", body: { token: "token-abc", activity: "REGISTRATION" } }]);
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
  assert.deepEqual(confirms[0].body, { token: "tok-1", activity: "REGISTRATION" });
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
  assert.deepEqual(h.calls[0], { url: "/search", body: { prn: "e1", activity: "REGISTRATION" } });
  assert.equal(h.els.cardPhoto.attributes.src, "/photo/s-1"); // the operator verifies the face
  h.els.confirmBtn.dispatch("click");
  await flush();
  assert.deepEqual(h.calls[1], { url: "/confirm", body: { student_id: "s-1", activity: "REGISTRATION" } });
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

// ── STEP 5: Touch-primary / Coarse pointer refocus behaviour ──────────────────
test("touch-primary / coarse-pointer device disables ALL automatic refocus paths", async () => {
  const h = harness({ isTouch: true });
  // 1. init() must not focus scan box
  assert.equal(h.els.scan.focusCount, 0);

  // 2. submitScan()
  h.replies.push(READY);
  await h.screen.submitScan("tok-touch");
  assert.equal(h.els.scan.focusCount, 0);

  // 3. confirm()
  h.replies.push(CONFIRMED);
  await h.screen.confirm();
  assert.equal(h.els.scan.focusCount, 0);

  // 4. scheduleReset()
  h.runTimers();
  assert.equal(h.els.scan.focusCount, 0);

  // 5. search()
  h.els.searchInput.value = "PRN123";
  h.replies.push(READY);
  await h.screen.search();
  assert.equal(h.els.scan.focusCount, 0);

  // 6. focusScan() direct call also respects touch guard
  h.screen.focusScan();
  assert.equal(h.els.scan.focusCount, 0);
});

test("non-touch / desktop device keeps all automatic refocus paths enabled", async () => {
  const h = harness({ isTouch: false });
  // 1. init() focuses scan box
  assert.ok(h.els.scan.focusCount >= 1);
  let prev = h.els.scan.focusCount;

  // 2. submitScan() refocuses
  h.replies.push(READY);
  await h.screen.submitScan("tok-desk");
  assert.ok(h.els.scan.focusCount > prev);
  prev = h.els.scan.focusCount;

  // 3. confirm() refocuses
  h.replies.push(CONFIRMED);
  await h.screen.confirm();
  assert.ok(h.els.scan.focusCount > prev);
  prev = h.els.scan.focusCount;

  // 4. scheduleReset() refocuses
  h.runTimers();
  assert.ok(h.els.scan.focusCount > prev);
  prev = h.els.scan.focusCount;

  // 5. search() refocuses
  h.els.searchInput.value = "PRN123";
  h.replies.push(READY);
  await h.screen.search();
  assert.ok(h.els.scan.focusCount > prev);
});

