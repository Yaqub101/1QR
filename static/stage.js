/* The Stage Controller screen (SYSTEM_SPEC 13): CURRENT / NEXT / AFTER NEXT, DISPLAY NEXT, HOME, PREVIOUS,
 * SEARCH, SKIP, COMPLETE, TAKE OVER.
 *
 * createStageScreen(deps) holds all the behaviour and touches the page only through `deps` (tested under
 * node: tests/js/stage.test.js). Rules it keeps:
 *   - one request at a time, so a rapid double press of DISPLAY NEXT or COMPLETE sends ONE request
 *     (the server also refuses to double-advance);
 *   - HOME is the emergency control: Escape sends it immediately, even while another request is in flight;
 *   - SKIP asks for a reason first and sends nothing without one;
 *   - when another laptop has taken over, every action is disabled and TAKE OVER is offered.
 * The operator only ever reads the server's plain sentences.
 */
(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = { createStageScreen: factory() };
  else root.createStageScreen = factory();
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  const TEMPORARY = "One moment, please try again.";
  const ACTIONS = ["displayNext", "home", "previous", "skip", "complete", "searchBtn"];

  function createStageScreen(deps) {
    const { els, post, connect, doc } = deps;
    let busy = false;
    let youControl = false;

    const say = (message) => { els.message.textContent = message || ""; };

    function setSlot(name, photo, card) {
      name.textContent = card ? card.name : "";
      photo.setAttribute("src", card ? card.photo_url : "");
    }

    function renderResults(matches) {
      if (!els.results || !doc || !els.results.replaceChildren) return;
      els.results.replaceChildren(
        ...(matches || []).map((m) => {
          const row = doc.createElement("div");
          row.className = "result";
          const label = doc.createElement("span");
          label.textContent = `${m.name} — position ${m.queue_position} (${m.status})`;
          const button = doc.createElement("button");
          button.textContent = "SHOW";
          button.addEventListener("click", () => act("/stage/display", { student_id: m.student_id }));
          row.appendChild(label);
          row.appendChild(button);
          return row;
        })
      );
    }

    function render(state) {
      if (!state) return;
      youControl = !!state.you_control;
      setSlot(els.currentName, els.currentPhoto, state.current);
      setSlot(els.nextName, els.nextPhoto, state.next);
      setSlot(els.afterNextName, els.afterNextPhoto, state.after_next);
      for (const key of ACTIONS) els[key].disabled = !youControl;
      els.takeOver.hidden = youControl;
      if (!youControl) {
        els.banner.className = "banner amber locked";
        say("Another laptop is running the stage. Use TAKE OVER to control it from here.");
      } else if (els.banner.className.includes("locked")) {
        els.banner.className = "banner blue";
      }
      const shown = state.led_name || (state.current && state.current.name);
      els.led.textContent = state.led_mode === "SHOWING"
        ? `Audience screen: showing ${shown || "a student"}`
        : "Audience screen: holding screen";
    }

    function handle(reply, quiet) {
      if (reply && reply.state) render(reply.state);
      if (reply && reply.matches) renderResults(reply.matches);
      if (reply && reply.detail && reply.detail.message) {
        say(reply.detail.message);
        els.banner.className = "banner red";
      } else if (reply && reply.message && !quiet) {
        say(reply.message);
        if (youControl) els.banner.className = "banner green";
      }
    }

    async function send(url, body) {
      try {
        return await post(url, body || {});
      } catch (_) {
        return { detail: { message: TEMPORARY } };
      }
    }

    // One request at a time; anything pressed while one is in flight is ignored.
    async function act(url, body) {
      if (busy || !youControl) return;
      busy = true;
      try { handle(await send(url, body)); } finally { busy = false; }
    }

    // EMERGENCY: never blocked by an in-flight request and never blocked by a lost-control screen state
    // (the server decides; a laptop that is not in control is simply told so).
    async function emergencyHome() {
      handle(await send("/stage/home", {}));
    }

    async function skip() {
      const reason = (els.skipReason.value || "").trim();
      if (!reason) { say("Please give a reason for skipping."); els.banner.className = "banner red"; return; }
      await act("/stage/skip", { reason });
      els.skipReason.value = "";
    }

    async function search() {
      const q = (els.searchInput.value || "").trim();
      if (!q) return;
      await act("/stage/search", { q });
    }

    async function takeOver() {
      if (busy) return;
      busy = true;
      try { handle(await send("/stage/takeover", {})); } finally { busy = false; }
    }

    function handleKey(event) {
      if (event && event.key === "Escape") {
        if (event.preventDefault) event.preventDefault();
        emergencyHome();
      }
    }

    function start() {
      els.displayNext.addEventListener("click", () => act("/stage/display-next"));
      els.complete.addEventListener("click", () => act("/stage/complete"));
      els.previous.addEventListener("click", () => act("/stage/previous"));
      els.home.addEventListener("click", () => emergencyHome());
      els.skip.addEventListener("click", skip);
      els.searchBtn.addEventListener("click", search);
      els.takeOver.addEventListener("click", takeOver);
      connect({ state: render });
    }

    return { start, handleKey, render };
  }

  return createStageScreen;
});

/* ---- browser wiring (skipped under node) --------------------------------------------------------- */
(function () {
  if (typeof document === "undefined" || typeof window === "undefined") return;
  const rootEl = document.getElementById("stage-root");
  if (!rootEl) return;
  const byId = (id) => document.getElementById(id);
  const els = {
    displayNext: byId("display-next"), complete: byId("complete"), home: byId("home"), previous: byId("previous"),
    skip: byId("skip"), skipReason: byId("skip-reason"), searchBtn: byId("search-btn"), searchInput: byId("search-input"),
    takeOver: byId("take-over"), message: byId("message"), banner: byId("banner"), led: byId("led"), results: byId("results"),
    currentName: byId("current-name"), nextName: byId("next-name"), afterNextName: byId("after-next-name"),
    currentPhoto: byId("current-photo"), nextPhoto: byId("next-photo"), afterNextPhoto: byId("after-next-photo"),
  };

  async function post(url, body) {
    const response = await fetch(url, {
      method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    if (response.status === 401) { window.location.href = "/login"; }
    try { return await response.json(); } catch (_) { return { detail: { message: "One moment, please try again." } }; }
  }

  const screenRef = { current: null };
  function connect(handlers) {
    const source = new EventSource("/stage/events"); // pushes the private state on every change; reconnects itself
    source.addEventListener("state", (event) => { try { handlers.state(JSON.parse(event.data)); } catch (_) { /* ignore */ } });
    return () => source.close();
  }

  const screen = window.createStageScreen({ els, post, connect, doc: document });
  screenRef.current = screen;
  screen.start();
  document.addEventListener("keydown", screen.handleKey);
  // Take control when the screen opens; if another laptop already has it, the screen offers TAKE OVER.
  post("/stage/control", {})
    .then((reply) => { if (reply && reply.state) screen.render(reply.state); });
})();
