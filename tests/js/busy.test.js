// Loading states for slow Admin actions (static/busy.js), run with `node --test tests/js/busy.test.js` (also run by
// tests/test_station_engine.py, which globs tests/js/*.test.js). A tiny fake DOM stands in for the browser.
// What matters: a slow form's buttons are disabled and relabelled and a second submit is swallowed; a submit the
// Admin cancelled (confirm() -> Cancel) does not go busy; a download shows "Preparing download…" until the file
// has arrived, ignores repeat clicks, saves under the server's file name, and turns any refusal into one sentence.
const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");

const Busy = require(path.join(__dirname, "..", "..", "static", "busy.js"));

// ------------------------------------------------------------------ a very small DOM
class ClassList {
  constructor() { this.set = new Set(); }
  add(c) { this.set.add(c); }
  remove(c) { this.set.delete(c); }
  contains(c) { return this.set.has(c); }
}

class El {
  constructor(tag, attrs = {}) {
    this.tagName = tag.toUpperCase();
    this.attrs = {};
    this.dataset = {};
    this.classList = new ClassList();
    this.children = [];
    this.parent = null;
    this.textContent = "";
    this.value = "";
    this.disabled = false;
    this.style = {};
    this.clicked = 0;
    for (const [k, v] of Object.entries(attrs)) this.setAttribute(k, v);
  }
  setAttribute(k, v) {
    this.attrs[k] = String(v);
    if (k.startsWith("data-")) this.dataset[k.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase())] = String(v);
    if (k === "type") this.type = String(v);
  }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  removeAttribute(k) { delete this.attrs[k]; }
  append(...kids) { kids.forEach((k) => { k.parent = this; this.children.push(k); }); return this; }
  appendChild(k) { this.append(k); return k; }
  remove() { if (this.parent) { this.parent.children = this.parent.children.filter((c) => c !== this); this.parent = null; } }
  insertAdjacentElement(where, el) {
    const p = this.parent;
    const i = p.children.indexOf(this);
    el.parent = p;
    p.children.splice(i + 1, 0, el);
    return el;
  }
  get lastElementChild() { return this.children[this.children.length - 1] || null; }
  all() { return this.children.flatMap((c) => [c, ...c.all()]); }
  querySelectorAll() {    // only ever asked for submit buttons
    return this.all().filter((e) => (e.tagName === "BUTTON" && (!e.type || e.type === "submit")) ||
                                    (e.tagName === "INPUT" && e.type === "submit"));
  }
  click() { this.clicked += 1; }
}

function fakeDocument() {
  const body = new El("body");
  return { body, createElement: (tag) => new El(tag) };
}

function event(extra = {}) {
  return { defaultPrevented: false, button: 0, preventDefault() { this.defaultPrevented = true; }, ...extra };
}

function formDeps(doc) {
  const queue = [];
  return { deps: { document: doc, defer: (fn) => queue.push(fn) }, tick: () => queue.splice(0).forEach((fn) => fn()) };
}

function slowForm(doc, attrs = { "data-busy": "Importing…" }) {
  const form = new El("form", attrs);
  const input = new El("input", { type: "file", name: "file" });
  const button = new El("button", { type: "submit" });
  button.textContent = "Import 12 students";
  const other = new El("button", { type: "button" });   // not a submit button: left alone
  other.textContent = "Help";
  form.append(input, button, other);
  doc.body.append(form);
  return { form, button, other };
}

// ------------------------------------------------------------------ forms
test("a slow form goes busy: button disabled, relabelled, spinner class, aria-busy", () => {
  const doc = fakeDocument();
  const { form, button, other } = slowForm(doc);
  const { deps, tick } = formDeps(doc);
  const e = event();
  assert.equal(Busy.onSubmit(form, e, deps), true);
  assert.equal(e.defaultPrevented, false, "the first submit goes through");
  assert.equal(form.getAttribute("aria-busy"), "true");
  assert.equal(button.disabled, false, "not disabled until the next tick, so its own value is still submitted");
  tick();
  assert.equal(button.disabled, true);
  assert.equal(button.textContent, "Importing…");
  assert.ok(button.classList.contains("is-busy"));
  assert.equal(other.disabled, false);
  assert.equal(other.textContent, "Help");
});

test("a second submit (double-click, Enter twice) is swallowed", () => {
  const doc = fakeDocument();
  const { form } = slowForm(doc);
  const { deps, tick } = formDeps(doc);
  Busy.onSubmit(form, event(), deps);
  tick();
  const again = event();
  assert.equal(Busy.onSubmit(form, again, deps), false);
  assert.equal(again.defaultPrevented, true);
  const third = event();
  Busy.onSubmit(form, third, deps);
  assert.equal(third.defaultPrevented, true);
});

test("a long import shows its keep-this-page-open note", () => {
  const doc = fakeDocument();
  const note = "Importing students & photos… Please keep this page open. This may take several minutes.";
  const { form, button } = slowForm(doc, { "data-busy": "Importing…", "data-busy-note": note });
  Busy.onSubmit(form, event(), formDeps(doc).deps);
  const shown = form.children[form.children.indexOf(button) + 1];
  assert.equal(shown.className, "busy-note");
  assert.equal(shown.textContent, note);
  assert.equal(shown.getAttribute("role"), "status");
});

test("a submit the Admin cancelled at a confirm() box does not go busy", () => {
  const doc = fakeDocument();
  const { form, button } = slowForm(doc, { "data-busy": "Deleting…" });
  const { deps, tick } = formDeps(doc);
  const cancelled = event({ defaultPrevented: true });
  assert.equal(Busy.onSubmit(form, cancelled, deps), false);
  tick();
  assert.equal(button.disabled, false);
  assert.equal(form.getAttribute("aria-busy"), null);
  assert.equal(Busy.onSubmit(form, event(), deps), true, "the next real submit still works");
});

test("resetForm (a page restored by Back) puts everything back", () => {
  const doc = fakeDocument();
  const { form, button } = slowForm(doc, { "data-busy": "Importing…", "data-busy-note": "Please wait." });
  const { deps, tick } = formDeps(doc);
  Busy.onSubmit(form, event(), deps);
  tick();
  Busy.resetForm(form);
  assert.equal(button.disabled, false);
  assert.equal(button.textContent, "Import 12 students");
  assert.equal(button.classList.contains("is-busy"), false);
  assert.equal(form.children.some((c) => c.className === "busy-note"), false);
  assert.equal(Busy.onSubmit(form, event(), deps), true);
});

test("without a data-busy label the default one is used", () => {
  const doc = fakeDocument();
  const { form, button } = slowForm(doc, { "data-busy": "" });
  const { deps, tick } = formDeps(doc);
  Busy.onSubmit(form, event(), deps);
  tick();
  assert.equal(button.textContent, Busy.DEFAULT_FORM_LABEL);
});

// ------------------------------------------------------------------ downloads
function response({ status = 200, type = "application/pdf", disposition = 'attachment; filename="passes-all.pdf"', body = "PDF", url } = {}) {
  const headers = { "content-type": type, "content-disposition": disposition };
  return {
    ok: status >= 200 && status < 300, status, url,
    headers: { get: (k) => headers[k.toLowerCase()] || null },
    blob: async () => ({ size: body.length }),
    json: async () => JSON.parse(body),
  };
}

function downloadSetup(respond) {
  const doc = fakeDocument();
  const link = new El("a", { href: "/admin/passes/download_all", "data-download": "" });
  link.href = "/admin/passes/download_all";
  link.textContent = "Download all passes (PDF)";
  doc.body.append(link);
  let release;
  const gate = new Promise((r) => { release = r; });
  const fetched = [];
  const made = [];
  const navigated = [];
  const created = [];
  const make = doc.createElement;
  doc.createElement = (tag) => { const el = make(tag); created.push(el); return el; };
  const win = {
    fetch: (url, opts) => { fetched.push({ url, opts }); return gate.then(respond); },
    Blob: function () {},
    URL: { createObjectURL: (b) => { made.push(b); return "blob:1"; }, revokeObjectURL: () => {} },
  };
  const deps = { document: doc, window: win, later: () => {}, navigate: (u) => navigated.push(u) };
  return { doc, link, deps, release, fetched, made, navigated, created };
}

test("a download shows Preparing download… until the file has arrived, then saves it under the server's name", async () => {
  const s = downloadSetup(() => response());
  const e = event();
  const done = Busy.onDownloadClick(s.link, e, s.deps);
  assert.equal(e.defaultPrevented, true);
  assert.equal(s.link.textContent, Busy.DEFAULT_DOWNLOAD_LABEL);
  assert.equal(s.link.getAttribute("aria-disabled"), "true");
  assert.ok(s.link.classList.contains("is-busy"));
  assert.equal(s.fetched[0].opts.credentials, "same-origin");
  s.release();
  await done;
  const saved = s.created.find((c) => c.tagName === "A");
  assert.equal(saved.download, "passes-all.pdf");
  assert.equal(saved.href, "blob:1");
  assert.equal(saved.clicked, 1);
  assert.equal(s.doc.body.children.includes(saved), false, "the temporary link is removed again");
  assert.equal(s.made.length, 1);
  assert.equal(s.link.textContent, "Download all passes (PDF)");
  assert.equal(s.link.classList.contains("is-busy"), false);
  assert.equal(s.link.getAttribute("aria-busy"), null);
});

test("clicking again while a download is being prepared does not fetch it twice", async () => {
  const s = downloadSetup(() => response());
  const done = Busy.onDownloadClick(s.link, event(), s.deps);
  const again = event();
  assert.equal(Busy.onDownloadClick(s.link, again, s.deps), undefined);
  assert.equal(again.defaultPrevented, true, "the browser does not follow the link either");
  assert.equal(s.fetched.length, 1);
  s.release();
  await done;
  await Busy.onDownloadClick(s.link, event(), s.deps);
  assert.equal(s.fetched.length, 2, "after it finished, a new click works");
});

test("a custom busy label is used", () => {
  const s = downloadSetup(() => response());
  s.link.dataset.busy = "Preparing all passes…";
  Busy.onDownloadClick(s.link, event(), s.deps);
  assert.equal(s.link.textContent, "Preparing all passes…");
});

test("a JSON refusal is shown as its one plain sentence", async () => {
  const s = downloadSetup(() => response({ status: 409, type: "application/json", disposition: "",
    body: JSON.stringify({ detail: { code: "NO_TOKEN", message: "That student has no QR yet." } }) }));
  const done = Busy.onDownloadClick(s.link, event(), s.deps);
  s.release();
  await done;
  const note = s.doc.body.children.find((c) => c.className === "busy-note");
  assert.equal(note.textContent, "That student has no QR yet.");
  assert.ok(note.classList.contains("busy-error"));
  assert.equal(s.link.textContent, "Download all passes (PDF)");
});

test("an HTML answer (a redirect carrying an error) is shown as the page it is", async () => {
  const s = downloadSetup(() => response({ type: "text/html; charset=utf-8", disposition: "", url: "/admin/passes?error=x" }));
  const done = Busy.onDownloadClick(s.link, event(), s.deps);
  s.release();
  await done;
  assert.deepEqual(s.navigated, ["/admin/passes?error=x"]);
});

test("a network failure says so plainly and the link works again", async () => {
  const s = downloadSetup(() => { throw new TypeError("Failed to fetch"); });
  const done = Busy.onDownloadClick(s.link, event(), s.deps);
  s.release();
  await done;
  const note = s.doc.body.children.find((c) => c.className === "busy-note");
  assert.equal(note.textContent, Busy.DOWNLOAD_FAILED);
  assert.equal(s.link.dataset.busyState, undefined);
});

test("a browser without fetch/Blob just follows the link", () => {
  const s = downloadSetup(() => response());
  s.deps.window = {};
  const e = event();
  assert.equal(Busy.onDownloadClick(s.link, e, s.deps), undefined);
  assert.equal(e.defaultPrevented, false);
});

test("ctrl/cmd-click (open in a new tab) is left to the browser", () => {
  const s = downloadSetup(() => response());
  const e = event({ ctrlKey: true });
  Busy.onDownloadClick(s.link, e, s.deps);
  assert.equal(e.defaultPrevented, false);
  assert.equal(s.fetched.length, 0);
});

test("filenameFrom reads both spellings of Content-Disposition", () => {
  assert.equal(Busy.filenameFrom('attachment; filename="audit-20261015-1100.csv"', "x"), "audit-20261015-1100.csv");
  assert.equal(Busy.filenameFrom("attachment; filename=passes.pdf", "x"), "passes.pdf");
  assert.equal(Busy.filenameFrom("attachment; filename*=UTF-8''r%C3%A9sum%C3%A9.csv", "x"), "résumé.csv");
  assert.equal(Busy.filenameFrom("", "download"), "download");
});
