// The Caller screen's logic under node, with a MOCKED clock (role/flow redesign, Phase R3).
//   - shows ONLY the name and the programme / degree of the student the LED shows;
//   - a "waiting" message when the LED is on the holding screen (nobody to call);
//   - if contact with the server is lost for 5 seconds, the name is taken away and a warning is shown, so the
//     caller never reads out a name that may no longer be the one on the LED; it recovers by itself.
const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");

const { createCallerScreen } = require(path.join(__dirname, "..", "..", "static", "caller.js"));

function el(props = {}) {
  return { hidden: false, textContent: "", className: "", ...props };
}

function harness() {
  const els = { name: el(), programme: el(), status: el(), card: el({ hidden: true }) };
  let now = 0;
  let handlers = null;
  const timers = [];
  createCallerScreen({
    els, now: () => now, lostMs: 5000, watchMs: 500,
    connect: (h) => { handlers = h; return () => {}; },
    setInterval: (fn) => { timers.push(fn); return timers.length; },
  }).start();
  return {
    els,
    send: (payload) => handlers.state(payload),
    ping: () => handlers.ping(),
    advance: (ms) => { now += ms; },
    tick: () => timers.forEach((fn) => fn()),
  };
}

const SHOWING = (name, programme) => ({ mode: "SHOWING", version: 7, student: { name, programme } });
const HOME = { mode: "HOME", version: 8, student: null };

test("before any contact the caller sees no name, only that the screen is connecting", () => {
  const h = harness();
  assert.equal(h.els.card.hidden, true);
  assert.equal(h.els.name.textContent, "");
  assert.match(h.els.status.textContent, /connecting/i);
});

test("a SHOWING update shows the name and the programme, large", () => {
  const h = harness();
  h.send(SHOWING("Priya Nair", "Master of Business Administration (Finance)"));
  assert.equal(h.els.card.hidden, false);
  assert.equal(h.els.name.textContent, "Priya Nair");
  assert.equal(h.els.programme.textContent, "Master of Business Administration (Finance)");
});

test("the screen only ever reads the name and the programme, even if the server sent more", () => {
  const h = harness();
  h.send({ mode: "SHOWING", version: 1, student: { name: "Asha", programme: "B.Tech", prn: "PRN-SECRET", school: "Eng", photo_url: "/x" } });
  const shown = JSON.stringify(h.els);
  assert.ok(!shown.includes("PRN-SECRET") && !shown.includes("Eng") && !shown.includes("/x"));
});

test("HOME takes the name away at once and says there is nobody to call", () => {
  const h = harness();
  h.send(SHOWING("Asha", "B.Tech"));
  h.send(HOME);
  assert.equal(h.els.card.hidden, true);
  assert.equal(h.els.name.textContent, "");
  assert.match(h.els.status.textContent, /waiting for the stage/i);
});

test("LOSING THE SERVER: after 5 seconds without contact the name is taken away and a warning shown (mocked time)", () => {
  const h = harness();
  h.send(SHOWING("Asha", "B.Tech"));
  h.advance(4900); h.tick();
  assert.equal(h.els.card.hidden, false, "still within 5 seconds of the last contact");
  h.advance(200); h.tick();
  assert.equal(h.els.card.hidden, true);
  assert.equal(h.els.name.textContent, "");
  assert.match(h.els.status.textContent, /connection lost/i);
  assert.match(h.els.status.className, /red/);
});

test("heartbeats keep the name up indefinitely while the server is alive", () => {
  const h = harness();
  h.send(SHOWING("Asha", "B.Tech"));
  for (let i = 0; i < 30; i++) { h.advance(2000); h.ping(); h.tick(); }
  assert.equal(h.els.card.hidden, false);
  assert.equal(h.els.name.textContent, "Asha");
});

test("when contact returns the screen shows the CURRENT student, not the one from before the loss", () => {
  const h = harness();
  h.send(SHOWING("Asha", "B.Tech"));
  h.advance(6000); h.tick();
  h.send(SHOWING("Ravi", "B.Com"));
  assert.equal(h.els.card.hidden, false);
  assert.equal(h.els.name.textContent, "Ravi");
  assert.doesNotMatch(h.els.status.textContent, /connection lost/i);
});

test("a ping after a loss does not bring back an old name: only a fresh state does", () => {
  const h = harness();
  h.send(SHOWING("Asha", "B.Tech"));
  h.advance(6000); h.tick();
  h.ping(); h.tick();
  assert.equal(h.els.card.hidden, true);
  assert.equal(h.els.name.textContent, "");
});

test("a malformed message never shows a half card", () => {
  const h = harness();
  h.send(SHOWING("Asha", "B.Tech"));
  for (const bad of [null, "x", { mode: "SHOWING", student: { programme: "B.Tech" } }, { mode: "SHOWING", student: null }]) {
    h.send(bad);
    assert.equal(h.els.card.hidden, true);
    assert.equal(h.els.name.textContent, "");
  }
});

test("Unicode names are shown untouched", () => {
  const h = harness();
  h.send(SHOWING("Zoë Ångström-Iyer · श्रेया", "M.Sc"));
  assert.equal(h.els.name.textContent, "Zoë Ångström-Iyer · श्रेया");
});
