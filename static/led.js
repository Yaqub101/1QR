/* The public LED page (SYSTEM_SPEC 13 and 18).
 *
 * createLedScreen(deps) holds all the behaviour and touches the page only through `deps`, so it is tested
 * under node with a mocked clock (tests/js/led.test.js). Rules it keeps:
 *   - shows ONLY the five approved fields: name, photo, programme, school, award (medal). It reads nothing else
 *     from a message, even if the server sent more;
 *   - a holding screen between students, and before the first contact;
 *   - if there has been NO contact with the Stadium server for 10 seconds, the holding screen appears
 *     (until then the last student stays up); when contact returns the screen recovers by itself;
 *   - the next few photos are fetched ahead of time so they appear instantly.
 * "Contact" is any state message or heartbeat ping. The server sends a ping every 2 seconds.
 */
(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = { createLedScreen: factory() };
  else root.createLedScreen = factory();
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  const MAX_PRELOAD = 5;

  function createLedScreen(deps) {
    const { els, connect, now } = deps;
    const holdMs = deps.holdMs != null ? deps.holdMs : 10000;
    const watchMs = deps.watchMs != null ? deps.watchMs : 500;
    const preload = deps.preload || function () {};
    const fit = deps.fit || function () {};
    const preloaded = new Set();
    let lastContactAt = null;

    function showHolding(holding) {
      els.holding.hidden = false;
      els.stage.hidden = true;
      if (holding && typeof holding === "object") {
        if (typeof holding.title === "string") els.holdingTitle.textContent = holding.title;
        if (typeof holding.text === "string") els.holdingText.textContent = holding.text;
      }
    }

    function showStudent(student) {
      // Only the five approved fields are ever read.
      els.name.textContent = student.name;
      els.programme.textContent = typeof student.programme === "string" ? student.programme : "";
      els.school.textContent = typeof student.school === "string" ? student.school : "";
      els.award.textContent = typeof student.award === "string" ? student.award : "";
      els.photo.setAttribute("src", typeof student.photo_url === "string" ? student.photo_url : "");
      els.holding.hidden = true;
      els.stage.hidden = false;
      fit(els.name);
    }

    function preloadPhotos(list) {
      if (!Array.isArray(list)) return;
      for (const item of list.slice(0, MAX_PRELOAD)) {
        const url = item && item.photo_url;
        if (typeof url === "string" && !preloaded.has(url)) {
          preloaded.add(url);
          preload(url);
        }
      }
    }

    function render(payload) {
      if (!payload || typeof payload !== "object") { showHolding(); return; } // never a broken card
      if (payload.mode === "SHOWING" && payload.student && typeof payload.student.name === "string") showStudent(payload.student);
      else showHolding(payload.holding);
      preloadPhotos(payload.preload);
    }

    const contact = () => { lastContactAt = now(); };

    function watchdog() {
      // The 10-second promise: no contact for holdMs -> holding screen.
      if (lastContactAt === null || now() - lastContactAt >= holdMs) showHolding();
    }

    function start() {
      showHolding(); // before the first contact the audience sees the holding screen, never a blank
      connect({
        state(payload) { contact(); render(payload); },
        ping() { contact(); },
        error() { /* the browser reconnects by itself; the watchdog decides what the audience sees */ },
      });
      (deps.setInterval || setInterval)(watchdog, watchMs);
    }

    return { start };
  }

  return createLedScreen;
});

/* ---- browser wiring (skipped under node) --------------------------------------------------------- */
(function () {
  if (typeof document === "undefined" || typeof window === "undefined") return;
  const holding = document.getElementById("holding");
  if (!holding) return;
  const byId = (id) => document.getElementById(id);

  // Shrink the name until it fits its box (long names, several lines).
  function fit(element) {
    element.style.fontSize = "";
    let size = parseFloat(window.getComputedStyle(element).fontSize);
    let guard = 60;
    while (guard-- > 0 && size > 28 && (element.scrollHeight > element.parentElement.clientHeight * 0.5 || element.scrollWidth > element.clientWidth)) {
      size -= 3;
      element.style.fontSize = size + "px";
    }
  }

  function connect(handlers) {
    const source = new EventSource("/led/events"); // reconnects automatically
    source.addEventListener("state", (event) => {
      try { handlers.state(JSON.parse(event.data)); } catch (_) { /* a bad message never blanks the screen */ }
    });
    source.addEventListener("ping", () => handlers.ping());
    source.onerror = () => handlers.error();
    return () => source.close();
  }

  window.createLedScreen({
    els: {
      holding, holdingTitle: byId("holding-title"), holdingText: byId("holding-text"), stage: byId("stage"),
      photo: byId("photo"), name: byId("name"), programme: byId("programme"), school: byId("school"), award: byId("award"),
    },
    connect, now: Date.now, holdMs: 10000, watchMs: 500, fit,
    preload: (url) => { const image = new Image(); image.src = url; },
  }).start();
})();
