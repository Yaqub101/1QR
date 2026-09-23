/* The internal Caller screen (role/flow redesign, Phase R3).
 *
 * Shows the name and the programme / degree of the student on the public LED, so the name called aloud always
 * matches the screen. It is read-only: only the Stage operator changes what it shows. The data comes from
 * /caller/events, which is built from the very same payload as the LED and changes on the same signal.
 *
 * createCallerScreen(deps) holds all the behaviour and touches the page only through `deps` (tested under node
 * with a mocked clock: tests/js/caller.test.js). Rules it keeps:
 *   - reads ONLY `name` and `programme` from a message, even if the server sent more;
 *   - holding screen on the LED -> no name, "Waiting for the stage.";
 *   - no contact for 5 seconds -> the name is taken away and "Connection lost" is shown, so a caller never reads
 *     out a name that may no longer be the one on the LED. It comes back only with a fresh state message (a
 *     heartbeat alone does not bring back an old name).
 * "Contact" is any state message or heartbeat ping. The server sends a ping every 2 seconds.
 */
(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = { createCallerScreen: factory() };
  else root.createCallerScreen = factory();
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  const CONNECTING = "Connecting…";
  const WAITING = "Waiting for the stage.";
  const LOST = "Connection lost. Do not call a name until it comes back.";

  function createCallerScreen(deps) {
    const { els, connect, now } = deps;
    const lostMs = deps.lostMs != null ? deps.lostMs : 5000;
    const watchMs = deps.watchMs != null ? deps.watchMs : 500;
    let lastContactAt = null;
    let lost = false;

    function status(text, colour) {
      els.status.textContent = text;
      els.status.className = "caller-status " + colour;
      els.status.hidden = false;
    }

    function clearName() {
      els.card.hidden = true;
      els.name.textContent = "";
      els.programme.textContent = "";
    }

    function render(payload) {
      const student = payload && typeof payload === "object" ? payload.student : null;
      if (payload && payload.mode === "SHOWING" && student && typeof student.name === "string") {
        els.name.textContent = student.name;
        els.programme.textContent = typeof student.programme === "string" ? student.programme : "";
        els.card.hidden = false;
        els.status.textContent = "";
        els.status.hidden = true;
      } else {
        clearName();
        status(WAITING, "blue");
      }
    }

    function watchdog() {
      if (lastContactAt !== null && now() - lastContactAt >= lostMs && !lost) {
        lost = true;
        clearName();
        status(LOST, "red");
      }
    }

    function start() {
      clearName();
      status(CONNECTING, "blue");
      connect({
        state(payload) { lastContactAt = now(); lost = false; render(payload); },
        ping() { lastContactAt = now(); if (lost) status(CONNECTING, "blue"); },
        error() { /* the browser reconnects by itself; the watchdog decides what the caller sees */ },
      });
      (deps.setInterval || setInterval)(watchdog, watchMs);
    }

    return { start };
  }

  return createCallerScreen;
});

/* ---- browser wiring (skipped under node) --------------------------------------------------------- */
(function () {
  if (typeof document === "undefined" || typeof window === "undefined") return;
  const card = document.getElementById("caller-card");
  if (!card) return;
  const byId = (id) => document.getElementById(id);

  function connect(handlers) {
    const source = new EventSource("/caller/events"); // reconnects automatically
    source.addEventListener("state", (event) => {
      try { handlers.state(JSON.parse(event.data)); } catch (_) { /* a bad message never shows a half card */ }
    });
    source.addEventListener("ping", () => handlers.ping());
    source.onerror = () => handlers.error();
    return () => source.close();
  }

  window.createCallerScreen({
    els: { card, name: byId("caller-name"), programme: byId("caller-programme"), status: byId("caller-status") },
    connect, now: Date.now, lostMs: 5000, watchMs: 500,
  }).start();
})();
