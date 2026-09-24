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

  // USB/Bluetooth barcode scanners act like a keyboard, sending rapid keystrokes (< 60ms inter-key)
  // followed by Enter. Keystrokes inside real form inputs/textareas are ignored so manual PRN typing
  // or modal inputs are never swallowed or disrupted. Slow human typing outside inputs is discarded.
  function createScannerBuffer(opts) {
    const maxBurstGapMs = opts && opts.maxBurstGapMs != null ? opts.maxBurstGapMs : 60;
    const now = (opts && opts.now) || Date.now;
    const onScan = (opts && opts.onScan) || function () {};
    const onConfirm = (opts && opts.onConfirm) || function () {};

    let buffer = "";
    let lastKeyAt = 0;

    function reset() {
      buffer = "";
      lastKeyAt = 0;
    }

    function handleKeydown(event) {
      if (!event) return;
      const target = event.target;
      // Never intercept keystrokes if the user is typing into an input, textarea, or select
      if (target) {
        if (typeof target.closest === "function" && target.closest("input, textarea, select")) {
          return;
        }
        const tag = (target.tagName || "").toLowerCase();
        if (tag === "input" || tag === "textarea" || tag === "select") {
          return;
        }
      }

      const key = event.key;
      if (!key) return;

      if (key === "Enter") {
        const t = now();
        if (buffer.length > 0) {
          if (t - lastKeyAt <= maxBurstGapMs) {
            const scanned = buffer;
            reset();
            if (event.preventDefault) event.preventDefault();
            onScan(scanned);
            return;
          }
          // Slow typing before Enter: discard
          reset();
        } else {
          // Empty buffer on Enter: confirm pending card
          onConfirm(event);
        }
        return;
      }

      // Buffer single printable characters (ignore Control, Alt, Shift, Meta, Tab, etc.)
      if (key.length === 1) {
        const t = now();
        if (buffer.length > 0 && t - lastKeyAt > maxBurstGapMs) {
          // Gap exceeded: previous keystrokes were slow human typing, start fresh
          buffer = "";
        }
        buffer += key;
        lastKeyAt = t;
      }
    }

    return { handleKeydown, reset, get buffer() { return buffer; } };
  }

  return { stripScannerSuffix, createDebouncer, classifyResult, createScannerBuffer };
});
