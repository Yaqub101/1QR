// Stage Controller screen logic under node: what would be visibly wrong to the operator.
// Since the role/flow redesign (Phase R2) there is ONE advance action, NEXT: it records the degree for the
// student on stage and shows the next one. The screen shows CURRENT plus the next fifteen WAITING.
const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");

const { createStageScreen } = require(path.join(__dirname, "..", "..", "static", "stage.js"));

function el(props = {}) {
  const e = {
    value: "", textContent: "", hidden: false, disabled: false, className: "", attributes: {}, handlers: {}, children: [],
    setAttribute(k, v) { this.attributes[k] = v; },
    addEventListener(type, fn) { (this.handlers[type] ||= []).push(fn); },
    click() { (this.handlers.click || []).forEach((fn) => fn({ preventDefault() {} })); },
    appendChild(child) { this.children.push(child); return child; },
    replaceChildren(...kids) { this.children = kids; },
    ...props,
  };
  return e;
}

function harness() {
  const els = {};
  for (const k of ["next", "showAgain", "home", "previous", "searchBtn", "skip", "takeOver"]) els[k] = el();
  for (const k of ["message", "banner", "led", "currentName", "currentProgramme", "currentPhoto", "waiting", "depth", "results"]) els[k] = el();
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

const CARD = (name, pos) => ({ student_id: "id-" + name, name, photo_url: "/photo/" + name, programme: "B.Tech " + name,
  school: "Eng", queue_position: pos, has_display_data: true });
const WAITING = ["Ravi", "Meena", "Kiran"].map((n, i) => CARD(n, i + 2));
const STATE = (youControl, extra = {}) => ({
  version: 1, you_control: youControl, controller: { username: youControl ? "eng-stage" : "other-stage" }, led_mode: "SHOWING",
  current: CARD("Asha", 1), next: WAITING[0], after_next: WAITING[1], waiting: WAITING, previous: null, queue_depth: 3, ...extra,
});
const flush = () => new Promise((resolve) => setImmediate(resolve));
// The label on each row of the waiting list (the row's first child is its text).
const rowText = (h) => h.els.waiting.children.map((row) => row.children[0].textContent);

test("CURRENT shows the student on stage with their programme and photo", () => {
  const h = harness();
  h.state(STATE(true));
  assert.equal(h.els.currentName.textContent, "Asha");
  assert.equal(h.els.currentProgramme.textContent, "B.Tech Asha");
  assert.equal(h.els.currentPhoto.attributes.src, "/photo/Asha");
});

test("the WAITING list shows every waiting student the server sends, in order, with the count", () => {
  const h = harness();
  const fifteen = Array.from({ length: 15 }, (_, i) => CARD("S" + i, i + 2));
  h.state(STATE(true, { waiting: fifteen, queue_depth: 40 }));
  assert.equal(h.els.waiting.children.length, 15);
  assert.ok(rowText(h)[0].startsWith("S0") && rowText(h)[14].startsWith("S14"));
  assert.ok(h.els.depth.textContent.includes("40"));
});

test("NEXT names the student this screen shows on stage, so a stale or repeated press cannot advance twice", async () => {
  const h = harness();
  h.state(STATE(true));
  h.els.next.click(); await flush();
  assert.deepEqual(h.calls[0], { url: "/stage/next", body: { expect_current: "id-Asha" } });
  h.state(STATE(true, { current: null }));
  h.els.next.click(); await flush();
  assert.deepEqual(h.calls[1], { url: "/stage/next", body: { expect_current: null } });
});

test("a rapid double click on NEXT sends ONE request", async () => {
  const h = harness();
  h.state(STATE(true));
  const release = h.hold();
  h.els.next.click(); h.els.next.click(); h.els.next.click();
  await flush();
  assert.equal(h.calls.length, 1);
  release(); await flush();
});

test("a presenter clicker (Page Down / Right arrow) presses NEXT; Enter and space do not", async () => {
  const h = harness();
  h.state(STATE(true));
  for (const key of ["Enter", " ", "h", "Delete"]) h.screen.handleKey({ key, preventDefault() {} });
  await flush();
  assert.equal(h.calls.length, 0);
  h.screen.handleKey({ key: "PageDown", preventDefault() {} }); await flush();
  h.screen.handleKey({ key: "ArrowRight", preventDefault() {} }); await flush();
  assert.deepEqual(h.calls.map((c) => c.url), ["/stage/next", "/stage/next"]);
});

test("the NEXT keys do nothing while typing in the search or reason box", async () => {
  const h = harness();
  h.state(STATE(true));
  h.screen.handleKey({ key: "ArrowRight", target: h.els.searchInput, preventDefault() {} });
  h.screen.handleKey({ key: "PageDown", target: h.els.skipReason, preventDefault() {} });
  await flush();
  assert.equal(h.calls.length, 0);
});

test("SEND on a waiting student sends them with the student on stage named", async () => {
  const h = harness();
  h.state(STATE(true));
  const meenaRow = h.els.waiting.children[1];
  meenaRow.children[1].click(); await flush();
  assert.deepEqual(h.calls[0], { url: "/stage/display", body: { student_id: "id-Meena", expect_current: "id-Asha" } });
});

test("SHOW AGAIN re-shows the student on stage after HOME", async () => {
  const h = harness();
  h.state(STATE(true, { led_mode: "HOME" }));
  h.els.showAgain.click(); await flush();
  assert.deepEqual(h.calls[0], { url: "/stage/show-again", body: {} });
});

test("ONE-KEY EMERGENCY HOME: Escape sends HOME straight away, even while another request is in flight", async () => {
  const h = harness();
  h.state(STATE(true));
  const release = h.hold();
  h.els.next.click();                           // a NEXT is still waiting on the server
  h.screen.handleKey({ key: "Escape", preventDefault() {} });
  await flush();
  assert.deepEqual(h.calls.map((c) => c.url), ["/stage/next", "/stage/home"]);
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

test("when another laptop has taken over, every action is disabled and TAKE OVER appears; clicks and keys do nothing", async () => {
  const h = harness();
  h.state(STATE(false));
  for (const k of ["next", "showAgain", "home", "previous", "skip"]) assert.equal(h.els[k].disabled, true, k);
  assert.equal(h.els.takeOver.hidden, false);
  assert.ok(h.els.message.textContent.includes("another laptop") || h.els.banner.className.includes("locked"));
  h.els.next.click(); h.screen.handleKey({ key: "PageDown", preventDefault() {} }); await flush();
  assert.equal(h.calls.length, 0);
});

test("TAKE OVER asks the server, then the screen becomes active again", async () => {
  const h = harness();
  h.state(STATE(false));
  h.replies.push({ state: STATE(true) });
  h.els.takeOver.click(); await flush();
  assert.deepEqual(h.calls[0], { url: "/stage/takeover", body: {} });
  assert.equal(h.els.next.disabled, false);
  assert.equal(h.els.takeOver.hidden, true);
});

test("a refusal from the server is shown as the server's plain sentence", async () => {
  const h = harness();
  h.state(STATE(true, { current: null }));
  h.replies.push({ detail: { code: "QUEUE_EMPTY", message: "Nobody is waiting in the queue." } });
  h.els.next.click(); await flush();
  assert.equal(h.els.message.textContent, "Nobody is waiting in the queue.");
});

test("the LED status is shown so the operator knows what the audience sees", () => {
  const h = harness();
  h.state(STATE(true, { led_mode: "HOME" }));
  assert.ok(/holding/i.test(h.els.led.textContent));
  h.state(STATE(true, { led_mode: "SHOWING" }));
  assert.ok(h.els.led.textContent.includes("Asha"));
});

test("the first live state replaces 'Connecting…' with 'Ready.' (it must not stay on screen once data arrives)", () => {
  const h = harness();
  h.els.message.textContent = "Connecting…";
  h.state(STATE(true));
  assert.equal(h.els.message.textContent, "Ready.");
  h.els.message.textContent = "Degree recorded. Showing the next student.";
  h.state(STATE(true));
  assert.equal(h.els.message.textContent, "Degree recorded. Showing the next student."); // later states keep the last message
});
