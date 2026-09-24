/**
 * caller.js — Caller screen: LED-mirror card + live scrolling queue list.
 *
 * Two independent SSE streams:
 *   /caller/events      → fires when the LED changes (same signal as the LED screen)
 *   /caller/queue-events → fires when the QUEUED list changes (new scan or dismiss)
 *
 * The queue list is fetched fresh from /caller/queue on every nudge from /caller/queue-events.
 * The LED mirror is driven by /caller/state on every nudge from /caller/events.
 */
(function () {
  "use strict";

  // ─── DOM refs ────────────────────────────────────────────────────────────
  var statusBar     = document.getElementById("cq-status");
  var ledCard       = document.getElementById("cq-led-card");
  var ledWaiting    = document.getElementById("cq-led-status");
  var ledName       = document.getElementById("cq-led-name");
  var ledProgramme  = document.getElementById("cq-led-programme");
  var countEl       = document.getElementById("cq-count");
  var list          = document.getElementById("cq-list");
  var emptyEl       = document.getElementById("cq-empty");

  // ─── LED mirror ──────────────────────────────────────────────────────────
  function fetchLed() {
    fetch("/caller/state", { credentials: "same-origin" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) {
        if (!data) return;
        if (data.student && data.mode !== "HOME") {
          ledName.textContent = data.student.name || "";
          ledProgramme.textContent = data.student.programme || "";
          ledName.hidden = false;
          ledProgramme.hidden = false;
          ledWaiting.hidden = true;
        } else {
          ledName.hidden = true;
          ledProgramme.hidden = true;
          ledWaiting.hidden = false;
          ledWaiting.textContent = "Waiting for the stage.";
        }
      })
      .catch(function () {});
  }

  // ─── Queue list ──────────────────────────────────────────────────────────
  var queueData = {};   // student_id → {student_id, name, prn, programme, school, photo_url, queue_position}

  function fetchQueue() {
    fetch("/caller/queue", { credentials: "same-origin" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) {
        if (!data) return;
        renderQueue(data.students, data.total);
      })
      .catch(function () {});
  }

  function renderQueue(students, total) {
    // Compute the set of IDs in the new list
    var newIds = {};
    students.forEach(function (s) { newIds[s.student_id] = s; });

    // Remove rows that are no longer present
    Object.keys(queueData).forEach(function (id) {
      if (!newIds[id]) {
        var el = document.getElementById("cq-row-" + id);
        if (el) el.remove();
        delete queueData[id];
      }
    });

    // Add/update rows that are new or changed
    // Build a position map so we can insert in order
    students.forEach(function (s, index) {
      if (!queueData[s.student_id]) {
        // New row: insert in queue_position order
        queueData[s.student_id] = s;
        var row = buildRow(s);
        // Insert before the row with the next-higher queue_position
        var inserted = false;
        var allRows = list.querySelectorAll(".cq-row");
        for (var i = 0; i < allRows.length; i++) {
          var rowPos = parseInt(allRows[i].dataset.pos, 10);
          if (rowPos > s.queue_position) {
            list.insertBefore(row, allRows[i]);
            inserted = true;
            break;
          }
        }
        if (!inserted) list.appendChild(row);
      }
      // If already in list, no update needed (data is identical — queue rows are stable)
    });

    // Update count
    countEl.textContent = total;
    emptyEl.hidden = (students.length > 0 || Object.keys(queueData).length > 0);
  }

  function buildRow(s) {
    var li = document.createElement("li");
    li.className = "cq-row";
    li.id = "cq-row-" + s.student_id;
    li.dataset.pos = s.queue_position;

    // Photo
    var img = document.createElement("img");
    img.className = "cq-photo";
    img.loading = "lazy";
    img.alt = s.name;
    img.src = s.photo_url;
    img.onerror = function () {
      // Replace broken image with placeholder icon
      var ph = document.createElement("div");
      ph.className = "cq-photo-placeholder";
      ph.setAttribute("aria-hidden", "true");
      ph.textContent = "👤";
      li.replaceChild(ph, img);
    };

    // Info
    var info = document.createElement("div");
    info.className = "cq-info";

    var nameEl = document.createElement("div");
    nameEl.className = "cq-name";
    nameEl.textContent = s.name;

    var metaEl = document.createElement("div");
    metaEl.className = "cq-meta";
    metaEl.textContent = s.prn + " · " + s.school;

    var posEl = document.createElement("span");
    posEl.className = "cq-pos";
    posEl.textContent = "#" + s.queue_position;

    info.appendChild(nameEl);
    info.appendChild(metaEl);
    info.appendChild(posEl);

    // Complete button
    var btn = document.createElement("button");
    btn.className = "cq-btn-complete";
    btn.type = "button";
    btn.textContent = "Complete";
    btn.setAttribute("aria-label", "Complete — " + s.name);
    btn.onclick = function () { dismissStudent(s.student_id, li, btn); };

    li.appendChild(img);
    li.appendChild(info);
    li.appendChild(btn);
    return li;
  }

  function dismissStudent(studentId, rowEl, btn) {
    btn.disabled = true;
    rowEl.classList.add("cq-row--completing");
    fetch("/caller/dismiss/" + studentId, {
      method: "POST",
      credentials: "same-origin",
    })
      .then(function (r) {
        if (r.ok) {
          // Remove the row from local state immediately; SSE will confirm for other devices
          delete queueData[studentId];
          rowEl.remove();
          updateCountFromDom();
        } else {
          // Restore on failure
          rowEl.classList.remove("cq-row--completing");
          btn.disabled = false;
        }
      })
      .catch(function () {
        rowEl.classList.remove("cq-row--completing");
        btn.disabled = false;
      });
  }

  function updateCountFromDom() {
    var n = Object.keys(queueData).length;
    countEl.textContent = n;
    emptyEl.hidden = n > 0;
  }

  // ─── SSE helpers ─────────────────────────────────────────────────────────
  var WATCHDOG_MS  = 5500;  // same as the existing caller watchdog
  var RECONNECT_MS = 3000;

  function openSSE(url, onMessage) {
    var es, watchdog;

    function resetWatchdog() {
      clearTimeout(watchdog);
      watchdog = setTimeout(function () {
        setStatus("lost");
        es && es.close();
        setTimeout(reconnect, RECONNECT_MS);
      }, WATCHDOG_MS);
    }

    function reconnect() {
      es = new EventSource(url);
      es.onopen = function () { setStatus("ok"); resetWatchdog(); };
      es.addEventListener("state", function (ev) {
        resetWatchdog();
        onMessage(ev.data);
      });
      es.addEventListener("ping", function () { resetWatchdog(); });
      es.onerror = function () {
        // EventSource reconnects automatically; just show lost status
        setStatus("lost");
        resetWatchdog();
      };
    }

    reconnect();
  }

  // ─── Status bar ──────────────────────────────────────────────────────────
  var connectedCount = 0;   // how many SSE connections are "ok"

  function setStatus(state) {
    // We have two SSE connections; only show "ok" when both are happy.
    // For simplicity, show the worst state.
    statusBar.className = "cq-status cq-status--" + state;
    statusBar.textContent = state === "ok"
      ? "Live"
      : state === "connecting"
      ? "Connecting…"
      : "Connection lost — reconnecting…";
  }

  // ─── Boot ─────────────────────────────────────────────────────────────────
  setStatus("connecting");

  // Initial data fetch
  fetchLed();
  fetchQueue();

  // LED mirror stream
  openSSE("/caller/events", function () { fetchLed(); });

  // Queue list stream
  openSSE("/caller/queue-events", function () { fetchQueue(); });

})();
