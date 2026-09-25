/* The public LED page (SYSTEM_SPEC 13 and 18).
 *
 * createLedScreen(deps) holds all the behaviour and touches the page only through `deps`, so it is tested
 * under node with a mocked clock (tests/js/led.test.js). Rules it keeps:
 *   - shows ONLY the approved display snapshot fields: name, photo, programme, school/faculty, award. It reads
 *     nothing else from a message, even if the server sent more;
 *   - a holding screen (Holding-Screen.png) between students and before the first contact;
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

  const FACULTY_ENUM_MAP = {
    "SCIENCE": "Faculty of Basic & Applied Science",
    "ENGINEERING": "Faculty of Engineering & Technology",
    "MANAGEMENT": "Faculty of Management & Commerce",
    "SOCIAL_SCI": "Faculty of Social Science & Humanities",
    "DESIGN": "Faculty of Design",
    "INTERDISCIPLINARY": "Faculty of Interdisciplinary Studies",
    "PERFORMING_ARTS": "Faculty of Performing Arts",
    "UNMAPPED": "",
    // ERP school names mapped to the same full display strings
    "BASIC AND APPLIED SCIENCES": "Faculty of Basic & Applied Science",
    "SCHOOL OF BASIC AND APPLIED SCIENCES": "Faculty of Basic & Applied Science",
    "FACULTY OF BASIC & APPLIED SCIENCE": "Faculty of Basic & Applied Science",
    "FACULTY OF BASIC AND APPLIED SCIENCES": "Faculty of Basic & Applied Science",
    "ENGINEERING & TECHNOLOGY": "Faculty of Engineering & Technology",
    "FACULTY OF ENGINEERING AND TECHNOLOGY": "Faculty of Engineering & Technology",
    "SCHOOL OF ENGINEERING AND TECHNOLOGY": "Faculty of Engineering & Technology",
    "FACULTY OF ENGINEERING & TECHNOLOGY": "Faculty of Engineering & Technology",
    "MANAGEMENT AND COMMERCE": "Faculty of Management & Commerce",
    "INSTITUTE OF MANAGEMENT AND RESEARCH": "Faculty of Management & Commerce",
    "FACULTY OF MANAGEMENT & COMMERCE": "Faculty of Management & Commerce",
    "FACULTY OF MANAGEMENT AND COMMERCE": "Faculty of Management & Commerce",
    "SOCIAL SCIENCES AND HUMANITIES": "Faculty of Social Science & Humanities",
    "SCHOOL OF SOCIAL SCIENCES AND HUMANITIES": "Faculty of Social Science & Humanities",
    "FACULTY OF SOCIAL SCIENCE & HUMANITIES": "Faculty of Social Science & Humanities",
    "FACULTY OF SOCIAL SCIENCES AND HUMANITIES": "Faculty of Social Science & Humanities",
    "FACULTY OF DESIGN": "Faculty of Design",
    "INSTITUTE OF DESIGN": "Faculty of Design",
    "FACULTY OF INTERDISCIPLINARY STUDIES": "Faculty of Interdisciplinary Studies",
    "FACULTY OF PERFORMING ARTS": "Faculty of Performing Arts",
    "MAHAGAMI GURUKUL": "Faculty of Performing Arts",
  };

  function formatFaculty(value) {
    if (!value || typeof value !== "string") return "";
    const cleaned = value.trim().toUpperCase().replace(/\s+/g, " ");
    if (cleaned === "UNMAPPED") return "";
    if (FACULTY_ENUM_MAP[cleaned]) return FACULTY_ENUM_MAP[cleaned];
    if (cleaned.startsWith("FACULTY OF")) return value.trim();
    return "";
  }

  function getInitials(name) {
    if (!name || typeof name !== "string") return "";
    const parts = name.trim().split(/\s+/).filter(Boolean);
    if (parts.length === 0) return "";
    if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase();
    const first = parts[0][0];
    const last = parts[parts.length - 1][0];
    return (first + last).toUpperCase();
  }

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
        if (typeof holding.title === "string" && els.holdingTitle) els.holdingTitle.textContent = holding.title;
        if (typeof holding.text === "string" && els.holdingText) els.holdingText.textContent = holding.text;
      }
    }

    function showStudent(student) {
      // Only the approved fields are ever read
      if (els.name) els.name.textContent = student.name;
      if (els.programme) els.programme.textContent = typeof student.programme === "string" ? student.programme : "";
      if (els.school) els.school.textContent = typeof student.school === "string" ? student.school : "";
      if (els.award) els.award.textContent = typeof student.award === "string" ? student.award : "";

      // Faculty mapping
      const facultyText = formatFaculty(student.faculty || student.school);
      if (els.faculty) {
        els.faculty.textContent = facultyText;
      }
      if (els.facultyContainer && els.facultyContainer.style) {
        els.facultyContainer.style.display = facultyText ? "" : "none";
      }

      // Batch year (from student record if present, else config value)
      const defaultBatch = (els.batchYearContainer && els.batchYearContainer.dataset && els.batchYearContainer.dataset.defaultBatch) || "2024-2026";
      if (els.batchYear) {
        els.batchYear.textContent = student.batch_year || student.batch || defaultBatch;
      }

      // Photo / Initials fallback in gold frame
      const photoUrl = typeof student.photo_url === "string" ? student.photo_url.trim() : "";
      const initials = getInitials(student.name);

      if (els.candidateInitials) {
        els.candidateInitials.textContent = initials || "--";
      }

      const photoEl = els.photo || els.candidateImage;
      if (photoEl) {
        if (photoUrl && !photoUrl.endsWith("/nonsense") && photoUrl !== "/static/placeholder.svg") {
          photoEl.setAttribute("src", photoUrl);
          if (photoEl.style) {
            photoEl.style.display = "block";
          }
          if (els.candidateInitials && els.candidateInitials.style) {
            els.candidateInitials.style.display = "none";
          }
        } else {
          if (photoEl.removeAttribute) photoEl.removeAttribute("src");
          if (photoEl.style) {
            photoEl.style.display = "none";
          }
          if (els.candidateInitials && els.candidateInitials.style) {
            els.candidateInitials.style.display = "flex";
          }
        }
      }

      els.holding.hidden = true;
      els.stage.hidden = false;

      if (els.name) fit(els.name);
      if (els.faculty) fit(els.faculty);
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

    return { start, formatFaculty, getInitials };
  }

  return createLedScreen;
});

/* ---- browser wiring (skipped under node) --------------------------------------------------------- */
(function () {
  if (typeof document === "undefined" || typeof window === "undefined") return;
  const holding = document.getElementById("holding");
  if (!holding) return;
  const byId = (id) => document.getElementById(id);

  const candidateImage = byId("candidateImage") || byId("photo");
  const candidateInitials = byId("candidateInitials");

  if (candidateImage) {
    candidateImage.addEventListener("load", () => {
      candidateImage.style.display = "block";
      if (candidateInitials) candidateInitials.style.display = "none";
    });
    candidateImage.addEventListener("error", () => {
      candidateImage.style.display = "none";
      if (candidateInitials) candidateInitials.style.display = "flex";
    });
  }

  // Shrink font-size in steps if element would overflow max 2 lines or box width
  function fit(element) {
    if (!element || !window.getComputedStyle) return;
    element.style.fontSize = "";
    let size = parseFloat(window.getComputedStyle(element).fontSize) || 46;
    let guard = 60;
    while (guard-- > 0 && size > 18) {
      const maxHeight = size * 2.35 + 4;
      const overflowsHeight = element.scrollHeight > maxHeight;
      const overflowsWidth = element.scrollWidth > element.clientWidth;
      if (!overflowsHeight && !overflowsWidth) {
        break;
      }
      size -= 2;
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
      holding,
      holdingTitle: byId("holding-title"),
      holdingText: byId("holding-text"),
      stage: byId("stage"),
      photo: candidateImage,
      candidateImage,
      candidateInitials,
      name: byId("name"),
      batchYear: byId("batchYear"),
      batchYearContainer: byId("batchYearContainer"),
      faculty: byId("faculty"),
      facultyContainer: byId("facultyContainer"),
      programme: byId("programme"),
      school: byId("school"),
      award: byId("award"),
    },
    connect,
    now: Date.now,
    holdMs: 10000,
    watchMs: 500,
    fit,
    preload: (url) => { const image = new Image(); image.src = url; },
  }).start();
})();
