/* The Stage Controller screen (SYSTEM_SPEC 13, role/flow redesign Phase R2): CURRENT, the next fifteen WAITING,
 * NEXT, SHOW AGAIN, HOME, PREVIOUS, SEARCH, SKIP, TAKE OVER.
 *
 * NEXT is the one advance action: the student on stage received the degree, and the next one goes on stage and
 * on the LED. SEND on a waiting student does the same with that student. Both name the student this screen
 * shows on stage (`expect_current`), so the server can refuse a stale or repeated press.
 *
 * createStageScreen(deps) holds all the behaviour and touches the page only through `deps` (tested under
 * node: tests/js/stage.test.js). Rules it keeps:
 *   - one request at a time, so a rapid double press of NEXT sends ONE request (the server also refuses to
 *     double-advance);
 *   - a presenter clicker (Page Down / Right arrow) presses NEXT, except while typing in a box; Enter and
 *     space never do, so a stray key cannot advance the ceremony;
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
  const ACTIONS = ["next", "showAgain", "home", "previous", "skip", "searchBtn"];
  const NEXT_KEYS = ["PageDown", "ArrowRight"];

  function createStageScreen(deps) {
    const { els, post, connect, doc } = deps;
    let busy = false;
    let youControl = false;
    let onStage = null; // the student_id this screen shows as CURRENT (null: nobody)

    const say = (message) => { els.message.textContent = message || ""; };

    function studentRow(card, label, onSend) {
      const row = doc.createElement("li");
      row.className = "result";
      const text = doc.createElement("span");
      text.textContent = label;
      const button = doc.createElement("button");
      button.textContent = "SEND";
      button.disabled = !youControl;
      button.addEventListener("click", onSend);
      row.appendChild(text);
      row.appendChild(button);
      return row;
    }

    function send(card) {
      return () => act("/stage/display", { student_id: card.student_id, expect_current: onStage });
    }

    function renderWaiting(waiting, depth) {
      if (!els.waiting || !els.waiting.replaceChildren) return;
      els.waiting.replaceChildren(
        ...(waiting || []).map((c) => studentRow(c, `${c.name} — ${c.programme || ""}`, send(c)))
      );
      if (els.depth) els.depth.textContent = depth == null ? "" : `(${depth} in the queue)`;
    }

    function renderResults(matches) {
      if (!els.results || !doc || !els.results.replaceChildren) return;
      els.results.replaceChildren(
        ...(matches || []).map((m) => studentRow(m, `${m.name} — position ${m.queue_position} (${m.status})`, send(m)))
      );
    }

    function render(state) {
      if (!state) return;
      youControl = !!state.you_control;
      const current = state.current;
      onStage = current ? current.student_id : null;
      els.currentName.textContent = current ? current.name : "";
      if (els.currentProgramme) els.currentProgramme.textContent = current ? current.programme || "" : "";
      els.currentPhoto.setAttribute("src", current ? current.photo_url : "");
      renderWaiting(state.waiting, state.queue_depth);
      for (const key of ACTIONS) els[key].disabled = !youControl;
      els.takeOver.hidden = youControl;
      if (!youControl) {
        els.banner.className = "banner amber locked";
        say("Another laptop is running the stage. Use TAKE OVER to control it from here.");
      } else if (els.banner.className.includes("locked")) {
        els.banner.className = "banner blue";
      }
      // The first live state means the screen is connected: never leave "Connecting…" up once data is showing.
      if (youControl && els.message.textContent === "Connecting…") say("Ready.");
      const shown = state.led_name || (current && current.name);
      els.led.textContent = state.led_mode === "SHOWING"
        ? `Audience screen: showing ${shown || "a student"}`
        : "Audience screen: holding screen";

      if (els.freezeWarning) {
        let warning = "";
        if (state.display_snapshot_count === 0) {
          warning = "No display data has been frozen: the LED will stay on the holding screen until display data is frozen.";
        } else if (current && !current.has_display_data) {
          warning = "This student has no approved display data: the LED will stay on the holding screen until display data is frozen.";
        }
        if (warning) {
          if (els.freezeWarningText) els.freezeWarningText.textContent = warning;
          els.freezeWarning.hidden = false;
        } else {
          els.freezeWarning.hidden = true;
        }
      }
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

    async function request(url, body) {
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
      try { handle(await request(url, body)); } finally { busy = false; }
    }

    const next = () => act("/stage/next", { expect_current: onStage });

    // EMERGENCY: never blocked by an in-flight request and never blocked by a lost-control screen state
    // (the server decides; a laptop that is not in control is simply told so).
    async function emergencyHome() {
      handle(await request("/stage/home", {}));
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
      try { handle(await request("/stage/takeover", {})); } finally { busy = false; }
    }

    const typing = (event) => event && (event.target === els.searchInput || event.target === els.skipReason);

    function handleKey(event) {
      if (!event) return;
      if (event.key === "Escape") {
        if (event.preventDefault) event.preventDefault();
        emergencyHome();
      } else if (NEXT_KEYS.includes(event.key) && !typing(event)) {
        if (event.preventDefault) event.preventDefault();
        next();
      }
    }

    function start() {
      els.next.addEventListener("click", next);
      els.showAgain.addEventListener("click", () => act("/stage/show-again"));
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
    next: byId("next"), showAgain: byId("show-again"), home: byId("home"), previous: byId("previous"),
    skip: byId("skip"), skipReason: byId("skip-reason"), searchBtn: byId("search-btn"), searchInput: byId("search-input"),
    takeOver: byId("take-over"), message: byId("message"), banner: byId("banner"), led: byId("led"), results: byId("results"),
    currentName: byId("current-name"), currentProgramme: byId("current-programme"), currentPhoto: byId("current-photo"),
    waiting: byId("waiting"), depth: byId("depth"),
    freezeWarning: byId("freeze-warning"), freezeWarningText: byId("freeze-warning-text"),
  };

  const scrollContainer = byId("waiting-scroll-container");
  const scrollLoader = byId("queue-scroll-loader");

  async function post(url, body) {
    const response = await fetch(url, {
      method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    if (response.status === 401) { window.location.href = "/login"; }
    try { return await response.json(); } catch (_) { return { detail: { message: "One moment, please try again." } }; }
  }

  function connect(handlers) {
    const source = new EventSource("/stage/events"); // pushes the private state on every change; reconnects itself
    source.addEventListener("state", (event) => { try { handlers.state(JSON.parse(event.data)); } catch (_) { /* ignore */ } });
    return () => source.close();
  }

  const screen = window.createStageScreen({ els, post, connect, doc: document });
  screen.start();
  document.addEventListener("keydown", screen.handleKey);

  // Take control when the screen opens; if another laptop already has it, the screen offers TAKE OVER.
  post("/stage/control", {})
    .then((reply) => { if (reply && reply.state) screen.render(reply.state); });

  // ---------------- Infinite Scroll for the Stage Manager Queue ----------------
  let loadedStudentIds = new Set();
  let totalInQueue = 0;
  let isLoadingMore = false;

  function studentRowElement(card) {
    const row = document.createElement("li");
    row.className = "result stage-queue-row";
    const info = document.createElement("div");
    info.className = "stage-queue-row-info";
    const nameEl = document.createElement("span");
    nameEl.className = "queue-student-name";
    nameEl.textContent = card.name;
    const progEl = document.createElement("span");
    progEl.className = "queue-student-prog muted";
    progEl.textContent = card.programme || "";
    info.appendChild(nameEl);
    if (card.programme) info.appendChild(progEl);

    const button = document.createElement("button");
    button.type = "button";
    button.className = "button small queue-send-btn";
    button.textContent = "SEND";
    button.addEventListener("click", () => {
      post("/stage/display", { student_id: card.student_id, expect_current: null })
        .then((reply) => { if (reply && reply.state) screen.render(reply.state); });
    });

    row.appendChild(info);
    row.appendChild(button);
    return row;
  }

  async function loadMoreQueue() {
    if (isLoadingMore || loadedStudentIds.size >= totalInQueue) return;
    isLoadingMore = true;
    if (scrollLoader) scrollLoader.hidden = false;
    try {
      const offset = loadedStudentIds.size;
      const resp = await fetch(`/stage/queue?offset=${offset}&limit=20`, { credentials: "same-origin" });
      if (resp.ok) {
        const data = await resp.json();
        totalInQueue = data.total || 0;
        if (Array.isArray(data.students)) {
          data.students.forEach((student) => {
            if (!loadedStudentIds.has(student.student_id)) {
              loadedStudentIds.add(student.student_id);
              if (els.waiting) els.waiting.appendChild(studentRowElement(student));
            }
          });
        }
      }
    } catch (_) {
      // transient network hiccup: let user scroll again to retry
    } finally {
      isLoadingMore = false;
      if (scrollLoader) scrollLoader.hidden = true;
    }
  }

  if (scrollContainer) {
    scrollContainer.addEventListener("scroll", () => {
      if (scrollContainer.scrollTop + scrollContainer.clientHeight >= scrollContainer.scrollHeight - 60) {
        loadMoreQueue();
      }
    });
  }

  // Update total count and track IDs whenever stage state changes
  const origRender = screen.render;
  if (origRender) {
    screen.render = function (state) {
      origRender(state);
      if (state) {
        totalInQueue = state.queue_depth || 0;
        loadedStudentIds.clear();
        (state.waiting || []).forEach((c) => loadedStudentIds.add(c.student_id));
      }
    };
  }
})();
