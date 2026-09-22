// Stage Controller screen logic under node: what would be visibly wrong to the operator.
const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");

const { createStageScreen } = require(path.join(__dirname, "..", "..", "static", "stage.js"));

function el(props = {}) {
  const e = {
    value: "", textContent: "", hidden: false, disabled: false, className: "", attributes: {}, handlers: {},
    setAttribute(k, v) { this.attributes[k] = v; },
    addEventListener(type, fn) { (this.handlers[type] ||= []).push(fn); },
    click() { (this.handlers.click || []).forEach((fn) => fn({ preventDefault() {} })); },
    ...props,
  };
  return e;
}

function harness() {
  const els = {};
  for (const k of ["displayNext", "home", "previous", "searchBtn", "skip", "complete", "takeOver"]) els[k] = el();
  for (const k of ["message", "banner", "led", "currentName", "nextName", "afterNextName", "currentPhoto", "nextPhoto", "afterNextPhoto", "results"]) els[k] = el();
  els.searchInput = el(); els.skipReason = el();
  const calls = [];
  const replies = [];
  const gates = [];
  const post = async (url, body) => {
    calls.push({ url, body });
    if (gates.length) await gates.shift();
    return replies.length ? replies.shift() : { state: STATE(true) };
  };
  let handlers = null;
  const screen = createStageScreen({ els, post, connect: (h) => { handlers = h; return () => {}; }, doc: { createElement: () => el() } });
  screen.start();
  return {
    els, calls, screen, replies,
    state: (s) => handlers.state(s),
    hold: () => { let release; gates.push(new Promise((r) => { release = r; })); return release; },
  };
}

const CARD = (name) => ({ student_id: "id-" + name, name, photo_url: "/photo/" + name, programme: "B.Tech", school: "Eng", has_display_data: true });
const STATE = (youControl, extra = {}) => ({
  version: 1, you_control: youControl, controller: { username: youControl ? "eng-stage" : "other-stage" }, led_mode: "SHOWING",
  current: CARD("Asha"), next: CARD("Ravi"), after_next: CARD("Meena"), previous: null, queue_depth: 2, ...extra,
});
const flush = () => new Promise((resolve) => setImmediate(resolve));

test("the three positions are shown: CURRENT, NEXT, AFTER NEXT", () => {
  const h = harness();
  h.state(STATE(true));
  assert.equal(h.els.currentName.textContent, "Asha");
  assert.equal(h.els.nextName.textContent, "Ravi");
  assert.equal(h.els.afterNextName.textContent, "Meena");
  assert.equal(h.els.currentPhoto.attributes.src, "/photo/Asha");
});

test("a rapid double click on DISPLAY NEXT or COMPLETE sends ONE request", async () => {
  for (const button of ["displayNext", "complete"]) {
    const h = harness();
    h.state(STATE(true));
    const release = h.hold();
    h.els[button].click(); h.els[button].click(); h.els[button].click();
    await flush();
    assert.equal(h.calls.length, 1, button);
    release(); await flush();
  }
});

test("ONE-KEY EMERGENCY HOME: Escape sends HOME straight away, even while another request is in flight", async () => {
  const h = harness();
  h.state(STATE(true));
  const release = h.hold();
  h.els.complete.click();                       // a COMPLETE is still waiting on the server
  h.screen.handleKey({ key: "Escape", preventDefault() {} });
  await flush();
  assert.deepEqual(h.calls.map((c) => c.url), ["/stage/complete", "/stage/home"]);
  release(); await flush();
});

test("Escape works while typing in the search or reason box", async () => {
  const h = harness();
  h.state(STATE(true));
  h.els.searchInput.value = "asha";
  h.screen.handleKey({ key: "Escape", target: h.els.searchInput, preventDefault() {} });
  await flush();
  assert.equal(h.calls[0].url, "/stage/home");
});

test("other keys do nothing", async () => {
  const h = harness();
  h.state(STATE(true));
  for (const key of ["h", "Enter", " ", "Delete"]) h.screen.handleKey({ key, preventDefault() {} });
  await flush();
  assert.equal(h.calls.length, 0);
});

test("SKIP without a reason sends nothing and says why; with a reason it sends it trimmed", async () => {
  const h = harness();
  h.state(STATE(true));
  h.els.skipReason.value = "   ";
  h.els.skip.click(); await flush();
  assert.equal(h.calls.length, 0);
  assert.equal(h.els.message.textContent, "Please give a reason for skipping.");
  h.els.skipReason.value = "  Not present at the stage ";
  h.els.skip.click(); await flush();
  assert.deepEqual(h.calls[0], { url: "/stage/skip", body: { reason: "Not present at the stage" } });
});

test("when another laptop has taken over, every action is disabled and TAKE OVER appears; clicks do nothing", async () => {
  const h = harness();
  h.state(STATE(false));
  for (const k of ["displayNext", "home", "previous", "skip", "complete"]) assert.equal(h.els[k].disabled, true, k);
  assert.equal(h.els.takeOver.hidden, false);
  assert.ok(h.els.message.textContent.includes("another laptop") || h.els.banner.className.includes("locked"));
  h.els.displayNext.click(); h.els.complete.click(); await flush();
  assert.equal(h.calls.length, 0);
});

test("TAKE OVER asks the server, then the screen becomes active again", async () => {
  const h = harness();
  h.state(STATE(false));
  h.replies.push({ state: STATE(true) });
  h.els.takeOver.click(); await flush();
  assert.deepEqual(h.calls[0], { url: "/stage/takeover", body: {} });
  assert.equal(h.els.displayNext.disabled, false);
  assert.equal(h.els.takeOver.hidden, true);
});

test("a refusal from the server is shown as the server's plain sentence", async () => {
  const h = harness();
  h.state(STATE(true));
  h.replies.push({ detail: { code: "NOTHING_ON_STAGE", message: "Nobody is on stage." } });
  h.els.complete.click(); await flush();
  assert.equal(h.els.message.textContent, "Nobody is on stage.");
});

test("the LED status is shown so the operator knows what the audience sees", () => {
  const h = harness();
  h.state(STATE(true, { led_mode: "HOME" }));
  assert.ok(/holding/i.test(h.els.led.textContent));
  h.state(STATE(true, { led_mode: "SHOWING" }));
  assert.ok(h.els.led.textContent.includes("Asha"));
});
