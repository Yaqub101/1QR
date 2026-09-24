/**
 * caller.js — Caller screen: live queue list with first-row Next action & faculty color coding.
 *
 * Realtime:
 *   /events/queue (SSE) → broadcasts on any queue change (student queued, called, staged).
 *   Heartbeat: 2s pings from server.
 *   Fallback: 3s polling when SSE is disconnected or fails.
 *
 * Screen behaviour:
 *   - Only students where called_at IS NULL, ordered by queued_at ASC.
 *   - First row highlighted with prominent "NEXT" button.
 *   - Clicking NEXT sets called_at and removes student from list.
 *   - Each row is colour-coded by faculty:
 *       - Left bar: strong shade
 *       - Faculty badge: strong shade
 *       - Row tint: light shade
 */
(function (root, factory) {
  if (typeof module === "object" && module.exports) {
    module.exports = factory();
  } else {
    factory();
  }
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  var FACULTY_LABELS = {
    SCIENCE: "Science",
    ENGINEERING: "Engineering",
    MANAGEMENT: "Management",
    SOCIAL_SCI: "Social Sciences",
    DESIGN: "Design",
    INTERDISCIPLINARY: "Interdisciplinary",
    PERFORMING_ARTS: "Performing Arts",
    UNMAPPED: "General"
  };

  function formatFaculty(code) {
    return FACULTY_LABELS[code] || code || "General";
  }

  function buildRow(s, isFirst, posIndex, doc) {
    var d = doc || (typeof document !== "undefined" ? document : null);
    if (!d) return null;

    var li = d.createElement("li");
    li.className = "cq-row" + (isFirst ? " cq-row--first" : "") + (s.faculty ? " cq-faculty-" + String(s.faculty).toLowerCase() : "");
    li.id = "cq-row-" + s.student_id;
    li.dataset.studentId = s.student_id;

    // Colour coding by faculty:
    var strongColor = (s.palette && s.palette.strong) || "#58595B";
    var lightColor  = (s.palette && s.palette.light)  || "#E6E7E8";

    li.style.borderLeftColor = strongColor;
    if (li.style.setProperty) {
      li.style.setProperty("--cq-fac-strong", strongColor);
      li.style.setProperty("--cq-fac-light", lightColor);
    }

    // Photo thumbnail
    var photoContainer = d.createElement("div");
    photoContainer.className = "cq-photo-container";

    var img = d.createElement("img");
    img.className = "cq-photo";
    img.loading = "lazy";
    img.alt = s.name;
    img.src = s.photo_url || "/static/img/placeholder.svg";
    img.onerror = function () {
      var ph = d.createElement("div");
      ph.className = "cq-photo-placeholder";
      ph.setAttribute("aria-hidden", "true");
      ph.textContent = "👤";
      if (photoContainer.contains(img)) {
        photoContainer.replaceChild(ph, img);
      }
    };
    photoContainer.appendChild(img);

    // Info block
    var info = d.createElement("div");
    info.className = "cq-info";

    var headerRow = d.createElement("div");
    headerRow.className = "cq-info-header";

    var posEl = d.createElement("span");
    posEl.className = "cq-pos";
    posEl.textContent = "#" + (s.queue_position != null ? s.queue_position : posIndex);

    var badge = d.createElement("span");
    badge.className = "cq-fac-badge";
    badge.textContent = formatFaculty(s.faculty);
    badge.style.backgroundColor = strongColor;
    badge.style.color = "#ffffff";

    headerRow.appendChild(posEl);
    headerRow.appendChild(badge);

    var nameEl = d.createElement("div");
    nameEl.className = "cq-name";
    nameEl.textContent = s.name;

    var progEl = d.createElement("div");
    progEl.className = "cq-programme";
    progEl.textContent = s.programme || "";

    var metaEl = d.createElement("div");
    metaEl.className = "cq-meta";
    metaEl.textContent = (s.prn ? "PRN: " + s.prn : "") + (s.school ? " · " + s.school : "");

    info.appendChild(headerRow);
    info.appendChild(nameEl);
    info.appendChild(progEl);
    info.appendChild(metaEl);

    li.appendChild(photoContainer);
    li.appendChild(info);

    // Action button — ONLY on the first row
    if (isFirst) {
      var actionWrap = d.createElement("div");
      actionWrap.className = "cq-action-wrap";

      var nextBtn = d.createElement("button");
      nextBtn.type = "button";
      nextBtn.className = "cq-btn-next";
      nextBtn.setAttribute("aria-label", "Call next student: " + s.name);

      var btnTitle = d.createElement("span");
      btnTitle.className = "cq-btn-next-title";
      btnTitle.textContent = "NEXT";

      var btnSub = d.createElement("span");
      btnSub.className = "cq-btn-next-sub";
      btnSub.textContent = "Call to Dias";

      nextBtn.appendChild(btnTitle);
      nextBtn.appendChild(btnSub);

      actionWrap.appendChild(nextBtn);
      li.appendChild(actionWrap);
    }

    return li;
  }

  function renderQueue(students, total, elements, onCallNext, doc) {
    var d = doc || (typeof document !== "undefined" ? document : null);
    if (!elements) return;

    if (elements.countEl) {
      elements.countEl.textContent = total != null ? total : (students ? students.length : 0);
    }
    if (elements.list) {
      elements.list.innerHTML = "";
    }

    if (!students || students.length === 0) {
      if (elements.emptyEl) elements.emptyEl.hidden = false;
      return;
    }
    if (elements.emptyEl) elements.emptyEl.hidden = true;

    students.forEach(function (s, index) {
      var isFirst = (index === 0);
      var row = buildRow(s, isFirst, index + 1, d);
      if (isFirst && onCallNext && row) {
        var btn = row.querySelector(".cq-btn-next");
        if (btn) {
          btn.onclick = function () {
            onCallNext(s.student_id, btn, row);
          };
        }
      }
      if (elements.list && row) {
        elements.list.appendChild(row);
      }
    });
  }

  // If running under Node.js without a DOM, return public components for testing
  if (typeof document === "undefined") {
    return {
      FACULTY_LABELS: FACULTY_LABELS,
      formatFaculty: formatFaculty,
      buildRow: buildRow,
      renderQueue: renderQueue,
    };
  }

  // ─── Browser DOM Boot ──────────────────────────────────────────────────
  var statusBar = document.getElementById("cq-status");
  var countEl   = document.getElementById("cq-count");
  var list      = document.getElementById("cq-list");
  var emptyEl   = document.getElementById("cq-empty");
  var domEls    = { countEl: countEl, list: list, emptyEl: emptyEl };

  var currentStudents = [];
  var isCalling = false;

  function fetchQueue() {
    return fetch("/caller/queue", { credentials: "same-origin" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) {
        if (!data) return;
        currentStudents = data.students || [];
        renderQueue(currentStudents, data.total != null ? data.total : currentStudents.length, domEls, callNext);
      })
      .catch(function () {});
  }

  function callNext(studentId, btn, rowEl) {
    if (isCalling) return;
    isCalling = true;
    if (btn) btn.disabled = true;
    if (rowEl) rowEl.classList.add("cq-row--calling");

    fetch("/caller/next", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ student_id: studentId })
    })
      .then(function (r) {
        if (!r.ok) {
          throw new Error("Call next failed");
        }
        return r.json();
      })
      .then(function () {
        currentStudents = currentStudents.filter(function (s) { return s.student_id !== studentId; });
        renderQueue(currentStudents, currentStudents.length, domEls, callNext);
        fetchQueue();
      })
      .catch(function () {
        if (rowEl) rowEl.classList.remove("cq-row--calling");
        if (btn) btn.disabled = false;
        fetchQueue();
      })
      .finally(function () {
        isCalling = false;
      });
  }

  // ─── Status indicator ────────────────────────────────────────────────────
  function setStatus(state) {
    if (!statusBar) return;
    statusBar.className = "cq-status cq-status--" + state;
    if (state === "ok") {
      statusBar.textContent = "Live";
      statusBar.style.display = "none";
    } else if (state === "connecting") {
      statusBar.textContent = "Connecting to queue stream…";
      statusBar.style.display = "block";
    } else {
      statusBar.textContent = "Live stream offline — polling queue every 3s";
      statusBar.style.display = "block";
    }
  }

  // ─── Realtime SSE & Polling Fallback ─────────────────────────────────────
  var WATCHDOG_MS  = 6000;
  var RECONNECT_MS = 2500;
  var POLL_INTERVAL_MS = 3000;

  var sseSource   = null;
  var watchdogTimer = null;
  var pollInterval = null;

  function startPolling() {
    if (!pollInterval) {
      pollInterval = setInterval(fetchQueue, POLL_INTERVAL_MS);
    }
  }

  function stopPolling() {
    if (pollInterval) {
      clearInterval(pollInterval);
      pollInterval = null;
    }
  }

  function resetWatchdog() {
    clearTimeout(watchdogTimer);
    watchdogTimer = setTimeout(function () {
      setStatus("lost");
      startPolling();
      if (sseSource) {
        sseSource.close();
        sseSource = null;
      }
      setTimeout(connectSSE, RECONNECT_MS);
    }, WATCHDOG_MS);
  }

  function connectSSE() {
    if (typeof EventSource === "undefined") {
      setStatus("lost");
      startPolling();
      return;
    }

    if (sseSource) {
      try { sseSource.close(); } catch (_) {}
    }

    try {
      sseSource = new EventSource("/events/queue");

      sseSource.onopen = function () {
        setStatus("ok");
        stopPolling();
        resetWatchdog();
        fetchQueue();
      };

      sseSource.addEventListener("queue", function () {
        resetWatchdog();
        fetchQueue();
      });

      sseSource.addEventListener("queue_changed", function () {
        resetWatchdog();
        fetchQueue();
      });

      sseSource.onmessage = function () {
        resetWatchdog();
        fetchQueue();
      };

      sseSource.addEventListener("ping", function () {
        resetWatchdog();
      });

      sseSource.onerror = function () {
        setStatus("lost");
        startPolling();
        resetWatchdog();
      };
    } catch (_) {
      setStatus("lost");
      startPolling();
    }
  }

  // ─── Keyboard shortcut ──────────────────────────────────────────────────
  document.addEventListener("keydown", function (e) {
    if (e.key === "Enter" || e.key === " ") {
      if (e.target && (e.target.tagName === "INPUT" || e.target.tagName === "TEXTAREA")) return;
      var firstBtn = document.querySelector(".cq-row--first .cq-btn-next");
      if (firstBtn && !firstBtn.disabled) {
        e.preventDefault();
        firstBtn.click();
      }
    }
  });

  // ─── Boot ────────────────────────────────────────────────────────────────
  setStatus("connecting");
  fetchQueue();
  connectSSE();

  return {
    FACULTY_LABELS: FACULTY_LABELS,
    formatFaculty: formatFaculty,
    buildRow: buildRow,
    renderQueue: renderQueue,
  };
});
