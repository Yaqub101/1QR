/* Pure helpers for the operator screen (no DOM, so they are unit-tested under node).
 *
 *   stripScannerSuffix(raw)  -> the scanned code with the scanner's trailing Enter/newline/tab removed
 *   createDebouncer(opts)    -> drops a repeat of the same code inside a window, and anything while busy
 *   classifyResult(result)   -> { colour, sound } for READY / CONFIRMED / DUPLICATE / REJECTED / INVALID
 */
(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.StationLogic = factory();
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  // USB scanners behave like a keyboard: they type the code and then press Enter. Depending on the
  // model that arrives as \n, \r, \r\n, a tab, or a NUL. If two scans land together, take the first.
  function stripScannerSuffix(raw) {
    if (typeof raw !== "string") return "";
    const lines = raw.split(String.fromCharCode(0)).join("").split(/[\r\n]+/);
    for (const line of lines) {
      const trimmed = line.trim();
      if (trimmed) return trimmed;
    }
    return "";
  }

  // A double-read of one QR must not become two requests; a different student never waits on a window.
  function createDebouncer(opts) {
    const windowMs = opts && opts.windowMs != null ? opts.windowMs : 1500;
    const now = (opts && opts.now) || Date.now;
    let lastValue = null;
    let lastAt = -Infinity;
    let busy = false;
    return {
      accept(value) {
        if (busy) return false;
        const t = now();
        if (value === lastValue && t - lastAt < windowMs) return false;
        lastValue = value;
        lastAt = t;
        return true;
      },
      begin() { busy = true; },
      end() { busy = false; },
      get busy() { return busy; },
    };
  }

  const VIEWS = {
    READY: { colour: "blue", sound: null },        // a card is showing; nothing has been done yet
    CONFIRMED: { colour: "green", sound: "success" },
    DUPLICATE: { colour: "amber", sound: "duplicate" },
    REJECTED: { colour: "red", sound: "rejected" },
    INVALID: { colour: "red", sound: "rejected" },
    ERROR: { colour: "red", sound: "rejected" },
  };

  function classifyResult(result) {
    const view = VIEWS[result] || VIEWS.ERROR; // anything unknown is treated as a refusal, never as green
    return { colour: view.colour, sound: view.sound };
  }

  return { stripScannerSuffix, createDebouncer, classifyResult };
});
