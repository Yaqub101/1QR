// Loading states for the slow Admin actions: imports, downloads, exports, bulk and destructive actions.
//
// Opt-in, so ordinary page navigation is untouched:
//   <form data-busy="Importing…" data-busy-note="Please keep this page open.">   a slow form
//   <a href="/…/export" data-download>Download CSV</a>                          a file download
//   <a … data-download data-busy="Preparing passes…">                            (custom busy text)
//
// A busy form: its submit button(s) are disabled and relabelled, a spinner and the note appear, and a second
// submit is swallowed (double-click, Enter twice). The page then navigates as usual; a page restored from the
// back/forward cache is reset so its buttons work again.
//
// A download cannot tell the page when it has finished if the browser simply follows the link, so the file is
// fetched instead: the link shows "Preparing download…" (disabled) until the file has arrived, then the file is
// saved under the server's own name. An error page is shown as the server sent it (a redirect with its message),
// a JSON refusal as its one plain sentence. Without fetch/Blob support the link just works as a plain link.
//
// No progress percentages: the server does not report any, so none is invented.
(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.Busy = factory();
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  var DEFAULT_FORM_LABEL = "Working…";
  var DEFAULT_DOWNLOAD_LABEL = "Preparing download…";
  var DOWNLOAD_FAILED = "The download could not be prepared. Please try again.";

  function submitButtons(form) {
    return Array.prototype.slice.call(form.querySelectorAll('button[type="submit"], button:not([type]), input[type="submit"]'));
  }

  function setLabel(el, label) {
    if (el.tagName === "INPUT") {
      if (el.dataset.busyOriginal === undefined) el.dataset.busyOriginal = el.value;
      el.value = label;
    } else {
      if (el.dataset.busyOriginal === undefined) el.dataset.busyOriginal = el.textContent;
      el.textContent = label;
    }
  }

  function restoreLabel(el) {
    if (el.dataset.busyOriginal === undefined) return;
    if (el.tagName === "INPUT") el.value = el.dataset.busyOriginal;
    else el.textContent = el.dataset.busyOriginal;
    delete el.dataset.busyOriginal;
  }

  function showNote(doc, anchor, text) {
    var note = doc.createElement("p");
    note.className = "busy-note";
    note.setAttribute("role", "status");
    note.setAttribute("aria-live", "polite");
    note.textContent = text;
    anchor.insertAdjacentElement("afterend", note);
    return note;
  }

  // ------------------------------------------------------------------ forms
  function isBusy(el) { return el.dataset.busyState === "on"; }

  // Returns true when the submit was allowed through, false when it was swallowed as a repeat.
  function onSubmit(form, event, deps) {
    if (isBusy(form)) {
      event.preventDefault();
      return false;
    }
    if (event.defaultPrevented) return false;          // e.g. the Admin pressed Cancel on a confirm() box
    form.dataset.busyState = "on";
    form.setAttribute("aria-busy", "true");
    var label = form.dataset.busy || DEFAULT_FORM_LABEL;
    var buttons = submitButtons(form);
    // Disabled on the next tick, not now: a submit button that is disabled while the form is being submitted
    // drops its own name=value from the request.
    deps.defer(function () {
      buttons.forEach(function (b) {
        b.disabled = true;
        b.classList.add("is-busy");
        setLabel(b, label);
      });
    });
    var anchor = buttons.length ? buttons[buttons.length - 1] : form.lastElementChild;
    var text = form.dataset.busyNote || "";
    if (text && anchor) form._busyNote = showNote(deps.document, anchor, text);
    return true;
  }

  function resetForm(form) {
    delete form.dataset.busyState;
    form.removeAttribute("aria-busy");
    submitButtons(form).forEach(function (b) {
      b.disabled = false;
      b.classList.remove("is-busy");
      restoreLabel(b);
    });
    if (form._busyNote) { form._busyNote.remove(); form._busyNote = null; }
  }

  // ------------------------------------------------------------------ downloads
  function filenameFrom(disposition, fallback) {
    if (!disposition) return fallback;
    var star = /filename\*\s*=\s*UTF-8''([^;]+)/i.exec(disposition);
    if (star) { try { return decodeURIComponent(star[1].trim()); } catch (e) { /* fall through */ } }
    var plain = /filename\s*=\s*"?([^";]+)"?/i.exec(disposition);
    return plain ? plain[1].trim() : fallback;
  }

  function canFetchDownloads(win) {
    return !!(win && win.fetch && win.Blob && win.URL && win.URL.createObjectURL);
  }

  function endDownload(link) {
    delete link.dataset.busyState;
    link.removeAttribute("aria-busy");
    link.removeAttribute("aria-disabled");
    link.classList.remove("is-busy");
    restoreLabel(link);
  }

  function reportDownloadError(deps, link, message) {
    var old = link._busyNote;
    if (old) old.remove();
    var note = showNote(deps.document, link, message);
    note.classList.add("busy-error");
    link._busyNote = note;
  }

  // Returns a promise that settles when the download attempt is over (for tests); undefined when ignored.
  function onDownloadClick(link, event, deps) {
    if (event.defaultPrevented || event.button > 0 || event.metaKey || event.ctrlKey || event.shiftKey) return undefined;
    if (!canFetchDownloads(deps.window)) return undefined;    // a plain link: the browser downloads it
    event.preventDefault();
    if (isBusy(link)) return undefined;                        // already preparing: ignore the repeat click
    link.dataset.busyState = "on";
    link.setAttribute("aria-busy", "true");
    link.setAttribute("aria-disabled", "true");
    link.classList.add("is-busy");
    setLabel(link, link.dataset.busy || DEFAULT_DOWNLOAD_LABEL);
    if (link._busyNote) { link._busyNote.remove(); link._busyNote = null; }

    var win = deps.window;
    return win.fetch(link.href, { credentials: "same-origin", headers: { Accept: "*/*" } })
      .then(function (res) {
        var type = (res.headers.get("content-type") || "").toLowerCase();
        var disposition = res.headers.get("content-disposition") || "";
        if (res.ok && /attachment/i.test(disposition)) {
          return res.blob().then(function (blob) {
            var url = win.URL.createObjectURL(blob);
            var a = deps.document.createElement("a");
            a.href = url;
            a.download = filenameFrom(disposition, "download");
            a.style.display = "none";
            deps.document.body.appendChild(a);
            a.click();
            a.remove();
            deps.later(function () { win.URL.revokeObjectURL(url); }, 60000);
          });
        }
        if (type.indexOf("text/html") !== -1) {                // a page (e.g. a redirect carrying the error)
          deps.navigate(res.url || link.href);
          return undefined;
        }
        if (type.indexOf("application/json") !== -1) {
          return res.json().then(function (body) {
            var detail = body && body.detail;
            var message = detail && typeof detail === "object" ? detail.message : (typeof detail === "string" ? detail : null);
            reportDownloadError(deps, link, message || DOWNLOAD_FAILED);
          }, function () { reportDownloadError(deps, link, DOWNLOAD_FAILED); });
        }
        reportDownloadError(deps, link, DOWNLOAD_FAILED);
        return undefined;
      })
      .catch(function () { reportDownloadError(deps, link, DOWNLOAD_FAILED); })
      .then(function () { endDownload(link); });
  }

  // ------------------------------------------------------------------ wiring
  function install(doc, win) {
    var deps = {
      document: doc,
      window: win,
      defer: function (fn) { win.setTimeout(fn, 0); },
      later: function (fn, ms) { win.setTimeout(fn, ms); },
      navigate: function (url) { win.location.href = url; },
    };
    doc.addEventListener("submit", function (event) {
      var form = event.target;
      if (form && form.matches && form.matches("form[data-busy]")) onSubmit(form, event, deps);
    });
    doc.addEventListener("click", function (event) {
      var link = event.target && event.target.closest ? event.target.closest("a[data-download]") : null;
      if (link) onDownloadClick(link, event, deps);
    });
    // Back/forward cache: a page restored exactly as it was left would still show "Importing…".
    win.addEventListener("pageshow", function (event) {
      if (!event.persisted) return;
      Array.prototype.forEach.call(doc.querySelectorAll("form[data-busy-state]"), resetForm);
      Array.prototype.forEach.call(doc.querySelectorAll("a[data-busy-state]"), endDownload);
    });
    return deps;
  }

  if (typeof document !== "undefined" && typeof window !== "undefined" && !(typeof module === "object" && module.exports)) {
    install(document, window);
  }

  return {
    DEFAULT_FORM_LABEL: DEFAULT_FORM_LABEL,
    DEFAULT_DOWNLOAD_LABEL: DEFAULT_DOWNLOAD_LABEL,
    DOWNLOAD_FAILED: DOWNLOAD_FAILED,
    onSubmit: onSubmit,
    resetForm: resetForm,
    onDownloadClick: onDownloadClick,
    filenameFrom: filenameFrom,
    install: install,
  };
});
