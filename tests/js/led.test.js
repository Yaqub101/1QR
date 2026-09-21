// The public LED page's logic under node, with a MOCKED clock (nothing sleeps for real):
//   - shows only approved fields, holding screen between students
//   - keeps the last student for 10 seconds after contact with the Stadium server is lost, then shows the
//     holding screen; recovers by itself when contact returns
const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");

const { createLedScreen } = require(path.join(__dirname, "..", "..", "static", "led.js"));

function el(props = {}) {
  return { hidden: false, textContent: "", attributes: {}, setAttribute(k, v) { this.attributes[k] = v; }, ...props };
}

function harness(overrides = {}) {
  const els = {
    holding: el({ hidden: true }), holdingTitle: el(), holdingText: el(), stage: el({ hidden: true }),
    photo: el(), name: el(), programme: el(), school: el(), award: el(),
  };
  let now = 0;
  let handlers = null;
  const timers = [];
  const preloaded = [];
  const fitted = [];
  let closed = 0;
  const screen = createLedScreen({
    els, now: () => now, holdMs: 10000, watchMs: 500,
    connect: (h) => { handlers = h; return () => { closed += 1; }; },
    setInterval: (fn, ms) => { timers.push({ fn, ms }); return timers.length; }, clearInterval: () => {},
    preload: (url) => preloaded.push(url), fit: (element) => fitted.push(element),
    ...overrides,
  });
  screen.start();
  return {
    els, preloaded, fitted, screen,
    send: (payload) => handlers.state(payload),
    ping: () => handlers.ping(),
    fail: () => handlers.error(),
    advance: (ms) => { now += ms; },
    tick: () => timers.forEach((t) => t.fn()),
    get closed() { return closed; },
    get watchMs() { return timers[0] && timers[0].ms; },
  };
}

const HOLDING = { title: "Annual Convocation 2026", text: "Welcome, graduates" };
const SHOWING = {
  mode: "SHOWING", version: 5, holding: HOLDING,
  student: { name: "Asha Rao", photo_url: "/led/photo/abc123", programme: "B.Tech Computer Science", school: "School of Engineering", award: "Gold medal" },
  preload: [{ photo_url: "/led/photo/p1" }, { photo_url: "/led/photo/p2" }, { photo_url: "/led/photo/p3" }],
};
const HOME = { mode: "HOME", version: 6, holding: HOLDING, student: null, preload: [{ photo_url: "/led/photo/p1" }] };

test("before any contact the audience sees the holding screen, never a blank or a stale student", () => {
  const h = harness();
  assert.equal(h.els.holding.hidden, false);
  assert.equal(h.els.stage.hidden, true);
});

test("a SHOWING update shows exactly the approved fields and hides the holding screen", () => {
  const h = harness();
  h.send(SHOWING);
  assert.equal(h.els.stage.hidden, false);
  assert.equal(h.els.holding.hidden, true);
  assert.equal(h.els.name.textContent, "Asha Rao");
  assert.equal(h.els.programme.textContent, "B.Tech Computer Science");
  assert.equal(h.els.school.textContent, "School of Engineering");
  assert.equal(h.els.award.textContent, "Gold medal");
  assert.equal(h.els.photo.attributes.src, "/led/photo/abc123");
});

test("the page only ever reads the five approved fields, even if the server sent more", () => {
  const h = harness();
  h.send({ ...SHOWING, student: { ...SHOWING.student, prn: "E7000123", student_id: "s-1", phone: "9999999999" } });
  const shown = JSON.stringify([h.els.name, h.els.programme, h.els.school, h.els.award, h.els.photo]);
  for (const secret of ["E7000123", "s-1", "9999999999"]) assert.ok(!shown.includes(secret), secret);
});

test("HOME reverts to the holding screen immediately, with the event branding", () => {
  const h = harness();
  h.send(SHOWING);
  h.send(HOME);
  assert.equal(h.els.holding.hidden, false);
  assert.equal(h.els.stage.hidden, true);
  assert.equal(h.els.holdingTitle.textContent, "Annual Convocation 2026");
  assert.equal(h.els.holdingText.textContent, "Welcome, graduates");
});

test("LOSING THE SERVER: the last student stays for 10 seconds, then the holding screen appears (mocked time)", () => {
  const h = harness();
  h.send(SHOWING);           // contact at t = 0
  h.advance(2000); h.fail(); // the connection drops at t = 2 s; nothing more arrives
  h.advance(7400); h.tick(); // t = 9.4 s after the drop... 9.4 s since last contact
  h.advance(0);
  assert.equal(h.els.stage.hidden, false, "still showing the student at 9.4 s");
  h.advance(500); h.tick();  // t = 9.9 s since the drop's start; last contact was at 0 so that is 9.9 s
  assert.equal(h.els.stage.hidden, false, "still showing at 9.9 s");
  h.advance(100); h.tick();  // 10.0 s since last contact
  assert.equal(h.els.stage.hidden, true, "holding screen at 10 s");
  assert.equal(h.els.holding.hidden, false);
});

test("the 10 seconds run from the LAST contact, not from the error event", () => {
  const h = harness();
  h.send(SHOWING);
  h.advance(6000); h.ping();          // still connected at 6 s
  h.advance(3000); h.fail();          // drops at 9 s
  h.advance(500); h.tick();           // 9.5 s after the last ping... 3.5 s since contact
  assert.equal(h.els.stage.hidden, false);
  h.advance(6400); h.tick();          // 9.9 s since the last contact (the ping at 6 s)
  assert.equal(h.els.stage.hidden, false);
  h.advance(200); h.tick();           // 10.1 s
  assert.equal(h.els.stage.hidden, true);
});

test("heartbeats keep the student on screen indefinitely while the server is alive", () => {
  const h = harness();
  h.send(SHOWING);
  for (let i = 0; i < 30; i++) { h.advance(2000); h.ping(); h.tick(); }  // a full minute of quiet heartbeats
  assert.equal(h.els.stage.hidden, false);
});

test("when contact returns the screen recovers by itself and shows the current state", () => {
  const h = harness();
  h.send(SHOWING);
  h.advance(11000); h.tick();
  assert.equal(h.els.holding.hidden, false);          // gone to the holding screen
  h.send({ ...SHOWING, version: 9, student: { ...SHOWING.student, name: "Ravi Kumar" } });
  assert.equal(h.els.stage.hidden, false);
  assert.equal(h.els.name.textContent, "Ravi Kumar");  // not the stale student
});

test("a lost connection while on the holding screen stays on the holding screen", () => {
  const h = harness();
  h.send(HOME);
  h.advance(60000); h.tick();
  assert.equal(h.els.holding.hidden, false);
  assert.equal(h.els.stage.hidden, true);
});

test("the watchdog checks often enough for a 10 second promise", () => {
  const h = harness();
  assert.ok(h.watchMs <= 1000);
});

test("the next photos are preloaded once each, at most five", () => {
  const h = harness();
  h.send({ ...SHOWING, preload: Array.from({ length: 8 }, (_, i) => ({ photo_url: `/led/photo/p${i}` })) });
  assert.equal(h.preloaded.length, 5);
  h.send(SHOWING);
  assert.deepEqual(h.preloaded.slice(0, 5), ["/led/photo/p0", "/led/photo/p1", "/led/photo/p2", "/led/photo/p3", "/led/photo/p4"]);
  assert.equal(new Set(h.preloaded).size, h.preloaded.length); // never fetched twice
});

test("a very long name is fitted to the screen", () => {
  const h = harness();
  h.send({ ...SHOWING, student: { ...SHOWING.student, name: "Venkata Subramanian Lakshmi Narasimha Rajagopalan Iyer" } });
  assert.ok(h.fitted.includes(h.els.name));
});

test("Unicode names are shown untouched", () => {
  const h = harness();
  h.send({ ...SHOWING, student: { ...SHOWING.student, name: "श्रेया शर्मा", school: "இயற்பியல் துறை" } });
  assert.equal(h.els.name.textContent, "श्रेया शर्मा");
  assert.equal(h.els.school.textContent, "இயற்பியல் துறை");
});

test("a malformed message never blanks the screen", () => {
  const h = harness();
  h.send(SHOWING);
  h.send(null);
  h.send({ mode: "SHOWING" });          // no student
  h.send({ garbage: true });
  assert.equal(h.els.holding.hidden, false); // falls back to the holding screen, never a broken student card
});
